"""Local OCR provenance import: never treats OCR output as verified stock.

Expected workbook sheet: "Все позиции" from the reviewed PharmDesk Excel.
Expected source archive: original "ЛС.zip" containing PNG/JPEG screenshots.
Both files are uploaded to the user's local application, never GitHub.
"""
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
import os
import unicodedata
from zipfile import ZipFile, BadZipFile

from openpyxl import load_workbook
from sqlalchemy import select

from .database import ROOT
from .models import InventoryEvent, ReviewRecord, SourceImage

MAX_WORKBOOK = 8 * 1024 * 1024
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024
MAX_TOTAL_IMAGES = 85 * 1024 * 1024
MAX_IMAGE_COUNT = 250
IMAGE_DIRECTORY = ROOT / "private_data" / "review_images"

HEADERS = [
    "Ящик", "Название ящика", "Код", "Наименование товара",
    "Производитель", "Статус сверки", "Исходное изображение",
    "Строка на снимке",
]


class ReviewImportError(ValueError):
    pass


def _clean(value, max_length, label, required=True):
    result = str(value if value is not None else "").strip()
    if required and not result:
        raise ReviewImportError(f"Отсутствует обязательное поле: {label}")
    if len(result) > max_length:
        raise ReviewImportError(f"Слишком длинное поле: {label}")
    return result


def _sku(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value if value is not None else "").strip()


def normalized_filename(value):
    name = unicodedata.normalize("NFC", PurePosixPath(str(value).replace("\\", "/")).name)
    if not name or len(name) > 180 or name in (".", ".."):
        raise ReviewImportError("Некорректное имя изображения.")
    return name


def image_key(value):
    return normalized_filename(value).casefold()


def parse_workbook(raw):
    if not raw or len(raw) > MAX_WORKBOOK:
        raise ReviewImportError("Нужен непустой XLSX-файл размером до 8 МБ.")
    try:
        book = load_workbook(BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise ReviewImportError("Не удалось открыть XLSX-файл.") from exc
    try:
        if "Все позиции" not in book.sheetnames:
            raise ReviewImportError('В книге отсутствует лист «Все позиции». Нужна исправленная полная книга.')
        sheet = book["Все позиции"]
        rows = sheet.iter_rows(values_only=True)
        headers = next(rows, None)
        if not headers:
            raise ReviewImportError("Лист пуст.")
        index = {str(x).strip(): n for n, x in enumerate(headers) if x is not None}
        missing = [name for name in HEADERS if name not in index]
        if missing:
            raise ReviewImportError("Отсутствуют столбцы: " + ", ".join(missing))
        parsed = []
        seen = set()
        for number, values in enumerate(rows, start=2):
            if number > 5000:
                raise ReviewImportError("Слишком много строк (максимум 4998).")
            if not any(x is not None for x in values):
                continue

            def cell(column):
                idx = index.get(column)
                return values[idx] if idx is not None and idx < len(values) else None

            try:
                drawer = int(cell("Ящик"))
                if drawer < 1 or drawer > 999999:
                    raise ValueError("Out of range")
            except (TypeError, ValueError) as exc:
                raise ReviewImportError(f"Строка {number}: неверный номер ящика.") from exc
            code = _clean(_sku(cell("Код")), 80, f"код строки {number}")
            name = _clean(cell("Наименование товара"), 350, f"наименование строки {number}")
            maker = _clean(cell("Производитель"), 300, "производитель", required=False)
            drawer_name = _clean(cell("Название ящика"), 200, "название ящика", required=False) or "Без названия"
            source = normalized_filename(cell("Исходное изображение"))
            if Path(source).suffix.casefold() not in (".png", ".jpg", ".jpeg"):
                raise ReviewImportError(f"Строка {number}: нет корректной ссылки на исходное изображение.")
            try:
                source_row = int(cell("Строка на снимке"))
                if source_row < 1 or source_row > 300:
                    raise ValueError("row")
            except (ValueError, TypeError) as exc:
                raise ReviewImportError(f"Строка {number}: неверная позиция на изображении.") from exc
            key = (drawer, code)
            if key in seen:
                raise ReviewImportError(f"Строка {number}: повтор кода {code} в ящике №{drawer}.")
            seen.add(key)
            status = _clean(cell("Статус сверки"), 120, "статус", required=False)
            note = _clean(cell("Замечания"), 2000, "замечания", required=False)
            original = _clean(cell("Прежнее название"), 350, "прежнее название", required=False)
            parsed.append({
                "drawer_number": drawer, "drawer_name": drawer_name,
                "article_code": code, "proposed_name": name,
                "proposed_manufacturer": maker, "source_file": source,
                "source_row": source_row, "ocr_status": status,
                "quality_notes": note, "original_name": original,
                "priority": "требует ручной проверки" in status.casefold(),
            })
        if not parsed:
            raise ReviewImportError("В книге нет записей для проверки.")
        return parsed
    finally:
        book.close()


def import_reviews(db, raw):
    parsed = parse_workbook(raw)
    existing = db.scalars(select(ReviewRecord)).all()
    by_key = {(x.drawer_number, x.article_code): x for x in existing}
    created = updated = kept = 0
    try:
        for entry in parsed:
            key = (entry["drawer_number"], entry["article_code"])
            record = by_key.get(key)
            if record is None:
                record = ReviewRecord(**entry)
                db.add(record)
                created += 1
            elif record.state != "pending" or record.checked_at is not None:
                kept += 1
            else:
                for field, value in entry.items():
                    setattr(record, field, value)
                record.updated_at = datetime.now(timezone.utc)
                updated += 1
        db.add(InventoryEvent(
            event_type="review_import",
            description=f"OCR review queue: {created} new, {updated} refreshed, {kept} manually reviewed kept."
        ))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "total_in_file": len(parsed), "high_priority": sum(x["priority"] for x in parsed),
        "created": created, "updated_pending": updated,
        "preserved_manual_decisions": kept,
        "notice": "Распознавание не считается ручной проверкой. Ассортимент не изменён."
    }


