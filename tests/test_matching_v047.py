"""Synthetic regression tests for evidence-first v0.4.7 GRLS comparison."""
from datetime import date
from hashlib import sha256
import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.importer import import_rows
from app.matching_audit import matching_audit
from app.matching_worker import run_matching, MAX_CANDIDATES_PER_ITEM
from app.models import (
    GrlsImportBatch, GrlsImportRow, MatchingCandidate, MatchingItem, MatchingRun,
    StockItem, Medicine,
)
from app.search import normalize
from app.trade_candidates import trade_name_from_snapshot
from app.matching_parse import stock_attributes, source_attributes, evaluate


def _synthetic_environment():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory


def test_trade_resolver_never_uses_family_prefixes():
    known = {normalize(x) for x in [
        "Миг", "Миг 400", "Креон 10000", "Креон 25000",
        "Гепариновая мазь", "Акридерм", "Акридерм ГК",
        "Лориста", "Конкор", "Конкор Кор"
    ]}
    assert trade_name_from_snapshot("Миг 400 таб. 400 мг №20", known) == "миг 400"
    assert trade_name_from_snapshot("Креон 10000 капс. №20", known) == "креон 10000"
    assert trade_name_from_snapshot("Гепариновая мазь туба 25 г", known) == "гепариновая мазь"
    assert trade_name_from_snapshot("Акридерм ГК крем 30 г", known) == "акридерм гк"
    assert trade_name_from_snapshot("Конкор Кор таб. 2.5 мг", known) == "конкор кор"
    assert normalize(trade_name_from_snapshot("Миг 800 таб. 800 мг", known)) != normalize("Миг")
    assert normalize(trade_name_from_snapshot("Акридерм СК крем", known)) != normalize("Акридерм")
    assert normalize(trade_name_from_snapshot("Лориста Н таб. 50 мг", known)) != normalize("Лориста")


def test_uncertain_doses_and_wrong_forms_not_accepted():
    stock = stock_attributes("Валз таб. 10 мг")
    wrong = source_attributes("таблетки 10 мг/мл")
    form_wrong = source_attributes("крем 10 мг")
    assert next(e for e in evaluate(stock, wrong, exact_trade=True, source_status="Действующий")["evidence"]
                if e["field"] == "dose")["outcome"] == "conflict"
    assert next(e for e in evaluate(stock, form_wrong, exact_trade=True, source_status="Действующий")["evidence"]
                if e["field"] == "form")["outcome"] == "conflict"
    a = source_attributes("таблетки, 0,05%-0,1%+1")
    assert a["dose_uncertain"]
    multi = source_attributes("таблетки 10 мг; таблетки 20 мг")
    assert multi["dose_uncertain"]
    comparison = evaluate(stock_attributes("Валз таб. 10 мг + 20 мг"), multi,
                          exact_trade=True, source_status="Действующий")
    assert comparison["category"] == "ambiguous"


def test_evaluates_all_41_then_displays_best_30_without_mutating_inventory():
    engine, factory = _synthetic_environment()
    try:
        with factory() as db:
            import_rows(db, [{"Код": "X-47", "Наименование товара": "Валз таб. 160 мг",
                             "Производитель": "Учебный завод"}], 11, "Тестовый ящик")
            batch = GrlsImportBatch(
                sha256=sha256(b"synthetic-47").hexdigest(), archive_name="synthetic.zip",
                snapshot_on=date(2026, 11, 5), row_count=41,
            )
            db.add(batch)
            db.flush()
            for i in range(41):
                form = "таблетки, 160 мг" if i == 40 else "мазь, 10 мг"
                reg = "TEST-041" if i == 40 else f"TEST-{i:03d}"
                db.add(GrlsImportRow(
                    batch_id=batch.id, source_workbook="synthetic-test.xlsx",
                    source_row=i + 2, status_from_filename="Действующий",
                    registration_number=reg, registration_key=normalize(reg),
                    trade_name="Валз", trade_key=normalize("Валз"),
                    inn_raw="Синтетические сведения", release_forms_raw=form,
                    certificate_holder="Учебный держатель",
                ))
            db.commit()
            from app.matching_worker import inventory_digest
            digest, total = inventory_digest(db, batch)
            run = MatchingRun(batch_id=batch.id, input_digest=digest,
                              algorithm_version="0.4.7-evidence-1", state="queued", total=total)
            db.add(run)
            db.commit()
            run_id = run.id
        run_matching(run_id, session_factory=factory)
        with factory() as db:
            run = db.get(MatchingRun, run_id)
            assert run.state == "done"
            assert run.total == run.processed == 1
            item = db.scalar(select(MatchingItem).where(MatchingItem.run_id == run_id))
            assert item.candidate_total == 41
            meta = json.loads(item.parsed_json)
            assert meta["evaluated_count"] == 41
            assert meta["evaluation_complete"] is True
            candidates = db.scalars(
                select(MatchingCandidate).where(MatchingCandidate.item_id == item.id)
                .order_by(MatchingCandidate.rank)
            ).all()
            assert len(candidates) == MAX_CANDIDATES_PER_ITEM
            assert candidates[0].source_row.registration_number == "TEST-041"
            assert run.strong == 0  # Multiple registration records are not verified.
            report = matching_audit(db, run_id)
            assert report["candidate_pairs_by_exact_title"] == 41
            assert report["evaluated_candidate_pairs"] == 41
            assert report["displayed_candidate_pairs"] == 30
            assert report["hidden_candidate_pairs"] == 11
            assert report["items_with_form_and_dose_match_among_displayed"] == 1
            assert db.scalar(select(StockItem)).article_code == "X-47"
            assert db.scalar(select(Medicine)).name == "Валз таб. 160 мг"
    finally:
        engine.dispose()


def test_old_run_audit_does_not_claim_all_evaluated():
    engine, factory = _synthetic_environment()
    try:
        with factory() as db:
            batch = GrlsImportBatch(
                sha256=sha256(b"old synthetic").hexdigest(), archive_name="synthetic.zip",
                snapshot_on=date(2026, 11, 5), row_count=0,
            )
            db.add(batch)
            db.flush()
            run = MatchingRun(batch_id=batch.id, input_digest="x" * 64,
                              algorithm_version="0.4.5-conservative-1", state="done", total=1)
            db.add(run)
            db.flush()
            db.add(MatchingItem(
                run_id=run.id, stock_item_id=99, medicine_id=99,
                drawer_number=1, article_code="OLD", stock_name="Условный товар",
                stock_fingerprint="x" * 64, parsed_json="{}", candidate_total=50,
                category="ambiguous", reasons_json="[]",
            ))
            db.commit()
            result = matching_audit(db, run.id)
            assert result["evaluation_count_known"] is False
            assert result["evaluated_candidate_pairs"] is None
            assert result["hidden_candidate_pairs"] == 50
    finally:
        engine.dispose()
