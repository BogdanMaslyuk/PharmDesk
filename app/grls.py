"""Local GRLS file-import preview/confirmation and unverified source search.

No live scraping, no automatic medicine links and no clinical assertions.
"""
from datetime import date
from hashlib import sha256
from pathlib import Path
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from .database import ROOT, get_db
from .grls_import import MAX_ZIP_BYTES, GrlsImportError, parse_grls_archive, store_grls_batch
from .grls_fetch import fetch_official_grls
from .manage import _check_csrf, _csrf
from .models import GrlsImportBatch, GrlsImportRow, Medicine
from .search import normalize
from .trade_candidates import source_candidates, trade_name_from_stock

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")

# The file remains local only between user preview and explicit confirmation.
PREVIEW_FOLDER = ROOT / "private_data" / "grls_previews"


def _page(request, filename, **extra):
    return templates.TemplateResponse(
        request, filename, {"page": "catalog", "csrf_token": _csrf(request), **extra}
    )


def _loading_page(request, error=None, preview=None):
    batches = []
    if preview:
        batches = [{"name": x, "count": count} for x, count in preview["status_counts"].items()]
    return _page(request, "grls_import.html", error=error, preview=preview, status_rows=batches)


def _prepare_preview(db, raw, filename):
    """Shared two-step staging for direct official download and manual ZIP upload."""
    parsed = parse_grls_archive(raw)
    if db.scalar(select(GrlsImportBatch.id).where(GrlsImportBatch.sha256 == parsed["sha256"])):
        raise GrlsImportError("Этот архив уже импортирован. Повторное добавление запрещено.")
    PREVIEW_FOLDER.mkdir(parents=True, exist_ok=True)
    temporary = PREVIEW_FOLDER / (parsed["sha256"] + ".zip")
    # File is named solely after SHA-256; never trust the source filename as a path.
    temporary.write_bytes(raw)
    public = {key: value for key, value in parsed.items() if key != "rows"}
    public["original_filename"] = Path((filename or "grls.zip").replace("\\", "/")).name[:180]
    public["old"] = (date.today() - parsed["snapshot_on"]).days > 30
    return public


@router.get("/catalog/grls/import", response_class=HTMLResponse)
def grls_upload(request: Request):
    return _loading_page(request)


@router.post("/catalog/grls/fetch", response_class=HTMLResponse)
def grls_fetch(request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db)):
    """Attempt direct official download and preview; no permanent writes yet."""
    _check_csrf(request, csrf_token)
    try:
        raw, source = fetch_official_grls()
        preview = _prepare_preview(db, raw, "GRLS_official_download.zip")
        preview["automatic_download"] = True
        return _loading_page(request, preview=preview)
    except (GrlsImportError, OSError) as exc:
        return _loading_page(request, error=str(exc))





