"""Functional tests for the initial MVP; no medical assertions are made."""
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models import Drawer, StockItem
from app.importer import import_rows, rows_from_file


@pytest.fixture()
def db():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db):
    def fake_db():
        yield db
    app.dependency_overrides[get_db] = fake_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_empty_dashboard(client):
    response = client.get('/')
    assert response.status_code == 200
    assert 'Карта ящиков' in response.text


def test_demo_import_and_search(client, db):
    path = Path(__file__).resolve().parents[1] / 'examples' / 'demo_inventory.csv'
    data = list(rows_from_file(path))
    first = import_rows(db, data, 101, 'Учебный ассортимент (вымышленные записи)', demo=True)
    assert first['created'] == len(data)
    assert first['skipped'] == 0
    repeat = import_rows(db, data, 101, 'Учебный ассортимент (вымышленные записи)', demo=True)
    assert repeat['created'] == 0
    assert repeat['updated'] == len(data)
    assert db.scalar(select(func.count(StockItem.id))) == len(data)
    assert db.scalar(select(func.count(Drawer.id))) == 1

    response = client.get('/drawers/101')
    assert response.status_code == 200
    assert 'Парацетамол учебный' in response.text
    assert 'Не проверены' in response.text

    response = client.get('/medicines?q=DEMO-01')
    assert response.status_code == 200
    assert 'Парацетамол учебный таб. 500мг №10' in response.text
    response = client.get('/api/search?q=DEMO-01')
    assert response.status_code == 200
    assert response.json()[0]['drawer'] == 101
    assert client.get('/drawers/999').status_code == 404


def test_no_fabricated_interactions(client):
    response = client.post('/api/interactions/check', json={'medicine_names': ['Препарат А', 'Препарат Б']})
    assert response.status_code == 200
    result = response.json()
    assert result['checked'] is False
    assert result['status'] == 'not_configured'
    assert result['findings'] == []
    assert 'НЕ выполнена' in result['message']
    assert client.post('/api/interactions/check', json={'medicine_names':['один']}).status_code == 422


def test_import_ignores_incomplete_rows(db):
    result = import_rows(db, [
        {'Код': '1212', 'Наименование товара': 'Тестовая позиция', 'Производитель': 'Компания'},
        {'Код': '1213', 'Наименование товара': ''},
    ], 2, 'Тест')
    assert result == {'created': 1, 'updated': 0, 'skipped': 1}
    item = db.scalar(select(StockItem))
    assert item.quantity is None
    assert item.availability == 'не проверено'
    assert not item.medicine.ingredients
