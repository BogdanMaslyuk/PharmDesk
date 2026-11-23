"""Read-only local triage for PharmDesk 0.4.7; no external network or DB writes.

Run after validation_v047 prepare/run:
  python -m scripts.v047_review_triage

Outputs (local/private, never overwrite previous review files):
reports/quality_triage_v047_v2.json and
reports/manual_review_critical_50_v2.csv.
"""
import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
import sqlite3
import sys

from .validation_v047 import (
    ROOT, PRODUCTION_ROOT, WORKSPACE_ROOT, ValidationError,
    _is_inside, assert_isolated, _open_ro, _same_data, _json,
    _read_manifest, _safe_excel_text, _sha256_file,
)

GROUPS = (
    ("critical", 20, "Противоречие формы, дозы или высвобождения у первого кандидата"),
    ("lost", 2, "В версии 0.4.7 исчезли кандидаты"),
    ("new", 8, "Появились кандидаты после исправления названия"),
    ("changed", 8, "Изменился лучший кандидат"),
    ("many", 6, "Более 30 регистрационных кандидатов"),
    ("none", 4, "Кандидаты отсутствуют"),
    ("stable", 2, "Контрольные позиции без смены лидера"),
)


def _rows(db, run_id):
    return {int(row["stock_item_id"]): dict(row) for row in db.execute("""
        SELECT id, stock_item_id, article_code, drawer_number, stock_name,
               category, candidate_total, parsed_json, reasons_json
        FROM matching_items WHERE run_id = ?
    """, (run_id,))}


def _best(db, run_id):
    output = {}
    for row in db.execute("""
        SELECT mi.stock_item_id AS stock_id, mc.grls_row_id AS grls_id,
               r.registration_number AS reg, r.trade_name AS grls_name,
               r.status_from_filename AS source_status,
               mc.id AS candidate_id, mc.rank AS rank
        FROM matching_candidates mc
        JOIN matching_items mi ON mi.id = mc.item_id
        JOIN grls_import_rows r ON r.id = mc.grls_row_id
        WHERE mi.run_id = ?
        ORDER BY mi.stock_item_id, mc.rank, mc.id
    """, (run_id,)):
        k = int(row["stock_id"])
        if k not in output:
            output[k] = dict(row)
    return output


def _evidence(db, candidates):
    result = {}
    ids = [c["candidate_id"] for c in candidates.values()]
    if not ids:
        return result
    # SQLite may impose a limit on bound variables; use bounded batches.
    for offset in range(0, len(ids), 400):
        chunk = ids[offset:offset + 400]
        placeholders = ",".join(["?"] * len(chunk))
        for row in db.execute(
            f"SELECT candidate_id, field, outcome FROM matching_evidence "
            f"WHERE candidate_id IN ({placeholders})", chunk
        ):
            result.setdefault(row["candidate_id"], {})[row["field"]] = row["outcome"]
    return result


def _stable_sort_key(row):
    return (hashlib.sha256(str(row["stock_id"]).encode("ascii")).hexdigest(),
            row["stock_id"])


def select_priority_review(records, *, sample_size=50):
    """Deterministic, nonrepresentative 50-row safety-first selection."""
    selected, chosen = [], set()
    for group, quota, _ in GROUPS:
        if group == "critical":
            eligible = [r for r in records if r["critical"]]
        elif group == "many":
            eligible = [r for r in records if r["candidate_total"] > 30]
        elif group == "none":
            eligible = [r for r in records if r["candidate_total"] == 0]
        else:
            eligible = [r for r in records if r["group"] == group]
        eligible.sort(key=_stable_sort_key)
        taken = 0
        for record in eligible:
            if len(selected) >= sample_size or taken >= quota:
                break
            if record["stock_id"] in chosen:
                continue
            selected.append((group, record))
            chosen.add(record["stock_id"])
            taken += 1
    for record in sorted(records, key=_stable_sort_key):
        if len(selected) >= sample_size:
            break
        if record["stock_id"] not in chosen:
            selected.append(("fill", record))
            chosen.add(record["stock_id"])
    if len(selected) != min(sample_size, len(records)):
        raise ValidationError("Не удалось сформировать уникальную выборку.")
    return selected


