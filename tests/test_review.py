"""v0.3 regression: OCR is never trusted until explicitly reviewed with source image."""
import re
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import ReviewRecord, ReviewDecision, StockItem, SubstanceEvidence
from app.review_import import parse_workbook, prepare_images, ReviewImportError


@pytest.fixture()
def environment(tmp_path, monkeypatch):
    # Prevent tests from writing source images into repository paths.
    import app.review_import as importer
    import app.review as review
    local_photos = tmp_path / "review_images"
    monkeypatch.setattr(importer, "IMAGE_DIRECTORY", local_photos)
    monkeypatch.setattr(review, "IMAGE_DIRECTORY", local_photos)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            html = client.get("/review").text
            token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
            yield client, db, token, local_photos
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def workbook(name="Валз таб. 160 мг", status="Требует ручной проверки"):
    wb = Workbook()
    ws = wb.active
    ws.title = "Все позиции"
    ws.append([
        "Ящик", "Название ящика", "Код", "Наименование товара",
        "Производитель", "Дата добавления", "Статус сверки", "Замечания",
        "Исходное изображение", "Строка на снимке", "Прежнее название",
    ])
    ws.append([
        13, "Сартаны (по рецепту)", "417157", name, "Изготовитель",
        "03.03.2026", status, "Название сверить", "13 1.PNG", 7, "Bans таб. 160 мг",
    ])
    ws.append([
        13, "Сартаны (по рецепту)", "1573310", "Кардосал Плюс",
        "Изготовитель", "03.03.2026", "Сверено двумя распознаваниями",
        None, "13 1.PNG", 8, "Kapaocan",
    ])
    data = BytesIO()
    wb.save(data)
    return data.getvalue()


def photos():
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("ЛС/13 1.PNG", b"\x89PNG\r\n\x1a\n" + b"fake-png-test" * 30)
    return data.getvalue()


def upload(client, token, kind, filename, contents):
    return client.post(f"/review/upload/{kind}",
        data={"csrf_token": token},
        files={"file": (filename, contents, "application/octet-stream")})


def test_workbook_is_queue_only_and_photo_is_required(environment):
    client, db, token, path = environment
    response = upload(client, token, "workbook", "review.xlsx", workbook())
    assert response.status_code == 200
    assert "2" in response.text
    assert db.scalar(select(func.count(ReviewRecord.id))) == 2
    assert db.scalar(select(func.count(StockItem.id))) == 0
    assert db.scalar(select(func.count(ReviewRecord.id)).where(ReviewRecord.priority.is_(True))) == 1

    rec = db.scalar(select(ReviewRecord).where(ReviewRecord.article_code == "417157"))
    assert rec.state == "pending"

    not_yet = client.post(f"/review/{rec.id}/approve", data={
        "csrf_token": token, "code": "417157", "name": "Валз таб. 160 мг",
        "manufacturer": "Изготовитель", "visually_checked": "yes",
    })
    assert "Сначала загрузите исходный архив" in not_yet.text
    assert db.scalar(select(func.count(StockItem.id))) == 0

    image_result = upload(client, token, "images", "originals.zip", photos())
    assert image_result.status_code == 200
    assert "Фотографий в локальной базе" in image_result.text
    assert client.get(f"/review/{rec.id}").status_code == 200
    image_page = client.get("/review")
    assert "Фото есть" in image_page.text

    approved = client.post(f"/review/{rec.id}/approve", data={
        "csrf_token": token, "code": "417157", "name": "Валз таб. 160 мг",
        "manufacturer": "Изготовитель", "visually_checked": "yes",
        "comment": "Checked against original screenshot",
    }, follow_redirects=False)
    assert approved.status_code == 303
    db.refresh(rec)
    assert rec.state == "approved"
    assert db.scalar(select(func.count(StockItem.id))) == 1
    item = db.scalar(select(StockItem))
    assert item.medicine.name == "Валз таб. 160 мг"
    assert item.source == "сверено с фото"
    assert db.scalar(select(func.count(ReviewDecision.id))) == 1
    src = client.get(f"/review/{rec.id}").text
    assert "13 1.PNG" in src


