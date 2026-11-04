"""Safety regression for local-only maintenance operations."""
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.maintenance import backup, diagnose, APP_VERSION
from app.models import Drawer, Medicine, ReviewRecord, SourceImage, StockItem
from app.review_progress import drawer_progress


def initialized(tmp_path):
    dbfile = tmp_path / "pharmdesk.sqlite3"
    engine = create_engine("sqlite:///" + str(dbfile))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        drawer = Drawer(number=13, name="Сартаны")
        medicine = Medicine(fingerprint="валз::", name="Валз")
        session.add(StockItem(drawer=drawer, article_code="417157", medicine=medicine))
        session.add(ReviewRecord(
            drawer_number=13, drawer_name="Сартаны", article_code="417157",
            proposed_name="Валз", proposed_manufacturer="", source_file="13.png",
            source_row=1, priority=True, state="pending",
        ))
        session.add(SourceImage(
            original_filename="13.png", name_key="13.png",
            stored_filename="aa123.png", sha256="1" * 64
        ))
        session.commit()
    engine.dispose()
    return dbfile


def test_backup_has_consistent_sqlite_and_source_photo_and_sha256(tmp_path):
    dbfile = initialized(tmp_path)
    source_dir = tmp_path / "private_data" / "review_images"
    source_dir.mkdir(parents=True)
    photo = source_dir / "aa123.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image-test")
    original_hash = hashlib.sha256(dbfile.read_bytes()).hexdigest()
    info = backup(root=tmp_path, db_file=dbfile)
    archive = Path(info["path"])
    assert archive.is_file()
    assert info["version"] == APP_VERSION
    assert info["files"] == 2
    assert archive.parent == tmp_path / "backups"
    assert hashlib.sha256(dbfile.read_bytes()).hexdigest() == original_hash
    with ZipFile(archive) as z:
        assert z.testzip() is None
        manifest = json.loads(z.read("manifest.json"))
        assert set(manifest["files"]) == {
            "database/pharmdesk.sqlite3", "review_images/aa123.png"
        }
        for path, digest in manifest["files"].items():
            assert hashlib.sha256(z.read(path)).hexdigest() == digest
        restored = tmp_path / "backup_copy.sqlite3"
        restored.write_bytes(z.read("database/pharmdesk.sqlite3"))
    with sqlite3.connect(restored) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT article_code FROM stock_items").fetchone()[0] == "417157"


def test_backup_contains_recent_uncheckpointed_wal_data(tmp_path):
    dbfile = initialized(tmp_path)
    with sqlite3.connect(dbfile) as con:
        con.execute("PRAGMA journal_mode=WAL")
    writer = sqlite3.connect(dbfile)
    try:
        writer.execute("INSERT INTO drawers (number, name) VALUES (22, 'Добавлено')")
        writer.commit()
        result = backup(root=tmp_path, db_file=dbfile)
        with ZipFile(result["path"]) as z:
            copied = tmp_path / "wal_copy.sqlite3"
            copied.write_bytes(z.read("database/pharmdesk.sqlite3"))
        with sqlite3.connect(copied) as reader:
            assert reader.execute("SELECT name FROM drawers WHERE number=22").fetchone()[0] == "Добавлено"
    finally:
        writer.close()


def test_diagnostic_does_not_create_or_modify_data(tmp_path):
    dbfile = initialized(tmp_path)
    original = dbfile.read_bytes()
    state = diagnose(root=tmp_path, db_file=dbfile)
    assert state["ok"]
    assert state["integrity"] == "ok"
    assert state["drawers"] == 1
    assert state["stock_items"] == 1
    assert state["review_pending"] == 1
    assert state["review_priority"] == 1
    assert state["review_approved"] == 0
    assert state["review_excluded"] == 0
    assert state["photos"] == 1
    assert state["missing_photos"] == 1
    assert dbfile.read_bytes() == original
    assert not (tmp_path / "backups").exists()


def test_missing_and_damaged_databases_are_reported_without_creation(tmp_path):
    absent = tmp_path / "missing.sqlite3"
    result = diagnose(root=tmp_path, db_file=absent)
    assert not result["ok"] and not absent.exists()
    broken = tmp_path / "corrupt.sqlite3"
    broken.write_bytes(b"not a sqlite database")
    result = diagnose(root=tmp_path, db_file=broken)
    assert not result["ok"]
    assert broken.read_bytes() == b"not a sqlite database"


def test_progress_uses_real_status_and_does_not_count_pending_as_verified():
    records = [
        SimpleNamespace(drawer_number=13, state="pending", priority=True),
        SimpleNamespace(drawer_number=13, state="approved", priority=False),
        SimpleNamespace(drawer_number=13, state="excluded", priority=True),
        SimpleNamespace(drawer_number=23, state="pending", priority=False),
    ]
    rows = drawer_progress(records)
    assert rows == [
        {
            "drawer": 13, "total": 3, "pending": 1, "priority": 1,
            "approved": 1, "excluded": 1, "done": 2, "percent": 67,
        },
        {
            "drawer": 23, "total": 1, "pending": 1, "priority": 0,
            "approved": 0, "excluded": 0, "done": 0, "percent": 0,
        },
    ]
    assert drawer_progress([]) == []
