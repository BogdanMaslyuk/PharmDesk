"""PharmDesk v0.4.5 bulk matching: conservative clinical-data boundaries.

Only synthetic GRLS rows and synthetic pharmacy stock are used.
"""
from datetime import date
from hashlib import sha256
from io import BytesIO
import csv
import re

from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.matching as matching_routes
from app.database import Base, get_db
from app.importer import import_rows
from app.main import app
from app.matching_parse import (
    evaluate, extract_doses, extract_forms, source_attributes, stock_attributes,
)
from app.matching_worker import inventory_digest, run_matching
from app.models import (
    GrlsImportBatch, GrlsImportRow, MatchingCandidate, MatchingDecision,
    MatchingEvidence, MatchingItem, MatchingRun, Medicine,
    MedicineRegistrationLink, RegisteredProduct, StockItem, Drawer,
)
from app.search import normalize


def grls_row(batch, trade, form, reg, *, status="Действующий"):
    return GrlsImportRow(
        batch_id=batch.id, source_workbook="grls2026-11-05-1-Действующий.xlsx",
        source_row=int(reg.split("-")[-1]) + 2 if reg.split("-")[-1].isdigit() else 3,
        status_from_filename=status, registration_number=reg,
        registration_key=normalize(reg), trade_name=trade,
        trade_key=normalize(trade), inn_raw="Данные из вымышленной выгрузки",
        release_forms_raw=form, certificate_holder="Учебный держатель",
    )


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(
        matching_routes, "run_matching",
        lambda run_id: run_matching(run_id, session_factory=factory),
    )
    import_rows(db, [
        {"Код": "VALZ", "Наименование товара": "Валз таб. 160 мг", "Производитель": "Завод А"},
        {"Код": "GK", "Наименование товара":
         "Акридерм ГК туба(крем д/наружн. прим.) 0,05%-0,1%+1", "Производитель": "Завод Б"},
        {"Код": "VALZ-N", "Наименование товара": "Валз Н таб. 160 мг", "Производитель": "Завод В"},
        {"Код": "TEST-AMB", "Наименование товара": "Тестоформ таб. 20 мг", "Производитель": "Завод Г"},
    ], 58, "Учебный ящик")
    batch = GrlsImportBatch(
        sha256=sha256(b"fictional export").hexdigest(),
        archive_name="synthetic-test.zip",
        snapshot_on=date(2026, 11, 5),
        row_count=4,
    )
    db.add(batch)
    db.flush()
    # 1 unique full exact match, 1 GK mismatch in dose, 2 ambiguous same-name
    db.add_all([
        grls_row(batch, "Валз", "таблетки, 160 мг", "ЛП-101"),
        grls_row(batch, "Акридерм", "крем 0,05%, туба 30 г", "ЛП-102"),
        grls_row(batch, "Акридерм ГК", "крем 0,05% + 0,1% + 1%, туба 30 г", "ЛП-103"),
        grls_row(batch, "Тестоформ", "таблетки, 20 мг", "ЛП-104"),
        grls_row(batch, "Тестоформ", "таблетки, 20 мг", "ЛП-105"),
    ])
    batch.row_count = 5
    db.commit()
    try:
        with TestClient(app) as client:
            page = client.get("/manage/matching")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            yield client, db, batch, csrf
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def start(client, batch, csrf, *, force=""):
    return client.post("/manage/matching/start",
                       data={"csrf_token": csrf, "batch_id": batch.id, "force": force},
                       follow_redirects=False)


def test_form_normalization_never_conflates_creams_ointments_or_modified_release():
    assert extract_forms("крем для наружного применения") == ["крем"]
    assert extract_forms("мазь для наружного применения") == ["мазь"]
    stock = stock_attributes("Учебный таб. пролонгированного высвобождения 20 мг")
    source = source_attributes("таблетки 20 мг")
    cmp = evaluate(stock, source, exact_trade=True, source_status="Действующий")
    assert cmp["category"] == "ambiguous"
    assert next(f for f in cmp["evidence"] if f["field"] == "release")["outcome"] == "conflict"
    assert extract_forms("таблетки; крем") == ["крем", "таблетки"]


