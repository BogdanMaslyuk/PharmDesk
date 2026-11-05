"""Read-only heuristics for catalog data-quality review.

A warning is NOT a finding that a medicine name is wrong. Do not modify
stock, trust OCR, or infer therapeutic equivalence from these flags.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
import re

from .search import normalize


LATIN = re.compile(r"[A-Za-z]")
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
MIXED_WORD = re.compile(r"(?=[\w-]*[A-Za-z])(?=[\w-]*[А-Яа-яЁё])[\w-]+", re.UNICODE)
REPEATED = re.compile(r"([А-Яа-яЁёA-Za-z])\1{3,}", re.IGNORECASE)
PLACEHOLDER = re.compile(r"(?:\?{2,}|#{2,}|_{3,}|\b(?:неизвестно|неразборчиво)\b)", re.IGNORECASE)

REASONS = {
    "latin": "Латиница — проверить написание",
    "mixed": "Смешение алфавитов",
    "garbled": "Возможно повреждённое название",
    "duplicate": "Одинаковое название в одном ящике",
    "code_conflict": "Одинаковый код, разные названия в ящиках",
}


@dataclass(frozen=True)
class CatalogIssue:
    reason: str
    item_id: int
    drawer_number: int
    article_code: str
    name: str
    maker: str
    review_id: int | None
    review_state: str
    description: str


def audit_catalog(items, review_records):
    """Scan all provided stock rows without changing session or database."""
    reviews = {}
    for record in review_records:
        # A manually edited approved code can be pending until reviewed again.
        code = record.confirmed_code or record.article_code
        reviews[(record.drawer_number, code)] = record

    by_name = defaultdict(list)
    by_code = defaultdict(list)
    for item in items:
        name_key = normalize(item.medicine.name)
        maker_key = normalize(item.medicine.manufacturer or "")
        by_name[(item.drawer.number, name_key, maker_key)].append(item)
        by_code[normalize(item.article_code)].append(item)

    name_duplicates = set()
    for group in by_name.values():
        if len({item.article_code for item in group}) > 1:
            name_duplicates.update(item.id for item in group)

    conflicting_codes = set()
    for group in by_code.values():
        if len({normalize(item.medicine.name) for item in group}) > 1 and len({item.drawer.number for item in group}) > 1:
            conflicting_codes.update(item.id for item in group)

    counts = Counter()
    issues = []
    pending = confirmed = excluded = unlinked = 0
    for item in items:
        key = (item.drawer.number, item.article_code)
        record = reviews.get(key)
        state = record.state if record else "unlinked"
        if state == "approved":
            confirmed += 1
        elif state == "excluded":
            excluded += 1
        elif state == "pending":
            pending += 1
        else:
            unlinked += 1
        name = item.medicine.name
        maker = item.medicine.manufacturer or ""
        warnings = []

        # Name language is NOT proof of error, especially for trade names.
        if state != "approved":
            if MIXED_WORD.search(name):
                warnings.append(("mixed", "В одном фрагменте сочетаются кириллические и латинские буквы."))
            elif LATIN.search(name):
                warnings.append(("latin", "Латиница встречается в товарном названии. Это может быть корректно."))
            if REPEATED.search(name) or PLACEHOLDER.search(name):
                warnings.append(("garbled", "Подозрительные символы или повторяющиеся буквы."))
        if item.id in name_duplicates:
            warnings.append(("duplicate", "У разных товарных кодов совпадают название и производитель в этом ящике."))
        if item.id in conflicting_codes:
            warnings.append(("code_conflict", "Одинаковый код относится к разным названиям в других ящиках."))

        for reason, detail in warnings:
            counts[reason] += 1
            issues.append(CatalogIssue(
                reason=reason, item_id=item.id, drawer_number=item.drawer.number,
                article_code=item.article_code, name=name, maker=maker,
                review_id=record.id if record else None,
                review_state=state, description=detail,
            ))
    issues.sort(key=lambda issue: (
        issue.drawer_number, issue.name.casefold(), issue.article_code,
        issue.reason, issue.item_id
    ))
    return {
        "issues": issues,
        "counts": {reason: counts[reason] for reason in REASONS},
        "total_stock": len(items),
        "confirmed": confirmed, "pending": pending, "excluded": excluded,
        "without_review": unlinked,
        "total_flags": len(issues),
    }
