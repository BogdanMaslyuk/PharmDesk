"""Additional parsing and entry-point checks."""
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
from app.database import Base, get_db
from app.main import app
from app.importer import import_rows, rows_from_file


def test_drawer_and_drug_detail_pages_with_data():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        sample = Path(__file__).resolve().parents[1] / 'examples' / 'demo_inventory.csv'
        import_rows(db, rows_from_file(sample), 101, 'Учебный ассортимент (вымышленные записи)', demo=True)
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as client:
                assert client.get('/drawers?q=101').status_code == 200
                assert client.get('/drawers?q=Учебный').status_code == 200
                r = client.get('/medicines/2')
                assert r.status_code == 200
                assert 'Официальные сведения' in r.text
                assert 'не подключена' in r.text
                assert 'Другие товарные карточки' in r.text
        finally:
            app.dependency_overrides.clear()
    engine.dispose()
