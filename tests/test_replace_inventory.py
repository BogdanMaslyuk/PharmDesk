"""Safety regression: replacing historical OCR rows must not retain phantom stock.

The real user's uploaded inventory is intentionally NOT checked into GitHub.
"""
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.importer import import_rows
from app.models import Drawer, Medicine, StockItem
from app.replace_inventory import ReplaceError, plan_replace, replace_stock


def make_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)()


def sample_csv(with_rows):
    head = "Ящик;Название ящика;Код;Наименование товара;Производитель;Дата добавления\n"
    return (head + with_rows).encode("utf-8")


def test_replacement_removes_old_ocr_names_but_preserves_other_drawers():
    engine, db = make_session()
    try:
        import_rows(db, [
            {"Код": "417157", "Наименование товара": "Bans таб. 160мг"},
            {"Код": "1573310", "Наименование товара": "Kapaocan Плюс"},
            {"Код": "1353970", "Наименование товара": "Opancc 16мг"},
            {"Код": "OLD", "Наименование товара": "Ошибочный товар, отсутствует в исправленном файле"},
        ], 13, "Сартаны (по рецепту)")
        import_rows(db, [{"Код": "SAVE", "Наименование товара": "Товар другого ящика"}], 101, "Другой ящик")
        approved = sample_csv(
            "13;Сартаны (по рецепту);417157;Валз таб. 160мг;Производитель;03.03.2026\n"
            "13;Сартаны (по рецепту);1573310;Кардосал Плюс;Производитель;03.03.2026\n"
            "13;Сартаны (по рецепту);1353970;Ордисс таб. 16мг;Производитель;03.03.2026\n"
        )
        plan = plan_replace(db, approved, "approved.csv")
        assert plan["summary"]["existing_stock_rows_to_replace"] == 4
        assert db.scalar(select(func.count(StockItem.id))) == 5
        result = replace_stock(db, approved, "approved.csv")
        assert result["new_stock_rows"] == 3
        assert db.scalar(select(func.count(StockItem.id))) == 4
        names = [item.medicine.name for item in db.scalars(
            select(StockItem).join(Medicine).order_by(Medicine.name)
        ).all()]
        assert "Валз таб. 160мг" in names
        assert "Кардосал Плюс" in names
        assert "Ордисс таб. 16мг" in names
        assert "Товар другого ящика" in names
        assert not any("Bans" in name or "Kapaocan" in name or "Opancc" in name for name in names)
        assert db.scalars(select(Medicine).where(Medicine.name == "Bans таб. 160мг")).first() is None
        assert db.scalar(select(Drawer).where(Drawer.number == 101)).name == "Другой ящик"
        assert db.scalar(select(Drawer).where(Drawer.number == 13)).name == "Сартаны (по рецепту)"
    finally:
        db.close()
        engine.dispose()


def test_invalid_reviewed_input_does_not_erase_old_rows():
    engine, db = make_session()
    try:
        import_rows(db, [{"Код": "A", "Наименование товара": "Существующий товар"}], 13, "Сартаны")
        malformed = sample_csv("13;Сартаны;B;;Производитель;03.03.2026\n")
        try:
            replace_stock(db, malformed, "bad.csv")
        except ReplaceError:
            pass
        else:
            raise AssertionError("Should reject invalid data before deleting anything")
        item = db.scalar(select(StockItem))
        assert item is not None
        assert item.medicine.name == "Существующий товар"
    finally:
        db.close()
        engine.dispose()
