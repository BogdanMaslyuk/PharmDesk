"""Validation safety checks with synthetic database and no pharmacy data."""
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from app.database import Base
from app import models  # noqa: F401
from scripts import validation_v047 as validation


def _database(path):
    engine = create_engine("sqlite:///" + path.as_posix())
    Base.metadata.create_all(engine)
    engine.dispose()


def test_isolation_guards_block_working_tree_and_inside_production(tmp_path):
    production = tmp_path / "PharmDesk-git"
    production.mkdir()
    source = production / "pharmdesk.sqlite3"
    source.write_bytes(b"test")
    workspace = tmp_path / "_Validation_047"
    checkout = workspace / "PharmDesk-v047"
    checkout.mkdir(parents=True)

    validation.assert_isolated(source, workspace, checkout)
    with pytest.raises(validation.ValidationError):
        validation.assert_isolated(source, workspace, production)
    with pytest.raises(validation.ValidationError):
        validation.assert_isolated(source, production / "_Validation_047", checkout)
    with pytest.raises(validation.ValidationError):
        validation.assert_isolated(source, workspace, workspace / "PharmDesk-git")


def test_database_fingerprints_detect_protected_and_historical_mutation(tmp_path):
    path = tmp_path / "pharmdesk.sqlite3"
    _database(path)
    with validation._open_ro(path) as conn:
        snap = validation._snap(conn)
        assert snap["counts"]["drawers"] == 0
        assert snap["counts"]["grls_import_rows"] == 0
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO drawers (number, name) VALUES (27, 'Synthetic drawer')")
    with validation._open_ro(path) as conn:
        with pytest.raises(validation.ValidationError, match="drawers"):
            validation._same_data(conn, snap)


def test_adding_new_history_keeps_existing_rows_untouched(tmp_path):
    path = tmp_path / "pharmdesk.sqlite3"
    _database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("""INSERT INTO matching_runs
          (id, batch_id, algorithm_version, input_digest, state, total, processed,
           strong, ambiguous, not_found, errors, error_text, started_at)
          VALUES (1, 77, 'legacy-test', 'x', 'done', 0, 0, 0, 0, 0, 0, '', '2026-11-16')""")
    # The synthetic test creates a historical orphan; foreign-key checks must
    # catch it. Use an isolated existing batch in the main assertion below.
    with validation._open_ro(path) as conn:
        with pytest.raises(validation.ValidationError, match="foreign_key_check"):
            validation._snap(conn)


def test_safe_csv_prevents_spreadsheet_formula_injection():
    for value in ["=2+2", "+SUM(1)", "-command", "@HYPERLINK", "\t=NOW()"]:
        assert validation._safe_excel_text(value).startswith("'")
    assert validation._safe_excel_text("Обычный препарат") == "Обычный препарат"


def test_prepare_verified_backup_isolated(tmp_path, monkeypatch):
    production = tmp_path / "PharmDesk-git"
    production.mkdir()
    source = production / "pharmdesk.sqlite3"
    _database(source)
    workspace = tmp_path / "_Validation_047"
    checkout = workspace / "PharmDesk-v047"
    checkout.mkdir(parents=True)
    monkeypatch.setattr(validation, "ROOT", checkout)
    monkeypatch.setattr(validation, "_assert_server_stopped", lambda: None)
    original_hash = validation._sha256_file(source)
    args = SimpleNamespace(source=str(source), workspace=str(workspace))
    validation.prepare(args)
    assert validation._sha256_file(source) == original_hash
    test_db = workspace / "database" / "pharmdesk_validation.sqlite3"
    assert test_db.is_file()
    with validation._open_ro(test_db) as conn:
        validation._same_data(conn, validation._read_manifest(workspace)["baseline"])
    with pytest.raises(validation.ValidationError, match="Перезапись запрещена"):
        validation.prepare(args)


def test_catalogue_suggestions_use_the_same_complete_title_rules(tmp_path):
    from datetime import date
    from sqlalchemy.orm import Session
    from app.models import GrlsImportBatch, GrlsImportRow
    from app.search import normalize
    from app.trade_candidates import source_candidates, resolve_stock_trade_name

    path = tmp_path / "synthetic.sqlite3"
    engine = create_engine("sqlite:///" + path.as_posix())
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            batch = GrlsImportBatch(
                sha256="4" * 64, archive_name="synthetic.zip",
                snapshot_on=date(2026, 11, 5), row_count=4,
            )
            db.add(batch)
            db.flush()
            for n, trade in enumerate(["Миг", "Миг 400", "Акридерм", "Акридерм ГК"], 1):
                db.add(GrlsImportRow(
                    batch_id=batch.id, source_workbook="synthetic.xlsx",
                    source_row=n+1, status_from_filename="Действующий",
                    registration_number=f"TEST-{n}", registration_key=f"test{n}",
                    trade_name=trade, trade_key=normalize(trade),
                    inn_raw="", release_forms_raw="таблетки 400 мг",
                    certificate_holder="Учебный",
                ))
            db.commit()
            mig = source_candidates(db, batch.id, medicine_name="Миг 400 таб. 400 мг №20")
            assert [r.trade_name for r in mig] == ["Миг 400"]
            gk = source_candidates(db, batch.id, medicine_name="Акридерм ГК крем 30 г")
            assert [r.trade_name for r in gk] == ["Акридерм ГК"]
            assert source_candidates(db, batch.id, medicine_name="Миг 800 таб. 800 мг") == []
            assert source_candidates(db, batch.id, medicine_name="Акридерм СК крем") == []
            assert normalize(resolve_stock_trade_name(
                db, batch.id, "Миг 400 таб. 400 мг"
            )) == normalize("Миг 400")
    finally:
        engine.dispose()
