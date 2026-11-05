"""v0.4.3 — GRLS imports visible in main reference catalog, not silent staging."""
from datetime import date, timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.importer import import_rows
from app.main import app
from app.models import (
    GrlsImportBatch, GrlsImportRow, Ingredient,
    MedicineRegistrationLink, RegisteredProduct, StockItem,
)


@pytest.fixture()
def inventory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    app.dependency_overrides[get_db] = lambda: db
    import_rows(db, [{
        "Код": "TEST01", "Наименование товара": "Валз таб. 160 мг",
        "Производитель": "Учебное предприятие",
    }], 13, "Учебный ящик")
    try:
        with TestClient(app) as client:
            yield client, db
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def make_snapshot(db, stamp=None, *, prefix="a", rows=2):
    stamp = stamp or date.today()
    batch = GrlsImportBatch(
        sha256=sha256((prefix + str(stamp)).encode()).hexdigest(),
        snapshot_on=stamp, archive_name="GRLS_test_only.zip",
        row_count=rows,
    )
    db.add(batch)
    db.flush()
    for index in range(rows):
        db.add(GrlsImportRow(
            batch_id=batch.id,
            source_workbook=f"grls{stamp.isoformat()}-1-Действующий.xlsx",
            source_row=index + 5,
            status_from_filename="Действующий" if index != 1 else "Истёкший",
            registration_number=f"ЛП-{index + 1:06}",
            registration_key=f"лп{index + 1:06}",
            trade_name="Валз" if index == 0 else f"Препарат {index + 1}",
            trade_key="валз" if index == 0 else f"препарат{index + 1}",
            inn_raw="Валсартан" if index == 0 else "Учебное вещество",
            release_forms_raw="таблетки 160 мг" if index == 0 else "",
            certificate_holder="Держатель РУ",
        ))
    db.commit()
    return batch


def test_no_import_not_mistaken_for_a_successful_registry_import(inventory):
    client, db = inventory
    source = client.get("/catalog")
    assert source.status_code == 200
    assert "пока нет сохранённой выгрузки ГРЛС" in source.text
    assert "Карточек, сверенных человеком" in source.text
    assert "ИМПОРТИРОВАТЬ" in source.text
    home = client.get("/")
    assert "ГРЛС ещё не сохранён в этой базе" in home.text
    assert db.scalar(select(func.count(GrlsImportBatch.id))) == 0


def test_existing_staged_rows_appear_in_main_catalog_without_reimport(inventory):
    client, db = inventory
    batch = make_snapshot(db)
    page = client.get("/catalog")
    assert page.status_code == 200
    assert "ГРЛС · импортированные записи" in page.text
    assert "Загруженная выгрузка обнаружена" in page.text
    assert "Валз" in page.text
    assert "Валсартан" in page.text
    assert "Исходные данные реестра" in page.text
    assert batch.snapshot_on.strftime("%d.%m.%Y") in page.text
    assert f"/catalog/grls/row/" in page.text
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 0
    assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 0
    assert db.scalar(select(func.count(Ingredient.id))) == 0
    home = client.get("/")
    assert "Смотреть импортированные препараты" in home.text
    assert "ИМПОРТИРОВАННЫЕ СВЕДЕНИЯ ГРЛС" in home.text
    assert "2" in home.text
    item = db.scalar(select(StockItem))
    assert item.article_code == "TEST01"
    assert item.medicine.name == "Валз таб. 160 мг"


def test_search_by_inn_trade_and_registration_with_unverified_badge(inventory):
    client, db = inventory
    make_snapshot(db)
    for key in ["валсартан", "ВАЛСАРТАН", "Валз", "лп000001", "ЛП-000001"]:
        res = client.get("/catalog", params={"q": key})
        assert res.status_code == 200
        assert "Валз" in res.text
        assert "ЛП-000001" in res.text
        assert "Валсартан" in res.text
    missing = client.get("/catalog", params={"q": "метформин"})
    assert missing.status_code == 200
    assert "совпадений нет" in missing.text
    assert "не доказывает отсутствия регистрации" in missing.text
    expired = client.get("/catalog", params={"status": "Истёкший"})
    assert "Препарат 2" in expired.text
    assert 'href="/catalog/grls/row/' in expired.text
    assert 'href="/catalog/grls/row/1"' not in expired.text
    invalid = client.get("/catalog", params={"status": "неверный"})
    assert invalid.status_code == 200
    assert "Валз" in invalid.text


def test_curated_tab_separate_and_not_overwritten_by_grls(inventory):
    client, db = inventory
    make_snapshot(db)
    manual = RegisteredProduct(
        identity_key=sha256(b"manually verified").hexdigest(),
        registration_number="УЧ-123",
        trade_name="Подтверждённый вручную препарат",
        dosage_form="таблетки",
        strength_text="20 мг",
        manufacturer="Учебный изготовитель",
        source_title="ГРЛС Минздрава",
        source_url="https://grls.rosminzdrav.ru/example?id=123",
        checked_on=date.today(),
    )
    db.add(manual)
    db.commit()
    main = client.get("/catalog")
    assert "Валз" in main.text
    assert "Подтверждённый вручную препарат" not in main.text
    curated = client.get("/catalog", params={"view": "manual"})
    assert "Подтверждённый вручную препарат" in curated.text
    assert "Внесено вручную" in curated.text
    assert "Валсартан" not in curated.text
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 1
    assert db.scalar(select(func.count(GrlsImportRow.id))) == 2


def test_latest_snapshot_selected_search_filters_do_not_modify_source(inventory):
    client, db = inventory
    old = make_snapshot(db, date.today() - timedelta(days=2), prefix="older", rows=1)
    new = make_snapshot(db, date.today(), prefix="new", rows=34)
    page = client.get("/catalog")
    assert f"Выгрузка от {new.snapshot_on.strftime('%d.%m.%Y')}" in page.text
    assert ">34<" in page.text
    assert "Страница 1 из 2" in page.text
    page2 = client.get("/catalog", params={"page_number": 2})
    assert "Страница 2 из 2" in page2.text
    assert page2.status_code == 200
    prior = client.get("/catalog", params={"batch_id": old.id})
    assert prior.status_code == 200
    assert "Страница 1 из 1" not in prior.text
    assert "ЛП-000001" in prior.text
    before = db.scalar(select(func.count(GrlsImportRow.id)))
    assert before == 35
    _ = client.get("/catalog", params={"q": "валсартан", "status": "Действующий"})
    assert db.scalar(select(func.count(GrlsImportRow.id))) == before
