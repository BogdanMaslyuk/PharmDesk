"""Source-backed manual identity notes. Never automatically assign active ingredients."""
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from .database import get_db
from .manage import _check_csrf, _csrf
from .models import Medicine, SubstanceEvidence, InventoryEvent

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _get_medicine(db, medicine_id):
    medicine = db.get(Medicine, medicine_id)
    if medicine is None:
        raise HTTPException(404, "Товарная карточка не найдена.")
    return medicine


def _page(request, db, medicine, error=None):
    evidence = db.scalars(
        select(SubstanceEvidence)
        .where(SubstanceEvidence.medicine_id == medicine.id)
        .order_by(SubstanceEvidence.id.desc())
    ).all()
    return templates.TemplateResponse(request, "identity.html", {
        "page": "medicines", "csrf_token": _csrf(request),
        "medicine": medicine, "evidence": evidence, "error": error,
    })


@router.get("/medicines/{medicine_id}/identity", response_class=HTMLResponse)
def identity(medicine_id: int, request: Request, db: Session = Depends(get_db)):
    return _page(request, db, _get_medicine(db, medicine_id))


@router.post("/medicines/{medicine_id}/identity", response_class=HTMLResponse)
def save_identity(medicine_id: int, request: Request, csrf_token: str = Form(...),
                  substances: str = Form(...), source_url: str = Form(...),
                  note: str = Form(""), visually_checked: str = Form(""),
                  db: Session = Depends(get_db)):
    _check_csrf(request, csrf_token)
    medicine = _get_medicine(db, medicine_id)
    try:
        substances = substances.strip()
        note = note.strip()
        url = source_url.strip()
        if not substances or len(substances) > 500:
            raise ValueError("Укажите действующее вещество (или вещества) по инструкции, до 500 символов.")
        if len(note) > 1000:
            raise ValueError("Комментарий слишком длинный.")
        if len(url) > 800:
            raise ValueError("Ссылка на источник слишком длинная.")
        info = urlsplit(url)
        # Deliberately restrict initial evidence collection to the official
        # Russian medicines register; it is still checked only by a human.
        if info.scheme != "https" or info.hostname != "grls.minzdrav.gov.ru":
            raise ValueError("Для этой версии нужен адрес инструкции на https://grls.minzdrav.gov.ru/")
        if visually_checked != "yes":
            raise ValueError("Подтвердите, что лично сверили вещества с инструкцией.")
        db.add(SubstanceEvidence(
            medicine_id=medicine_id, substances_text=substances,
            source_title="ГРЛС Минздрава России (указано вручную)",
            source_url=url, notes=note, review_status="entered_by_user",
            checked_at=datetime.now(timezone.utc),
        ))
        db.add(InventoryEvent(
            event_type="substance_evidence",
            description=f"Manual identity note for medicine #{medicine_id}; source URL recorded."
        ))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _page(request, db, medicine, error=str(exc))
    return RedirectResponse(f"/medicines/{medicine_id}/identity?saved=1", status_code=303)
