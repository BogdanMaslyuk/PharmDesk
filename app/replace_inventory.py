"""Safely replace stock rows in drawers covered by a reviewed CSV/XLSX export.

NEVER auto-run during normal application startup. Requires explicit CLI confirmation.
Only inventory links in the listed drawers are replaced; other drawers and
medicine reference records are untouched.
"""
from datetime import datetime
from pathlib import Path
import sqlite3

from sqlalchemy import select
from sqlalchemy.orm import Session

from .batch import ImportErrorUser, prepare, MAX_BYTES
from .database import DATABASE_URL, ROOT
from .importer import import_rows
from .models import Drawer, InventoryEvent, StockItem


class ReplaceError(ValueError):
    pass


def plan_replace(db: Session, raw: bytes, filename: str):
    groups, drawer_names, errors, total, valid = prepare(raw, filename)
    if errors:
        raise ReplaceError(
            f"Found {len(errors)} invalid or duplicate rows. No changes made. "
            + "; ".join(errors[:4])
        )
    if not groups or not valid:
        raise ReplaceError("No valid stock rows; refusing to clear inventory.")
    drawer_numbers = sorted(groups)
    existing = db.scalars(
        select(Drawer).where(Drawer.number.in_(drawer_numbers))
    ).all()
    target_ids = [d.id for d in existing]
    old_count = 0
    if target_ids:
        old_count = len(db.scalars(
            select(StockItem.id).where(StockItem.drawer_id.in_(target_ids))
        ).all())
    return {
        "groups": groups, "drawer_names": drawer_names,
        "summary": {
            "file_rows": total,
            "accepted_rows": valid,
            "drawer_count": len(groups),
            "existing_stock_rows_to_replace": old_count,
            "new_stock_rows": valid,
            "drawers": drawer_numbers,
            "unmentioned_drawers_unchanged": True,
        },
    }


def replace_stock(db: Session, raw: bytes, filename: str):
    """Replace specified drawers' stock atomically; caller must back up beforehand."""
    plan = plan_replace(db, raw, filename)
    numbers = plan["summary"]["drawers"]
    existing = db.scalars(select(Drawer).where(Drawer.number.in_(numbers))).all()
    ids = [drawer.id for drawer in existing]
    try:
        if ids:
            records = db.scalars(
                select(StockItem).where(StockItem.drawer_id.in_(ids))
            ).all()
            for item in records:
                db.delete(item)
            db.flush()
        created = 0
        for number, rows in plan["groups"].items():
            result = import_rows(db, rows, number, plan["drawer_names"][number], commit=False)
            created += result["created"]
            if result["skipped"]:
                raise ReplaceError("Unexpected skipped row; cancelling transaction.")
        if created != plan["summary"]["accepted_rows"]:
            raise ReplaceError("Row-count mismatch; cancelling transaction.")
        db.add(InventoryEvent(
            event_type="replace_inventory",
            description=(
                f"Replaced inventory in {len(numbers)} drawers; "
                f"removed {plan['summary']['existing_stock_rows_to_replace']}, "
                f"loaded {created} from {Path(filename).name}"
            )[:500],
        ))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return plan["summary"]


def backup_local_database():
    """Make a consistent SQLite backup before destructive stock replacement."""
    if not DATABASE_URL.startswith("sqlite:///"):
        raise ReplaceError("Only the default local SQLite database is supported.")
    db_path = Path(DATABASE_URL.removeprefix("sqlite:///")).resolve()
    if not db_path.is_file():
        raise ReplaceError("Local database not found: " + str(db_path))
    backup_dir = ROOT / "backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = backup_dir / f"before_inventory_replacement_{stamp}.sqlite3"
    with sqlite3.connect(str(db_path)) as source, sqlite3.connect(str(target)) as saved:
        source.backup(saved)
    if target.stat().st_size == 0:
        raise ReplaceError("Backup failed: empty file. Inventory not changed.")
    return target


def checked_file_data(filename: str):
    path = Path(filename)
    if not path.is_file():
        raise ReplaceError(f"File does not exist: {path}")
    if path.stat().st_size > MAX_BYTES:
        raise ReplaceError("File exceeds 4 MB.")
    return path.read_bytes()
