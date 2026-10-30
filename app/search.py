"""Search normalization for Russian medicine titles and internal stock codes.

Casefold handles Cyrillic reliably (unlike SQLite's default case-insensitive LIKE).
Local MVP intentionally scans only the local inventory, not the external market.
"""
import re


def normalize(value):
    return re.sub(r"[^\w]+", "", str(value or "").casefold().replace("ё", "е"))


def matches(haystack, query):
    words = re.findall(r"[\w]+", str(query or "").casefold().replace("ё", "е"))
    if not words:
        return True
    corpus = normalize(haystack)
    return all(normalize(word) in corpus for word in words)


def stock_matches(item, query):
    medicine = item.medicine
    terms = [
        medicine.name, medicine.manufacturer or "", item.article_code,
        str(item.drawer.number) if "drawer" in item.__dict__ else "",
        " ".join(ingredient.name for ingredient in medicine.ingredients),
    ]
    return matches(" ".join(terms), query)


def drawer_matches(drawer, query):
    return matches(f"{drawer.number} {drawer.name} {drawer.location or ''}", query)