def test_import_does_not_revert_manual_confirmation(environment):
    client, db, token, _ = environment
    upload(client, token, "workbook", "review.xlsx", workbook())
    upload(client, token, "images", "originals.zip", photos())
    rec = db.scalar(select(ReviewRecord).where(ReviewRecord.article_code == "417157"))
    client.post(f"/review/{rec.id}/approve", data={
        "csrf_token": token, "code": "417157", "name": "Валз 160мг — исправлено вручную",
        "manufacturer": "Изготовитель", "visually_checked": "yes",
    })
    result = upload(client, token, "workbook", "review.xlsx", workbook("Bans"))
    assert result.status_code == 200
    db.refresh(rec)
    assert rec.state == "approved"
    assert rec.confirmed_name == "Валз 160мг — исправлено вручную"
    assert db.scalar(select(StockItem)).medicine.name == rec.confirmed_name


def test_exclusion_removes_legacy_stock_with_confirmation(environment):
    client, db, token, _ = environment
    upload(client, token, "workbook", "review.xlsx", workbook())
    upload(client, token, "images", "originals.zip", photos())
    rec = db.scalar(select(ReviewRecord).where(ReviewRecord.article_code == "417157"))
    client.post(f"/review/{rec.id}/approve", data={
        "csrf_token": token, "code": "417157", "name": "Валз",
        "manufacturer": "", "visually_checked": "yes",
    })
    refused = client.post(f"/review/{rec.id}/exclude", data={
        "csrf_token": token, "reason": "wrong", "typed_confirmation": "no",
    })
    assert "ИСКЛЮЧИТЬ" in refused.text
    assert db.scalar(select(func.count(StockItem.id))) == 1

    deleted = client.post(f"/review/{rec.id}/exclude", data={
        "csrf_token": token, "reason": "Исключить ошибочный товар",
        "typed_confirmation": "ИСКЛЮЧИТЬ",
    }, follow_redirects=False)
    assert deleted.status_code == 303
    assert db.scalar(select(func.count(StockItem.id))) == 0
    db.refresh(rec)
    assert rec.state == "excluded"


def test_import_rejects_duplicate_and_not_real_image():
    data = workbook()
    parsed = parse_workbook(data)
    assert len(parsed) == 2
    assert sum(x["priority"] for x in parsed) == 1
    fake_zip = BytesIO()
    with ZipFile(fake_zip, "w") as archive:
        archive.writestr("bad.PNG", b"not an image" * 20)
    with pytest.raises(ReviewImportError):
        prepare_images(fake_zip.getvalue())


def test_bad_csrf_cannot_approve(environment):
    client, db, token, _ = environment
    upload(client, token, "workbook", "review.xlsx", workbook())
    upload(client, token, "images", "originals.zip", photos())
    rec = db.scalar(select(ReviewRecord).where(ReviewRecord.article_code == "417157"))
    r = client.post(f"/review/{rec.id}/approve", data={
        "csrf_token": "incorrect", "code": "417157", "name": "Bad",
        "visually_checked": "yes",
    })
    assert r.status_code == 403
    assert db.scalar(select(func.count(StockItem.id))) == 0


