"""v0.4.1: safe offline parsing of real-documented GRLS ZIP/XLSX structure."""
from datetime import date, timedelta
from hashlib import sha256
from io import BytesIO
import re
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.grls_import import GrlsImportError, parse_grls_archive
from app.importer import import_rows
from app.main import app
from app.models import (
    GrlsImportBatch, GrlsImportRow, Ingredient,
    MedicineRegistrationLink, RegisteredProduct,
    RegisteredSubstance, StockItem,
)

HEADERS = [
    "Номер регистрационного удостоверения",
    "Дата регистрации",
    "Дата окончания действия регистрационного удостоверения",
    "Дата аннулирования регистрационного удостоверения",
    "Юридическое лицо, на имя которого выдано регистрационное удостоверение",
    "Торговое наименование лекарственного препарата",
    "Международное непатентованное или химическое наименование",
    "Формы выпуска",
    "Сведения о стадиях производства",
    "Нормативная документация",
    "Фармако-терапевтическая группа",
    "Наличие лекарственного препарата в перечне ЖНВЛП",
    "Наличие в лекарственном препарате наркотических средств, психотропных веществ",
    "Статус признания лекарственного препарата орфанным",
]


def book(trade="Валз", reg="ЛП-000001", status="Действующий", without_header=False):
    w = Workbook()
    s = w.active
    s.title = "Сведения ГРЛС"
    s.append(["Государственный реестр лекарственных средств"])
    headings = [x for x in HEADERS if not (without_header and x == "Формы выпуска")]
    s.append(headings)
    vals = {
        "Номер регистрационного удостоверения": reg,
        "Дата регистрации": "01.01.2026",
        "Юридическое лицо, на имя которого выдано регистрационное удостоверение": "Держатель регистрации",
        "Торговое наименование лекарственного препарата": trade,
        "Международное непатентованное или химическое наименование": "Валсартан",
        "Формы выпуска": "таблетки покрытые плёночной оболочкой, 160 мг №28",
    }
    s.append([vals.get(h) for h in headings])
    output = BytesIO()
    w.save(output)
    return output.getvalue()


def archive(*, stamp=None, mixed=False, bad_name=False, missing_header=False):
    stamp = stamp or date.today()
    data = BytesIO()
    with ZipFile(data, "w") as z:
        base = f"grls{stamp.isoformat()}-1-"
        file1 = "unfamiliar.xlsx" if bad_name else base + "Действующий.xlsx"
        z.writestr(file1, book(without_header=missing_header))
        other_date = stamp + timedelta(days=1) if mixed else stamp
        z.writestr(f"grls{other_date.isoformat()}-1-Исключенный.xlsx",
                   book(trade="Пример исключённого", reg="ЛП-000002"))
    return data.getvalue()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import app.grls as grls
    monkeypatch.setattr(grls, "PREVIEW_FOLDER", tmp_path / "grls_previews")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    app.dependency_overrides[get_db] = lambda: db
    import_rows(db, [{
        "Код": "TEST-160", "Наименование товара": "Валз таб. 160 мг",
        "Производитель": "Учебный завод",
    }], 13, "Учебный ящик")
    item = db.scalar(select(StockItem))
    try:
        with TestClient(app) as client:
            response = client.get("/catalog/grls/import")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
            yield client, db, csrf, item.medicine_id, tmp_path
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def preview(client, csrf, raw, *, attested="yes"):
    return client.post("/catalog/grls/preview",
        data={"csrf_token": csrf, "claimed_official": attested},
        files={"file": ("grls_download.zip", raw, "application/zip")})


def confirm(client, csrf, raw, *, word="ИМПОРТИРОВАТЬ"):
    return client.post("/catalog/grls/confirm", data={
        "csrf_token": csrf, "digest": sha256(raw).hexdigest(),
        "original_filename": "grls_download.zip",
        "typed_confirmation": word,
    }, follow_redirects=False)


def test_parser_preserves_raw_forms_statuses_and_provenance():
    raw = archive()
    parsed = parse_grls_archive(raw)
    assert parsed["count"] == 2
    assert parsed["sha256"] == sha256(raw).hexdigest()
    assert parsed["snapshot_on"] == date.today()
    assert parsed["status_counts"] == {
        "Действующий": 1, "Исключённый": 1,
    }
    assert len(parsed["source_files"]) == 2
    assert parsed["rows"][0]["source_row"] == 3
    assert parsed["rows"][0]["registration_number"] == "ЛП-000001"
    assert parsed["rows"][0]["trade_key"] == "валз"
    assert parsed["rows"][0]["release_forms_raw"] == "таблетки покрытые плёночной оболочкой, 160 мг №28"
    assert parsed["rows"][0]["certificate_holder"] == "Держатель регистрации"


