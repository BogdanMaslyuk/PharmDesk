"""Conservative medicine-form and strength extraction for bulk GRLS matching.

Never infer active ingredients from inventory, infer a dose from missing
units, or merge separate product-release variants. Uncertain source strings
are downgraded to manual review.
"""
from decimal import Decimal, InvalidOperation
import re

from .trade_candidates import trade_name_from_stock
from .search import normalize

FORM_PATTERNS = (
    ("крем", r"\bкрем(?:а|ы)?\b"),
    ("мазь", r"\bмаз(?:ь|и)\b"),
    ("гель", r"\bгел(?:ь|я|и)\b"),
    ("таблетки", r"\bтаб(?:л|летк[аиуы]|летки)?\b"),
    ("капсулы", r"\bкапс(?:ул[аы]|улы)?\b"),
    ("раствор", r"\b(?:р[\s.-]*р|р[\s.-]*ств|раствор(?:а)?)\b"),
    ("суспензия", r"\bсусп(?:ензия|ензии)?\b"),
    ("сироп", r"\bсироп(?:а)?\b"),
    ("капли", r"\bкапл(?:и|я|ей)\b"),
    ("спрей", r"\bспрей\b"),
    ("аэрозоль", r"\bаэрозол(?:ь|я|и)\b"),
    ("суппозитории", r"\b(?:супп(?:озитории)?|свеч[иа])\b"),
    ("порошок", r"\b(?:пор(?:ошок|ошка)?|порошок)\b"),
    ("гранулы", r"\bгранул(?:ы|а)?\b"),
)
FORMS = tuple((name, re.compile(regex, re.I)) for name, regex in FORM_PATTERNS)

# Decimal strengths with explicit units. "мл" alone is package volume, NOT dose.
_DOSE_RE = re.compile(
    r"(?<![а-яa-z0-9])(\d+(?:[.,]\d+)?)\s*"
    r"(мг\s*/\s*мл|мкг\s*/\s*мл|мг|мкг|мег|ме|ед|%|г)"
    r"(?![а-яa-z])", re.I
)
_STANDALONE_PLUS = re.compile(
    r"(?:\+|[-–])\s*\d+(?:[.,]\d+)?(?![\d.,])\s*(?![%а-яa-z])",
    re.I,
)
_PACK_RE = re.compile(r"(?:№|N\s*)\s*(\d{1,4})", re.I)
_PACKAGE_GRAMS = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(г|мл)\b", re.I)


def _clean(text):
    return str(text or "").lower().replace("ё", "е").replace("\xa0", " ")


def extract_forms(raw):
    text = _clean(raw)
    return sorted(name for name, pattern in FORMS if pattern.search(text))


def extract_doses(raw):
    """Return (normalized dose tokens, ambiguous).

    A bare number after a combination delimiter cannot be assigned an
    ingredient or unit. Grams alone often mean packaging and must not
    justify an automatic strong match.
    """
    text = _clean(raw)
    values = set()
    ambiguous = False
    for match in _DOSE_RE.finditer(text):
        amount = match.group(1).replace(",", ".")
        unit = re.sub(r"\s+", "", match.group(2))
        if unit == "мег":
            unit = "мкг"
        if unit == "г":
            # Could be ingredient mass, package mass or both.
            ambiguous = True
            continue
        if unit in {"ме", "ед"}:
            unit = "ме"
        try:
            decimal = Decimal(amount)
        except InvalidOperation:
            ambiguous = True
            continue
        values.add((format(decimal.normalize(), "f"), unit))
    if _STANDALONE_PLUS.search(text):
        ambiguous = True
    return sorted(values), ambiguous


def is_modified_release(raw):
    text = _clean(raw)
    return bool(re.search(r"\b(?:пролонг\w*|замедлен\w*|модифицир\w*|ретард)\b", text))


def package_description(raw):
    text = _clean(raw)
    packs = _PACK_RE.findall(text)
    # Package size is auxiliary and never a basis for a strong match.
    size = _PACKAGE_GRAMS.findall(text)
    parts = ["№" + n for n in packs[:5]]
    parts += [f"{n} {u}" for n, u in size[:5]]
    return ", ".join(dict.fromkeys(parts))


