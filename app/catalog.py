"""Manually sourced registry reference cards, kept separate from pharmacy stock.

No automatic matching, scraping or registry-status assertions. Active substance
names are transcribed from a source and cannot drive interaction/analogue checks.
"""
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

from .catalog_sources import (
    CatalogValidationError, check_field, parse_substance_names,
    registry_identity_key, safe_official_url,
)
from .database import get_db
from .manage import _check_csrf, _csrf
from .models import (
    InventoryEvent, Medicine, MedicineRegistrationLink, RegisteredComposition,
    RegisteredProduct, RegisteredSubstance,
)
from .search import matches, normalize

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _medicine(db, medicine_id):
    medicine = db.get(Medicine, medicine_id)
    if medicine is None:
        raise HTTPException(404, "Товарная карточка не найдена.")
    return medicine


def _record_statement():
    return select(RegisteredProduct).options(
        selectinload(RegisteredProduct.substances).selectinload(RegisteredComposition.substance)
    )


def _registry_page(request, db, medicine, error=None):
    links = db.scalars(
        select(MedicineRegistrationLink)
        .options(
            selectinload(MedicineRegistrationLink.product)
            .selectinload(RegisteredProduct.substances)
            .selectinload(RegisteredComposition.substance)
        )
        .where(MedicineRegistrationLink.medicine_id == medicine.id)
        .order_by(MedicineRegistrationLink.id.desc())
    ).all()
    available = db.scalars(
        _record_statement().order_by(RegisteredProduct.id.desc()).limit(200)
    ).all()
    return templates.TemplateResponse(request, "registry_item.html", {
        "page": "catalog", "csrf_token": _csrf(request),
        "medicine": medicine, "links": links, "available": available,
        "error": error,
    })