def generate_triage(db, baseline, *, sample_size=50):
    old_runs = db.execute("""
        SELECT id FROM matching_runs
        WHERE algorithm_version='0.4.5-conservative-1' AND state='done'
        ORDER BY id DESC LIMIT 1
    """).fetchone()
    new_runs = db.execute("""
        SELECT id FROM matching_runs
        WHERE algorithm_version='0.4.7-evidence-1' AND state='done'
        ORDER BY id DESC LIMIT 1
    """).fetchone()
    if not new_runs:
        raise ValidationError("Нет завершённой проверки алгоритмом 0.4.7.")
    old_id = old_runs["id"] if old_runs else None
    new_id = new_runs["id"]
    old = _rows(db, old_id) if old_id is not None else {}
    new = _rows(db, new_id)
    if len(new) != baseline["counts"]["stock_items"]:
        raise ValidationError("Число товаров нового анализа не совпадает с базовой копией.")
    if old_id is not None and set(old) != set(new):
        raise ValidationError("Состав товаров в старой и новой проверке различается.")
    old_best = _best(db, old_id) if old_id is not None else {}
    new_best = _best(db, new_id)
    facts = _evidence(db, new_best)

    records = []
    counts = Counter()
    best_fields = {f: Counter() for f in ("name", "form", "dose", "release", "package")}
    for stock_id, curr in sorted(new.items()):
        prev = old.get(stock_id)
        before = old_best.get(stock_id)
        after = new_best.get(stock_id)
        parsed = json.loads(curr["parsed_json"] or "{}")
        states = facts.get(after["candidate_id"], {}) if after else {}
        if old_id is None:
            group = "unknown"
        elif prev["candidate_total"] == 0 and curr["candidate_total"] > 0:
            group = "new"
        elif prev["candidate_total"] > 0 and curr["candidate_total"] == 0:
            group = "lost"
        elif (before or {}).get("grls_id") != (after or {}).get("grls_id"):
            group = "changed"
        else:
            group = "stable"
        counts[group] += 1
        counts["category_" + curr["category"]] += 1
        if curr["candidate_total"] == 0:
            counts["no_candidates"] += 1
        if curr["candidate_total"] > 30:
            counts["many_candidates"] += 1
        if not parsed.get("evaluation_complete", False):
            counts["partial_or_unknown_evaluation"] += 1
        if states.get("form") == states.get("dose") == "match":
            counts["best_form_dose_match"] += 1
        if any(states.get(field) == "conflict" for field in ("form", "dose", "release")):
            counts["best_critical_conflict"] += 1
        for field in best_fields:
            if field in states:
                best_fields[field][states[field]] += 1
        records.append({
            "stock_id": stock_id,
            "article_code": curr["article_code"],
            "drawer": curr["drawer_number"],
            "stock_name": curr["stock_name"],
            "previous_category": prev["category"] if prev else "",
            "new_category": curr["category"],
            "group": group,
            "old_reg": before["reg"] if before else "",
            "new_reg": after["reg"] if after else "",
            "old_grls_id": before["grls_id"] if before else "",
            "new_grls_id": after["grls_id"] if after else "",
            "new_grls_name": after["grls_name"] if after else "",
            "source_status_snapshot": after["source_status"] if after else "",
            "candidate_total": curr["candidate_total"],
            "evaluated": parsed.get("evaluated_count", ""),
            "shown": parsed.get("displayed_count", ""),
            "reason": " / ".join(json.loads(curr["reasons_json"] or "[]")),
            "trade_extracted": parsed.get("trade", ""),
            "form": states.get("form", ""),
            "dose": states.get("dose", ""),
            "release": states.get("release", ""),
            "package": states.get("package", ""),
            "critical": any(states.get(field) == "conflict"
                            for field in ("form", "dose", "release")),
        })

    selected = select_priority_review(records, sample_size=sample_size)
    critical_selected = sum(bool(r["critical"]) for _, r in selected)
    # For this historical dataset 14 critical cases can all fit in 50.
    # If there are more than the bounded priority quota, report the gap.
    critical_total = counts["best_critical_conflict"]

    summary = {
        "version": 1,
        "old_run_id": old_id,
        "new_run_id": new_id,
        "items": len(records),
        "counts": dict(counts),
        "best_candidate_evidence": {k: dict(v) for k,v in best_fields.items()},
        "sample_size": len(selected),
        "critical_selected": critical_selected,
        "critical_total": critical_total,
        "critical_unselected": critical_total - critical_selected,
        "priority_version": 2,
        "sample_categories": dict(Counter(group for group,_ in selected)),
        "sampling": "Целевая первичная выборка, не репрезентативна для оценки точности.",
        "data_scope": "Агрегаты; исходные товарные названия и коды в JSON не включены.",
        "validation_warning": (
            "Совпадение извлечённых признаков не подтверждает регистрацию, "
            "состав, медицинскую идентичность или взаимозаменяемость."
        ),
    }
    return summary, selected


