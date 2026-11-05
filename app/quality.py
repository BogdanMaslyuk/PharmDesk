"""Read-only, local-only catalogue quality report."""
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

from .catalog_quality import REASONS, audit_catalog
from .database import get_db
from .models import Medicine, ReviewRecord, StockItem

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


@router.get("/manage/quality", response_class=HTMLResponse)
def quality_report(
    request: Request,
    reason: str = Query("all"),
    drawer: int | None = Query(None, ge=1, le=999999),
    q: str = Query("", max_length=100),
    page_number: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    items = db.scalars(
        select(StockItem).options(
            selectinload(StockItem.medicine),
            selectinload(StockItem.drawer),
        )
    ).all()
    records = db.scalars(select(ReviewRecord)).all()
    report = audit_catalog(items, records)
    if reason not in (*REASONS, "all"):
        reason = "all"
    from .search import matches
    shown = [
        issue for issue in report["issues"]
        if (reason == "all" or issue.reason == reason)
        and (drawer is None or issue.drawer_number == drawer)
        and (not q.strip() or matches(
            f"{issue.name} {issue.maker} {issue.article_code} {issue.drawer_number}", q
        ))
    ]
    page_size = 50
    pages = max(1, (len(shown) + page_size - 1) // page_size)
    page_number = min(page_number, pages)
    visible = shown[(page_number - 1) * page_size: page_number * page_size]
    return templates.TemplateResponse(request, "quality.html", {
        "page": "manage", "report": report, "issues": visible,
        "reasons": REASONS, "selected_reason": reason,
        "drawer": drawer, "q": q, "page_number": page_number,
        "pages": pages, "filtered_count": len(shown),
        "drawers": sorted({item.drawer.number for item in items}),
    })
