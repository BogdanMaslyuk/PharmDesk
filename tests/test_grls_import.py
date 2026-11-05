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


def test_source_candidate_requires_exact_reg_and_trade_then_checks_form_strength(env):
    client, db, csrf, medicine_id, _ = env
    raw = archive()
    assert preview(client, csrf, raw).status_code == 200
    assert confirm(client, csrf, raw).status_code == 303
    candidate = db.scalar(select(GrlsImportRow).where(GrlsImportRow.trade_key == "валз"))
    target = f"/medicines/{medicine_id}/registry"
    form = {
        "csrf_token": csrf, "candidate_id": candidate.id,
        "registration_number": candidate.registration_number,
        "trade_name": candidate.trade_name,
        "dosage_form": "таблетки", "strength_text": "160 мг",
        "manufacturer": "Учебный завод",
        "substances": "Валсартан",
        "source_url": "https://grls.rosminzdrav.ru/Default.aspx?id=source-record",
        "comparison_note": "",
        "source_checked": "yes", "product_checked": "yes",
    }
    invalid_num = dict(form, registration_number="ЛП-000007")
    denied = client.post(target, data=invalid_num)
    assert denied.status_code == 200
    assert "Номер регистрационного удостоверения не совпадает" in denied.text
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 0

    invalid_name = dict(form, trade_name="Валз-комби")
    denied = client.post(target, data=invalid_name)
    assert "Торговое название не совпадает" in denied.text
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 0

    uncertain = dict(form, strength_text="80 мг")
    denied = client.post(target, data=uncertain)
    assert "дозировка не найдена точно" in denied.text or "дозировка не найдена" in denied.text or "дозировка" in denied.text
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 0

    valid = client.post(target, data=form, follow_redirects=False)
    assert valid.status_code == 303
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 1
    assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 1
    link = db.scalar(select(MedicineRegistrationLink))
    assert "SHA-256" in link.comparison_note
    assert f"строка {candidate.source_row}" in link.comparison_note
    stock = db.scalar(select(StockItem))
    assert stock.article_code == "TEST-160"
    assert stock.medicine.name == "Валз таб. 160 мг"


def test_realistic_grls_banner_changed_status_long_forms_and_footer():
    from datetime import datetime
    big_forms = "таблетки 160 мг; " * 260  # more than 3500 characters
    assert len(big_forms) > 3500
    w = Workbook()
    s = w.active
    s.append([None] * 3 + ["Государственный реестр на дату выгрузки"])
    s.append([""] * len(HEADERS))
    # In a real GRLS XLSX the columns start at C; insert two blank columns.
    s.append([None, None] + HEADERS)
    s.append([None, None, "Изменённый"])
    rec = {
        "Номер регистрационного удостоверения": "ЛП-00001",
        "Торговое наименование лекарственного препарата": "Учебный препарат",
        "Международное непатентованное или химическое наименование": "Учебное МНН",
        "Формы выпуска": big_forms,
    }
    s.append([None, None] + [rec.get(x) for x in HEADERS])
    s.append([None, None, "05.11.2026 05:00:00"])
    buf = BytesIO()
    w.save(buf)
    raw = BytesIO()
    with ZipFile(raw, "w") as z:
        z.writestr(f"grls{date.today().isoformat()}-1-Изменённый.xlsx", buf.getvalue())
    parsed = parse_grls_archive(raw.getvalue())
    assert parsed["count"] == 1
    assert parsed["status_counts"] == {"Изменённый": 1}
    assert parsed["rows"][0]["registration_number"] == "ЛП-00001"
    assert parsed["rows"][0]["release_forms_raw"] == big_forms.strip()
    assert parsed["rows"][0]["source_row"] == 5


def test_direct_fetch_shows_preview_but_does_not_change_stock(env, monkeypatch):
    import app.grls as grls
    client, db, csrf, medicine_id, _ = env
    raw = archive()
    def mock_fetch():
        return raw, {"page": "https://grls.minzdrav.gov.ru/grls.aspx"}
    monkeypatch.setattr(grls, "fetch_official_grls", mock_fetch)
    before = db.scalar(select(StockItem))
    original = (before.id, before.article_code, before.medicine.name)
    shown = client.post("/catalog/grls/fetch", data={"csrf_token": csrf})
    assert shown.status_code == 200
    assert "Предварительный просмотр" in shown.text
    assert "Получено через HTTPS" in shown.text
    assert db.scalar(select(func.count(GrlsImportRow.id))) == 0
    assert db.scalar(select(func.count(GrlsImportBatch.id))) == 0
    assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 0
    assert confirm(client, csrf, raw).status_code == 303
    after = db.scalar(select(StockItem))
    assert (after.id, after.article_code, after.medicine.name) == original
    assert db.scalar(select(func.count(GrlsImportRow.id))) == 2
    denied = client.post("/catalog/grls/fetch", data={"csrf_token": "bad"})
    assert denied.status_code == 403


def test_official_download_is_pinned_to_government_domain():
    from app.grls_fetch import fetch_official_grls, OFFICIAL_PAGE
    class FakeResponse:
        def __init__(self, payload, url):
            self.payload = BytesIO(payload)
            self.url = url
            self.status = 200
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def geturl(self):return self.url
        def read(self, size=-1):return self.payload.read(size)
    class FakeOpener:
        def __init__(self, page, zip_blob, redirect=None):
            self.page = page.encode("utf-8")
            self.blob = zip_blob
            self.redirect = redirect
        def open(self, request, timeout=10):
            url = request.full_url
            if url == OFFICIAL_PAGE:
                return FakeResponse(self.page, OFFICIAL_PAGE)
            return FakeResponse(self.blob, self.redirect or url)

    control = ("<button onclick=\"go('GetGRLS.ashx?"
               "FileGUID=93f711c0-66a1-4b78-a546-90065477151c"
               "&UserReq=6443605')\">ZIP</button>")
    raw = archive()
    data, meta = fetch_official_grls(opener=FakeOpener(control, raw))
    assert data == raw
    assert meta["downloaded"] is True
    with pytest.raises(GrlsImportError):
        fetch_official_grls(opener=FakeOpener(control, raw, "https://evil.example/zip"))
    with pytest.raises(GrlsImportError):
        fetch_official_grls(opener=FakeOpener("<html>no download button</html>", raw))


def test_missing_forms_in_official_record_are_retained_but_flagged():
    w = Workbook()
    s = w.active
    s.append(HEADERS)
    rec = {
        "Номер регистрационного удостоверения": "ЛП-004040",
        "Торговое наименование лекарственного препарата": "Учебный препарат без формы",
        "Международное непатентованное или химическое наименование": "Учебное МНН",
        # Real GRLS files have some missing form descriptions.
    }
    s.append([rec.get(x) for x in HEADERS])
    inner = BytesIO()
    w.save(inner)
    outer = BytesIO()
    with ZipFile(outer, "w") as z:
        z.writestr(f"grls{date.today().isoformat()}-1-Действующий.xlsx", inner.getvalue())
    parsed = parse_grls_archive(outer.getvalue())
    assert parsed["count"] == 1
    assert parsed["missing_forms"] == 1
    assert parsed["rows"][0]["release_forms_raw"] == ""
    assert parsed["rows"][0]["registration_number"] == "ЛП-004040"
