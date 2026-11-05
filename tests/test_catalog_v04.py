"""v0.4: explicit, sourced registrations without automatic clinical deductions."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.catalog_sources import (
    CatalogValidationError, parse_substance_names,
    registry_identity_key, safe_official_url,
)
from app.database import Base, get_db
from app.importer import import_rows
from app.main import app
from app.models import (
    ClinicalRecord, Ingredient, Medicine, MedicineRegistrationLink,
    RegisteredComposition, RegisteredProduct, RegisteredSubstance, StockItem,
)


REGISTRY_URL = "https://grls.rosminzdrav.ru/Default.aspx?test=example-record"


@pytest.fixture
def environment():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    app.dependency_overrides[get_db] = lambda: db
    try:
        import_rows(db, [{
            "Код": "DEMO-A", "Наименование товара": "Учебное Альфа таб. 10 мг",
            "Производитель": "Учебный завод",
        }], 101, "Учебный ящик")
        item = db.scalar(select(StockItem))
        with TestClient(app) as client:
            page = client.get(f"/medicines/{item.medicine_id}/registry")
            assert page.status_code == 200
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            yield client, db, csrf, item.medicine_id
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def form(csrf, *, substances="Учебное вещество А\nУчебное вещество Б",
         source=REGISTRY_URL, trade="Учебное Альфа", strength="10 мг",
         note="", checked=True):
    return {
        "csrf_token": csrf,
        "registration_number": "УЧ-0001/01",
        "trade_name": trade,
        "dosage_form": "таблетки",
        "strength_text": strength,
        "manufacturer": "Учебный завод",
        "substances": substances,
        "source_url": source,
        "comparison_note": note,
        "source_checked": "yes" if checked else "",
        "product_checked": "yes" if checked else "",
    }


def test_manual_registry_catalog_and_source_preserved_without_stock_mutation(environment):
    client, db, csrf, medicine_id = environment
    existing = db.scalar(select(StockItem))
    original = (existing.id, existing.article_code, existing.medicine.name)

    response = client.post(f"/medicines/{medicine_id}/registry", data=form(csrf), follow_redirects=False)
    assert response.status_code == 303
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 1
    assert db.scalar(select(func.count(RegisteredSubstance.id))) == 2
    assert db.scalar(select(func.count(RegisteredComposition.id))) == 2
    assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 1

    entry = db.scalar(select(RegisteredProduct))
    assert entry.registration_number == "УЧ-0001/01"
    assert entry.strength_text == "10 мг"
    assert entry.source_url == REGISTRY_URL
    assert entry.source_title == "ГРЛС Минздрава России"
    assert entry.verification_method == "manual_reference_check"
    assert db.scalar(select(MedicineRegistrationLink)).state == "active"

    assert db.scalar(select(func.count(Ingredient.id))) == 0
    assert db.scalar(select(func.count(ClinicalRecord.id))) == 0
    stock = db.scalar(select(StockItem))
    assert (stock.id, stock.article_code, stock.medicine.name) == original
    assert client.get("/api/search", params={"q": "Учебное вещество А"}).json() == []

    page = client.get("/catalog")
    assert page.status_code == 200
    assert "Учебное Альфа" in page.text
    assert "Внесено вручную" in page.text
    assert "Учебное вещество А" in page.text
    assert client.get("/catalog", params={"q": "Учебное вещество Б"}).status_code == 200
    detail = client.get(f"/catalog/{entry.id}")
    assert detail.status_code == 200
    assert REGISTRY_URL.replace("&", "&amp;") in detail.text
    assert "Сопоставлено человеком" in detail.text


def test_non_official_and_non_specific_links_rejected_without_writes(environment):
    client, db, csrf, medicine_id = environment
    for invalid in [
        "http://grls.rosminzdrav.ru/Default.aspx?record=1",
        "https://grls.rosminzdrav.ru.evil.example/record",
        "https://grls.rosminzdrav.ru/",
        "https://grls.rosminzdrav.ru/Default.aspx",
        "https://evil.example@grls.rosminzdrav.ru/Default.aspx?record=1",
        "javascript:alert(1)",
    ]:
        response = client.post(f"/medicines/{medicine_id}/registry",
                               data=form(csrf, source=invalid))
        assert response.status_code == 200
        assert "form-error" in response.text
        assert db.scalar(select(func.count(RegisteredProduct.id))) == 0
        assert db.scalar(select(func.count(StockItem.id))) == 1

    missing_check = client.post(f"/medicines/{medicine_id}/registry",
                                data=form(csrf, checked=False))
    assert "Подтвердите проверку" in missing_check.text
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 0
    assert client.post(f"/medicines/{medicine_id}/registry", data=form("wrong")).status_code == 403


def test_duplicate_source_and_substance_rejected(environment):
    client, db, csrf, medicine_id = environment
    duplicate_names = client.post(f"/medicines/{medicine_id}/registry",
        data=form(csrf, substances="Учебное вещество А\nучебное  вещество А"))
    assert "Повтор" in duplicate_names.text
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 0

    first = client.post(f"/medicines/{medicine_id}/registry", data=form(csrf), follow_redirects=False)
    assert first.status_code == 303
    second = client.post(f"/medicines/{medicine_id}/registry", data=form(csrf))
    assert "уже внесена" in second.text
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 1
    assert db.scalar(select(func.count(RegisteredSubstance.id))) == 2
    assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 1


def test_name_or_dose_mismatch_requires_explanation(environment):
    client, db, csrf, medicine_id = environment
    formdata = form(csrf, trade="Учебное Бета", strength="20 мг")
    response = client.post(f"/medicines/{medicine_id}/registry", data=formdata)
    assert "подробное обоснование" in response.text
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 0
    formdata["comparison_note"] = "Сведения об упаковке были перепроверены вручную."
    success = client.post(f"/medicines/{medicine_id}/registry",
                          data=formdata, follow_redirects=False)
    assert success.status_code == 303
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 1


def test_existing_link_attach_revoke_and_reactivate(environment):
    client, db, csrf, medicine_id = environment
    created = client.post(f"/medicines/{medicine_id}/registry", data=form(csrf),
                          follow_redirects=False)
    assert created.status_code == 303
    entry = db.scalar(select(RegisteredProduct))
    import_rows(db, [{
        "Код": "DEMO-B", "Наименование товара": "Учебное Альфа таб. 10 мг №20",
        "Производитель": "Другой учебный изготовитель",
    }], 102, "Учебный ящик")
    second = db.scalar(select(Medicine).where(Medicine.manufacturer == "Другой учебный изготовитель"))
    assert second is not None

    posted = client.post(f"/medicines/{second.id}/registry/attach",
        data={"csrf_token": csrf, "registered_id": entry.id, "source_checked": "yes",
              "product_checked": "yes", "comparison_note": "Проверено вручную по источнику"},
        follow_redirects=False)
    assert posted.status_code == 303
    link = db.scalar(select(MedicineRegistrationLink).where(
        MedicineRegistrationLink.medicine_id == second.id
    ))
    assert link.state == "active"
    assert db.scalar(select(func.count(StockItem.id))) == 2
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 1

    rejected = client.post(f"/medicines/{second.id}/registry/{link.id}/revoke", data={
        "csrf_token": csrf, "reason": "Ошибочная связь", "typed_confirmation": "NO"
    })
    assert "ОТМЕНИТЬ" in rejected.text
    db.refresh(link)
    assert link.state == "active"

    revoked = client.post(f"/medicines/{second.id}/registry/{link.id}/revoke", data={
        "csrf_token": csrf, "reason": "Пользователь нашёл другое соответствие",
        "typed_confirmation": "ОТМЕНИТЬ"
    }, follow_redirects=False)
    assert revoked.status_code == 303
    db.refresh(link)
    assert link.state == "revoked"
    assert db.scalar(select(func.count(StockItem.id))) == 2
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 1

    again = client.post(f"/medicines/{second.id}/registry/attach",
        data={"csrf_token": csrf, "registered_id": entry.id, "source_checked": "yes",
              "product_checked": "yes", "comparison_note": "Подтверждено после повторной сверки"},
        follow_redirects=False)
    assert again.status_code == 303
    db.refresh(link)
    assert link.state == "active"
    assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 2
    from app.models import RegistryLinkEvent
    events = db.scalars(select(RegistryLinkEvent).where(RegistryLinkEvent.link_id == link.id)
                        .order_by(RegistryLinkEvent.id)).all()
    assert [e.action for e in events] == ["linked", "revoked", "relinked"]
    assert events[1].explanation == "Пользователь нашёл другое соответствие"
    history_page = client.get(f"/medicines/{second.id}/registry")
    assert "История сопоставления (3)" in history_page.text


def test_source_validation_and_identity_keys():
    url, name = safe_official_url("https://pharma.eaeunion.org/pharma/registers/26/ru/?rid=123")
    assert name == "Реестр лекарственных средств ЕАЭС"
    assert url.startswith("https://")
    with pytest.raises(CatalogValidationError):
        safe_official_url("https://grls.rosminzdrav.ru/")
    with pytest.raises(CatalogValidationError):
        safe_official_url("https://grls.rosminzdrav.ru@evil.example/path")
    with pytest.raises(CatalogValidationError):
        safe_official_url("https://grls.minzdrav.gov.ru/record")
    assert safe_official_url("https://grls.minzdrav.gov.ru/record", allow_legacy=True)[0].startswith("https://")
    assert parse_substance_names("Вещество А\nВещество Б") == ["Вещество А", "Вещество Б"]
    assert registry_identity_key("№1", "Альфа", "таблетки", "10 мг", "Завод") == registry_identity_key(
        "№1", "АЛЬФА", "таблетки", "10мг", "Завод"
    )


def test_new_tables_additive_for_existing_stock(tmp_path):
    from sqlalchemy import inspect
    path = tmp_path / "saved.sqlite3"
    engine = create_engine("sqlite:///" + str(path))
    legacy_names = {
        table.name for table in Base.metadata.sorted_tables
        if table.name not in {
            "registered_products", "registered_substances",
            "registered_compositions", "medicine_registration_links",
            "registry_link_events",
        }
    }
    with engine.begin() as conn:
        Base.metadata.create_all(bind=conn, tables=[
            table for table in Base.metadata.sorted_tables if table.name in legacy_names
        ])
    with sessionmaker(bind=engine)() as session:
        from app.models import Drawer
        session.add(Drawer(number=42, name="Существующий ящик"))
        session.commit()
    Base.metadata.create_all(bind=engine)
    assert {"registered_products", "registered_substances",
            "registered_compositions", "medicine_registration_links"} <= set(
                inspect(engine).get_table_names()
            )
    with sessionmaker(bind=engine)() as session:
        assert session.scalar(select(func.count(RegisteredProduct.id))) == 0
        assert session.scalar(select(func.count(StockItem.id))) == 0
        from app.models import Drawer
        assert session.scalar(select(Drawer).where(Drawer.number == 42)).name == "Существующий ящик"
    engine.dispose()


def test_full_inventory_replacement_preserves_curated_registry_links(environment):
    client, db, csrf, medicine_id = environment
    created = client.post(f"/medicines/{medicine_id}/registry",
                          data=form(csrf), follow_redirects=False)
    assert created.status_code == 303
    # Replacing stock is explicitly allowed, but must never erase a linked
    # medicine's provenance merely because its old stock row is gone.
    from app.replace_inventory import replace_stock
    raw = (
        "Ящик;Название ящика;Код;Наименование товара;Производитель\n"
        "101;Учебный ящик;DEMO-A;Другой учебный товар 10 мг;Учебный завод\n"
    ).encode("utf-8")
    summary = replace_stock(db, raw, "replacement.csv")
    assert summary["new_stock_rows"] == 1
    assert db.get(Medicine, medicine_id) is not None
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 1
    link = db.scalar(select(MedicineRegistrationLink).where(
        MedicineRegistrationLink.medicine_id == medicine_id
    ))
    assert link is not None and link.state == "active"
    assert db.scalar(select(StockItem)).medicine.name == "Другой учебный товар 10 мг"
    # The new stock medicine remains unlinked: no automatic transfer by code.
    assert db.scalar(select(StockItem)).medicine.id != medicine_id


def test_one_active_registration_at_a_time_prevents_conflicting_substances(environment):
    client, db, csrf, medicine_id = environment
    initial = client.post(f"/medicines/{medicine_id}/registry", data=form(csrf),
                          follow_redirects=False)
    assert initial.status_code == 303
    conflict = form(csrf)
    conflict["registration_number"] = "УЧ-0002/01"
    conflict["substances"] = "Другое учебное вещество"
    denied = client.post(f"/medicines/{medicine_id}/registry", data=conflict)
    assert "уже связана другая" in denied.text
    assert db.scalar(select(func.count(RegisteredProduct.id))) == 1
    assert db.scalar(select(func.count(RegisteredSubstance.id))) == 2
    assert db.scalar(select(func.count(MedicineRegistrationLink.id))) == 1
