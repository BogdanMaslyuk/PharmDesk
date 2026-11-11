"""Local bulk-GRLS matching UI, history, review and safe CSV/XLSX export."""
import csv
from datetime import datetime, timezone
from io import BytesIO, StringIO
import json
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

from .database import get_db
from .manage import _check_csrf, _csrf
from .matching_worker import ALGORITHM_VERSION, inventory_digest, run_matching, stock_hash
from .models import (
    GrlsImportBatch, GrlsImportRow, MatchingCandidate, MatchingDecision,
    MatchingEvidence, MatchingItem, MatchingRun, Medicine, StockItem,
    MedicineRegistrationLink,
)
from .search import normalize

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _page(request, template, **context):
    return templates.TemplateResponse(
        request, template, {"page": "manage", "csrf_token": _csrf(request), **context}
    )


def _last_batch(db):
    return db.scalar(
        select(GrlsImportBatch)
        .order_by(GrlsImportBatch.snapshot_on.desc(), GrlsImportBatch.id.desc())
        .limit(1)
    )


def _get_run(db, run_id):
    run = db.get(MatchingRun, run_id)
    if run is None:
        raise HTTPException(404, "Массовая проверка не найдена.")
    return run


def _get_item(db, run_id, item_id):
    item = db.scalar(
        select(MatchingItem)
        .where(MatchingItem.id == item_id, MatchingItem.run_id == run_id)
        .options(
            selectinload(MatchingItem.candidates)
            .selectinload(MatchingCandidate.source_row),
            selectinload(MatchingItem.candidates)
            .selectinload(MatchingCandidate.evidence),
        )
    )
    if item is None:
        raise HTTPException(404, "Товар не найден в этом запуске проверки.")
    return item


def _run_state(db):
    return db.scalar(select(MatchingRun).where(
        MatchingRun.state.in_(("queued", "running"))
    ).order_by(MatchingRun.id.desc()).limit(1))


@router.get("/manage/matching", response_class=HTMLResponse)
def matching_home(request: Request, db: Session = Depends(get_db)):
    runs = db.scalars(
        select(MatchingRun).order_by(MatchingRun.id.desc()).limit(20)
    ).all()
    batches = db.scalars(
        select(GrlsImportBatch)
        .order_by(GrlsImportBatch.snapshot_on.desc(), GrlsImportBatch.id.desc())
        .limit(50)
    ).all()
    batch = batches[0] if batches else None
    stocks = db.scalar(select(func.count(StockItem.id))) or 0
    active = _run_state(db)
    return _page(request, "matching_home.html", runs=runs, batches=batches,
                 batch=batch, stocks=stocks, active=active, algorithm=ALGORITHM_VERSION)


