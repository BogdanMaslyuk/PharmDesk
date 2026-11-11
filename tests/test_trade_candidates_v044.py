"""Regression: do not confuse ordinary Akридерм with combination Akридерм ГК.

All GRLS entries here are synthetic test records. No real registry
information is manufactured or copied into the pharmacy medicine card.
"""
from datetime import date
from hashlib import sha256
import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.importer import import_rows
from app.main import app
from app.models import GrlsImportBatch, GrlsImportRow, MedicineRegistrationLink, RegisteredProduct, StockItem
from app.search import normalize
from app.trade_candidates import source_candidates, trade_name_from_stock, trade_name_key


def test_trade_name_extraction_keeps_brand_suffixes():
    assert trade_name_from_stock(
        "Акридерм ГК туба(крем д/наружн. прим.) 0,05%-0,1%+1"
    ) == "акридерм гк"
    assert trade_name_key(
        "Акридерм ГК туба(крем д/наружн. прим.) 0,05%-0,1%+1"
    ) == "акридермгк"
    assert trade_name_key("Акридерм туба мазь 0,05%") == "акридерм"
    assert trade_name_key("Акридерм Гента крем 0,05%") == "акридермгента"
    assert trade_name_key("Акридерм СК мазь 0,05%") == "акридермск"
    assert trade_name_key("Акридерм ГК") != trade_name_key("Акридерм")
    assert trade_name_key("Но-шпа таблетки 40 мг") == "ношпа"
    assert trade_name_key("Валз таб. 160 мг") == "валз"
    assert trade_name_key("Валз Н таб. 160 мг") == "валзн"
    assert trade_name_key("Валз") != trade_name_key("Валз Н")


def setup():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    import_rows(db, [{
        "Код": "97439",
        "Наименование товара": "Акридерм ГК туба(крем д/наружн. прим.) 0,05%-0,1%+1",
        "Производитель": "Учебный производитель",
    }], 58, "Тестовый ящик")
    item = db.scalar(select(StockItem))
    batch = GrlsImportBatch(
        sha256=sha256(b"synthetic-akriderm-family").hexdigest(),
        archive_name="synthetic-test-only.zip",
        snapshot_on=date.today(), row_count=4,
    )
    db.add(batch)
    db.flush()
    names = [
        ("Акридерм", "Бетаметазон", "ЛП-TEST-001", "крем 0,05%", "Действующий"),
        ("Акридерм ГК", "Бетаметазон+Гентамицин+Клотримазол", "ЛП-TEST-002", "крем 0,05%+0,1%+1%", "Действующий"),
        ("Акридерм Гента", "Бетаметазон+Гентамицин", "ЛП-TEST-003", "крем 0,05%+0,1%", "Действующий"),
        ("Акридерм СК", "Бетаметазон+Салициловая кислота", "ЛП-TEST-004", "мазь 0,05%", "Действующий"),
    ]
    for i, (trade, inn, reg, form, status) in enumerate(names, start=1):
        db.add(GrlsImportRow(
            batch_id=batch.id,
            source_workbook=f"grls{date.today().isoformat()}-1-Действующий.xlsx",
            source_row=i+2, status_from_filename=status,
            registration_number=reg, registration_key=normalize(reg),
            trade_name=trade, trade_key=normalize(trade),
            inn_raw=inn, release_forms_raw=form,
            certificate_holder="Учебный источник",
        ))
    db.commit()
    return engine, db, item.medicine_id, batch


def test_source_candidates_only_same_full_trade_name():
    engine, db, medicine_id, batch = setup()
    try:
        matching = source_candidates(db, batch.id, medicine_name="Акридерм ГК туба(крем)", limit=20)
        assert [x.trade_name for x in matching] == ["Акридерм ГК"]
        ordinary = source_candidates(db, batch.id, medicine_name="Акридерм крем", limit=20)
        assert [x.trade_name for x in ordinary] == ["Акридерм"]
        assert source_candidates(db, batch.id, medicine_name="Акридерм ГКП туба", limit=20) == []
        assert source_candidates(db, batch.id, medicine_name="Валз таб. 160 мг", limit=20) == []
        assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 0
    finally:
        db.close()
        engine.dispose()


def test_medicine_page_and_source_search_exclude_other_drug_variants():
    engine, db, medicine_id, batch = setup()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            detail = client.get(f"/medicines/{medicine_id}")
            assert detail.status_code == 200
            assert "Акридерм ГК" in detail.text
            assert 'href="/catalog/grls/row/2"' in detail.text
            assert 'href="/catalog/grls/row/1"' not in detail.text
            assert 'href="/catalog/grls/row/3"' not in detail.text
            matches = client.get(f"/catalog/grls/matches/{medicine_id}")
            assert matches.status_code == 200
            assert "Ожидаемое название по товару" in matches.text
            assert 'href="/catalog/grls/row/2"' in matches.text
            assert 'href="/catalog/grls/row/1"' not in matches.text
            assert 'href="/catalog/grls/row/3"' not in matches.text
            # A deliberately broadened manual search is never an approval.
            broad = client.get(f"/catalog/grls/matches/{medicine_id}", params={"q": "Акридерм"})
            assert "Внимание" in broad.text
            assert 'href="/catalog/grls/row/1"' in broad.text
            assert db.scalar(select(func.count(RegisteredProduct.id))) == 0
            assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 0
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def test_wrong_family_member_cannot_be_import_source_linked_to_combination():
    engine, db, medicine_id, batch = setup()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            response = client.get(f"/medicines/{medicine_id}/registry")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
            post = client.post(f"/medicines/{medicine_id}/registry", data={
                "csrf_token": csrf,
                "candidate_id": 1, # Synthetic plain Аkридерм
                "registration_number": "ЛП-TEST-001",
                "trade_name": "Акридерм",
                "dosage_form": "крем для наружного применения",
                "strength_text": "0,05%",
                "manufacturer": "Учебный производитель",
                "substances": "Бетаметазон",
                "source_url": "https://grls.rosminzdrav.ru/Default.aspx?record=test-akriderm",
                "comparison_note": "Врач и фармацевт вручную перепроверили название.",
                "source_checked": "yes", "product_checked": "yes",
            })
            assert post.status_code == 200
            assert "Полное торговое название аптечного товара не совпадает" in post.text
            assert db.scalar(select(func.count(RegisteredProduct.id))) == 0
            assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 0
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()
