"""Local inventory management routes. Not intended for public deployment."""
import hmac
import secrets

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request
from pathlib import Path

from .batch import ImportErrorUser, batch_import, MAX_BYTES
from .database import get_db
from .models import Drawer, InventoryEvent, Medicine, StockItem

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _csrf(request: Request):
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def _check_csrf(request: Request, supplied: str):
    expected = request.session.get("csrf", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Форма устарела. Обновите страницу и попробуйте снова.")


def _render(request: Request, filename: str, **data):
    return templates.TemplateResponse(request, filename, {"page": "manage", "csrf_token": _csrf(request), **data})


def _text(value, maximum, label, required=True):
    clean = value.strip()
    if required and not clean:
        raise ValueError(f"Заполните поле «{label}».")
    if len(clean) > maximum:
        raise ValueError(f"Слишком длинное поле «{label}» (максимум {maximum} символов).")
    return clean


def _drawer_number(value):
    if value < 1 or value > 999999:
        raise ValueError("Номер ящика должен быть от 1 до 999999.")
    return value


def _event(db: Session, action: str, text: str):
    db.add(InventoryEvent(event_type=action, description=text[:500]))


def _medicine(db: Session, name: str, manufacturer: str):
    fingerprint = f"{name.casefold()}::{manufacturer.casefold()}"
    medicine = db.scalar(select(Medicine).where(Medicine.fingerprint == fingerprint))
    if medicine is None:
        medicine = Medicine(name=name, manufacturer=manufacturer, fingerprint=fingerprint)
        db.add(medicine)
        db.flush()
    return medicine


def _item(db: Session, item_id: int):
    item = db.scalar(select(StockItem).options(selectinload(StockItem.medicine), selectinload(StockItem.drawer)).where(StockItem.id == item_id))
    if item is None:
        raise HTTPException(404, "Товарная позиция не найдена.")
    return item


def _drawer(db: Session, number: int):
    drawer = db.scalar(select(Drawer).where(Drawer.number == number))
    if drawer is None:
        raise HTTPException(404, "Ящик не найден.")
    return drawer


def _item_form(request, db, item, drawer, error=None):
    drawers = db.scalars(select(Drawer).order_by(Drawer.number)).all()
    return _render(request, "manage_item.html", item=item, drawer=drawer, drawers=drawers, error=error)


def _drawer_form(request, drawer=None, error=None):
    return _render(request, "manage_drawer.html", drawer=drawer, error=error)


@router.get("/manage", response_class=HTMLResponse)
def manage(request: Request, db: Session = Depends(get_db)):
    drawers = db.scalars(select(Drawer).order_by(Drawer.number)).all()
    events = db.scalars(select(InventoryEvent).order_by(InventoryEvent.id.desc()).limit(12)).all()
    return _render(request, "manage.html", drawers=drawers, events=events)


@router.get("/manage/drawers/new", response_class=HTMLResponse)
def new_drawer(request: Request):
    return _drawer_form(request)


@router.post("/manage/drawers/new", response_class=HTMLResponse)
def create_drawer(request: Request, csrf_token: str = Form(...), number: int = Form(...),
                  name: str = Form(...), location: str = Form(""), description: str = Form(""),
                  db: Session = Depends(get_db)):
    _check_csrf(request, csrf_token)
    try:
        _drawer_number(number)
        name = _text(name, 200, "Название")
        location = _text(location, 200, "Расположение", required=False)
        description = _text(description, 2000, "Описание", required=False)
        if db.scalar(select(Drawer).where(Drawer.number == number)) is not None:
            raise ValueError(f"Ящик №{number} уже существует.")
        db.add(Drawer(number=number, name=name, location=location or None, description=description or None))
        _event(db, "create_drawer", f"Создан ящик №{number}: {name}")
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _drawer_form(request, error=str(exc))
    except IntegrityError:
        db.rollback()
        return _drawer_form(request, error="Ящик с этим номером уже существует.")
    return RedirectResponse(f"/drawers/{number}", status_code=303)


@router.get("/manage/drawers/{number}/edit", response_class=HTMLResponse)
def edit_drawer(request: Request, number: int, db: Session = Depends(get_db)):
    return _drawer_form(request, drawer=_drawer(db, number))


@router.post("/manage/drawers/{number}/edit", response_class=HTMLResponse)
def update_drawer(request: Request, number: int, csrf_token: str = Form(...),
                  name: str = Form(...), location: str = Form(""), description: str = Form(""),
                  db: Session = Depends(get_db)):
    _check_csrf(request, csrf_token)
    drawer = _drawer(db, number)
    try:
        drawer.name = _text(name, 200, "Название")
        drawer.location = _text(location, 200, "Расположение", required=False) or None
        drawer.description = _text(description, 2000, "Описание", required=False) or None
        _event(db, "update_drawer", f"Изменён ящик №{number}: {drawer.name}")
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _drawer_form(request, drawer=drawer, error=str(exc))
    return RedirectResponse(f"/drawers/{number}", status_code=303)


@router.get("/manage/drawers/{number}/items/new", response_class=HTMLResponse)
def new_item(request: Request, number: int, db: Session = Depends(get_db)):
    return _item_form(request, db, None, _drawer(db, number))


@router.post("/manage/drawers/{number}/items/new", response_class=HTMLResponse)
def create_item(request: Request, number: int, csrf_token: str = Form(...),
                article_code: str = Form(...), name: str = Form(...),
                manufacturer: str = Form(""), db: Session = Depends(get_db)):
    _check_csrf(request, csrf_token)
    drawer = _drawer(db, number)
    try:
        code = _text(article_code, 80, "Код")
        name = _text(name, 350, "Название товара")
        maker = _text(manufacturer, 300, "Производитель", required=False)
        exists = db.scalar(select(StockItem).where(StockItem.drawer_id == drawer.id, StockItem.article_code == code))
        if exists is not None:
            raise ValueError(f"Код {code} уже есть в ящике №{number}.")
        medicine = _medicine(db, name, maker)
        item = StockItem(article_code=code, drawer=drawer, medicine=medicine, source="вручную")
        db.add(item)
        _event(db, "create_item", f"Добавлен {code} — {name} в ящик №{number}")
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _item_form(request, db, None, drawer, error=str(exc))
    except IntegrityError:
        db.rollback()
        return _item_form(request, db, None, drawer, error="Такая позиция уже существует.")
    return RedirectResponse(f"/drawers/{number}", status_code=303)


@router.get("/manage/items/{item_id}/edit", response_class=HTMLResponse)
def edit_item(request: Request, item_id: int, db: Session = Depends(get_db)):
    item = _item(db, item_id)
    return _item_form(request, db, item, item.drawer)


@router.post("/manage/items/{item_id}/edit", response_class=HTMLResponse)
def update_item(request: Request, item_id: int, csrf_token: str = Form(...),
                article_code: str = Form(...), name: str = Form(...),
                manufacturer: str = Form(""), db: Session = Depends(get_db)):
    _check_csrf(request, csrf_token)
    item = _item(db, item_id)
    drawer = item.drawer
    try:
        code = _text(article_code, 80, "Код")
        name = _text(name, 350, "Название товара")
        maker = _text(manufacturer, 300, "Производитель", required=False)
        duplicate = db.scalar(select(StockItem).where(
            StockItem.drawer_id == drawer.id, StockItem.article_code == code, StockItem.id != item.id
        ))
        if duplicate is not None:
            raise ValueError(f"Код {code} уже есть в ящике №{drawer.number}.")
        item.medicine = _medicine(db, name, maker)
        item.article_code = code
        _event(db, "update_item", f"Изменена позиция {code}: {name} (ящик №{drawer.number})")
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _item_form(request, db, item, drawer, error=str(exc))
    except IntegrityError:
        db.rollback()
        return _item_form(request, db, item, drawer, error="Такая позиция уже существует.")
    return RedirectResponse(f"/drawers/{drawer.number}", status_code=303)


@router.post("/manage/items/{item_id}/move", response_class=HTMLResponse)
def move_item(request: Request, item_id: int, csrf_token: str = Form(...),
              target_drawer: int = Form(...), db: Session = Depends(get_db)):
    _check_csrf(request, csrf_token)
    item = _item(db, item_id)
    source = item.drawer.number
    destination = _drawer(db, target_drawer)
    if item.drawer_id == destination.id:
        return RedirectResponse(f"/manage/items/{item_id}/edit", status_code=303)
    collision = db.scalar(select(StockItem).where(
        StockItem.drawer_id == destination.id, StockItem.article_code == item.article_code
    ))
    if collision is not None:
        return _item_form(request, db, item, item.drawer, error="В целевом ящике уже есть товар с таким кодом. Перенос отменён.")
    item.drawer = destination
    _event(db, "move_item", f"Позиция {item.article_code}: ящик №{source} → №{destination.number}")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _item_form(request, db, item, item.drawer, error="При переносе возник конфликт кодов.")
    return RedirectResponse(f"/drawers/{destination.number}", status_code=303)


@router.post("/manage/items/{item_id}/delete", response_class=HTMLResponse)
def delete_item(request: Request, item_id: int, csrf_token: str = Form(...),
                confirm: str = Form(""), db: Session = Depends(get_db)):
    _check_csrf(request, csrf_token)
    item = _item(db, item_id)
    drawer = item.drawer
    if confirm != "DELETE":
        return _item_form(request, db, item, drawer, error="Для удаления введите DELETE.")
    code = item.article_code
    db.delete(item)
    _event(db, "delete_item", f"Удалена товарная позиция {code} из ящика №{drawer.number}")
    db.commit()
    return RedirectResponse(f"/drawers/{drawer.number}", status_code=303)


@router.get("/manage/import", response_class=HTMLResponse)
def import_page(request: Request):
    return _render(request, "manage_import.html")


@router.post("/manage/import/{action}")
async def import_upload(request: Request, action: str,
                        csrf_token: str = Form(...), file: UploadFile = File(...),
                        default_drawer: str = Form(""), default_name: str = Form("Без названия"),
                        db: Session = Depends(get_db)):
    _check_csrf(request, csrf_token)
    if action not in ("preview", "apply"):
        raise HTTPException(404, "Неизвестная операция импорта.")
    if default_drawer.strip():
        try:
            drawer_num = int(default_drawer)
            _drawer_number(drawer_num)
        except (ValueError, TypeError):
            return JSONResponse({"error": "Некорректный номер ящика по умолчанию."}, status_code=422)
    else:
        drawer_num = None
    try:
        title = _text(default_name, 200, "Название ящика", required=False) or "Без названия"
        raw = await file.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ImportErrorUser("Файл слишком большой. Максимум 4 МБ.")
        result = batch_import(db, raw, file.filename or "", drawer_num, title, preview=action == "preview")
        if action == "apply" and result["created"] + result["updated"]:
            _event(db, "batch_import", f"Импорт {file.filename}: {result['created']} новых, {result['updated']} обновлённых, {result['drawers']} ящиков")
            db.commit()
        return result
    except (ValueError, ImportErrorUser) as exc:
        db.rollback()
        return JSONResponse({"error": str(exc)}, status_code=422)
    finally:
        await file.close()