@router.post("/manage/matching/start")
def matching_start(
    request: Request, background_tasks: BackgroundTasks,
    csrf_token: str = Form(...), batch_id: int = Form(...),
    force: str = Form(""), db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    active = _run_state(db)
    if active:
        return RedirectResponse(f"/manage/matching/{active.id}", status_code=303)
    batch = db.get(GrlsImportBatch, batch_id)
    if batch is None:
        raise HTTPException(400, "Не выбрана существующая выгрузка ГРЛС.")
    digest, total = inventory_digest(db, batch)
    if total == 0:
        raise HTTPException(400, "Товарных записей нет. Сначала загрузите ассортимент.")
    if force != "yes":
        previous = db.scalar(
            select(MatchingRun).where(
                MatchingRun.batch_id == batch_id,
                MatchingRun.input_digest == digest,
                MatchingRun.algorithm_version == ALGORITHM_VERSION,
                MatchingRun.state == "done",
            ).order_by(MatchingRun.id.desc()).limit(1)
        )
        if previous:
            return RedirectResponse(f"/manage/matching/{previous.id}?reused=1", status_code=303)
    run = MatchingRun(
        batch_id=batch.id, input_digest=digest, algorithm_version=ALGORITHM_VERSION,
        state="queued", total=total,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    # Starlette runs this after the response is sent, in the app's threadpool.
    # One task per running job; no external server or new service required.
    background_tasks.add_task(run_matching, run.id)
    return RedirectResponse(f"/manage/matching/{run.id}", status_code=303)


@router.get("/manage/matching/{run_id}/status")
def matching_status(run_id: int, db: Session = Depends(get_db)):
    run = _get_run(db, run_id)
    return {
        "id": run.id, "state": run.state, "total": run.total,
        "processed": run.processed, "strong": run.strong,
        "ambiguous": run.ambiguous, "not_found": run.not_found,
        "error": run.error_text,
    }


@router.get("/manage/matching/{run_id}", response_class=HTMLResponse)
def matching_results(
    run_id: int, request: Request, q: str = Query("", max_length=100),
    category: str = Query("all", max_length=20),
    drawer: str = Query("", max_length=10),
    page_number: int = Query(1, ge=1), db: Session = Depends(get_db),
):
    run = _get_run(db, run_id)
    if category not in ("all", "strong", "ambiguous", "none"):
        category = "all"
    stmt = select(MatchingItem).where(MatchingItem.run_id == run_id)
    if category != "all":
        stmt = stmt.where(MatchingItem.category == category)
    drawer_no = int(drawer) if drawer.isdecimal() and int(drawer) > 0 else None
    if drawer_no is not None:
        stmt = stmt.where(MatchingItem.drawer_number == drawer_no)
    if q.strip():
        # SQL search over a small local result set; never scan full GRLS.
        stmt = stmt.where(or_(
            MatchingItem.stock_name.ilike("%" + q + "%"),
            MatchingItem.article_code.ilike("%" + q + "%"),
        ))
    count = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    limit = 40
    pages = max(1, (count + limit - 1) // limit)
    page_number = min(page_number, pages)
    items = db.scalars(
        stmt.order_by(MatchingItem.id).offset((page_number - 1) * limit).limit(limit)
    ).all()
    drawers = db.scalars(
        select(MatchingItem.drawer_number).where(MatchingItem.run_id == run_id)
        .distinct().order_by(MatchingItem.drawer_number)
    ).all()
    return _page(request, "matching_results.html", run=run, items=items,
                 count=count, pages=pages, page_number=page_number,
                 q=q, category=category, drawer=drawer_no, drawers=drawers)


@router.get("/manage/matching/{run_id}/item/{item_id}", response_class=HTMLResponse)
def matching_item_detail(
    run_id: int, item_id: int, request: Request, db: Session = Depends(get_db),
):
    run = _get_run(db, run_id)
    item = _get_item(db, run_id, item_id)
    source_rows = sorted(item.candidates, key=lambda r: (r.rank, r.id))
    decisions = db.scalars(
        select(MatchingDecision)
        .where(MatchingDecision.item_id == item.id)
        .order_by(MatchingDecision.id.desc())
    ).all()
    actual = db.scalar(
        select(StockItem)
        .options(selectinload(StockItem.medicine), selectinload(StockItem.drawer))
        .where(StockItem.id == item.stock_item_id)
    )
    stale = actual is None or stock_hash(actual) != item.stock_fingerprint
    return _page(
        request, "matching_item.html",
        run=run, item=item, candidates=source_rows,
        decisions=decisions, stale=stale,
        parsed=json.loads(item.parsed_json),
        reasons=json.loads(item.reasons_json),
    )


@router.post("/manage/matching/{run_id}/item/{item_id}/decision")
def matching_decision(
    run_id: int, item_id: int, request: Request, csrf_token: str = Form(...),
    candidate_id: int = Form(...), action: str = Form(...),
    comment: str = Form(""), db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    run = _get_run(db, run_id)
    if run.state != "done":
        raise HTTPException(409, "Завершите сопоставление до ручных решений.")
    item = _get_item(db, run_id, item_id)
    actual = db.scalar(
        select(StockItem).options(
            selectinload(StockItem.medicine), selectinload(StockItem.drawer)
        ).where(StockItem.id == item.stock_item_id)
    )
    if actual is None or stock_hash(actual) != item.stock_fingerprint:
        raise HTTPException(409, "Товар изменился после анализа. Повторите проверку.")
    if action not in ("rejected", "deferred"):
        raise HTTPException(400, "Неизвестное решение. Для подтверждения нужна отдельная ручная сверка.")
    candidate = db.scalar(select(MatchingCandidate).where(
        MatchingCandidate.id == candidate_id,
        MatchingCandidate.item_id == item.id,
    ))
    if candidate is None:
        raise HTTPException(404, "Кандидат не принадлежит данному товару.")
    reason = comment.strip()
    if action == "rejected" and not reason:
        raise HTTPException(400, "Укажите причину отклонения.")
    if len(reason) > 1000:
        raise HTTPException(400, "Комментарий превышает 1000 символов.")
    db.add(MatchingDecision(item_id=item.id, candidate_id=candidate.id,
                            action=action, comment=reason))
    db.commit()
    return RedirectResponse(
        f"/manage/matching/{run_id}/item/{item_id}?decision=saved",
        status_code=303,
    )


# Prevent formula execution when CSV/XLSX is opened in spreadsheet software.
def _safe_cell(value):
    text = str(value or "")
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def _report_rows(db, run_id):
    items = db.scalars(
        select(MatchingItem)
        .where(MatchingItem.run_id == run_id)
        .order_by(MatchingItem.id)
    ).all()
    headers = [
        "Код", "Ящик", "Товар", "Категория", "Кандидатов",
        "Регистрационный номер", "Название ГРЛС", "МНН из выгрузки",
        "Формы выпуска из выгрузки", "Статус на дату выгрузки",
        "Причины и замечания",
    ]
    yield headers
    for item in items:
        best = db.scalar(
            select(MatchingCandidate)
            .options(selectinload(MatchingCandidate.source_row))
            .where(MatchingCandidate.item_id == item.id)
            .order_by(MatchingCandidate.rank).limit(1)
        )
        source = best.source_row if best else None
        yield [
            item.article_code, item.drawer_number, item.stock_name,
            {"strong": "Сильный кандидат (не подтверждён)",
             "ambiguous": "Неоднозначно", "none": "Нет кандидатов"}.get(item.category, ""),
            item.candidate_total, source.registration_number if source else "",
            source.trade_name if source else "",
            source.inn_raw if source else "",
            source.release_forms_raw if source else "",
            source.status_from_filename if source else "",
            " / ".join(json.loads(item.reasons_json)),
        ]


@router.get("/manage/matching/{run_id}/export.csv")
def export_matching_csv(run_id: int, db: Session = Depends(get_db)):
    run = _get_run(db, run_id)
    if run.state != "done":
        raise HTTPException(409, "Экспорт возможен после завершения анализа.")
    output = StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    for row in _report_rows(db, run_id):
        writer.writerow([_safe_cell(value) for value in row])
    return Response(
        content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="pharmdesk_matching_{run_id}.csv"'},
    )


@router.get("/manage/matching/{run_id}/export.xlsx")
def export_matching_xlsx(run_id: int, db: Session = Depends(get_db)):
    run = _get_run(db, run_id)
    if run.state != "done":
        raise HTTPException(409, "Экспорт возможен после завершения анализа.")
    wb = Workbook()
    ws = wb.active
    ws.title = "Сопоставление ГРЛС"
    for row in _report_rows(db, run_id):
        ws.append([_safe_cell(x)[:32767] for x in row])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col, width in {"A": 14, "B": 12, "C": 42, "D": 32,
                       "E": 14, "F": 22, "G": 30, "H": 30,
                       "I": 65, "J": 28, "K": 65}.items():
        ws.column_dimensions[col].width = width
    stream = BytesIO()
    wb.save(stream)
    return Response(
        content=stream.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="pharmdesk_matching_{run_id}.xlsx"'},
    )
