"""Reproducible bulk GRLS analysis. No modifications to pharmacy or source rows.

Every StockItem has a result, including variants stored under the same
Medicine. Only indexed full-name candidate lookup is used. No source
is ever accepted as clinically/legally verified by this worker.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .database import SessionLocal
from .matching_parse import evaluate, source_attributes, stock_attributes
from .trade_candidates import trade_name_from_snapshot
from .models import (
    GrlsImportRow, MatchingCandidate, MatchingEvidence,
    MatchingItem, MatchingRun, StockItem, Medicine,
)
from .search import normalize

ALGORITHM_VERSION = "0.4.5-conservative-1"
MAX_CANDIDATES_PER_ITEM = 30
# Guard against unusually large title families. Do not claim full coverage if exceeded.
MAX_EVALUATED_PER_ITEM = 2000


def stock_hash(item):
    basis = [item.id, item.medicine_id, item.article_code, item.drawer.number,
             item.medicine.name, item.medicine.manufacturer or "",
             item.medicine.form or "", item.medicine.dosage or "",
             item.medicine.package or ""]
    return sha256(json.dumps(basis, ensure_ascii=False).encode("utf-8")).hexdigest()


def inventory_digest(db, batch):
    """Stable digest includes source identity and every current stock record."""
    h = sha256((batch.sha256 + "|" + ALGORITHM_VERSION).encode("ascii"))
    items = db.scalars(
        select(StockItem)
        .options(selectinload(StockItem.medicine), selectinload(StockItem.drawer))
        .order_by(StockItem.id)
    ).all()
    for item in items:
        h.update(stock_hash(item).encode("ascii"))
    return h.hexdigest(), len(items)


def _matching_rows(db, batch_id, trade_key):
    """Fetch exact title records for evidence-first ranking, not ID-first truncation."""
    if len(trade_key) < 3 or trade_key.isdigit():
        return 0, [], False
    stmt = select(GrlsImportRow).where(
        GrlsImportRow.batch_id == batch_id,
        GrlsImportRow.trade_key == trade_key,
    )
    rows = db.scalars(
        stmt.order_by(GrlsImportRow.id).limit(MAX_EVALUATED_PER_ITEM + 1)
    ).all()
    if len(rows) <= MAX_EVALUATED_PER_ITEM:
        return len(rows), rows, False
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or len(rows)
    return total, rows[:MAX_EVALUATED_PER_ITEM], True


def _resolve_trade(db, batch_id, inventory_name):
    """Resolve exact title from snapshot only at a safe form/package boundary."""
    import re
    words = re.findall(r"[а-яёa-z0-9]+", str(inventory_name or "").casefold().replace("ё", "е"))
    phrases = [" ".join(words[:i]) for i in range(1, min(len(words), 12) + 1)]
    keys = {normalize(p) for p in phrases if len(normalize(p)) >= 3}
    if not keys:
        return inventory_name
    known = set(db.scalars(select(GrlsImportRow.trade_key).where(
        GrlsImportRow.batch_id == batch_id,
        GrlsImportRow.trade_key.in_(keys),
    ).distinct()).all())
    return trade_name_from_snapshot(inventory_name, known)


def _rank_candidate(pair):
    """Evidence-only ranking; no estimated medical probability or verification."""
    row, comp = pair
    states = {ev["field"]: ev["outcome"] for ev in comp["evidence"]}
    critical = ("name", "form", "dose", "release")
    conflicts = sum(states.get(k) == "conflict" for k in critical)
    matched = sum(states.get(k) == "match" for k in ("name", "form", "dose"))
    uncertain = sum(states.get(k) in ("ambiguous", "unknown") for k in ("form", "dose", "release"))
    return (
        conflicts,
        -matched,
        uncertain,
        0 if states.get("package") == "match" else 1,
        0 if row.status_from_filename in ("Действующий", "Выдано по правилам ЕАЭС") else 1,
        row.registration_number or "",
        row.id,
    )


def _analyze_one(db, run, item):
    resolved_trade = _resolve_trade(db, run.batch_id, item.medicine.name)
    parsed = stock_attributes(item.medicine.name, trade_override=resolved_trade)
    parsed["manufacturer"] = item.medicine.manufacturer or ""
    current = MatchingItem(
        run_id=run.id, stock_item_id=item.id,
        medicine_id=item.medicine_id,
        drawer_number=item.drawer.number,
        article_code=item.article_code,
        stock_name=item.medicine.name,
        stock_manufacturer=item.medicine.manufacturer or "",
        stock_fingerprint=stock_hash(item),
        parsed_json=json.dumps(parsed, ensure_ascii=False),
        category="none", candidate_total=0,
    )
    db.add(current)
    db.flush()
    total, rows, truncated_evaluation = _matching_rows(db, run.batch_id, parsed["trade_key"])
    current.candidate_total = total
    parsed["evaluated_count"] = len(rows)
    parsed["evaluation_complete"] = not truncated_evaluation
    parsed["displayed_count"] = min(MAX_CANDIDATES_PER_ITEM, len(rows))
    current.parsed_json = json.dumps(parsed, ensure_ascii=False)
    if not rows:
        current.category = "none"
        current.reasons_json = json.dumps(
            ["По полному торговому названию кандидаты в выбранной выгрузке не найдены. "
             "Это не подтверждает отсутствие регистрации."],
            ensure_ascii=False,
        )
        run.not_found += 1
        return

    candidate_results = []
    for row in rows:
        source = source_attributes(row.release_forms_raw)
        source["trade"] = row.trade_name
        source["holder"] = row.certificate_holder or ""
        comparison = evaluate(
            parsed, source, exact_trade=normalize(row.trade_name) == parsed["trade_key"],
            source_status=row.status_from_filename, total_same_name=total,
        )
        candidate_results.append((row, comparison))
    # Incompleteness or family-name ambiguity must not be hidden by ranking.
    candidate_results.sort(key=_rank_candidate)
    visible_results = candidate_results[:MAX_CANDIDATES_PER_ITEM]
    strong = (not truncated_evaluation and total == 1
              and candidate_results[0][1]["category"] == "strong")
    current.category = "strong" if strong else "ambiguous"
    if strong:
        run.strong += 1
    else:
        run.ambiguous += 1
    reasons = []
    if truncated_evaluation:
        reasons.append(f"Ограничение вычислений: проверено {len(rows)} из {total}; "
                       "полнота выбора не гарантирована.")
    if total > MAX_CANDIDATES_PER_ITEM:
        reasons.append(f"Оценено {len(rows)} из {total}, показаны лучшие "
                       f"{len(visible_results)} по извлечённым признакам; "
                       "скрытые альтернативы требуют дополнительной проверки.")
    if total > 1:
        reasons.append(f"Найдено {total} регистрационных записей с тем же полным названием.")
    # Preserve detailed comparison for each candidate, not just a score.
    for rank, (row, comp) in enumerate(visible_results, start=1):
        match = MatchingCandidate(
            item_id=current.id, grls_row_id=row.id,
            category=comp["category"], rank=rank,
        )
        db.add(match)
        db.flush()
        for fact in comp["evidence"]:
            db.add(MatchingEvidence(candidate_id=match.id, **fact))
        if rank == 1:
            reasons.extend(comp["reasons"])
    if not reasons:
        reasons = ["Кандидат требует ручной сверки."]
    current.reasons_json = json.dumps(list(dict.fromkeys(reasons)), ensure_ascii=False)


def run_matching(run_id, *, session_factory=None):
    """Background job with periodic commits and durable progress.

    Stale input data marks results stale instead of silently presenting them
    as current. On failure, previously verified registration links are intact.
    """
    session_factory = session_factory or SessionLocal
    with session_factory() as db:
        run = db.get(MatchingRun, run_id)
        if run is None or run.state != "queued":
            return
        try:
            batch = run.batch
            digest, total = inventory_digest(db, batch)
            if digest != run.input_digest:
                run.state = "stale"
                run.error_text = "Ассортимент изменился после запуска. Создайте новый анализ."
                run.finished_at = datetime.now(timezone.utc)
                db.commit()
                return
            items = db.scalars(
                select(StockItem)
                .options(selectinload(StockItem.medicine), selectinload(StockItem.drawer))
                .order_by(StockItem.id)
            ).all()
            run.total = total
            run.state = "running"
            db.commit()
            for index, item in enumerate(items, start=1):
                _analyze_one(db, run, item)
                run.processed = index
                if index % 20 == 0:
                    db.commit()
            current_digest, _ = inventory_digest(db, batch)
            if current_digest != run.input_digest:
                run.state = "stale"
                run.error_text = "Ассортимент изменился во время сопоставления. Результаты устарели."
            else:
                run.state = "done"
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
        except Exception as exc:
            db.rollback()
            row = db.get(MatchingRun, run_id)
            if row:
                row.state = "failed"
                row.finished_at = datetime.now(timezone.utc)
                # Avoid logging user-provided field content to server/UI.
                row.error_text = "Ошибка обработки: " + type(exc).__name__
                db.commit()
            raise