def _is_valid_image(name, binary):
    suffix = Path(name).suffix.casefold()
    if suffix == ".png":
        return binary.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in (".jpg", ".jpeg"):
        return binary.startswith(b"\xff\xd8\xff")
    return False


def prepare_images(raw):
    if not raw or len(raw) > MAX_ARCHIVE:
        raise ReviewImportError("Нужен ZIP-архив изображений до 32 МБ.")
    images = {}
    total = 0
    try:
        with ZipFile(BytesIO(raw)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if len(images) >= MAX_IMAGE_COUNT:
                    raise ReviewImportError("Слишком много изображений в архиве.")
                if info.flag_bits & 1:
                    raise ReviewImportError("Зашифрованный ZIP не поддерживается.")
                name = normalized_filename(info.filename)
                if name.startswith(".") or name.startswith("__"):
                    continue
                if Path(name).suffix.casefold() not in (".png", ".jpg", ".jpeg"):
                    continue
                if info.file_size > MAX_IMAGE or info.file_size < 100:
                    raise ReviewImportError(f"Недопустимый размер снимка: {name}.")
                total += info.file_size
                if total > MAX_TOTAL_IMAGES:
                    raise ReviewImportError("Слишком большой распакованный архив.")
                data = archive.read(info)
                if not _is_valid_image(name, data):
                    raise ReviewImportError(f"Повреждённое или неверное изображение: {name}.")
                key = image_key(name)
                if key in images:
                    raise ReviewImportError(f"В архиве повторяется имя изображения: {name}.")
                digest = sha256(data).hexdigest()
                storage_name = sha256(key.encode("utf-8")).hexdigest()[:32] + Path(name).suffix.lower()
                images[key] = (name, storage_name, digest, data)
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise ReviewImportError("Не удалось прочитать архив ZIP с изображениями.") from exc
    if not images:
        raise ReviewImportError("В архиве не найдены PNG/JPG-изображения.")
    return images


def import_images(db, raw, directory=None):
    images = prepare_images(raw)
    directory = Path(IMAGE_DIRECTORY if directory is None else directory)
    directory.mkdir(parents=True, exist_ok=True)
    existing = {x.name_key: x for x in db.scalars(select(SourceImage)).all()}
    created_files = []
    added = unchanged = conflicts = 0
    try:
        for key, (name, disk_name, digest, data) in images.items():
            record = existing.get(key)
            if record:
                if record.sha256 == digest:
                    unchanged += 1
                else:
                    conflicts += 1
                continue
            path = directory / disk_name
            if path.exists():
                raise ReviewImportError(f"Конфликт имени файла: {name}.")
            path.write_bytes(data)
            created_files.append(path)
            db.add(SourceImage(
                original_filename=name, name_key=key,
                stored_filename=disk_name, sha256=digest
            ))
            added += 1
        db.add(InventoryEvent(
            event_type="review_images",
            description=f"Review evidence: {added} photos added, {unchanged} present, {conflicts} name conflicts."
        ))
        db.commit()
    except Exception:
        db.rollback()
        for path in created_files:
            path.unlink(missing_ok=True)
        raise
    return {
        "in_archive": len(images), "added": added, "unchanged": unchanged,
        "conflicts": conflicts, "notice": "Фотографии сохранены только на этом компьютере, не в GitHub."
    }
