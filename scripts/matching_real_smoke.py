"""Opt-in synthetic load test against official public GRLS file.

This DOES NOT read any local pharmacist data, photographs or patient info.
The GitHub runner's performance is not the user's Windows performance.
"""
import os
from datetime import date
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from sqlalchemy import create_engine, insert, select, func
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.grls_fetch import fetch_official_grls
from app.grls_import import parse_grls_archive
from app.matching_worker import ALGORITHM_VERSION, inventory_digest, run_matching
from app.models import (
    Drawer, GrlsImportBatch, GrlsImportRow, MatchingRun, Medicine, StockItem
)

COUNT_GOODS = 1131


def main():
    started = perf_counter()
    zipped, _source = fetch_official_grls()
    parsed = parse_grls_archive(zipped)
    print("SOURCE", parsed["snapshot_on"], parsed["count"],
          "records, fetched+parsed in", round(perf_counter() - started, 1),
          "seconds", flush=True)
    assert parsed["count"] >= 100000
    with TemporaryDirectory(prefix="pharmdesk-matching-") as temp:
        path = Path(temp) / "synthetic.sqlite3"
        engine = create_engine("sqlite:///" + str(path))
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with factory() as db:
            batch = GrlsImportBatch(
                sha256=parsed["sha256"], archive_name="official_grls_public.zip",
                snapshot_on=parsed["snapshot_on"],
                row_count=parsed["count"],
                origin_statement="public_source_download_without_signature_verification",
            )
            db.add(batch)
            db.flush()
            for i in range(0, len(parsed["rows"]), 2000):
                db.execute(
                    insert(GrlsImportRow),
                    [dict(batch_id=batch.id, **row)
                     for row in parsed["rows"][i:i+2000]]
                )
                db.commit()
            # Generate artificial pharmacy titles from public trade names.
            # These are NOT actual items from the user's pharmacy.
            unique = []
            seen = set()
            for row in parsed["rows"]:
                if row["trade_key"] and len(row["trade_key"]) >= 4 and row["trade_key"] not in seen:
                    seen.add(row["trade_key"])
                    unique.append(row["trade_name"])
                if len(unique) == COUNT_GOODS:
                    break
            if len(unique) < COUNT_GOODS:
                raise RuntimeError("Insufficient distinct public names for synthetic test")
            drawer = Drawer(number=101, name="Синтетический ящик")
            db.add(drawer)
            db.flush()
            medicines = [
                Medicine(
                    name=trade + " таб. 160 мг",
                    manufacturer="Вымышленный изготовитель",
                    fingerprint=sha256(f"mock-{i}".encode()).hexdigest(),
                )
                for i, trade in enumerate(unique)
            ]
            db.add_all(medicines)
            db.flush()
            db.add_all([
                StockItem(
                    article_code=f"SYN-{i:05}", drawer_id=drawer.id,
                    medicine_id=medicine.id,
                )
                for i, medicine in enumerate(medicines, start=1)
            ])
            db.commit()
            signature, total = inventory_digest(db, batch)
            assert total == COUNT_GOODS
            run = MatchingRun(
                batch_id=batch.id,
                input_digest=signature,
                algorithm_version=ALGORITHM_VERSION,
                state="queued", total=total
            )
            db.add(run)
            db.commit()
            run_id = run.id
        analysis_start = perf_counter()
        run_matching(run_id, session_factory=factory)
        seconds = perf_counter() - analysis_start
        with factory() as db:
            completed = db.get(MatchingRun, run_id)
            assert completed.state == "done", completed.error_text
            assert completed.processed == COUNT_GOODS
            assert completed.strong + completed.ambiguous + completed.not_found == COUNT_GOODS
            print(
                "SYNTHETIC_LIVE_GRLS_RESULT",
                "records", parsed["count"],
                "goods", COUNT_GOODS,
                "seconds", round(seconds, 2),
                "strong", completed.strong,
                "ambiguous", completed.ambiguous,
                "no_candidate", completed.not_found,
                flush=True,
            )
            print(
                "NOTE: This is a synthetic workload on a Linux GitHub runner, "
                "not human validation and not a Windows benchmark.",
                flush=True,
            )
        engine.dispose()


if __name__ == "__main__":
    main()
