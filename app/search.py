"""Conservative, deterministic Russian inventory search.

Exact code/text matches ALWAYS take priority. Typo-tolerant candidates
appear only if no exact matches exist, are labeled as approximate,
and are NEVER used to infer a medicine's active ingredients or identity.
"""
import re

WORD_RE = re.compile(r"[\w]+", re.UNICODE)
CYRILLIC_RE = re.compile(r"^[а-я]+$", re.UNICODE)


def normalize(value):
    return re.sub(r"[^\w]+", "", str(value or "").casefold().replace("ё", "е"))


def tokens(value):
    return WORD_RE.findall(str(value or "").casefold().replace("ё", "е"))


def matches(haystack, query):
    words = tokens(query)
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


def bounded_distance(left, right, maximum=1):
    """Edit distance bounded by maximum; returns maximum+1 when too distant."""
    if left == right:
        return 0
    if abs(len(left) - len(right)) > maximum:
        return maximum + 1
    prev = list(range(len(right) + 1))
    for i, char in enumerate(left, 1):
        curr = [i]
        row_min = i
        for j, other in enumerate(right, 1):
            value = min(curr[-1] + 1, prev[j] + 1, prev[j - 1] + (char != other))
            curr.append(value)
            row_min = min(row_min, value)
        if row_min > maximum:
            return maximum + 1
        prev = curr
    return prev[-1]


def _match_approximate(item, query):
    """Only one Cyrillic typo in drug title, never codes or manufacturers.

    No fuzzy match for short words (<5 letters), digits or Latin words.
    A mistaken medicine must remain visibly a *candidate*, not a correction.
    """
    qwords = tokens(query)
    if not qwords or not all(CYRILLIC_RE.fullmatch(word) and len(word) >= 5 for word in qwords):
        return False
    name_words = tokens(item.medicine.name)
    for query_word in qwords:
        if not any(
            CYRILLIC_RE.fullmatch(name_word)
            and len(name_word) >= 5
            and query_word[0] == name_word[0]
            and abs(len(query_word) - len(name_word)) <= 1
            and bounded_distance(query_word, name_word, 1) <= 1
            for name_word in name_words
        ):
            return False
    return True


def _exact_rank(item, query):
    q = normalize(query)
    name = normalize(item.medicine.name)
    code = normalize(item.article_code)
    if q == code:
        return 0
    if q == name:
        return 1
    if name.startswith(q):
        return 2
    if q in name:
        return 3
    return 4


def search_stock(items, query):
    """Return (items, approximate) preserving exact-first behavior.

    Approximate search is only attempted if exact results are empty and
    remains limited to spelling of full medicine-name Cyrillic words.
    """
    q = str(query or "").strip()
    if not q:
        return list(items), False
    exact = [item for item in items if stock_matches(item, q)]
    if exact:
        exact.sort(key=lambda item: (
            _exact_rank(item, q), normalize(item.medicine.name),
            item.drawer.number, item.article_code, item.id
        ))
        return exact, False
    approximate = [item for item in items if _match_approximate(item, q)]
    approximate.sort(key=lambda item: (
        normalize(item.medicine.name), item.drawer.number, item.article_code, item.id
    ))
    return approximate, bool(approximate)
