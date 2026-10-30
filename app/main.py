"""First local MVP. Medical interaction checks are deliberately NOT simulated."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

from .database import get_db, init_db
from .models import ClinicalRecord, Drawer, Ingredient, Medicine, StockItem

HERE = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title='PharmDesk — рабочее место фармацевта',
    description='Поиск по ящикам и локальному ассортименту. Не предназначено для медицинских решений.',
    version='0.1.0',
    lifespan=lifespan,
)
app.mount('/static', StaticFiles(directory=HERE / 'static'), name='static')
templates = Jinja2Templates(directory=HERE / 'templates')


def render(request: Request, template: str, **context):
    return templates.TemplateResponse(request, template, {'page': '', **context})


@app.get('/', response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    drawers_count = db.scalar(select(func.count(Drawer.id))) or 0
    items_count = db.scalar(select(func.count(StockItem.id))) or 0
    medicines_count = db.scalar(select(func.count(Medicine.id))) or 0
    drawers = db.scalars(select(Drawer).order_by(Drawer.number).limit(12)).all()
    latest = db.scalars(select(StockItem).options(selectinload(StockItem.medicine), selectinload(StockItem.drawer)).order_by(StockItem.id.desc()).limit(6)).all()
    return render(request, 'home.html', page='home', drawers_count=drawers_count, items_count=items_count, medicines_count=medicines_count, drawers=drawers, latest=latest)


@app.get('/drawers', response_class=HTMLResponse)
def list_drawers(request: Request, q: str = Query('', max_length=100), db: Session = Depends(get_db)):
    statement = (select(Drawer, func.count(StockItem.id))
                 .outerjoin(StockItem, Drawer.id == StockItem.drawer_id)
                 .group_by(Drawer.id)
                 .order_by(Drawer.number))
    if q.strip():
        like = f'%{q.strip()}%'
        statement = statement.where(or_(Drawer.name.ilike(like), Drawer.location.ilike(like), cast(Drawer.number, String).ilike(like)))
    rows = db.execute(statement).all()
    return render(request, 'drawers.html', page='drawers', drawers=rows, q=q)


@app.get('/drawers/{number}', response_class=HTMLResponse)
def drawer_detail(number: int, request: Request, q: str = Query('', max_length=100), db: Session = Depends(get_db)):
    drawer = db.scalar(select(Drawer).where(Drawer.number == number))
    if not drawer:
        raise HTTPException(status_code=404, detail='Ящик не найден')
    statement = (select(StockItem).options(selectinload(StockItem.medicine).selectinload(Medicine.ingredients))
                 .where(StockItem.drawer_id == drawer.id).join(Medicine)
                 .order_by(Medicine.name))
    if q.strip():
        like = f'%{q.strip()}%'
        statement = statement.where(or_(Medicine.name.ilike(like), Medicine.manufacturer.ilike(like), StockItem.article_code.ilike(like), Medicine.ingredients.any(Ingredient.name.ilike(like))))
    items = db.scalars(statement).all()
    count = db.scalar(select(func.count(StockItem.id)).where(StockItem.drawer_id == drawer.id)) or 0
    return render(request, 'drawer.html', page='drawers', drawer=drawer, items=items, count=count, q=q)


@app.get('/medicines', response_class=HTMLResponse)
def medicines(request: Request, q: str = Query('', max_length=100), db: Session = Depends(get_db)):
    statement = select(StockItem).join(Medicine).options(selectinload(StockItem.medicine).selectinload(Medicine.ingredients), selectinload(StockItem.drawer)).order_by(Medicine.name)
    if q.strip():
        like = f'%{q.strip()}%'
        statement = statement.where(or_(Medicine.name.ilike(like), Medicine.manufacturer.ilike(like), StockItem.article_code.ilike(like), Medicine.ingredients.any(Ingredient.name.ilike(like))))
    items = db.scalars(statement.limit(150)).all()
    return render(request, 'medicines.html', page='medicines', items=items, q=q)


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
    return render(request, 'medicine.html', page='medicines', medicine=medicine, verified=verified, related=related)


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
    like = f'%{q.strip()}%'
    stocks = db.scalars(select(StockItem).join(Medicine).options(selectinload(StockItem.medicine), selectinload(StockItem.drawer))
                        .where(or_(Medicine.name.ilike(like), StockItem.article_code.ilike(like), Medicine.ingredients.any(Ingredient.name.ilike(like))))
                        .order_by(Medicine.name).limit(10)).all()
    return [{'id': s.medicine.id, 'name': s.medicine.name, 'code': s.article_code, 'drawer': s.drawer.number} for s in stocks]


@app.get('/health')
def health():
    return {'status': 'ok', 'version': '0.1.0'}
