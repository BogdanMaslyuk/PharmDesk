"""Regression and safety tests for local v0.2 inventory management."""
import re
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import Drawer, InventoryEvent, StockItem


@pytest.fixture()
def setup_client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()

    def fake_db():
        yield db

    app.dependency_overrides[get_db] = fake_db
    try:
        with TestClient(app) as client:
            page = client.get("/manage")
            assert page.status_code == 200
            token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/manage/drawers/new").text)
            assert token, "CSRF token missing"
            yield client, db, token.group(1)
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def test_manage_drawer_and_items(setup_client):
    client, db, csrf = setup_client
    r = client.post("/manage/drawers/new", data={"number": 23, "name": "Учебный шкаф", "location": "Зал 1", "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/drawers/23"
    assert client.get("/drawers/23").status_code == 200

    # Same number cannot create two drawers.
    duplicate = client.post("/manage/drawers/new", data={"number": 23, "name": "Двойник", "csrf_token": csrf})
    assert "уже существует" in duplicate.text
    assert db.scalar(select(func.count(Drawer.id))) == 1

    r = client.post("/manage/drawers/23/items/new", data={
        "article_code": "ABC-101", "name": "Учебное средство 10 мг", "manufacturer": "Учебный завод", "csrf_token": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303
    item = db.scalar(select(StockItem))
    assert item.medicine.name == "Учебное средство 10 мг"
    assert item.availability == "не проверено"

    collision = client.post("/manage/drawers/23/items/new", data={
        "article_code": "ABC-101", "name": "Дубликат", "csrf_token": csrf
    })
    assert "уже есть" in collision.text

    r = client.post(f"/manage/items/{item.id}/edit", data={
        "article_code": "ABC-101", "name": "Новое учебное название",
        "manufacturer": "Учебный завод", "csrf_token": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303
    db.refresh(item)
    assert item.medicine.name == "Новое учебное название"
    assert db.scalar(select(func.count(InventoryEvent.id))) >= 3


def test_csrf_blocks_unauthorized_post(setup_client):
    client, db, csrf = setup_client
    bad = client.post("/manage/drawers/new", data={"number": 30, "name": "Тест", "csrf_token": "forged"})
    assert bad.status_code == 403
    assert db.scalar(select(func.count(Drawer.id))) == 0


def test_moves_conflicts_and_confirmed_deletion(setup_client):
    client, db, csrf = setup_client
    for number in [23, 24]:
        assert client.post("/manage/drawers/new", data={
            "number": number, "name": "Ящик", "csrf_token": csrf
        }, follow_redirects=False).status_code == 303
    for number in [23, 24]:
        client.post(f"/manage/drawers/{number}/items/new", data={
            "article_code": "X-1", "name": f"Учебный {number}", "csrf_token": csrf
        })
    items = db.scalars(select(StockItem).order_by(StockItem.id)).all()
    assert len(items) == 2
    conflicted = client.post(f"/manage/items/{items[0].id}/move", data={
        "target_drawer": 24, "csrf_token": csrf
    })
    assert conflicted.status_code == 200
    assert "Перенос отменён" in conflicted.text
    assert items[0].drawer.number == 23

    # Remove the conflicting item only with explicit confirmation.
    refused = client.post(f"/manage/items/{items[1].id}/delete", data={
        "confirm": "no", "csrf_token": csrf,
    })
    assert refused.status_code == 200
    assert db.get(StockItem, items[1].id) is not None
    deleted = client.post(f"/manage/items/{items[1].id}/delete", data={
        "confirm": "DELETE", "csrf_token": csrf,
    }, follow_redirects=False)
    assert deleted.status_code == 303
    assert db.get(StockItem, items[1].id) is None
    moved = client.post(f"/manage/items/{items[0].id}/move", data={
        "target_drawer": 24, "csrf_token": csrf,
    }, follow_redirects=False)
    assert moved.status_code == 303
    assert moved.headers["location"] == "/drawers/24"


def test_preview_is_read_only_and_import_idempotent(setup_client):
    client, db, csrf = setup_client
    csv_data = (
        "Ящик;Название ящика;Код;Наименование товара;Производитель;Дата добавления\n"
        "23;Учебный 23;A01;Первый препарат;Изготовитель;01.01.2026\n"
        "24;Учебный 24;A01;Второй препарат;Изготовитель;01.01.2026\n"
        "23;Учебный 23;A01;Повтор в одном ящике;Изготовитель;01.01.2026\n"
    ).encode("utf-8")
    def upload(which, data=csv_data):
        return client.post(f"/manage/import/{which}",
            data={"csrf_token": csrf, "default_drawer": "", "default_name": "Без названия"},
            files={"file": ("multiple.csv", BytesIO(data), "text/csv")})

    preview = upload("preview")
    assert preview.status_code == 200
    assert preview.json()["created"] == 2
    assert preview.json()["skipped"] == 1
    assert db.scalar(select(func.count(Drawer.id))) == 0

    apply = upload("apply")
    assert apply.status_code == 200
    assert apply.json()["created"] == 2
    assert db.scalar(select(func.count(StockItem.id))) == 2
    assert db.scalar(select(func.count(Drawer.id))) == 2
    assert upload("preview").json()["updated"] == 2
    assert upload("apply").json()["created"] == 0
    assert db.scalar(select(func.count(StockItem.id))) == 2


def test_import_rejects_unsafe_or_invalid_input(setup_client):
    client, db, csrf = setup_client
    for content, filename in [(b"", "empty.csv"), (b"bad", "bad.txt"),
                              (b"\xff\xfe", "bad.csv")]:
        response = client.post("/manage/import/preview",
            data={"csrf_token": csrf},
            files={"file": (filename, content, "text/plain")})
        assert response.status_code == 422
    assert db.scalar(select(func.count(StockItem.id))) == 0