def test_decimal_dosage_and_combination_strengths_are_not_guessed():
    a, uncertain = extract_doses("0,05% + 0,1% + 1%")
    assert a == [("0.05", "%"), ("0.1", "%"), ("1", "%")]
    assert not uncertain
    b, uncertain_b = extract_doses("0,05%-0,1%+1")
    assert uncertain_b
    assert b == [("0.05", "%"), ("0.1", "%")]
    c, _ = extract_doses("10 мг/мл")
    d, _ = extract_doses("10 мг")
    assert c != d
    e, _ = extract_doses("0,5%")
    assert e != [("0.05", "%")]
    grams, uncertain_g = extract_doses("крем 0,05%, туба 30 г")
    assert grams == [("0.05", "%")]
    assert uncertain_g


def test_full_batch_results_provenance_inventory_and_grls_unchanged(env):
    client, db, batch, csrf = env
    before = [(i.id, i.article_code, i.medicine.name, i.drawer.number)
              for i in db.scalars(select(StockItem).order_by(StockItem.id))]
    imported_before = [(x.id, x.trade_name, x.release_forms_raw)
                       for x in db.scalars(select(GrlsImportRow).order_by(GrlsImportRow.id))]
    response = start(client, batch, csrf)
    assert response.status_code == 303
    run = db.scalar(select(MatchingRun).order_by(MatchingRun.id.desc()))
    db.refresh(run)
    assert run.state == "done"
    assert run.total == run.processed == 4
    assert run.strong == 1
    assert run.ambiguous == 2
    assert run.not_found == 1
    assert db.scalar(select(func.count(MatchingItem.id))) == 4
    assert db.scalar(select(func.count(MatchingEvidence.id))) >= 30
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 0
    assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 0
    assert [(i.id, i.article_code, i.medicine.name, i.drawer.number)
            for i in db.scalars(select(StockItem).order_by(StockItem.id))] == before
    assert [(x.id, x.trade_name, x.release_forms_raw)
            for x in db.scalars(select(GrlsImportRow).order_by(GrlsImportRow.id))] == imported_before
    categories = {i.article_code: i for i in db.scalars(select(MatchingItem))}
    assert categories["VALZ"].category == "strong"
    assert categories["GK"].category == "ambiguous"
    assert categories["VALZ-N"].category == "none"
    assert categories["TEST-AMB"].category == "ambiguous"
    assert "акридерм" not in categories["GK"].parsed_json or "акридерм гк" in categories["GK"].parsed_json
    assert all(c.source_row.trade_name == "Акридерм ГК"
               for c in db.scalars(select(MatchingCandidate).where(
                   MatchingCandidate.item_id == categories["GK"].id)))
    results = client.get(f"/manage/matching/{run.id}")
    assert results.status_code == 200
    assert "Сильные кандидаты" in results.text
    assert "Акридерм ГК" in results.text
    info = client.get(f"/manage/matching/{run.id}/item/{categories['GK'].id}")
    assert info.status_code == 200
    assert "Неполная запись" in info.text
    assert "МНН из выгрузки" in info.text


def test_idempotent_normal_run_and_explicit_forced_rerun(env):
    client, db, batch, csrf = env
    first = start(client, batch, csrf)
    assert first.status_code == 303
    second = start(client, batch, csrf)
    assert second.status_code == 303
    assert second.headers["location"].endswith("?reused=1")
    assert db.scalar(select(func.count(MatchingRun.id))) == 1
    again = start(client, batch, csrf, force="yes")
    assert again.status_code == 303
    assert db.scalar(select(func.count(MatchingRun.id))) == 2
    runs = db.scalars(select(MatchingRun).order_by(MatchingRun.id)).all()
    assert runs[0].input_digest == runs[1].input_digest