@router.post("/catalog/grls/preview", response_class=HTMLResponse)
async def grls_preview(
    request: Request, csrf_token: str = Form(...),
    file: UploadFile = File(...), claimed_official: str = Form(""),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    try:
        if claimed_official != "yes":
            raise GrlsImportError("Подтвердите, что самостоятельно скачали архив с официального сайта.")
        if not (file.filename or "").casefold().endswith(".zip"):
            raise GrlsImportError("Требуется оригинальная ZIP-выгрузка ГРЛС.")
        raw = await file.read(MAX_ZIP_BYTES + 1)
        return _loading_page(
            request, preview=_prepare_preview(db, raw, file.filename or "grls.zip")
        )
    except (GrlsImportError, OSError) as exc:
        return _loading_page(request, error=str(exc))
    finally:
        await file.close()


@router.post("/catalog/grls/confirm", response_class=HTMLResponse)
def grls_confirm(
    request: Request, csrf_token: str = Form(...),
    digest: str = Form(...), original_filename: str = Form("grls.zip"),
    typed_confirmation: str = Form(""),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    try:
        if typed_confirmation != "ИМПОРТИРОВАТЬ":
            raise GrlsImportError("Для загрузки в отдельный справочник введите «ИМПОРТИРОВАТЬ».")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise GrlsImportError("Неверный идентификатор предварительного просмотра.")
        archive = PREVIEW_FOLDER / (digest + ".zip")
        if not archive.is_file():
            raise GrlsImportError("Файл предварительного просмотра не найден. Загрузите ZIP повторно.")
        raw = archive.read_bytes()
        if sha256(raw).hexdigest() != digest:
            raise GrlsImportError("Контрольная сумма файла не совпала. Ничего не импортировано.")
        parsed = parse_grls_archive(raw)
        stored = store_grls_batch(db, parsed, original_filename)
        archive.unlink(missing_ok=True)
        return RedirectResponse(
            f"/catalog?view=grls&batch_id={stored['batch_id']}&imported=1", status_code=303
        )
    except GrlsImportError as exc:
        db.rollback()
        return _loading_page(request, error=str(exc))


def _selected_batch(db, batch_id):
    if batch_id:
        batch = db.get(GrlsImportBatch, batch_id)
    else:
        batch = db.scalar(select(GrlsImportBatch).order_by(GrlsImportBatch.id.desc()).limit(1))
    return batch


@router.get("/catalog/grls", response_class=HTMLResponse)
def grls_browse(
    request: Request, batch_id: int | None = Query(None, ge=1),
    q: str = Query("", max_length=100), status: str = Query("all", max_length=150),
    page_number: int = Query(1, ge=1), db: Session = Depends(get_db),
):
    batches = db.scalars(
        select(GrlsImportBatch).order_by(GrlsImportBatch.id.desc()).limit(60)
    ).all()
    batch = _selected_batch(db, batch_id)
    if batch is None:
        return _page(request, "grls.html", batches=batches, batch=None,
                     items=[], count=0, statuses=[], q=q, status=status,
                     page_number=1, pages=1)
    status_rows = db.execute(
        select(GrlsImportRow.status_from_filename, func.count(GrlsImportRow.id))
        .where(GrlsImportRow.batch_id == batch.id)
        .group_by(GrlsImportRow.status_from_filename)
    ).all()
    statuses = [{"name": name, "count": count} for name, count in status_rows]
    allowed = {"all"} | {x["name"] for x in statuses}
    if status not in allowed:
        status = "all"
    query = select(GrlsImportRow).where(GrlsImportRow.batch_id == batch.id)
    if status != "all":
        query = query.where(GrlsImportRow.status_from_filename == status)
    if q.strip():
        key = normalize(q)
        query = query.where(
            or_(GrlsImportRow.registration_key.contains(key),
                GrlsImportRow.trade_key.contains(key))
        )
    count = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, (count + 49) // 50)
    page_number = min(page_number, pages)
    rows = db.scalars(
        query.order_by(GrlsImportRow.trade_name, GrlsImportRow.registration_number, GrlsImportRow.id)
        .offset((page_number - 1) * 50).limit(50)
    ).all()
    return _page(request, "grls.html", batches=batches, batch=batch,
                 items=rows, count=count, statuses=statuses, q=q, status=status,
                 page_number=page_number, pages=pages,
                 old=(date.today() - batch.snapshot_on).days > 30)


@router.get("/catalog/grls/matches/{medicine_id}", response_class=HTMLResponse)
def grls_matches(
    medicine_id: int, request: Request, batch_id: int | None = Query(None, ge=1),
    q: str = Query("", max_length=100), db: Session = Depends(get_db),
):
    medicine = db.get(Medicine, medicine_id)
    if medicine is None:
        raise HTTPException(404, "Товар не найден.")
    batch = _selected_batch(db, batch_id)
    expected_trade = trade_name_from_stock(medicine.name)
    phrase = q.strip() or expected_trade
    rows = source_candidates(
        db, batch.id if batch else None, medicine_name=medicine.name,
        trade_name=phrase, limit=50,
    )
    return _page(request, "grls_matches.html", medicine=medicine, batch=batch,
                 batches=db.scalars(select(GrlsImportBatch).order_by(GrlsImportBatch.id.desc())).all(),
                 rows=rows, q=phrase, expected_trade=expected_trade,
                 query_differs=normalize(phrase) != normalize(expected_trade))


@router.get("/catalog/grls/row/{row_id}", response_class=HTMLResponse)
def grls_row(row_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(GrlsImportRow, row_id)
    if row is None:
        raise HTTPException(404, "Строка выгрузки не найдена.")
    return _page(request, "grls_row.html", row=row)
