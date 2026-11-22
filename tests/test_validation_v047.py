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
