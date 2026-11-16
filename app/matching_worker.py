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
from .models import (
    GrlsImportRow, MatchingCandidate, MatchingEvidence,
    MatchingItem, MatchingRun, StockItem, Medicine,
)
from .search import normalize

ALGORITHM_VERSION = "0.4.5-conservative-1"
MAX_CANDIDATES_PER_ITEM = 30


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
    if len(trade_key) < 3 or trade_key.isdigit():
        return 0, []
    stmt = select(GrlsImportRow).where(
        GrlsImportRow.batch_id == batch_id,
        GrlsImportRow.trade_key == trade_key,
    )
    found = db.scalars(
        stmt.order_by(GrlsImportRow.id).limit(MAX_CANDIDATES_PER_ITEM + 1)
    ).all()
    if len(found) <= MAX_CANDIDATES_PER_ITEM:
        return len(found), found
    count = db.scalar(select(func.count()).select_from(stmt.subquery())) or len(found)
    return count, found[:MAX_CANDIDATES_PER_ITEM]


def _analyze_one(db, run, item):
    parsed = stock_attributes(item.medicine.name)
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
    total, rows = _matching_rows(db, run.batch_id, parsed["trade_key"])
    current.candidate_total = total
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
    strong = (total == 1 and candidate_results[0][1]["category"] == "strong")
    current.category = "strong" if strong else "ambiguous"
    if strong:
        run.strong += 1
    else:
        run.ambiguous += 1
    reasons = []
    if total > MAX_CANDIDATES_PER_ITEM:
        reasons.append(
            f"В реестре {total} записей с таким названием; показаны первые "
            f"{MAX_CANDIDATES_PER_ITEM} без автоматического выбора."
        )
    if total > 1:
        reasons.append(f"Найдено {total} регистрационных записей с тем же полным названием.")
    # Preserve detailed comparison for each candidate, not just a score.
    for rank, (row, comp) in enumerate(
        sorted(candidate_results, key=lambda pair: (
            0 if pair[1]["category"] == "strong" else 1,
            0 if pair[0].status_from_filename == "Действующий" else 1,
            pair[0].registration_number, pair[0].id,
        )), start=1
    ):
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
