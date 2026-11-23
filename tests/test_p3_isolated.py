
"""End-to-end synthetic local P3 test; no pharmacy inventory enters GitHub."""
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    GrlsImportBatch, GrlsImportRow, MatchingItem, MatchingRun, StockItem
)
from app.importer import import_rows
from app.search import normalize
from scripts.validation_v047 import _sha256_file, _open_ro


def test_p3_comparison_preserves_v1_history_and_original_database(tmp_path):
    production = tmp_path / "Desktop" / "PharmDesk_Project" / "PharmDesk-git"
    production.mkdir(parents=True)
    db_path = production / "pharmdesk.sqlite3"
    engine = create_engine("sqlite:///" + db_path.as_posix())
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        import_rows(db, [{"Код": "DEMO-P3", "Наименование товара":
                         "Учебное лекарство таб. 500 мкг",
                         "Производитель": "Учебный производитель"}],
                    1, "Учебная полка")
        batch = GrlsImportBatch(
            sha256=sha256(b"p3-test").hexdigest(),
            archive_name="synthetic.zip", snapshot_on=date(2026, 11, 5),
            row_count=1,
        )
        db.add(batch)
        db.flush()
        db.add(GrlsImportRow(
            batch_id=batch.id, source_workbook="synthetic.xlsx", source_row=2,
            status_from_filename="Действующий", registration_number="SYN-P3",
            registration_key="synp3", trade_name="Учебное лекарство",
            trade_key=normalize("Учебное лекарство"), inn_raw="",
            release_forms_raw="таблетки 0,5 мг",
            certificate_holder="Тестовый держатель"
        ))
        db.add(MatchingRun(
            batch_id=batch.id, input_digest="1" * 64,
            algorithm_version="0.4.7-evidence-1",
            state="done", total=1, processed=1, not_found=1,
        ))
        db.flush()
        stock = db.scalar(select(StockItem))
        first = db.scalar(select(MatchingRun))
        db.add(MatchingRun(
            batch_id=batch.id, input_digest="0" * 64,
            algorithm_version="0.4.5-conservative-1",
            state="done", total=1, processed=1, not_found=1,
        ))
        db.add(MatchingItem(
            run_id=first.id, stock_item_id=stock.id, medicine_id=stock.medicine_id,
            drawer_number=1, article_code="DEMO-P3",
            stock_name="Учебное лекарство таб. 500 мкг",
            stock_fingerprint="0" * 64, parsed_json="{}",
            category="none", candidate_total=0, reasons_json="[]",
        ))
        db.commit()
    engine.dispose()
    original_hash = _sha256_file(db_path)

    workspace = production.parent / "_Validation_047"
    checkout = workspace / "PharmDesk-v047"
    checkout.mkdir(parents=True)
    root = Path(__file__).resolve().parents[1]
    for folder in ("app", "scripts"):
        shutil.copytree(root / folder, checkout / folder,
                        ignore=shutil.ignore_patterns("__pycache__"))
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env["PYTHONPATH"] = str(checkout)
    base = [sys.executable, "-X", "utf8", "-m"]
    arguments = ["--source", str(db_path), "--workspace", str(workspace)]
    def call(module, action=None):
        cmd = base + [module] + ([action] if action else []) + arguments
        return subprocess.run(
            cmd, cwd=checkout, env=env, capture_output=True, text=True,
            timeout=90,
        )
    prepared = call("scripts.validation_v047", "prepare")
    assert prepared.returncode == 0, f"{prepared.stdout}\n{prepared.stderr}"
    p3 = call("scripts.v047_p3_compare")
    assert p3.returncode == 0, f"{p3.stdout}\n{p3.stderr}"
    assert _sha256_file(db_path) == original_hash

    target = workspace / "database" / "pharmdesk_validation.sqlite3"
    with _open_ro(target) as conn:
        old = conn.execute(
            "SELECT state, processed FROM matching_runs WHERE algorithm_version=?",
            ("0.4.7-evidence-1",)
        ).fetchone()
        assert old == ("done", 1)
        new = conn.execute(
            "SELECT state, processed FROM matching_runs WHERE algorithm_version=?",
            ("0.4.7-evidence-2",)
        ).fetchone()
        assert new == ("done", 1)
        assert conn.execute("SELECT COUNT(*) FROM matching_items").fetchone()[0] == 2
    output = json.loads(
        (workspace / "reports" / "p3_comparison_v1_v2.json").read_text(encoding="utf-8")
    )
    assert output["old_algorithm"] == "0.4.7-evidence-1"
    assert output["new_algorithm"] == "0.4.7-evidence-2"
    assert output["processed_items"] == 1
    assert output["old_statuses"]["not_found"] == 1
    assert output["new_statuses"]["not_found"] == 0
    assert (workspace / "reports" / "p3_candidate_deltas_v1_v2.csv").is_file()

    source_export = call("scripts.v047_source_evidence")
    assert source_export.returncode == 0, (
        f"{source_export.stdout}\n{source_export.stderr}"
    )
    source_report = json.loads(
        (workspace / "reports" / "p3_source_evidence_16.json")
        .read_text(encoding="utf-8")
    )
    assert source_report["cases_count"] == 0  # Synthetic sample has no contradictions.
    assert _sha256_file(db_path) == original_hash

    repeated = call("scripts.v047_p3_compare")
    assert repeated.returncode != 0
    with _open_ro(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM matching_runs").fetchone()[0] == 3
