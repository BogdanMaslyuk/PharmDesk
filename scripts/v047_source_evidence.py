"""Read-only extraction of exact source evidence for 14 conflicts + 2 losses.

Uses existing isolated validation SQLite, not the pharmacy production database.
No article codes, drawer numbers or patient data are exported.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .validation_v047 import (
    ROOT, PRODUCTION_ROOT, WORKSPACE_ROOT,
    ValidationError, assert_isolated, _is_inside, _open_ro, _same_data,
    _read_manifest, _sha256_file, _json
)

V1 = "0.4.7-evidence-1"
V2 = "0.4.7-evidence-2"
LEGACY = "0.4.5-conservative-1"


def _latest_run(conn, algorithm):
    row = conn.execute(
        "SELECT id FROM matching_runs WHERE algorithm_version=? "
        "AND state='done' ORDER BY id DESC LIMIT 1", (algorithm,)
    ).fetchone()
    return int(row[0]) if row else None


def _candidate(conn, run_id, stock_id):
    if run_id is None:
        return None
    row = conn.execute("""
        SELECT mc.id AS candidate_id, r.registration_number, r.trade_name,
               r.release_forms_raw, r.inn_raw, r.source_workbook, r.source_row,
               r.status_from_filename
        FROM matching_items mi
        JOIN matching_candidates mc ON mc.item_id=mi.id AND mc.rank=1
        JOIN grls_import_rows r ON r.id=mc.grls_row_id
        WHERE mi.run_id=? AND mi.stock_item_id=?
        LIMIT 1
    """, (run_id, stock_id)).fetchone()
    if not row:
        return None
    entry = {k: row[k] for k in row.keys() if k != "candidate_id"}
    evidence = conn.execute(
        "SELECT field, outcome, registry_value, explanation "
        "FROM matching_evidence WHERE candidate_id=? ORDER BY id",
        (row["candidate_id"],)
    ).fetchall()
    entry["comparison"] = [dict(x) for x in evidence]
    return entry


def _family_sources(conn, stock_name, batch_id):
    from app.search import normalize
    words = stock_name.lower().split()
    stem = normalize(words[0]) if words else ""
    if len(stem) < 4:
        return []
    rows = conn.execute("""
        SELECT registration_number, trade_name, release_forms_raw,
               status_from_filename
        FROM grls_import_rows
        WHERE batch_id=? AND trade_key LIKE ? AND length(trade_key) < 70
        ORDER BY trade_key, registration_number, id LIMIT 15
    """, (batch_id, stem + "%")).fetchall()
    return [dict(x) for x in rows]


def run(args):
    production = Path(args.source or (PRODUCTION_ROOT / "pharmdesk.sqlite3")).resolve()
    workspace = Path(args.workspace or WORKSPACE_ROOT).resolve()
    assert_isolated(production, workspace, code_root=ROOT)
    manifest = _read_manifest(workspace)
    target = Path(manifest["test_db"]).resolve()
    backup = Path(manifest["backup"]).resolve()
    if production != Path(manifest["source"]).resolve():
        raise ValidationError("Путь к исходной базе изменён.")
    if not _is_inside(target, workspace / "database") or target == production:
        raise ValidationError("Тестовая SQLite не изолирована.")
    if not target.is_file() or not backup.is_file():
        raise ValidationError("Не найдены тестовая SQLite или резервная копия.")
    if _sha256_file(backup) != manifest["backup_sha256"]:
        raise ValidationError("Резервная копия изменилась.")
    output = workspace / "reports" / "p3_source_evidence_16.json"
    if output.exists():
        raise ValidationError("Исходная ведомость уже создана; перезапись запрещена.")

    with _open_ro(target) as conn:
        conn.row_factory = sqlite3.Row
        _same_data(conn, manifest["baseline"])
        older = _latest_run(conn, LEGACY)
        v1 = _latest_run(conn, V1)
        v2 = _latest_run(conn, V2)
        if older is None or v1 is None:
            raise ValidationError("Не найдены старые завершённые проверки.")
        batch = conn.execute(
            "SELECT batch_id FROM matching_runs WHERE id=?", (v1,)
        ).fetchone()
        critical = conn.execute("""
            SELECT DISTINCT mi.stock_item_id, mi.stock_name, 'critical' AS kind
            FROM matching_items mi
            JOIN matching_candidates mc ON mc.item_id=mi.id AND mc.rank=1
            JOIN matching_evidence ev ON ev.candidate_id=mc.id
            WHERE mi.run_id=? AND ev.field IN ('form','dose','release')
              AND ev.outcome='conflict'
        """, (v1,)).fetchall()
        lost = conn.execute("""
            SELECT curr.stock_item_id, curr.stock_name, 'lost' AS kind
            FROM matching_items curr
            JOIN matching_items prev ON curr.stock_item_id=prev.stock_item_id
            WHERE curr.run_id=? AND prev.run_id=?
              AND prev.candidate_total>0 AND curr.candidate_total=0
        """, (v1, older)).fetchall()
        cases = []
        for row in sorted(list(critical) + list(lost),
                          key=lambda x: (x["kind"], x["stock_name"])):
            sid = row["stock_item_id"]
            entry = {
                "type": row["kind"],
                "stock_name": row["stock_name"],
                "legacy_best": _candidate(conn, older, sid),
                "v1_best": _candidate(conn, v1, sid),
                "v2_best": _candidate(conn, v2, sid),
            }
            if row["kind"] == "lost":
                entry["possible_family_rows_unverified"] = _family_sources(
                    conn, row["stock_name"], batch["batch_id"]
                )
            cases.append(entry)
    result = {
        "cases_count": len(cases),
        "critical_count": len(critical),
        "lost_count": len(lost),
        "v1_run": v1,
        "v2_run": v2,
        "cases": cases,
        "warning": (
            "Извлечённые записи относятся к загруженному снимку ГРЛС и "
            "не подтверждают состав, регистрацию или взаимозаменяемость."
        ),
        "privacy": "Внутренние артикулы и ящики не выгружаются. Не публиковать в GitHub.",
    }
    _json(output, result)
    print("Подробные сведения ГРЛС выгружены, база открывалась только для чтения.")
    print("Критические позиции:", len(critical), "потерянные:", len(lost))
    print("Отчёт:", output)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Читающая ревизия 16 случаев")
    parser.add_argument("--source")
    parser.add_argument("--workspace")
    args = parser.parse_args(argv)
    try:
        run(args)
        return 0
    except (ValidationError, sqlite3.Error, OSError, KeyError, ValueError) as exc:
        print("ОСТАНОВЛЕНО:", exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
