"""Source allowlist and validated manual transcription for official drug records.

This module makes NO HTTP requests. A permitted URL is not evidence that the
contents, registration status, or the transcribed medicine details are correct.
"""
from hashlib import sha256
from urllib.parse import urlsplit
import re

from .search import normalize

OFFICIAL_HOSTS = {
    "grls.rosminzdrav.ru": "ГРЛС Минздрава России",
    "pharma.eaeunion.org": "Реестр лекарственных средств ЕАЭС",
}
LEGACY_HOSTS = {"grls.minzdrav.gov.ru": "Прежний адрес, сохранённый для старых заметок"}
SOURCE_LANDING = {
    "grls.rosminzdrav.ru": ("/", "/default.aspx", "/grls.aspx"),
    "grls.minzdrav.gov.ru": ("/",),
    "pharma.eaeunion.org": ("/", "/pharma/", "/pharma/registers/", "/pharma/registers/26/ru/"),
}


class CatalogValidationError(ValueError):
    pass


def safe_official_url(raw, *, specific=True, allow_legacy=False):
    url = str(raw or "").strip()
    if not url or len(url) > 800:
        raise CatalogValidationError("Укажите ссылку на официальный источник, не длиннее 800 символов.")
    if re.search(r"[\x00-\x20\x7f]", url):
        raise CatalogValidationError("Ссылка содержит недопустимые символы.")
    try:
        parsed = urlsplit(url)
        supplied_port = parsed.port
    except ValueError as exc:
        raise CatalogValidationError("Недопустимый адрес официального источника.") from exc
    allowed = {**OFFICIAL_HOSTS, **(LEGACY_HOSTS if allow_legacy else {})}
    if (parsed.scheme.casefold() != "https" or parsed.username or parsed.password
            or supplied_port is not None or parsed.hostname not in allowed):
        raise CatalogValidationError(
            "Допустимы только адреса ГРЛС Минздрава или официального реестра ЕАЭС (HTTPS)."
        )
    if specific and parsed.path.casefold() in SOURCE_LANDING.get(parsed.hostname, ()) and not parsed.query:
        raise CatalogValidationError("Нужна ссылка на конкретную запись или инструкцию, не на главную страницу реестра.")
    return url, allowed[parsed.hostname]


def check_field(value, *, label, limit, required=True):
    value = str(value or "").strip()
    if required and not value:
        raise CatalogValidationError("Заполните поле «" + label + "».")
    if len(value) > limit:
        raise CatalogValidationError("Поле «" + label + "» слишком длинное.")
    if any(ord(x) < 32 and x not in "\t\n" for x in value):
        raise CatalogValidationError("В поле «" + label + "» недопустимые символы.")
    return value


def parse_substance_names(raw):
    """One plain drug-substance name per line; never parse doses from free text."""
    cleaned = check_field(raw, label="Действующие вещества", limit=1200)
    parts = [part.strip() for part in cleaned.splitlines() if part.strip()]
    if not 1 <= len(parts) <= 10:
        raise CatalogValidationError("Укажите от одного до десяти действующих веществ, по одному на строку.")
    normalized = set()
    for part in parts:
        if len(part) > 200 or not re.search(r"[a-zа-яё]", part, re.IGNORECASE):
            raise CatalogValidationError("Укажите названия веществ, не более 200 символов каждое.")
        key = normalize(part)
        if not key or key in normalized:
            raise CatalogValidationError("Повтор или пустое название действующего вещества.")
        normalized.add(key)
    return parts


def registry_identity_key(registration_number, trade_name, dosage_form, strength_text, manufacturer):
    fields = (registration_number, trade_name, dosage_form, strength_text, manufacturer)
    canonical = "\u001f".join(normalize(field) for field in fields)
    return sha256(canonical.encode("utf-8")).hexdigest()
