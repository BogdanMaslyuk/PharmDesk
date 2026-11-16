"""Read-only diagnostic aggregation of historical matching runs.

Legacy runs are never presented as if all matching registry rows were evaluated.
No originals, registration links or matching decisions are modified.
"""
import json
from collections import Counter

from sqlalchemy import func, select

from .models import (
    MatchingCandidate, MatchingEvidence, MatchingItem, MatchingRun, GrlsImportRow,
)


def matching_audit(db, run_id):
    run = db.get(MatchingRun, run_id)
    if run is None:
        raise ValueError("Unknown matching run")
    items = db.execute(select(
        MatchingItem.id, MatchingItem.candidate_total, MatchingItem.parsed_json
    ).where(MatchingItem.run_id == run_id)).all()
    by_item = dict(db.execute(select(
        MatchingCandidate.item_id, func.count(MatchingCandidate.id)
    ).join(MatchingItem, MatchingCandidate.item_id == MatchingItem.id)
     .where(MatchingItem.run_id == run_id)
     .group_by(MatchingCandidate.item_id)).all())
    displayed = sum(by_item.values())
    total_name_pairs = sum(item.candidate_total for item in items)
    evaluated = 0
    evaluation_known = True
    incomplete = 0
    all_evidence_complete = True
    all_evidence = {}
    all_form_dose_matches = 0
    all_multiple_reg = 0
    for item in items:
        data = json.loads(item.parsed_json or "{}")
        if "all_evidence_counts" not in data:
            all_evidence_complete = False
        else:
            for field, states in data["all_evidence_counts"].items():
                dest = all_evidence.setdefault(field, {})
                for outcome, n in states.items():
                    dest[outcome] = dest.get(outcome, 0) + n
            all_form_dose_matches += bool(data.get("any_form_dose_match_evaluated", False))
            all_multiple_reg += data.get("distinct_registration_numbers_evaluated", 0) > 1
        if "evaluated_count" not in data:
            evaluation_known = False
        else:
            evaluated += int(data["evaluated_count"])
            if not data.get("evaluation_complete", False):
                incomplete += 1
    if not evaluation_known:
        evaluated = None

    states = db.execute(select(
        MatchingEvidence.field, MatchingEvidence.outcome, func.count(MatchingEvidence.id)
    ).join(MatchingCandidate, MatchingEvidence.candidate_id == MatchingCandidate.id)
     .join(MatchingItem, MatchingCandidate.item_id == MatchingItem.id)
     .where(MatchingItem.run_id == run_id)
     .group_by(MatchingEvidence.field, MatchingEvidence.outcome)).all()
    evidence = {}
    for field, outcome, count in states:
        evidence.setdefault(field, {})[outcome] = count

    # Both traits must match on the SAME candidate, not separate registry rows.
    valid_candidates = (select(MatchingEvidence.candidate_id)
        .join(MatchingCandidate, MatchingEvidence.candidate_id == MatchingCandidate.id)
        .join(MatchingItem, MatchingCandidate.item_id == MatchingItem.id)
        .where(MatchingItem.run_id == run_id,
               MatchingEvidence.field.in_(("form", "dose")),
               MatchingEvidence.outcome == "match")
        .group_by(MatchingEvidence.candidate_id)
        .having(func.count(func.distinct(MatchingEvidence.field)) == 2))
    with_form_dose = db.scalar(select(
        func.count(func.distinct(MatchingCandidate.item_id))
    ).where(MatchingCandidate.id.in_(valid_candidates))) or 0

    variants = db.execute(select(
        MatchingCandidate.item_id,
        func.count(func.distinct(GrlsImportRow.registration_number))
    ).join(GrlsImportRow, GrlsImportRow.id == MatchingCandidate.grls_row_id)
     .join(MatchingItem, MatchingCandidate.item_id == MatchingItem.id)
     .where(MatchingItem.run_id == run_id)
     .group_by(MatchingCandidate.item_id)).all()
    if all_evidence_complete:
        evidence = all_evidence
        with_form_dose = all_form_dose_matches
        multiple_regs = all_multiple_reg
    else:
        multiple_regs = sum(n > 1 for _, n in variants)
    return {
        "run_id": run_id,
        "algorithm": run.algorithm_version,
        "processed_items": len(items),
        "candidate_pairs_by_exact_title": total_name_pairs,
        "evaluated_candidate_pairs": evaluated,
        "evaluation_count_known": evaluation_known,
        "items_not_fully_evaluated": incomplete if evaluation_known else None,
        "displayed_candidate_pairs": displayed,
        "hidden_candidate_pairs": total_name_pairs - displayed,
        "items_with_form_and_dose_match": with_form_dose,
        "items_with_multiple_registration_numbers": multiple_regs,
        "evidence_counts": evidence,
        "evidence_scope": "all_evaluated" if all_evidence_complete else "displayed_only",
        "disclaimer": (
            "Признаки относятся к загруженному снимку ГРЛС. "
            "Совпадение не подтверждает состав, действующую регистрацию "
            "или взаимозаменяемость препарата."
        ),
    }