def test_review_decision_is_append_only_and_never_auto_confirms_registry(env):
    client, db, batch, csrf = env
    start(client, batch, csrf)
    run = db.scalar(select(MatchingRun))
    candidate = db.scalar(select(MatchingCandidate).join(MatchingItem).where(
        MatchingItem.article_code == "GK"
    ))
    item_id = candidate.item_id
    invalid = client.post(f"/manage/matching/{run.id}/item/{item_id}/decision", data={
        "csrf_token": csrf, "candidate_id": candidate.id,
        "action": "rejected", "comment": "",
    }, follow_redirects=False)
    assert invalid.status_code == 400
    wrong = client.post(f"/manage/matching/{run.id}/item/{item_id}/decision", data={
        "csrf_token": "bad", "candidate_id": candidate.id,
        "action": "deferred", "comment": "",
    }, follow_redirects=False)
    assert wrong.status_code == 403
    for action in ["deferred", "rejected"]:
        resp = client.post(f"/manage/matching/{run.id}/item/{item_id}/decision", data={
            "csrf_token": csrf, "candidate_id": candidate.id,
            "action": action, "comment": "Требуется проверка упаковки",
        }, follow_redirects=False)
        assert resp.status_code == 303
    decisions = db.scalars(select(MatchingDecision).order_by(MatchingDecision.id)).all()
    assert [x.action for x in decisions] == ["deferred", "rejected"]
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 0


def test_export_csv_excel_and_formula_safety(env):
    client, db, batch, csrf = env
    start(client, batch, csrf)
    run = db.scalar(select(MatchingRun))
    csv_result = client.get(f"/manage/matching/{run.id}/export.csv")
    assert csv_result.status_code == 200
    body = csv_result.content.decode("utf-8-sig")
    assert "Валз" in body and "ГРЛС" not in csv_result.headers.get("content-disposition","")
    data = list(csv.reader(body.splitlines(), delimiter=";"))
    assert len(data) == 5
    xlsx_result = client.get(f"/manage/matching/{run.id}/export.xlsx")
    assert xlsx_result.status_code == 200
    book = load_workbook(BytesIO(xlsx_result.content), read_only=True)
    assert book.active.max_row == 5
    assert book.active["C2"].value
    book.close()
    assert matching_routes._safe_cell("=SUM(1,2)").startswith("'")
    assert matching_routes._safe_cell("@HYPERLINK") == "'@HYPERLINK"
    assert matching_routes._safe_cell("+command") == "'+command"


def test_changed_stock_invalidates_cached_run_and_was_not_mutated(env):
    client, db, batch, csrf = env
    start(client, batch, csrf)
    run = db.scalar(select(MatchingRun))
    before_digest = run.input_digest
    medicine = db.scalar(select(Medicine).where(Medicine.name == "Валз таб. 160 мг"))
    medicine.name = "Валз таб. 80 мг"
    db.commit()
    new_digest, _ = inventory_digest(db, batch)
    assert before_digest != new_digest
    old_item = db.scalar(select(MatchingItem).where(MatchingItem.article_code == "VALZ"))
    page = client.get(f"/manage/matching/{run.id}/item/{old_item.id}")
    assert "Данные товара изменились" in page.text
    second = start(client, batch, csrf)
    assert second.status_code == 303
    assert db.scalar(select(func.count(MatchingRun.id))) == 2
    assert db.scalar(select(func.count(StockItem.id))) == 4


def test_running_job_blocks_concurrent_duplicate_launch(env):
    client, db, batch, csrf = env
    run = MatchingRun(batch_id=batch.id, input_digest="a"*64,
                      algorithm_version="0.4.5-conservative-1",
                      state="running", total=4)
    db.add(run)
    db.commit()
    response = start(client, batch, csrf)
    assert response.status_code == 303
    assert response.headers["location"] == f"/manage/matching/{run.id}"
    assert db.scalar(select(func.count(MatchingRun.id))) == 1
    status = client.get(f"/manage/matching/{run.id}/status").json()
    assert status["state"] == "running"
