"""Searching Russian names should work without matching letter case."""
import re
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base, get_db
from app.importer import import_rows
from app.main import app
from app.search import matches, normalize


def test_cyrillic_casefold_and_yo():
    assert normalize("  Ёлка - №20 ") == "елка20"
    assert matches("Парацетамол 500мг №20", "парацетамол")
    assert matches("Парацетамол 500мг №20", "500 мг")
    assert matches("Жёлтые таблетки №30", "желтые")
    assert not matches("Ибупрофен 200 мг", "парацетамол")


def test_search_and_drawer_filter():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        for no in (7, 8):
            import_rows(db, [{
                "Код": f"A-{no}", "Наименование товара": "Жёлтый Учебный Препарат 10 мг",
                "Производитель": "Учебная фабрика",
            }], no, f"Учебный ящик {no}")
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                response = client.get("/medicines?q=желтый&drawer=7")
                assert response.status_code == 200
                assert "A-7" in response.text
                assert "A-8" not in response.text
                response = client.get("/drawers?q=УЧЕБНЫЙ")
                assert "Учебный ящик 7" in response.text
                results = client.get("/api/search?q=ЖЕЛТЫЙ").json()
                assert len(results) == 2
                assert sorted(x["drawer"] for x in results) == [7, 8]
                assert client.get("/drawers/7?q=желтый").status_code == 200
        finally:
            app.dependency_overrides.clear()
    engine.dispose()
