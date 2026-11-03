"""Human confirmation of pharmacy OCR records against local source screenshots."""
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from .database import get_db
from .manage import _check_csrf, _csrf, _medicine
from .models import (
    Drawer, InventoryEvent, ReviewDecision, ReviewRecord,
    SourceImage, StockItem
)
from .review_import import (
    IMAGE_DIRECTORY, MAX_ARCHIVE, MAX_WORKBOOK,
    ReviewImportError, image_key, import_images, import_reviews
)
from .search import matches

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _render(request, filename, **data):
    return templates.TemplateResponse(
        request, filename, {"page": "review", "csrf_token": _csrf(request), **data}
    )


def _fetch(db, record_id):
    record = db.get(ReviewRecord, record_id)
    if record is None:
        raise HTTPException(404, "Строка для проверки не найдена.")
    return record


def _source(db, filename):
    return db.scalar(select(SourceImage).where(SourceImage.name_key == image_key(filename)))


def _current_stock(db, record):
    drawer = db.scalar(select(Drawer).where(Drawer.number == record.drawer_number))
    if drawer is None:
        return None
    code = record.confirmed_code if record.state == "approved" and record.confirmed_code else record.article_code
    return db.scalar(select(StockItem).where(
        StockItem.drawer_id == drawer.id, StockItem.article_code == code
    ))


def _detail(request, db, record, error=None):
    source = _source(db, record.source_file)
    current = _current_stock(db, record)
    decisions = db.scalars(
        select(ReviewDecision).where(ReviewDecision.review_id == record.id)
        .order_by(ReviewDecision.id.desc()).limit(12)
    ).all()
    next_record = db.scalar(
        select(ReviewRecord).where(
            ReviewRecord.state == "pending",
            ReviewRecord.priority.is_(True),
            ReviewRecord.id > record.id,
        ).order_by(ReviewRecord.id).limit(1)
    )
    if next_record is None:
        next_record = db.scalar(
            select(ReviewRecord).where(
                ReviewRecord.state == "pending",
                ReviewRecord.priority.is_(True),
                ReviewRecord.id != record.id,
            ).order_by(ReviewRecord.id).limit(1)
        )
    return _render(request, "review_item.html",
                   record=record, source=source, current=current,
                   decisions=decisions, error=error, next_record=next_record)


def _human_string(value, maximum, field, required=True):
    value = value.strip()
    if required and not value:
        raise ValueError(f"Заполните поле «{field}».")
    if len(value) > maximum:
        raise ValueError(f"Слишком длинное значение «{field}».")
    return value


