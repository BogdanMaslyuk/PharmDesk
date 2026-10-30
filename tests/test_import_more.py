"""Additional import safety tests for workbook and existing drawer labels."""
from io import BytesIO
from openpyxl import Workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.batch import batch_import
from app.models import Drawer, StockItem


def test_xlsx_preview_and_import_without_replacing_drawer_label():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        db.add(Drawer(number=23, name="Проверенное название", location="Стеллаж 1"))
        db.commit()

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Номер ящика", "Код", "Наименование товара", "Производитель"])
        sheet.append([23, 123, "Пробный препарат", "Учебный изготовитель"])
        output = BytesIO()
        workbook.save(output)
        raw = output.getvalue()

        preview = batch_import(db, raw, "my-drawers.xlsx", preview=True)
        assert preview["created"] == 1
        assert db.scalar(select(Drawer).where(Drawer.number == 23)).name == "Проверенное название"
        assert db.scalars(select(StockItem)).all() == []

        apply = batch_import(db, raw, "my-drawers.xlsx", preview=False)
        assert apply["created"] == 1
        drawer = db.scalar(select(Drawer).where(Drawer.number == 23))
        assert drawer.name == "Проверенное название"
        item = db.scalar(select(StockItem))
        assert item.article_code == "123"
    engine.dispose()