def test_preview_is_read_only_then_explicit_commit_only_adds_staging(env):
    client, db, csrf, medicine_id, tmp_path = env
    raw = archive()
    before = db.scalar(select(StockItem))
    before_tuple = (before.id, before.article_code, before.medicine.name)
    page = preview(client, csrf, raw)
    assert page.status_code == 200
    assert "Предварительный просмотр" in page.text
    assert "ИМПОРТИРОВАТЬ" in page.text
    assert "ЛП-000001" in page.text
    assert db.scalar(select(func.count(GrlsImportBatch.id))) == 0
    assert db.scalar(select(func.count(GrlsImportRow.id))) == 0
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 0
    assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 0

    denied = confirm(client, csrf, raw, word="NO")
    assert denied.status_code == 200
    assert "ИМПОРТИРОВАТЬ" in denied.text
    assert db.scalar(select(func.count(GrlsImportRow.id))) == 0

    saved = confirm(client, csrf, raw)
    assert saved.status_code == 303
    assert "/catalog/grls?" in saved.headers["location"]
    assert db.scalar(select(func.count(GrlsImportBatch.id))) == 1
    assert db.scalar(select(func.count(GrlsImportRow.id))) == 2
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 0
    assert db.scalar(select(func.count(RegisteredSubstance.id))) == 0
    assert db.scalar(select(func.count(Ingredient.id))) == 0
    assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 0
    item = db.scalar(select(StockItem))
    assert (item.id, item.article_code, item.medicine.name) == before_tuple

    batch = db.scalar(select(GrlsImportBatch))
    assert batch.snapshot_on == date.today()
    assert batch.origin_statement == "user_supplied_unverified"
    browser = client.get("/catalog/grls")
    assert "Валз" in browser.text
    assert "Источник не удостоверен" in client.get("/catalog/grls/row/1").text
    assert client.get("/catalog/grls", params={"q": "ЛП-000002"}).status_code == 200

    second = preview(client, csrf, raw)
    assert "уже импортирован" in second.text
    assert db.scalar(select(func.count(GrlsImportRow.id))) == 2


def test_exact_name_candidates_do_not_autolink_or_assume_strength(env):
    client, db, csrf, medicine_id, _ = env
    raw = archive()
    preview(client, csrf, raw)
    assert confirm(client, csrf, raw).status_code == 303
    found = client.get(f"/catalog/grls/matches/{medicine_id}")
    assert found.status_code == 200
    assert "Кандидат" in found.text or "кандидат" in found.text
    assert "Валз" in found.text
    assert "Начать ручную сверку" in found.text
    source_row = db.scalar(select(GrlsImportRow).where(GrlsImportRow.trade_key == "валз"))
    selected = client.get(f"/medicines/{medicine_id}/registry",
                          params={"candidate_id": source_row.id})
    assert selected.status_code == 200
    assert "Дозировка, лекарственная форма, состав" in selected.text
    assert 'value="ЛП-000001"' in selected.text
    assert 'value="Валз"' in selected.text
    assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 0
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 0


def test_bad_formats_are_rejected_without_database_effect(env):
    client, db, csrf, _, _ = env
    files = [
        b"not a zip",
        archive(bad_name=True),
        archive(missing_header=True),
        archive(mixed=True),
    ]
    for raw in files:
        resp = preview(client, csrf, raw)
        assert resp.status_code == 200
        assert "form-error" in resp.text
        assert db.scalar(select(func.count(GrlsImportRow.id))) == 0
    missing_claim = preview(client, csrf, archive(), attested="")
    assert "официального сайта" in missing_claim.text
    assert db.scalar(select(func.count(GrlsImportBatch.id))) == 0


def test_bad_hash_and_csrf_are_rejected_without_changes(env):
    client, db, csrf, _, _ = env
    raw = archive()
    preview(client, csrf, raw)
    wrong = client.post("/catalog/grls/confirm", data={
        "csrf_token": csrf, "digest": "d" * 64,
        "typed_confirmation": "ИМПОРТИРОВАТЬ",
    })
    assert "не найден" in wrong.text
    denied = client.post("/catalog/grls/confirm", data={
        "csrf_token": "bad", "digest": sha256(raw).hexdigest(),
        "typed_confirmation": "ИМПОРТИРОВАТЬ",
    })
    assert denied.status_code == 403
    assert db.scalar(select(func.count(GrlsImportBatch.id))) == 0


def test_archived_expired_snapshot_is_imported_only_as_historical(env):
    client, db, csrf, _, _ = env
    old_date = date.today() - timedelta(days=60)
    old_raw = archive(stamp=old_date)
    response = preview(client, csrf, old_raw)
    assert response.status_code == 200
    assert "Устаревшая выгрузка" in response.text
    assert confirm(client, csrf, old_raw).status_code == 303
    batch = db.scalar(select(GrlsImportBatch))
    assert batch.snapshot_on == old_date
    assert "Снимок старше 30 дней" in client.get("/catalog/grls").text