def test_drug_identity_is_only_manually_sourced(environment):
    client, db, token, _ = environment
    from app.importer import import_rows
    import_rows(db, [{
        "Код": "DEMO-X", "Наименование товара": "Учебный препарат",
        "Производитель": "Учебный завод",
    }], 100, "Учебная группа")
    item = db.scalar(select(StockItem))
    url = f"/medicines/{item.medicine_id}/identity"
    assert client.get(url).status_code == 200
    bad = client.post(url, data={
        "csrf_token": token, "substances": "Учебное вещество",
        "source_url": "https://example.org/invalid", "visually_checked": "yes",
    })
    assert "ГРЛС Минздрава" in bad.text
    assert db.scalar(select(func.count(SubstanceEvidence.id))) == 0
    ok = client.post(url, data={
        "csrf_token": token, "substances": "Учебное вещество",
        "source_url": "https://grls.minzdrav.gov.ru/", "visually_checked": "yes",
    }, follow_redirects=False)
    assert ok.status_code == 303
    assert db.scalar(select(func.count(SubstanceEvidence.id))) == 1


def test_manual_code_edit_requires_recheck_and_stays_locked(environment):
    client, db, token, _ = environment
    from app.importer import import_rows
    from app.review_import import import_reviews
    upload(client, token, "workbook", "review.xlsx", workbook())
    upload(client, token, "images", "originals.zip", photos())
    rec = db.scalar(select(ReviewRecord).where(ReviewRecord.article_code == "417157"))
    ok = client.post(f"/review/{rec.id}/approve", data={
        "csrf_token": token, "code": "417157", "name": "Валз",
        "manufacturer": "Изготовитель", "visually_checked": "yes",
    }, follow_redirects=False)
    assert ok.status_code == 303
    item = db.scalar(select(StockItem))
    changed = client.post(f"/manage/items/{item.id}/edit", data={
        "csrf_token": token, "article_code": "417158",
        "name": "Валз — ручное изменение", "manufacturer": "Изготовитель",
    }, follow_redirects=False)
    assert changed.status_code == 303
    db.refresh(rec)
    assert rec.state == "pending"
    assert rec.confirmed_code == "417158"
    assert rec.checked_at is not None
    assert db.scalar(select(StockItem)).source == "вручную, требуется сверка"

    old = import_rows(db, [{
        "Код": "417157", "Наименование товара": "Bans 160 мг",
        "Производитель": "Изготовитель",
    }], 13, "Сартаны (по рецепту)")
    new = import_rows(db, [{
        "Код": "417158", "Наименование товара": "Kapaocan",
        "Производитель": "Изготовитель",
    }], 13, "Сартаны (по рецепту)")
    assert old["protected"] == 1
    assert new["protected"] == 1
    assert db.scalar(select(func.count(StockItem.id))) == 1
    assert db.scalar(select(StockItem)).medicine.name == "Валз — ручное изменение"

    again = import_reviews(db, workbook("Bans"))
    assert again["preserved_manual_decisions"] == 1
    db.refresh(rec)
    assert rec.confirmed_code == "417158"
    assert rec.state == "pending"

    reapproved = client.post(f"/review/{rec.id}/approve", data={
        "csrf_token": token, "code": "417158",
        "name": "Валз — перепроверен по фото",
        "manufacturer": "Изготовитель", "visually_checked": "yes",
    }, follow_redirects=False)
    assert reapproved.status_code == 303
    db.refresh(rec)
    assert rec.state == "approved"
    assert db.scalar(select(StockItem)).medicine.name == "Валз — перепроверен по фото"


def test_review_page_reports_drawer_progress_and_photo_zoom(environment):
    client, db, token, _ = environment
    upload(client, token, "workbook", "review.xlsx", workbook())
    page = client.get("/review")
    assert page.status_code == 200
    assert "Прогресс проверки по ящикам" in page.text
    assert '№13' in page.text
    assert "2" in page.text

    rec = db.scalar(select(ReviewRecord).where(ReviewRecord.article_code == "417157"))
    item_page = client.get(f"/review/{rec.id}")
    assert item_page.status_code == 200
    assert "Исходный размер" not in item_page.text
    upload(client, token, "images", "originals.zip", photos())
    item_page = client.get(f"/review/{rec.id}")
    assert "Исходный размер" in item_page.text
    assert "data-review-photo" in item_page.text
    assert "/static/review-photo.js" in item_page.text