def stock_attributes(name):
    return {
        "trade": trade_name_from_stock(name),
        "trade_key": normalize(trade_name_from_stock(name)),
        "forms": extract_forms(name),
        "doses": extract_doses(name)[0],
        "dose_uncertain": extract_doses(name)[1],
        "modified_release": is_modified_release(name),
        "package": package_description(name),
    }


def source_attributes(raw):
    doses, uncertain = extract_doses(raw)
    return {
        "forms": extract_forms(raw),
        "doses": doses,
        "dose_uncertain": uncertain,
        "modified_release": is_modified_release(raw),
        "package": package_description(raw),
    }


def evaluate(stock, source, *, exact_trade, source_status, total_same_name=1):
    """Four-state evidence for each field; no inferred clinical identity."""
    fields = []
    def add(field, outcome, stock_value, source_value, reason):
        fields.append({
            "field": field, "outcome": outcome,
            "stock_value": str(stock_value)[:350],
            "registry_value": str(source_value)[:600],
            "explanation": reason[:500],
        })
    add("name", "match" if exact_trade else "conflict",
        stock["trade"], source.get("trade", ""), "Полное торговое название; префикс не считается совпадением.")
    a, b = stock["forms"], source["forms"]
    form_state = (
        "unknown" if not a or not b else
        "match" if len(a) == len(b) == 1 and a == b else
        "ambiguous" if set(a) & set(b) else "conflict"
    )
    add("form", form_state, ", ".join(a), ", ".join(b),
        "Несколько форм в одной строке ГРЛС не объединяются в одну доказанную упаковку.")
    da, db = stock["doses"], source["doses"]
    if stock["dose_uncertain"] or source["dose_uncertain"]:
        dose_state = "ambiguous"
    elif not da or not db:
        dose_state = "unknown"
    elif da == db:
        dose_state = "match"
    elif set(map(tuple, da)) & set(map(tuple, db)):
        dose_state = "ambiguous"
    else:
        dose_state = "conflict"
    add("dose", dose_state, " + ".join(" ".join(d) for d in da),
        " + ".join(" ".join(d) for d in db),
        "Дозы сравниваются как десятичные значения с единицами; неполные комбинации требуют сверки.")
    release_state = "conflict" if (
        bool(stock["modified_release"]) != bool(source["modified_release"])
        and "таблетки" in a and "таблетки" in b
    ) else "match" if "таблетки" in a and "таблетки" in b else "unknown"
    add("release", release_state,
        "Модифицированное" if stock["modified_release"] else "Не указано",
        "Модифицированное" if source["modified_release"] else "Не указано",
        "Тип высвобождения нельзя автоматически считать одинаковым.")
    add("package", "unknown", stock["package"], source["package"],
        "Упаковка — дополнительный признак; по ней не подтверждается тождество препарата.")
    add("status", "unknown", "", source_status,
        "Категория относится только к дате выгрузки и не удостоверяет текущий статус.")
    # Even a strong candidate is unverified and must be manually reviewed.
    eligible_status = source_status in {"Действующий", "Выдано по правилам ЕАЭС"}
    strong = (
        exact_trade and form_state == "match"
        and dose_state == "match" and release_state != "conflict"
        and eligible_status and total_same_name == 1
    )
    reasons = []
    if not eligible_status:
        reasons.append("Категория реестра не подтверждает действующую регистрацию.")
    if total_same_name > 1:
        reasons.append("Есть несколько регистрационных строк с тем же торговым названием.")
    if form_state != "match":
        reasons.append("Лекарственная форма: " + form_state + ".")
    if dose_state != "match":
        reasons.append("Дозировка: " + dose_state + ".")
    if release_state == "conflict":
        reasons.append("Различаются признаки типа высвобождения.")
    if not reasons:
        reasons = ["Полное название, форма и дозировка совпадают; всё равно требуется проверка человеком."]
    return {"category": "strong" if strong else "ambiguous",
            "evidence": fields, "reasons": reasons}