def export_review(path, selection):
    headers = [
        "Группа", "Код товара", "Ящик", "Исходное название",
        "Категория раньше", "Категория сейчас", "Название из товара (предполож.)",
        "Регистрационный номер раньше", "Регистрационный номер сейчас",
        "Торговое название ГРЛС", "Статус файла снимка",
        "Кандидатов всего", "Проверено", "Показано",
        "Форма", "Доза", "Высвобождение", "Упаковка",
        "Причины", "Ручное решение", "Источник проверки", "Комментарий проверяющего",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(headers)
        for group, row in selection:
            writer.writerow([_safe_excel_text(v) for v in [
                group, row["article_code"], row["drawer"], row["stock_name"],
                row["previous_category"], row["new_category"], row["trade_extracted"],
                row["old_reg"], row["new_reg"], row["new_grls_name"],
                row["source_status_snapshot"], row["candidate_total"],
                row["evaluated"], row["shown"], row["form"], row["dose"],
                row["release"], row["package"], row["reason"], "", "", "",
            ]])


def run(args):
    source = Path(args.source or (PRODUCTION_ROOT / "pharmdesk.sqlite3")).resolve()
    workspace = Path(args.workspace or WORKSPACE_ROOT).resolve()
    assert_isolated(source, workspace, code_root=ROOT)
    manifest = _read_manifest(workspace)
    database = Path(manifest["test_db"]).resolve()
    backup = Path(manifest["backup"]).resolve()
    if source != Path(manifest["source"]).resolve():
        raise ValidationError("Исходный путь базы отличается от сохранённого.")
    if not _is_inside(database, workspace / "database") or database == source:
        raise ValidationError("Ошибка изоляции: тестовая SQLite находится вне тестовой области.")
    if not database.is_file() or not backup.is_file():
        raise ValidationError("Отсутствует тестовая база или проверенная копия.")
    if _sha256_file(backup) != manifest["backup_sha256"]:
        raise ValidationError("Изменилась контрольная резервная копия.")
    with _open_ro(database) as conn:
        conn.row_factory = sqlite3.Row
        _same_data(conn, manifest["baseline"])
        summary, sample = generate_triage(conn, manifest["baseline"])
    report_dir = workspace / "reports"
    summary_path = report_dir / "quality_triage_v047_v2.json"
    review_path = report_dir / "manual_review_critical_50_v2.csv"
    if summary_path.exists() or review_path.exists():
        raise ValidationError(
            "Файлы второй выборки уже существуют; чтобы не потерять ручные "
            "решения, их автоматическая перезапись запрещена."
        )
    _json(summary_path, summary)
    export_review(review_path, sample)
    print("Диагностика первой очереди завершена.")
    print("Товаров:", summary["items"])
    print("Группы изменений:", summary["counts"])
    print("Выборка для ручной проверки:", summary["sample_size"])
    print("Критические случаи в выборке:",
          summary["critical_selected"], "из", summary["critical_total"])
    print("Агрегированный отчёт:", summary_path)
    print("Локальная очередь:", review_path)
    print("Исходная и тестовая SQLite не изменялись.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Читающая проверка качества PharmDesk")
    parser.add_argument("--source")
    parser.add_argument("--workspace")
    args = parser.parse_args(argv)
    try:
        run(args)
        return 0
    except (ValidationError, sqlite3.Error, OSError, ValueError, KeyError) as exc:
        print("ОСТАНОВЛЕНО:", exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
