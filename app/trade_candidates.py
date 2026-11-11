"""Strict trade-name suggestions from an imported GRLS snapshot.

A family prefix (Акридерм) is NOT evidence that another variant
(Акридерм ГК / Гента / СК) is the same medicinal product.
This module is deliberately conservative: it can return no candidates,
and it never verifies INN, dose, formulation, current status or
interchangeability.
"""
import re

from sqlalchemy import case, select

from .models import GrlsImportRow
from .search import normalize

_WORD = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
# Inventory title contains brand first, followed by abbreviated forms,
# packaging and dose. These tokens end the *brand* part of the label.
# DO NOT list marketing suffixes like "ГК", "Гента", "Плюс", "Форте".
_AFTER_BRAND = frozenset({
    "таб", "табл", "таблетки", "таблетка",
    "капс", "капсулы", "капсула",
    "туб", "туба", "тубы",
    "мазь", "крем", "гель", "лосьон", "спрей", "аэрозоль",
    "капли", "кап", "капель",
    "р", "рр", "рств", "раствор",
    "сусп", "суспензия", "сироп",
    "пор", "порошок", "гран", "гранулы",
    "амп", "ампулы", "ампула", "фл", "флакон", "флаконы",
    "супп", "суппозитории", "свечи", "свеч",
    "саше", "пак", "пакет", "пакеты", "блистер",
    "мг", "мкг", "г", "мл", "л", "ед", "ме", "iu",
    "д", "для", "наружн", "прим", "внутр", "внутрен",
    "n", "no",
})


def trade_name_from_stock(inventory_name):
    """Extract provisional brand phrase preceding dosage/form/packaging.

    We do not infer INN from it or accept short family-name prefixes.
    """
    words = _WORD.findall(str(inventory_name or "").casefold().replace("ё", "е"))
    result = []
    for word in words:
        if (word in _AFTER_BRAND or word[0].isdigit()
                or len(result) >= 6):
            break
        result.append(word)
    return " ".join(result)


def trade_name_key(inventory_name):
    return normalize(trade_name_from_stock(inventory_name))


def source_candidates(db, batch_id, *, medicine_name, trade_name=None, limit=5):
    """Only identical full trade names: NEVER a shared first-word prefix.

    The optional trade_name is an explicit pharmacist search on the
    candidate page. A later attempt to link a source record still
    validates the actual stock trade name separately.
    """
    if not batch_id:
        return []
    phrase = trade_name if trade_name is not None else trade_name_from_stock(medicine_name)
    key = normalize(phrase)
    if len(key) < 3 or key.isdigit():
        return []
    stmt = (
        select(GrlsImportRow)
        .where(
            GrlsImportRow.batch_id == batch_id,
            GrlsImportRow.trade_key == key,
        )
        .order_by(
            case(
                (GrlsImportRow.status_from_filename == "Действующий", 0),
                (GrlsImportRow.status_from_filename == "Выдано по правилам ЕАЭС", 1),
                else_=2,
            ),
            GrlsImportRow.registration_number,
            GrlsImportRow.id,
        )
        .limit(limit)
    )
    return db.scalars(stmt).all()