@router.get("/catalog", response_class=HTMLResponse)
def catalog(request: Request, q: str = Query("", max_length=100),
            page_number: int = Query(1, ge=1), db: Session = Depends(get_db)):
    records = db.scalars(
        _record_statement().order_by(RegisteredProduct.trade_name, RegisteredProduct.id)
    ).all()
    filtered = [
        record for record in records
        if not q.strip() or matches(
            " ".join((record.registration_number, record.trade_name,
                      record.dosage_form, record.strength_text, record.manufacturer,
                      " ".join(x.substance.name for x in record.substances))),
            q,
        )
    ]
    limit = 30
    pages = max(1, (len(filtered) + limit - 1) // limit)
    page_number = min(page_number, pages)
    return templates.TemplateResponse(request, "catalog.html", {
        "page": "catalog", "items": filtered[(page_number - 1) * limit: page_number * limit],
        "count": len(filtered), "q": q, "page_number": page_number, "pages": pages,
        "total": len(records),
    })


@router.get("/catalog/{registered_id}", response_class=HTMLResponse)
def registered_detail(registered_id: int, request: Request, db: Session = Depends(get_db)):
    entry = db.scalar(_record_statement().where(RegisteredProduct.id == registered_id))
    if entry is None:
        raise HTTPException(404, "Регистрационная карточка не найдена.")
    links = db.scalars(
        select(MedicineRegistrationLink)
        .options(selectinload(MedicineRegistrationLink.medicine))
        .where(MedicineRegistrationLink.registered_product_id == registered_id)
        .order_by(MedicineRegistrationLink.id.desc())
    ).all()
    return templates.TemplateResponse(request, "catalog_detail.html", {
        "page": "catalog", "entry": entry, "links": links
    })


@router.get("/medicines/{medicine_id}/registry", response_class=HTMLResponse)
def registry_for_medicine(medicine_id: int, request: Request, db: Session = Depends(get_db)):
    return _registry_page(request, db, _medicine(db, medicine_id))


@router.post("/medicines/{medicine_id}/registry", response_class=HTMLResponse)
def add_registry_entry(
    medicine_id: int, request: Request, csrf_token: str = Form(...),
    registration_number: str = Form(...), trade_name: str = Form(...),
    dosage_form: str = Form(...), strength_text: str = Form(...),
    manufacturer: str = Form(""), substances: str = Form(...),
    source_url: str = Form(...), comparison_note: str = Form(""),
    source_checked: str = Form(""), product_checked: str = Form(""),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    medicine = _medicine(db, medicine_id)
    try:
        reg = check_field(registration_number, label="Регистрационный номер", limit=120)
        trade = check_field(trade_name, label="Торговое название", limit=250)
        form = check_field(dosage_form, label="Лекарственная форма", limit=200)
        strength = check_field(strength_text, label="Дозировка", limit=200)
        maker = check_field(manufacturer, label="Производитель", limit=300, required=False)
        note = check_field(comparison_note, label="Примечание", limit=1000, required=False)
        ingredient_names = parse_substance_names(substances)
        source, title = safe_official_url(source_url, specific=True)
        if source_checked != "yes" or product_checked != "yes":
            raise CatalogValidationError(
                "Подтвердите проверку источника и сопоставление с конкретным товаром."
            )
        key = registry_identity_key(reg, trade, form, strength, maker)
        current = db.scalar(select(RegisteredProduct).where(RegisteredProduct.identity_key == key))
        if current is not None:
            raise CatalogValidationError(
                "Такая регистрационная карточка уже внесена. Используйте связывание с существующей записью."
            )

        entry = RegisteredProduct(
            identity_key=key, registration_number=reg, trade_name=trade,
            dosage_form=form, strength_text=strength, manufacturer=maker,
            source_title=title, source_url=source, checked_on=date.today()
        )
        db.add(entry)
        db.flush()
        for ingredient_name in ingredient_names:
            normalized = normalize(ingredient_name)
            ingredient = db.scalar(
                select(RegisteredSubstance).where(RegisteredSubstance.name_key == normalized)
            )
            if ingredient is None:
                ingredient = RegisteredSubstance(name=ingredient_name, name_key=normalized)
                db.add(ingredient)
                db.flush()
            db.add(RegisteredComposition(product=entry, substance=ingredient))
        db.add(MedicineRegistrationLink(
            medicine_id=medicine.id, registered_product_id=entry.id,
            comparison_note=note, state="active",
        ))
        db.add(InventoryEvent(
            event_type="registry_manual_link",
            description=f"Manual catalog: medicine #{medicine.id} -> registered record #{entry.id}."
        ))
        db.commit()
    except (CatalogValidationError, IntegrityError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, CatalogValidationError) else "Конфликт данных. Ничего не сохранено."
        return _registry_page(request, db, medicine, error=message)
    return RedirectResponse(f"/medicines/{medicine_id}/registry?saved=1", status_code=303)


@router.post("/medicines/{medicine_id}/registry/attach", response_class=HTMLResponse)
def attach_existing(
    medicine_id: int, request: Request, csrf_token: str = Form(...),
    registered_id: int = Form(...), comparison_note: str = Form(""),
    source_checked: str = Form(""), product_checked: str = Form(""),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    medicine = _medicine(db, medicine_id)
    try:
        note = check_field(comparison_note, label="Примечание", limit=1000, required=False)
        if source_checked != "yes" or product_checked != "yes":
            raise CatalogValidationError("Сверьте регистрационную карточку и товар перед связыванием.")
        entry = db.get(RegisteredProduct, registered_id)
        if entry is None:
            raise CatalogValidationError("Регистрационная карточка не найдена.")
        existing = db.scalar(select(MedicineRegistrationLink).where(
            MedicineRegistrationLink.medicine_id == medicine.id,
            MedicineRegistrationLink.registered_product_id == entry.id
        ))
        if existing and existing.state == "active":
            raise CatalogValidationError("Эта регистрационная карточка уже связана с товаром.")
        if existing and existing.state == "revoked":
            existing.state = "active"
            existing.revoked_at = None
            existing.revoke_reason = ""
            existing.linked_at = datetime.now(timezone.utc)
            existing.comparison_note = note
            db.add(InventoryEvent(
                event_type="registry_manual_link",
                description=f"Manual re-link: medicine #{medicine.id}, record #{entry.id}."
            ))
        else:
            db.add(MedicineRegistrationLink(
                medicine_id=medicine.id, registered_product_id=entry.id,
                comparison_note=note, state="active"
            ))
            db.add(InventoryEvent(
                event_type="registry_manual_link",
                description=f"Manual link: medicine #{medicine.id}, record #{entry.id}."
            ))
        db.commit()
    except (CatalogValidationError, IntegrityError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, CatalogValidationError) else "Конфликт ссылки; запись не изменена."
        return _registry_page(request, db, medicine, error=message)
    return RedirectResponse(f"/medicines/{medicine_id}/registry?saved=1", status_code=303)


@router.post("/medicines/{medicine_id}/registry/{link_id}/revoke", response_class=HTMLResponse)
def revoke(
    medicine_id: int, link_id: int, request: Request, csrf_token: str = Form(...),
    reason: str = Form(...), typed_confirmation: str = Form(""),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    medicine = _medicine(db, medicine_id)
    try:
        explanation = check_field(reason, label="Причина отмены", limit=1000)
        if typed_confirmation != "ОТМЕНИТЬ":
            raise CatalogValidationError("Введите «ОТМЕНИТЬ», чтобы отозвать связь.")
        link = db.get(MedicineRegistrationLink, link_id)
        if link is None or link.medicine_id != medicine.id:
            raise CatalogValidationError("Связь с регистрационной карточкой не найдена.")
        if link.state != "active":
            raise CatalogValidationError("Связь уже была отменена.")
        link.state = "revoked"
        link.revoked_at = datetime.now(timezone.utc)
        link.revoke_reason = explanation
        db.add(InventoryEvent(
            event_type="registry_link_revoked",
            description=f"Manual registry link revoked: medicine #{medicine.id}, link #{link_id}."
        ))
        db.commit()
    except CatalogValidationError as exc:
        db.rollback()
        return _registry_page(request, db, medicine, error=str(exc))
    return RedirectResponse(f"/medicines/{medicine_id}/registry?revoked=1", status_code=303)
