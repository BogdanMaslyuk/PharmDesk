"""Manually sourced registry reference cards, kept separate from pharmacy stock.

No automatic matching, scraping or registry-status assertions. Active substance
names are transcribed from a source and cannot drive interaction/analogue checks.
"""
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
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
    RegisteredProduct, RegisteredSubstance, RegistryLinkEvent, GrlsImportRow,
    GrlsImportBatch, MatchingCandidate, MatchingItem,
    MatchingRun, MatchingDecision,
)
from .search import matches, normalize
from .trade_candidates import trade_name_key

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _require_explanation(medicine_name, trade_name, strength, note):
    """Explicit explanation for product names/doses not visibly matching.

    An explanation does not certify identity; it creates an audit trail
    requiring the reviewer to justify such a manual link.
    """
    # A common family prefix is not a drug identity:
    # Акридерм and Акридерм ГК must not silently compare as the same brand.
    same_brand = trade_name_key(medicine_name) == normalize(trade_name)
    dose_visible = normalize(strength) in normalize(medicine_name)
    if (not same_brand or not dose_visible) and len(note) < 20:
        raise CatalogValidationError(
            "Название или дозировка не совпадают с товарной строкой. "
            "Укажите в примечании подробное обоснование сопоставления (не менее 20 символов)."
        )


def _medicine(db, medicine_id):
    medicine = db.get(Medicine, medicine_id)
    if medicine is None:
        raise HTTPException(404, "Товарная карточка не найдена.")
    return medicine


def _check_grls_candidate(db, candidate_id, medicine_name, reg, trade, form, strength, comment):
    """Compare a human-confirmed card with its selected source-snapshot row.

    This is local textual validation ONLY, not a live official verification.
    Disagreement in registration number or name blocks a source-derived card.
    Disagreement in raw release text needs a documented explanation.
    """
    if candidate_id is None:
        return comment
    candidate = db.get(GrlsImportRow, candidate_id)
    if candidate is None:
        raise CatalogValidationError("Выбранная исходная строка ГРЛС не найдена.")
    if normalize(reg) != candidate.registration_key:
        raise CatalogValidationError(
            "Номер регистрационного удостоверения не совпадает с выбранной строкой выгрузки."
        )
    if normalize(trade) != candidate.trade_key:
        raise CatalogValidationError(
            "Торговое название не совпадает с выбранной строкой выгрузки."
        )
    if trade_name_key(medicine_name) != candidate.trade_key:
        raise CatalogValidationError(
            "Полное торговое название аптечного товара не совпадает с выбранной строкой ГРЛС. "
            "Препараты одной торговой линейки (например, Акридерм и Акридерм ГК) "
            "нельзя связывать как идентичные. Уточните товарную запись по первоисточнику."
        )
    source_forms = normalize(candidate.release_forms_raw)
    form_seen = normalize(form) in source_forms
    strength_seen = normalize(strength) in source_forms
    if (not form_seen or not strength_seen) and len(comment) < 40:
        raise CatalogValidationError(
            "Форма выпуска или дозировка не найдена точно в исходной строке. "
            "После проверки актуального реестра подробно объясните расхождение "
            "в примечании (не менее 40 символов)."
        )
    origin_note = (
        f"\nИсходная выгрузка ГРЛС: пакет #{candidate.batch_id}, "
        f"файл {candidate.source_workbook}, строка {candidate.source_row}, "
        f"SHA-256 {candidate.batch.sha256}. "
        f"Сравнение текста формы: {'совпало' if form_seen else 'не совпало'}; "
        f"дозировки: {'совпало' if strength_seen else 'не совпало'}. "
        "Это не проверка регистрации через сеть."
    )
    if len(comment) + len(origin_note) > 2000:
        raise CatalogValidationError("Примечание с информацией об источнике слишком длинное.")
    return comment + origin_note


def _ensure_no_conflicting_active_link(db, medicine_id, intended_product_id=None):
    active = db.scalar(
        select(MedicineRegistrationLink).where(
            MedicineRegistrationLink.medicine_id == medicine_id,
            MedicineRegistrationLink.state == "active",
        )
    )
    if active is not None and active.registered_product_id != intended_product_id:
        raise CatalogValidationError(
            "С этим товаром уже связана другая действующая регистрационная карточка. "
            "Сначала проверьте и отмените прежнюю связь, затем подтвердите новую."
        )


