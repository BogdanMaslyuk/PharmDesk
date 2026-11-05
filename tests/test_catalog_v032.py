"""v0.3.2: exact-first search and read-only catalogue quality reporting."""
import re
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.catalog_quality import audit_catalog
from app.database import Base, get_db
from app.importer import import_rows
from app.main import app
from app.models import Drawer, Medicine, ReviewRecord, StockItem
from app.search import bounded_distance, matches, normalize, search_stock


@pytest.fixture
def env():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            yield client, db
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def add(db, drawer, code, name, maker=""):
    import_rows(db, [{
        "Код": code, "Наименование товара": name,
        "Производитель": maker
    }], drawer, "Учебный ящик")


def test_exact_first_and_cyrillic_one_letter_typo(env):
    client, db = env
    add(db, 13, "T-01", "Бетагистин 16 мг", "Учебная компания")
    add(db, 13, "T-02", "Бетагестин 16 мг", "Учебная компания")
    exact = client.get("/api/search", params={"q": "бетагестин"}).json()
    assert [row["code"] for row in exact] == ["T-02"]
    assert not exact[0]["approximate"]

    # Misspelling with no exact match has a clearly labelled candidate.
    nearby = client.get("/api/search", params={"q": "бетагистен"}).json()
    assert len(nearby) == 1
    assert nearby[0]["code"] == "T-01"
    assert nearby[0]["approximate"] is True

    html = client.get("/medicines", params={"q": "бетагистен"}).text
    assert "Точных совпадений нет" in html
    assert "Похоже по написанию" in html
    assert "T-01" in html

    html = client.get("/drawers/13", params={"q": "бетагистен"}).text
    assert "похожие написания" in html.lower()


def test_code_search_stays_exact_and_dosages_do_not_fuzz(env):
    client, db = env
    add(db, 13, "417157", "Валз таб. 160 мг")
    add(db, 14, "417158", "Валз таб. 80 мг")
    assert client.get("/api/search", params={"q": "417157"}).json()[0]["code"] == "417157"
    assert client.get("/api/search", params={"q": "417159"}).json() == []
    assert client.get("/api/search", params={"q": "161 мг"}).json() == []
    assert client.get("/api/search", params={"q": "валз"}).json()[0]["approximate"] is False


def test_fuzzy_is_not_therapeutic_or_short_guessing(env):
    client, db = env
    add(db, 13, "S-1", "Лозартан 50 мг")
    add(db, 13, "S-2", "Валсартан 80 мг")
    assert client.get("/api/search", params={"q": "Валз"}).json() == []
    assert client.get("/api/search", params={"q": "валсартон"}).json()[0]["code"] == "S-2"
    assert client.get("/api/search", params={"q": "лозартон"}).json()[0]["code"] == "S-1"
    assert client.get("/api/search", params={"q": "лозартан 80"}).json() == []
    assert client.get("/api/search", params={"q": "valsartan"}).json() == []


def test_search_normalization_and_distance():
    assert normalize(" ЁЛКА — 25 мг") == "елка25мг"
    assert matches("500 мг. табл. №20", "500 мг")
    assert bounded_distance("бетагестин", "бетагистин") == 1
    assert bounded_distance("кардиомагнил", "кардиомегнил") == 1
    assert bounded_distance("валз", "валсартан") > 1


def test_quality_flags_and_deduplicates_without_mutating_db(env):
    client, db = env
    add(db, 13, "A-1", "Bans 160 мг", "Завод")
    add(db, 13, "A-2", "Bans 160 мг", "Завод")
    add(db, 13, "A-3", "Неизвестно ???", "Завод")
    add(db, 13, "A-4", "РитоNавир", "Завод")
    add(db, 13, "A-5", "Товар проверенный", "Завод")
    add(db, 13, "C-4", "Препарат один", "Завод")
    add(db, 14, "C-4", "Другое название", "Завод")

    db.add(ReviewRecord(
        drawer_number=13, drawer_name="Учебный ящик",
        article_code="A-5", proposed_name="Товар проверенный",
        proposed_manufacturer="Завод", source_file="sheet_1.png",
        source_row=1, state="approved", confirmed_code="A-5",
    ))
    db.commit()

    items = db.scalars(select(StockItem)).all()
    for item in items:
        _ = item.medicine, item.drawer
    records = db.scalars(select(ReviewRecord)).all()
    report = audit_catalog(items, records)
    assert report["total_stock"] == 7
    assert report["confirmed"] == 1
    assert report["counts"]["latin"] == 2
    assert report["counts"]["mixed"] >= 1
    assert report["counts"]["duplicate"] == 2
    assert report["counts"]["code_conflict"] == 2
    assert report["counts"]["garbled"] >= 1

    before = [(s.id, s.article_code, s.medicine.name) for s in items]
    page = client.get("/manage/quality")
    assert page.status_code == 200
    assert "Контроль качества каталога" in page.text
    assert "Bans 160 мг" in page.text
    assert "Предупреждение не означает ошибку" in page.text
    page = client.get("/manage/quality", params={"reason": "code_conflict", "drawer": 14})
    assert "Другое название" in page.text
    assert "Bans 160 мг" not in page.text
    after = [(s.id, s.article_code, s.medicine.name)
             for s in db.scalars(select(StockItem)).all()]
    assert before == after


def test_quality_no_duplicate_on_exact_same_stock_code_across_drawers(env):
    client, db = env
    add(db, 13, "Q01", "Валз 160 мг")
    add(db, 23, "Q01", "Валз 160 мг")
    report = audit_catalog(db.scalars(select(StockItem)).all(), [])
    assert report["counts"]["code_conflict"] == 0
    assert report["counts"]["duplicate"] == 0


def test_search_filter_does_not_query_other_drawers(env):
    client, db = env
    add(db, 13, "D-13", "Бетагистин 16 мг")
    add(db, 23, "D-23", "Бетагестин 16 мг")
    response = client.get("/medicines", params={"q": "бетагистен", "drawer": 13})
    assert response.status_code == 200
    assert "D-13" in response.text
    assert "D-23" not in response.text