@router.get("/review", response_class=HTMLResponse)
def queue(request: Request, q: str = Query("", max_length=100),
          state: str = Query("pending"), priority: int = Query(1, ge=0, le=1),
          drawer: int | None = Query(None, ge=1), page_number: int = Query(1, ge=1),
          db: Session = Depends(get_db)):
    if state not in ("pending", "approved", "excluded", "all"):
        state = "pending"
    rows = db.scalars(select(ReviewRecord)).all()
    totals = {
        "all": len(rows),
        "pending": sum(x.state == "pending" for x in rows),
        "priority": sum(x.state == "pending" and x.priority for x in rows),
        "approved": sum(x.state == "approved" for x in rows),
        "excluded": sum(x.state == "excluded" for x in rows),
        "photos": db.scalar(select(func.count(SourceImage.id))) or 0,
    }
    filtered = [
        r for r in rows if
        (state == "all" or r.state == state) and
        (not priority or r.priority) and
        (drawer is None or r.drawer_number == drawer) and
        (not q.strip() or matches(f"{r.proposed_name} {r.article_code} {r.drawer_number} {r.proposed_manufacturer}", q))
    ]
    filtered.sort(key=lambda r: (not r.priority, r.drawer_number, r.source_row or 0, r.id))
    per_page = 30
    pages = max(1, (len(filtered) + per_page - 1) // per_page)
    page_number = min(pages, page_number)
    current = filtered[(page_number - 1) * per_page:page_number * per_page]
    photos = {x.name_key for x in db.scalars(select(SourceImage)).all()}
    photo_presence = {r.id: image_key(r.source_file) in photos for r in current}
    drawers = sorted({r.drawer_number for r in rows})
    return _render(request, "review.html", rows=current, counts=totals,
                   photo_presence=photo_presence, drawers=drawers, q=q,
                   state=state, priority=priority, drawer=drawer,
                   page_number=page_number, pages=pages, filtered_count=len(filtered),
                   import_result=None, import_error=None)


@router.post("/review/upload/{kind}", response_class=HTMLResponse)
async def upload(request: Request, kind: str, csrf_token: str = Form(...),
                 file: UploadFile = File(...), db: Session = Depends(get_db)):
    _check_csrf(request, csrf_token)
    if kind not in ("workbook", "images"):
        raise HTTPException(404, "Неизвестный тип файла.")
    limit = MAX_WORKBOOK if kind == "workbook" else MAX_ARCHIVE
    error = None
    result = None
    try:
        if kind == "workbook" and Path(file.filename or "").suffix.casefold() != ".xlsx":
            raise ReviewImportError("Выберите полную исправленную книгу Excel (.xlsx).")
        if kind == "images" and Path(file.filename or "").suffix.casefold() != ".zip":
            raise ReviewImportError("Выберите исходный архив фотографий (.zip).")
        raw = await file.read(limit + 1)
        if len(raw) > limit:
            raise ReviewImportError("Файл превышает допустимый размер.")
        result = import_reviews(db, raw) if kind == "workbook" else import_images(db, raw)
    except ReviewImportError as exc:
        db.rollback()
        error = str(exc)
    finally:
        await file.close()
    # Avoid automatic stock changes. Redirect upload status to the queue with a message.
    response = queue(request, q="", state="pending", priority=1, drawer=None, page_number=1, db=db)
    response.context["import_result"] = result
    response.context["import_error"] = error
    if error:
        response.status_code = 422
    return response


@router.get("/review/image/{image_id}")
def show_image(image_id: int, db: Session = Depends(get_db)):
    record = db.get(SourceImage, image_id)
    if record is None:
        raise HTTPException(404, "Изображение не найдено.")
    path = IMAGE_DIRECTORY / record.stored_filename
    if not path.is_file():
        raise HTTPException(404, "Локальный файл изображения отсутствует.")
    mime = "image/png" if path.suffix.casefold() == ".png" else "image/jpeg"
    return FileResponse(
        path, media_type=mime,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"}
    )


@router.get("/review/{record_id}", response_class=HTMLResponse)
def detail(record_id: int, request: Request, db: Session = Depends(get_db)):
    return _detail(request, db, _fetch(db, record_id))


@router.post("/review/{record_id}/approve", response_class=HTMLResponse)
def approve(record_id: int, request: Request, csrf_token: str = Form(...),
            code: str = Form(...), name: str = Form(...),
            manufacturer: str = Form(""), comment: str = Form(""),
            visually_checked: str = Form(""), db: Session = Depends(get_db)):
    _check_csrf(request, csrf_token)
    record = _fetch(db, record_id)
    try:
        new_code = _human_string(code, 80, "Код")
        new_name = _human_string(name, 350, "Название")
        new_maker = _human_string(manufacturer, 300, "Производитель", required=False)
        note = _human_string(comment, 1000, "Комментарий", required=False)
        if visually_checked != "yes":
            raise ValueError("Подтвердите, что сравнили строку с оригиналом.")
        image = _source(db, record.source_file)
        if image is None or not (IMAGE_DIRECTORY / image.stored_filename).is_file():
            raise ValueError("Сначала загрузите исходный архив фотографий.")
        drawer = db.scalar(select(Drawer).where(Drawer.number == record.drawer_number))
        if drawer is None:
            drawer = Drawer(number=record.drawer_number, name=record.drawer_name)
            db.add(drawer)
            db.flush()
        previous_code = record.confirmed_code if record.state == "approved" and record.confirmed_code else record.article_code
        old = db.scalar(select(StockItem).where(
            StockItem.drawer_id == drawer.id, StockItem.article_code == previous_code
        ))
        target = db.scalar(select(StockItem).where(
            StockItem.drawer_id == drawer.id, StockItem.article_code == new_code
        ))
        if target and (old is None or target.id != old.id):
            raise ValueError(f"В ящике уже есть другая запись с кодом {new_code}; сохранение отменено.")
        medicine = _medicine(db, new_name, new_maker)
        if old:
            old.article_code = new_code
            old.medicine = medicine
            old.source = "сверено с фото"
        else:
            db.add(StockItem(
                drawer=drawer, article_code=new_code, medicine=medicine, source="сверено с фото"
            ))
        db.add(ReviewDecision(
            review_id=record.id, action="approved",
            previous_code=previous_code,
            previous_name=record.confirmed_name or record.proposed_name,
            selected_code=new_code, selected_name=new_name, comment=note,
        ))
        record.confirmed_code = new_code
        record.confirmed_name = new_name
        record.confirmed_manufacturer = new_maker
        record.state = "approved"
        record.checked_at = datetime.now(timezone.utc)
        record.updated_at = datetime.now(timezone.utc)
        db.add(InventoryEvent(
            event_type="manual_review",
            description=f"Confirmed source screenshot: drawer {record.drawer_number}, code {new_code}."
        ))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _detail(request, db, record, error=str(exc))
    return RedirectResponse(f"/review/{record_id}?saved=1", status_code=303)


@router.post("/review/{record_id}/exclude", response_class=HTMLResponse)
def exclude(record_id: int, request: Request, csrf_token: str = Form(...),
            reason: str = Form(...), typed_confirmation: str = Form(""),
            db: Session = Depends(get_db)):
    _check_csrf(request, csrf_token)
    record = _fetch(db, record_id)
    try:
        explanation = _human_string(reason, 1000, "Причина исключения")
        if typed_confirmation != "ИСКЛЮЧИТЬ":
            raise ValueError('Для удаления товара введите «ИСКЛЮЧИТЬ».')
        old = _current_stock(db, record)
        code = old.article_code if old else (record.confirmed_code or record.article_code)
        if old:
            db.delete(old)
        db.add(ReviewDecision(
            review_id=record.id, action="excluded",
            previous_code=code, previous_name=record.confirmed_name or record.proposed_name,
            selected_code=None, selected_name=None, comment=explanation,
        ))
        record.state = "excluded"
        record.confirmed_code = None
        record.confirmed_name = None
        record.confirmed_manufacturer = None
        record.checked_at = datetime.now(timezone.utc)
        record.updated_at = datetime.now(timezone.utc)
        db.add(InventoryEvent(
            event_type="excluded_ocr",
            description=f"Removed suspect stock: drawer {record.drawer_number}, code {code}."
        ))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _detail(request, db, record, error=str(exc))
    return RedirectResponse(f"/review/{record_id}?saved=1", status_code=303)
