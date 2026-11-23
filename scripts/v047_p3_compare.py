"""Run algorithm v2 on existing isolated PharmDesk test SQLite only.

The production SQLite is never opened for writing, and existing v1 test results
and legacy history remain immutable. Results are local, not sent anywhere.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from .validation_v047 import (
    ROOT, PRODUCTION_ROOT, WORKSPACE_ROOT, HISTORY_TABLES,
    ValidationError, _assert_server_stopped, assert_isolated,
    _open_ro, _same_data, _read_manifest, _sha256_file,
    _table_hash, _json, _comparison_csv, _is_inside,
)

NEW_ALGORITHM = "0.4.7-evidence-2"
OLD_ALGORITHM = "0.4.7-evidence-1"


def _history_fingerprint(conn):
    result = {}
    for table in HISTORY_TABLES:
        max_id = conn.execute(
            f'SELECT COALESCE(MAX(id), 0) FROM "{table}"'
        ).fetchone()[0]
        result[table] = {
            "max_id": max_id,
            "sha256": _table_hash(conn, table, max_id=max_id),
        }
    return result


def _ensure_history_unchanged(conn, snapshot):
    for table, state in snapshot.items():
        actual = _table_hash(conn, table, max_id=state["max_id"])
        if actual != state["sha256"]:
            raise ValidationError(
                "Ранее сохранённые строки были изменены: " + table
            )


def run(args):
    source = Path(args.source or (PRODUCTION_ROOT / "pharmdesk.sqlite3")).resolve()
    workspace = Path(args.workspace or WORKSPACE_ROOT).resolve()
    assert_isolated(source, workspace, code_root=ROOT)
    _assert_server_stopped()
    manifest = _read_manifest(workspace)
    target = Path(manifest["test_db"]).resolve()
    backup = Path(manifest["backup"]).resolve()
    if source != Path(manifest["source"]).resolve():
        raise ValidationError("Путь рабочей базы отличается от исходного.")
    if not target.is_file() or not _is_inside(target, workspace / "database"):
        raise ValidationError("Отсутствует изолированная SQLite.")
    if source == target or _is_inside(target, source.parent):
        raise ValidationError("ЗАПРЕЩЕНО: тестовая база совпадает с рабочей.")
    if _sha256_file(backup) != manifest["backup_sha256"]:
        raise ValidationError("Резервная копия повреждена.")
    reports = workspace / "reports"
    out_json = reports / "p3_comparison_v1_v2.json"
    out_csv = reports / "p3_candidate_deltas_v1_v2.csv"
    if out_json.exists() or out_csv.exists():
        raise ValidationError("Отчёты P3 уже существуют; перезапись запрещена.")
    with _open_ro(target) as conn:
        _same_data(conn, manifest["baseline"])
        history_before = _history_fingerprint(conn)

    # The application's connection string is set before its first import.
    os.environ["DATABASE_URL"] = "sqlite:///" + target.as_posix()
    from sqlalchemy import select
    from app.database import DATABASE_URL, SessionLocal
    from app.models import MatchingRun, GrlsImportBatch
    from app.matching_worker import ALGORITHM_VERSION, inventory_digest, run_matching
    from app.matching_audit import matching_audit

    if ALGORITHM_VERSION != NEW_ALGORITHM:
        raise ValidationError(
            "Ожидалась новая версия алгоритма: " + NEW_ALGORITHM
        )
    if not DATABASE_URL.startswith("sqlite:///") or (
        Path(DATABASE_URL[len("sqlite:///"):]).resolve() != target
    ):
        raise ValidationError("Приложение подключилось к неправильной SQLite.")
    with SessionLocal() as db:
        old = db.scalar(
            select(MatchingRun).where(
                MatchingRun.algorithm_version == OLD_ALGORITHM,
                MatchingRun.state == "done",
            ).order_by(MatchingRun.id.desc()).limit(1)
        )
        if old is None:
            raise ValidationError("Сначала должна существовать проверка алгоритма v1.")
        if old.processed != manifest["baseline"]["counts"]["stock_items"]:
            raise ValidationError("Проверка v1 неполная.")
        if db.scalar(select(MatchingRun).where(
            MatchingRun.algorithm_version == NEW_ALGORITHM
        ).limit(1)) is not None:
            raise ValidationError("Проверка v2 уже создана; дублирование запрещено.")
        batch = db.get(GrlsImportBatch, old.batch_id)
        digest, n = inventory_digest(db, batch)
        if n != manifest["baseline"]["counts"]["stock_items"]:
            raise ValidationError("Число товаров изменилось.")
        new = MatchingRun(
            batch_id=old.batch_id,
            input_digest=digest, algorithm_version=NEW_ALGORITHM,
            state="queued", total=n,
        )
        db.add(new)
        db.commit()
        new_id = new.id
        old_id = old.id

    print("P3: запускаю алгоритм v2 только на тестовой SQLite.")
    started = time.monotonic()
    run_matching(new_id)

    with SessionLocal() as db:
        new = db.get(MatchingRun, new_id)
        if new is None or new.state != "done" or new.processed != n:
            raise ValidationError("Новый запуск завершился не полностью.")
        old_audit = matching_audit(db, old_id)
        new_audit = matching_audit(db, new_id)
        comparison = _comparison_csv(db, old_id, new_id, out_csv)
        compact = {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "old_run_id": old_id,
            "new_run_id": new_id,
            "old_algorithm": OLD_ALGORITHM,
            "new_algorithm": NEW_ALGORITHM,
            "processed_items": new.processed,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "old_statuses": {
                "strong": db.get(MatchingRun, old_id).strong,
                "ambiguous": db.get(MatchingRun, old_id).ambiguous,
                "not_found": db.get(MatchingRun, old_id).not_found,
            },
            "new_statuses": {
                "strong": new.strong,
                "ambiguous": new.ambiguous,
                "not_found": new.not_found,
            },
            "old_audit": {
                "with_form_and_dose_match": old_audit["items_with_form_and_dose_match"],
                "evidence_counts": old_audit["evidence_counts"],
                "evaluated_pairs": old_audit["evaluated_candidate_pairs"],
            },
            "new_audit": {
                "with_form_and_dose_match": new_audit["items_with_form_and_dose_match"],
                "evidence_counts": new_audit["evidence_counts"],
                "evaluated_pairs": new_audit["evaluated_candidate_pairs"],
            },
            "comparison": comparison,
            "note": (
                "Результат основан на загруженном снимке ГРЛС. Улучшение формата "
                "дозы не подтверждает состав или регистрацию конкретной упаковки."
            ),
        }
    with _open_ro(target) as conn:
        _same_data(conn, manifest["baseline"])
        _ensure_history_unchanged(conn, history_before)
    _json(out_json, compact)
    print("P3 завершён: все старые данные проверены и сохранены.")
    print("Обработано:", n)
    print("Изменений первого кандидата (v1 → v2):", comparison["changed_first_candidate"])
    print("Сравнительный отчёт:", out_json)
    print("Локальный CSV:", out_csv)
    print("Результат не является ручным подтверждением препаратов.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Безопасное сравнение алгоритмов P3 на локальной тестовой SQLite"
    )
    parser.add_argument("--source", help="Путь рабочей SQLite (только проверка)")
    parser.add_argument("--workspace", help="Изолированная папка")
    args = parser.parse_args(argv)
    try:
        run(args)
        return 0
    except (ValidationError, sqlite3.Error, OSError, ValueError) as exc:
        print("ОСТАНОВЛЕНО:", exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
