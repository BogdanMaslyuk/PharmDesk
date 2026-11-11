"""First local MVP. Medical interaction checks are deliberately NOT simulated."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import String, case, cast, func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request
from starlette.middleware.sessions import SessionMiddleware

from .database import get_db, init_db, SessionLocal
from .models import ClinicalRecord, Drawer, Ingredient, Medicine, StockItem, ReviewRecord, MedicineRegistrationLink, RegisteredProduct, RegisteredComposition, GrlsImportBatch, GrlsImportRow, MatchingRun, MatchingItem
from .search import search_stock, drawer_matches, normalize
from .trade_candidates import source_candidates
from .version import APP_VERSION

HERE = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Local single-process launcher: unfinished background work does not
    # survive server shutdown. Never silently leave a permanent "running"
    # state that prevents all future analyses.
    with SessionLocal() as db:
        interrupted = db.scalars(select(MatchingRun).where(
            MatchingRun.state.in_(("queued", "running"))
        )).all()
        for run in interrupted:
            run.state = "interrupted"
            run.finished_at = datetime.now(timezone.utc)
            run.error_text = (
                "Сервер был остановлен до завершения анализа. "
                "Предыдущие промежуточные результаты сохранены; запустите новый анализ."
            )
        if interrupted:
            db.commit()
    yield


app = FastAPI(
    title='PharmDesk — рабочее место фармацевта',
    description='Поиск по ящикам и локальному ассортименту. Не предназначено для медицинских решений.',
    version=APP_VERSION,
    lifespan=lifespan,
)
app.add_middleware(SessionMiddleware, secret_key=os.getenv('PHARMDESK_SESSION_SECRET') or secrets.token_urlsafe(32), same_site='strict', https_only=False)
app.mount('/static', StaticFiles(directory=HERE / 'static'), name='static')
templates = Jinja2Templates(directory=HERE / 'templates')


def render(request: Request, template: str, **context):
    return templates.TemplateResponse(request, template, {'page': '', **context})


@app.get('/', response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    drawers_count = db.scalar(select(func.count(Drawer.id))) or 0
    items_count = db.scalar(select(func.count(StockItem.id))) or 0
    medicines_count = db.scalar(select(func.count(func.distinct(StockItem.medicine_id)))) or 0
    drawers = db.scalars(select(Drawer).order_by(Drawer.number).limit(12)).all()
    latest = db.scalars(select(StockItem).options(selectinload(StockItem.medicine), selectinload(StockItem.drawer)).order_by(StockItem.id.desc()).limit(6)).all()
    registry_batch = db.scalar(select(GrlsImportBatch).order_by(
        GrlsImportBatch.snapshot_on.desc(), GrlsImportBatch.id.desc()
    ).limit(1))
    curated_count = db.scalar(select(func.count(RegisteredProduct.id))) or 0
    return render(
        request, 'home.html', page='home',
        drawers_count=drawers_count, items_count=items_count,
        medicines_count=medicines_count, drawers=drawers, latest=latest,
        registry_batch=registry_batch, curated_count=curated_count,
    )


@app.get('/drawers', response_class=HTMLResponse)
def list_drawers(request: Request, q: str = Query('', max_length=100), db: Session = Depends(get_db)):
    statement = (select(Drawer, func.count(StockItem.id))
                 .outerjoin(StockItem, Drawer.id == StockItem.drawer_id)
                 .group_by(Drawer.id).order_by(Drawer.number))
    rows = db.execute(statement).all()
    if q.strip():
        rows = [(drawer, count) for drawer, count in rows if drawer_matches(drawer, q)]
    return render(request, 'drawers.html', page='drawers', drawers=rows, q=q)

@app.get('/drawers/{number}', response_class=HTMLResponse)
def drawer_detail(number: int, request: Request, q: str = Query('', max_length=100), db: Session = Depends(get_db)):
    drawer = db.scalar(select(Drawer).where(Drawer.number == number))
    if not drawer:
        raise HTTPException(status_code=404, detail='Ящик не найден')
    statement = (select(StockItem)
                 .options(selectinload(StockItem.medicine).selectinload(Medicine.ingredients))
                 .where(StockItem.drawer_id == drawer.id).join(Medicine)
                 .order_by(Medicine.name))
    items = db.scalars(statement).all()
    items, approximate = search_stock(items, q)
    count = db.scalar(select(func.count(StockItem.id)).where(StockItem.drawer_id == drawer.id)) or 0
    quality = {}
    for record in db.scalars(select(ReviewRecord).where(ReviewRecord.drawer_number == number)).all():
        code = record.confirmed_code or record.article_code
        quality[code] = record
    return render(request, 'drawer.html', page='drawers', drawer=drawer, items=items, count=count, q=q, quality=quality, approximate=approximate)

@app.get('/medicines', response_class=HTMLResponse)
def medicines(request: Request, q: str = Query('', max_length=100),
              drawer: int | None = Query(None, ge=1), db: Session = Depends(get_db)):
    statement = (select(StockItem).join(Medicine)
                 .options(selectinload(StockItem.medicine).selectinload(Medicine.ingredients),
                          selectinload(StockItem.drawer)).order_by(Medicine.name))
    if drawer is not None:
        statement = statement.join(Drawer).where(Drawer.number == drawer)
    items = db.scalars(statement).all()
    items, approximate = search_stock(items, q)
    total = len(items)
    all_drawers = db.scalars(select(Drawer).order_by(Drawer.number)).all()
    all_reviews = db.scalars(select(ReviewRecord)).all()
    quality = {}
    for record in all_reviews:
        code = record.confirmed_code if record.state == "approved" and record.confirmed_code else record.article_code
        quality[(record.drawer_number, code)] = record
    return render(request, 'medicines.html', page='medicines', items=items[:150], total=total,
                  q=q, drawer=drawer, drawers=all_drawers, quality=quality, approximate=approximate)

@app.get('/medicines/{medicine_id}', response_class=HTMLResponse)
def medicine_detail(medicine_id: int, request: Request, db: Session = Depends(get_db)):
    medicine = db.scalar(select(Medicine).options(selectinload(Medicine.ingredients), selectinload(Medicine.stock_items).selectinload(StockItem.drawer)).where(Medicine.id == medicine_id))
    if not medicine:
        raise HTTPException(status_code=404, detail='Препарат не найден')
    verified = db.scalars(select(ClinicalRecord).where(ClinicalRecord.medicine_id == medicine_id, ClinicalRecord.is_verified.is_(True)).order_by(ClinicalRecord.section)).all()
    ingredient_ids = [x.id for x in medicine.ingredients]
    related = []
    if ingredient_ids:
        related = db.scalars(select(Medicine).join(Medicine.ingredients).where(Medicine.id != medicine.id, Medicine.ingredients.any(id=ingredient_ids[0])).distinct().limit(10)).all()
    registrations = db.scalars(
        select(MedicineRegistrationLink)
        .options(
            selectinload(MedicineRegistrationLink.product)
            .selectinload(RegisteredProduct.substances)
            .selectinload(RegisteredComposition.substance)
        )
        .where(
            MedicineRegistrationLink.medicine_id == medicine_id,
            MedicineRegistrationLink.state == "active",
        ).order_by(MedicineRegistrationLink.id.desc())
    ).all()
    # Read-only suggestions from the latest *imported snapshot*, not links.
    # Do not infer dosage, manufacturer, active substance or drug equivalence.
    registry_batch = db.scalar(
        select(GrlsImportBatch).order_by(
            GrlsImportBatch.snapshot_on.desc(), GrlsImportBatch.id.desc()
        ).limit(1)
    )
    candidate_rows = (
        source_candidates(
            db, registry_batch.id, medicine_name=medicine.name, limit=5
        )
        if registry_batch else []
    )
    latest_matching = db.scalar(
        select(MatchingRun)
        .where(MatchingRun.state == "done")
        .order_by(MatchingRun.id.desc()).limit(1)
    )
    matching_rows = (
        db.scalars(
            select(MatchingItem)
            .where(
                MatchingItem.run_id == latest_matching.id,
                MatchingItem.medicine_id == medicine_id,
            )
            .order_by(MatchingItem.drawer_number, MatchingItem.article_code)
            .limit(25)
        ).all()
        if latest_matching else []
    )
    return render(request, 'medicine.html', page='medicines', medicine=medicine,
                  verified=verified, related=related, registrations=registrations,
                  registry_batch=registry_batch, candidate_rows=candidate_rows,
                  matching_run=latest_matching, matching_rows=matching_rows)


@app.get('/interactions', response_class=HTMLResponse)
def interactions_page(request: Request):
    return render(request, 'interactions.html', page='interactions')


class InteractionRequest(BaseModel):
    medicine_names: list[str] = Field(min_length=2, max_length=20)


@app.post('/api/interactions/check')
def check_interactions(payload: InteractionRequest):
    names = list(dict.fromkeys(name.strip()[:120] for name in payload.medicine_names if name.strip()))
    if len(names) < 2:
        raise HTTPException(status_code=422, detail='Укажите минимум два препарата.')
    # No interaction database: never pretend a check was performed.
    # Submitted names are not stored in a patient profile or database.
    return {
        'status': 'not_configured',
        'checked': False,
        'input_names': names,
        'findings': [],
        'message': 'Проверка НЕ выполнена: база лекарственных взаимодействий не подключена. Отсутствие результатов не означает безопасность комбинации.',
    }


@app.get('/api/search')
def api_search(q: str = Query('', min_length=2, max_length=100), db: Session = Depends(get_db)):
    stocks = db.scalars(select(StockItem).join(Medicine)
        .options(selectinload(StockItem.medicine).selectinload(Medicine.ingredients), selectinload(StockItem.drawer))
        .order_by(Medicine.name)).all()
    found, approximate = search_stock(stocks, q)
    return [{'id': s.medicine.id, 'name': s.medicine.name, 'code': s.article_code,
             'drawer': s.drawer.number, 'approximate': approximate} for s in found[:10]]

@app.get('/health')
def health():
    return {'status': 'ok', 'version': APP_VERSION}

# Include management routes after the public catalogue routes.
from .manage import router as manage_router  # noqa: E402
app.include_router(manage_router)

from .review import router as review_router  # noqa: E402
app.include_router(review_router)

from .identity import router as identity_router  # noqa: E402
app.include_router(identity_router)

from .grls import router as grls_router  # noqa: E402
app.include_router(grls_router)

from .catalog import router as catalog_router  # noqa: E402
app.include_router(catalog_router)

from .quality import router as quality_router  # noqa: E402
app.include_router(quality_router)

from .matching import router as matching_router  # noqa: E402
app.include_router(matching_router)