def _record_statement():
    return select(RegisteredProduct).options(
        selectinload(RegisteredProduct.substances).selectinload(RegisteredComposition.substance)
    )


def _registry_page(request, db, medicine, error=None, candidate=None):
    links = db.scalars(
        select(MedicineRegistrationLink)
        .options(
            selectinload(MedicineRegistrationLink.product)
            .selectinload(RegisteredProduct.substances)
            .selectinload(RegisteredComposition.substance),
            selectinload(MedicineRegistrationLink.history)
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
        "error": error, "candidate": candidate,
    })


@router.get("/catalog", response_class=HTMLResponse)
def catalog(
    request: Request, q: str = Query("", max_length=100),
    view: str = Query("", max_length=20),
    batch_id: int | None = Query(None, ge=1),
    status: str = Query("all", max_length=150),
    page_number: int = Query(1, ge=1), db: Session = Depends(get_db),
):
    """Unified reference landing: source snapshots + human-reviewed records.

    Imported GRLS records are visibly labeled as unverified *source rows*,
    NOT medicine identities or approved links to local pharmacy stock.
    No data is copied between tables, modified, or interpreted clinically.
    """
    manual_total = db.scalar(select(func.count(RegisteredProduct.id))) or 0
    batch = (
        db.get(GrlsImportBatch, batch_id) if batch_id is not None
        else db.scalar(
            select(GrlsImportBatch)
            .order_by(GrlsImportBatch.snapshot_on.desc(), GrlsImportBatch.id.desc())
            .limit(1)
        )
    )
    selected = view if view in {"manual", "grls"} else (
        "grls" if batch else "manual"
    )
    if selected == "grls" and not batch:
        selected = "manual"
    limit = 30
    manual_items = []
    imported_items = []
    statuses = []
    count = 0
    if selected == "manual":
        # The curated subset is intentionally smaller and fits in memory.
        records = db.scalars(
            _record_statement().order_by(RegisteredProduct.trade_name, RegisteredProduct.id)
        ).all()
        filtered = [
            record for record in records
            if not q.strip() or matches(
                " ".join((
                    record.registration_number, record.trade_name,
                    record.dosage_form, record.strength_text, record.manufacturer,
                    " ".join(part.substance.name for part in record.substances),
                )), q
            )
        ]
        count = len(filtered)
        pages = max(1, (count + limit - 1) // limit)
        page_number = min(page_number, pages)
        manual_items = filtered[(page_number - 1) * limit: page_number * limit]
    else:
        status_rows = db.execute(
            select(GrlsImportRow.status_from_filename, func.count(GrlsImportRow.id))
            .where(GrlsImportRow.batch_id == batch.id)
            .group_by(GrlsImportRow.status_from_filename)
        ).all()
        statuses = [{"name": name, "count": total} for name, total in status_rows]
        if status not in {"all", *(item["name"] for item in statuses)}:
            status = "all"
        statement = select(GrlsImportRow).where(GrlsImportRow.batch_id == batch.id)
        if status != "all":
            statement = statement.where(GrlsImportRow.status_from_filename == status)
        search = q.strip()
        if search:
            # Normalized keys make registration/title searches insensitive to
            # case, spaces and punctuation. INN remains an unaltered source
            # field; check common Cyrillic capitalization variants on the DB.
            key = normalize(search)
            inn_terms = tuple(dict.fromkeys(
                (search, search.casefold(), search.capitalize(), search.upper())
            ))
            conditions = [
                GrlsImportRow.registration_key.contains(key),
                GrlsImportRow.trade_key.contains(key),
                *(GrlsImportRow.inn_raw.contains(term) for term in inn_terms),
            ]
            statement = statement.where(or_(*conditions))
        count = db.scalar(
            select(func.count()).select_from(statement.subquery())
        ) or 0
        pages = max(1, (count + limit - 1) // limit)
        page_number = min(page_number, pages)
        imported_items = db.scalars(
            statement.order_by(
                GrlsImportRow.trade_name, GrlsImportRow.registration_number,
                GrlsImportRow.id,
            ).offset((page_number - 1) * limit).limit(limit)
        ).all()
    return templates.TemplateResponse(request, "catalog.html", {
        "page": "catalog",
        "manual_items": manual_items, "imported_items": imported_items,
        "manual_total": manual_total, "batch": batch,
        "source_total": batch.row_count if batch else 0,
        "view": selected, "status": status, "statuses": statuses,
        "q": q, "count": count, "page_number": page_number, "pages": pages,
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
def registry_for_medicine(
    medicine_id: int, request: Request,
    candidate_id: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
):
    # Source export rows may prefill name/number only. Never copy INN,
    # form, dose, certificate holder or registration status into curated fields.
    candidate = db.get(GrlsImportRow, candidate_id) if candidate_id else None
    return _registry_page(request, db, _medicine(db, medicine_id), candidate=candidate)


@router.post("/medicines/{medicine_id}/registry", response_class=HTMLResponse)
def add_registry_entry(
    medicine_id: int, request: Request, csrf_token: str = Form(...),
    registration_number: str = Form(...), trade_name: str = Form(...),
    dosage_form: str = Form(...), strength_text: str = Form(...),
    manufacturer: str = Form(""), substances: str = Form(...),
    source_url: str = Form(...), comparison_note: str = Form(""),
    source_checked: str = Form(""), product_checked: str = Form(""),
    candidate_id: int | None = Form(None),
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
        source_note = _check_grls_candidate(db, candidate_id, medicine.name, reg, trade, form, strength, note)
        _require_explanation(medicine.name, trade, strength, note)
        note = source_note
        key = registry_identity_key(reg, trade, form, strength, maker)
        current = db.scalar(select(RegisteredProduct).where(RegisteredProduct.identity_key == key))
        if current is not None:
            raise CatalogValidationError(
                "Такая регистрационная карточка уже внесена. Используйте связывание с существующей записью."
            )
        _ensure_no_conflicting_active_link(db, medicine.id)

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
        link = MedicineRegistrationLink(
            medicine_id=medicine.id, registered_product_id=entry.id,
            comparison_note=note, state="active",
        )
        db.add(link)
        db.flush()
        db.add(RegistryLinkEvent(
            link_id=link.id, action="linked", explanation=note
        ))
        db.add(InventoryEvent(
            event_type="registry_manual_link",
            description=f"Manual catalog: medicine #{medicine.id} -> registered record #{entry.id}."
        ))
        if candidate_id is not None:
            # Only after the existing explicit official-source verification
            # succeeds: record corresponding bulk-review decisions.
            # No generated candidate is ever promoted automatically.
            matches = db.scalars(
                select(MatchingCandidate)
                .join(MatchingItem, MatchingItem.id == MatchingCandidate.item_id)
                .join(MatchingRun, MatchingRun.id == MatchingItem.run_id)
                .where(
                    MatchingItem.medicine_id == medicine.id,
                    MatchingCandidate.grls_row_id == candidate_id,
                    MatchingRun.state == "done",
                )
            ).all()
            for matched in matches:
                db.add(MatchingDecision(
                    item_id=matched.item_id, candidate_id=matched.id,
                    action="confirmed",
                    comment="Человек отдельно сверил официальный источник, форму и дозировку; "
                            f"регистрационная карточка №{entry.id}.",
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
        _require_explanation(medicine.name, entry.trade_name, entry.strength_text, note)
        _ensure_no_conflicting_active_link(db, medicine.id, entry.id)
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
            db.add(RegistryLinkEvent(
                link_id=existing.id, action="relinked", explanation=note
            ))
            db.add(InventoryEvent(
                event_type="registry_manual_link",
                description=f"Manual re-link: medicine #{medicine.id}, record #{entry.id}."
            ))
        else:
            link = MedicineRegistrationLink(
                medicine_id=medicine.id, registered_product_id=entry.id,
                comparison_note=note, state="active"
            )
            db.add(link)
            db.flush()
            db.add(RegistryLinkEvent(
                link_id=link.id, action="linked", explanation=note
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
        db.add(RegistryLinkEvent(
            link_id=link.id, action="revoked", explanation=explanation
        ))
        db.add(InventoryEvent(
            event_type="registry_link_revoked",
            description=f"Manual registry link revoked: medicine #{medicine.id}, link #{link_id}."
        ))
        db.commit()
    except CatalogValidationError as exc:
        db.rollback()
        return _registry_page(request, db, medicine, error=str(exc))
    return RedirectResponse(f"/medicines/{medicine_id}/registry?revoked=1", status_code=303)
