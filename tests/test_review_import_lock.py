"""Regression: accepted manual corrections cannot silently revert to OCR on later import."""
import pytest
from io import BytesIO
from zipfile import ZipFile

from openpyxl import Workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.importer import import_rows
from app.models import ReviewRecord, StockItem
from app.replace_inventory import plan_replace, ReplaceError
from app.review_import import import_reviews


def one_row_xlsx():
    w = Workbook()
    s = w.active
    s.title = "Все позиции"
    s.append([
        "Ящик", "Название ящика", "Код", "Наименование товара",
        "Производитель", "Статус сверки", "Исходное изображение",
        "Строка на снимке"
    ])
    s.append([13, "Сартаны", "417157", "Валз", "Производитель",
              "Требует ручной проверки", "13 1.PNG", 5])
    output = BytesIO()
    w.save(output)
    return output.getvalue()


def old_csv():
    return (
        "Ящик;Название ящика;Код;Наименование товара;Производитель\n"
        "13;Сартаны;417157;Bans таб. 160 мг;Производитель\n"
    ).encode("utf-8")


def test_import_does_not_override_manual_approval():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        import_reviews(db, one_row_xlsx())
        rec = db.scalar(select(ReviewRecord))
        # Emulate a human decision previously committed through the UI.
        from datetime import datetime, timezone
        rec.state = "approved"
        rec.checked_at = datetime.now(timezone.utc)
        rec.confirmed_code = "417157"
        rec.confirmed_name = "Валз"
        db.commit()
        existing = import_rows(db, [{
            "Код": "417157", "Наименование товара": "Валз",
            "Производитель": "Производитель"
        }], 13, "Сартаны")
        # Locked reviewed records are never overwritten by an OCR importer.
        assert existing["skipped"] == 1
        # Manually approved stock already exists in normal use; create it once
        # via a direct stock record here for the test.
        from app.models import Medicine, Drawer
        drawer = db.scalar(select(Drawer))
        medicine = Medicine(name="Валз", manufacturer="Производитель",
                            fingerprint="валз::производитель")
        db.add(medicine)
        db.flush()
        db.add(StockItem(drawer=drawer, article_code="417157", medicine=medicine,
                         source="сверено с фото"))
        db.commit()
        rejected = import_rows(db, [{
            "Код": "417157", "Наименование товара": "Bans таб. 160 мг",
            "Производитель": "Производитель"
        }], 13, "Сартаны")
        assert rejected["skipped"] == 1
        assert db.scalar(select(StockItem)).medicine.name == "Валз"
        with pytest.raises(ReplaceError):
            plan_replace(db, old_csv(), "old.csv")
    engine.dispose()
