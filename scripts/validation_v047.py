"""PharmDesk v0.4.7: local, isolated, reproducible matching validation.

Usage (from an ISOLATED git clone):
    python -m scripts.validation_v047 prepare
    python -m scripts.validation_v047 run

The production database is opened read-only, solely by prepare's SQLite
online-backup operation. No private records are uploaded or printed.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_ROOT = Path.home() / "Desktop" / "PharmDesk_Project" / "PharmDesk-git"
WORKSPACE_ROOT = Path.home() / "Desktop" / "PharmDesk_Project" / "_Validation_047"
PROTECTED_TABLES = (
    "drawers", "medicines", "stock_items", "grls_import_batches",
    "grls_import_rows", "registered_products", "registered_substances",
    "registered_compositions", "medicine_registration_links",
    "registry_link_events", "review_records", "review_decisions",
    "source_images", "substance_evidence", "inventory_events",
)
HISTORY_TABLES = (
    "matching_runs", "matching_items", "matching_candidates",
    "matching_evidence", "matching_decisions",
)


class ValidationError(RuntimeError):
    pass


def _is_inside(path, folder):
    path, folder = Path(path).resolve(), Path(folder).resolve()
    return path == folder or folder in path.parents


def assert_isolated(source, workspace, code_root=ROOT):
    """Fail closed on code/database path mistakes or symlink aliases."""
    source = Path(source).resolve()
    workspace = Path(workspace).resolve()
    code_root = Path(code_root).resolve()
    if source.name != "pharmdesk.sqlite3" or not source.is_file():
        raise ValidationError("Не найдена исходная SQLite PharmDesk.")
    if source.parent == workspace or _is_inside(workspace, source.parent):
        raise ValidationError("Тестовая область расположена внутри рабочей папки!")
    if not _is_inside(code_root, workspace) or _is_inside(code_root, source.parent):
        raise ValidationError(
            "Запрещён запуск из рабочего репозитория. Создайте отдельный git clone "
            "в _Validation_047/PharmDesk-v047."
        )
    if code_root.name != "PharmDesk-v047":
        raise ValidationError("Тестовый код должен находиться в папке PharmDesk-v047.")
    if (workspace / "database").resolve() == source.parent:
        raise ValidationError("Тестовая SQLite совпадает с рабочей.")


def _assert_server_stopped():
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.3):
            raise ValidationError(
                "Порт 8000 занят. Остановите рабочий сервер PharmDesk перед испытаниями."
            )
    except (ConnectionRefusedError, OSError, TimeoutError):
        pass


def _open_ro(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValidationError("SQLite отсутствует: " + str(path))
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=20)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_hash(conn, table, *, max_id=None):
    if not table.replace("_", "").isalnum():
        raise ValidationError("Некорректное имя таблицы.")
    digest = hashlib.sha256()
    condition = "" if max_id is None else " WHERE id <= ?"
    sql = f'SELECT * FROM "{table}"{condition} ORDER BY rowid'
    for row in conn.execute(sql, () if max_id is None else (max_id,)):
        for cell in row:
            # Explicit boundaries avoid ambiguity between consecutive values.
            value = repr(cell).encode("utf-8", errors="backslashreplace")
            digest.update(len(value).to_bytes(8, "little"))
            digest.update(value)
        digest.update(b"\n")
    return digest.hexdigest()


def _health(conn):
    check = conn.execute("PRAGMA integrity_check").fetchall()
    foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
    if check != [("ok",)]:
        raise ValidationError("PRAGMA integrity_check не прошёл.")
    if foreign:
        raise ValidationError(f"PRAGMA foreign_key_check: {len(foreign)} нарушений.")
    return {"integrity": "ok", "foreign_key_errors": 0}


def _snap(conn):
    existing = {x[0] for x in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    missing = set(PROTECTED_TABLES + HISTORY_TABLES) - existing
    if missing:
        raise ValidationError("Нет ожидаемых таблиц: " + ", ".join(sorted(missing)))
    counts = {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
              for table in PROTECTED_TABLES}
    history_limits = {table: conn.execute(
        f'SELECT COALESCE(MAX(id), 0) FROM "{table}"'
    ).fetchone()[0] for table in HISTORY_TABLES}
    return {
        "health": _health(conn),
        "counts": counts,
        "protected_hashes": {t: _table_hash(conn, t) for t in PROTECTED_TABLES},
        "history_max_ids": history_limits,
        "historical_hashes": {
            t: _table_hash(conn, t, max_id=history_limits[t]) for t in HISTORY_TABLES
        },
    }


def _same_data(conn, baseline):
    _health(conn)
    for table, expected in baseline["counts"].items():
        got = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        if got != expected or _table_hash(conn, table) != baseline["protected_hashes"][table]:
            raise ValidationError("Изменена исходная таблица: " + table)
    for table, max_id in baseline["history_max_ids"].items():
        got = _table_hash(conn, table, max_id=max_id)
        if got != baseline["historical_hashes"][table]:
            raise ValidationError("Изменена историческая таблица: " + table)


def _json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _settings(args):
    production = Path(args.source or (PRODUCTION_ROOT / "pharmdesk.sqlite3")).resolve()
    workspace = Path(args.workspace or WORKSPACE_ROOT).resolve()
    assert_isolated(production, workspace, code_root=ROOT)
    return production, workspace


def prepare(args):
    production, workspace = _settings(args)
    _assert_server_stopped()
    db_folder = workspace / "database"
    backup_folder = workspace / "backups"
    reports = workspace / "reports"
    target = db_folder / "pharmdesk_validation.sqlite3"
    manifest_path = reports / "baseline_manifest.json"
    if target.exists() or manifest_path.exists():
        raise ValidationError("Тестовая копия уже подготовлена. Перезапись запрещена.")
    if not production.is_file():
        raise ValidationError("Рабочая SQLite отсутствует.")
    required = production.stat().st_size * 3 + 128 * 1024 * 1024
    if shutil.disk_usage(workspace.parent if workspace.parent.exists()
                         else workspace.parent.parent).free < required:
        raise ValidationError("Недостаточно свободного места для двух проверенных копий.")
    backup_folder.mkdir(parents=True, exist_ok=True)
    db_folder.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    backup = backup_folder / ("baseline_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".sqlite3")
    if backup.exists():
        raise ValidationError("Имя резервной копии уже занято.")
    print("Создаю согласованную локальную копию SQLite (без выгрузки в сеть)...")
    start = time.monotonic()
    with _open_ro(production) as source:
        with sqlite3.connect(str(backup)) as dest:
            source.backup(dest, pages=256, sleep=0.02)
    try:
        with _open_ro(backup) as con:
            snapshot = _snap(con)
        shutil.copy2(backup, target)
        with _open_ro(target) as con:
            _same_data(con, snapshot)
        manifest = {
            "version": 1,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": str(production),
            "test_db": str(target),
            "backup": str(backup),
            "backup_sha256": _sha256_file(backup),
            "test_sha256_at_creation": _sha256_file(target),
            "baseline": snapshot,
            "notes": "Приватный локальный файл. Не публиковать в GitHub.",
        }
        _json(manifest_path, manifest)
        print("Тестовая среда подготовлена.")
        print("Ящики:", snapshot["counts"]["drawers"])
        print("Товары:", snapshot["counts"]["stock_items"])
        print("Строки ГРЛС:", snapshot["counts"]["grls_import_rows"])
        print("Время резервного копирования, с:", round(time.monotonic() - start, 1))
        print("Отчёт:", manifest_path)
    except Exception:
        target.unlink(missing_ok=True)
        # Keep backup for examination; it may be the only verified snapshot.
        raise


def _read_manifest(workspace):
    path = workspace / "reports" / "baseline_manifest.json"
    if not path.is_file():
        raise ValidationError("Сначала выполните prepare.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValidationError("Неизвестная версия манифеста.")
    return data


def _safe_excel_text(value):
    text = str(value if value is not None else "")
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def _comparison_csv(db, previous_id, new_id, outfile):
    from sqlalchemy import select
    from app.models import MatchingCandidate, MatchingItem
    def get_items(run_id):
        return {
            item.stock_item_id: item for item in db.scalars(
                select(MatchingItem).where(MatchingItem.run_id == run_id)
            ).all()
        }
    def first_candidates(items):
        if not items:
            return {}
        result = {}
        for row in db.scalars(
            select(MatchingCandidate).where(
                MatchingCandidate.item_id.in_([item.id for item in items.values()])
            ).order_by(MatchingCandidate.rank, MatchingCandidate.id)
        ).all():
            if row.item_id not in result:
                result[row.item_id] = row.grls_row_id
        return result
    old_items = get_items(previous_id) if previous_id is not None else {}
    new_items = get_items(new_id)
    old_best, new_best = first_candidates(old_items), first_candidates(new_items)
    summary = {"changed_first_candidate": 0, "newly_found": 0,
               "lost_candidates": 0, "same_first_candidate": 0,
               "without_previous_comparison": 0}
    with outfile.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow([
            "Код", "Ящик", "Исходное название", "Категория до",
            "Категория после", "Кандидатов до", "Кандидатов после",
            "Первая строка ГРЛС до (ID)", "Первая строка ГРЛС после (ID)",
            "Оценено после", "Скрыто после", "Причины после",
        ])
        for stock_id, new in sorted(new_items.items()):
            old = old_items.get(stock_id)
            before = old_best.get(old.id) if old else None
            after = new_best.get(new.id)
            if old is None:
                summary["without_previous_comparison"] += 1
            else:
                if old.candidate_total == 0 and new.candidate_total:
                    summary["newly_found"] += 1
                if old.candidate_total and new.candidate_total == 0:
                    summary["lost_candidates"] += 1
                if before == after:
                    summary["same_first_candidate"] += 1
                else:
                    summary["changed_first_candidate"] += 1
            meta = json.loads(new.parsed_json or "{}")
            writer.writerow([_safe_excel_text(v) for v in [
                new.article_code, new.drawer_number, new.stock_name,
                old.category if old else "Нет", new.category,
                old.candidate_total if old else None, new.candidate_total,
                before, after, meta.get("evaluated_count"),
                new.candidate_total - meta.get("displayed_count", 0),
                " / ".join(json.loads(new.reasons_json or "[]")),
            ]])
    return summary


def run(args):
    production, workspace = _settings(args)
    _assert_server_stopped()
    manifest = _read_manifest(workspace)
    target = Path(manifest["test_db"]).resolve()
    backup = Path(manifest["backup"]).resolve()
    if production != Path(manifest["source"]).resolve():
        raise ValidationError("Путь исходной базы изменился после подготовки.")
    if not target.is_file() or not backup.is_file():
        raise ValidationError("Тестовая база или резервная копия отсутствует.")
    if not _is_inside(target, workspace / "database"):
        raise ValidationError("Тестовая SQLite находится вне тестовой папки.")
    if _is_inside(target, production.parent) or target == production:
        raise ValidationError("ПОПЫТКА ИСПОЛЬЗОВАНИЯ РАБОЧЕЙ SQLite БЛОКИРОВАНА.")
    if _sha256_file(backup) != manifest["backup_sha256"]:
        raise ValidationError("Повреждена резервная копия.")
    if _sha256_file(target) != manifest["test_sha256_at_creation"]:
        raise ValidationError("Тестовая SQLite уже изменена. Повтор запрещён.")
    with _open_ro(target) as con:
        _same_data(con, manifest["baseline"])

    # Set path BEFORE importing the application's database module.
    os.environ["DATABASE_URL"] = "sqlite:///" + target.as_posix()
    from app.database import DATABASE_URL, SessionLocal, init_db
    from app.matching_worker import ALGORITHM_VERSION, inventory_digest, run_matching
    from app.matching_audit import matching_audit
    from app.models import GrlsImportBatch, MatchingRun
    from sqlalchemy import select
    if ALGORITHM_VERSION != "0.4.7-evidence-1":
        raise ValidationError("Версия алгоритма не соответствует ТЗ.")
    if Path(DATABASE_URL[len("sqlite:///"):]).resolve() != target:
        raise ValidationError("Приложение подключилось к неправильной SQLite.")

    print("Запускаю алгоритм только на отдельной тестовой базе...")
    started = time.monotonic()
    init_db()
    with SessionLocal() as db:
        batch = db.scalar(select(GrlsImportBatch).order_by(
            GrlsImportBatch.snapshot_on.desc(), GrlsImportBatch.id.desc()).limit(1))
        if batch is None:
            raise ValidationError("В тестовой базе нет снимка ГРЛС.")
        previous = db.scalar(select(MatchingRun).where(
            MatchingRun.algorithm_version == "0.4.5-conservative-1",
            MatchingRun.state == "done"
        ).order_by(MatchingRun.id.desc()).limit(1))
        previous_id = previous.id if previous is not None else None
        digest, n = inventory_digest(db, batch)
        if n != manifest["baseline"]["counts"]["stock_items"]:
            raise ValidationError("Количество товаров изменилось.")
        exists = db.scalar(select(MatchingRun).where(
            MatchingRun.algorithm_version == ALGORITHM_VERSION,
            MatchingRun.input_digest == digest,
            MatchingRun.state == "done",
        ).limit(1))
        if exists:
            raise ValidationError("Этот алгоритм уже был выполнен на данном наборе.")
        new = MatchingRun(
            batch_id=batch.id, input_digest=digest, algorithm_version=ALGORITHM_VERSION,
            state="queued", total=n,
        )
        db.add(new)
        db.commit()
        new_id = new.id
    run_matching(new_id)
    with SessionLocal() as db:
        run_record = db.get(MatchingRun, new_id)
        if not run_record or run_record.state != "done" or run_record.processed != n:
            raise ValidationError("Новый запуск завершился неуспешно или неполно.")
        report = matching_audit(db, new_id)
        report["elapsed_seconds_local"] = round(time.monotonic() - started, 2)
        report["comparison_previous_run_id"] = previous_id
        report["new_run_id"] = new_id
        comparison = _comparison_csv(
            db, previous_id, new_id, workspace / "reports" / "comparison_v045_v047.csv"
        )
        report["comparison"] = comparison
    with _open_ro(target) as conn:
        _same_data(conn, manifest["baseline"])
    _json(workspace / "reports" / "matching_diagnostic.json", report)
    _json(workspace / "reports" / "integrity_report.json", {
        "protected_tables_unchanged": True,
        "historical_rows_unchanged": True,
        "sqlite_health": "ok",
        "new_run_id": new_id,
    })
    print("Новый анализ завершён.")
    print("Обработано:", n)
    print("Оценено кандидатов:", report["evaluated_candidate_pairs"])
    print("Показано кандидатов:", report["displayed_candidate_pairs"])
    print("Изменился первый кандидат:", comparison["changed_first_candidate"])
    print("Отчёты:", workspace / "reports")
    print("Необходимо выполнить независимую ручную проверку. Слияние ветки запрещено.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Изолированная валидация PharmDesk")
    parser.add_argument("action", choices=["prepare", "run"])
    parser.add_argument("--source", help="Путь к рабочей pharmdesk.sqlite3")
    parser.add_argument("--workspace", help="Тестовая папка (вне основного репозитория)")
    args = parser.parse_args(argv)
    try:
        {"prepare": prepare, "run": run}[args.action](args)
        return 0
    except (ValidationError, sqlite3.Error, OSError, ValueError) as exc:
        print("ОСТАНОВЛЕНО:", str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
