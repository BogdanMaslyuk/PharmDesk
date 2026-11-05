"""Bounded parsing of user-downloaded official GRLS ZIP/XLSX exports.

Source format: Minzdrav technical requirements published 2026-09-01,
describing grlsYYYY-MM-DD-1-[status].xlsx and 14 column headings.
https://minzdrav.gov.ru/special/documents/9892-opisanie-tehnicheskih-trebovaniy-k-vzaimodeystviyu-informatsionnoy-sistemy-posrednicheskoy-tsifrovoy-platformy-s-gosudarstvennoy-informatsionnoy-sistemoy-reestrom

Import never certifies provenance or active registration status. The local
user supplies bytes and attests to the source; records remain separate
from manually curated drugs, inventory and therapeutic claims.
"""
from collections import Counter
from datetime import date
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
import re
from zipfile import BadZipFile, ZipFile, LargeZipFile

from openpyxl import load_workbook
from sqlalchemy import select

from .catalog_sources import CatalogValidationError
from .models import GrlsImportBatch, GrlsImportRow, InventoryEvent
from .search import normalize

MAX_ZIP_BYTES = 96 * 1024 * 1024
MAX_EXTRACTED_XLSX = 384 * 1024 * 1024
MAX_INNER_XML_SIZE = 700 * 1024 * 1024
MAX_BOOKS = 24
MAX_RECORDS = 250000
MAX_PER_BOOK = 150000
MAX_HEADER_SCAN = 18

# Machine-documented categories and historical archive categories.
KNOWN_STATUSES = {
    "действующий": "Действующий",
    "измененный": "Изменённый",
    "действует_на_подтверждении_государственной_регистрации": "Действует на подтверждении регистрации",
    "действует_в_иностранных_упаковках": "Действует в иностранных упаковках",
    "выдано_по_правилам_еаэс": "Выдано по правилам ЕАЭС",
    "исключенный": "Исключённый",
    "исключённый": "Исключённый",
    "истекший": "Истёкший",
    "истёкший": "Истёкший",
    "приостановлено_применение": "Приостановлено применение",
}
FILE_RE = re.compile(
    r"^grls(?P<stamp>\d{4}-\d{2}-\d{2})-\d+-(?P<status>.+)\.xlsx$",
    re.IGNORECASE
)
COL_ALIASES = {
    "registration_number": (
        "номер регистрационного удостоверения",
        "регистрационный номер",
    ),
    "trade_name": (
        "торговое наименование лекарственного препарата",
        "торговое наименование",
    ),
    "inn_raw": (
        "международное непатентованное или химическое наименование",
        "международное непатентованное наименование",
        "мнн",
    ),
    "release_forms_raw": (
        "формы выпуска",
        "форма выпуска",
    ),
    "certificate_holder": (
        "юридическое лицо на имя которого выдано регистрационное удостоверение",
        "держатель регистрационного удостоверения",
    ),
}
NEEDED_COLUMNS = {"registration_number", "trade_name", "release_forms_raw", "inn_raw"}


class GrlsImportError(ValueError):
    pass


def header_key(text):
    value = str(text or "").casefold().replace("ё", "е").replace("\xa0", " ")
    return " ".join(re.findall(r"[а-яa-z0-9]+", value))


def identify_header(values):
    result = {}
    for index, value in enumerate(values):
        h = header_key(value)
        for field, aliases in COL_ALIASES.items():
            if field not in result and h in (header_key(a) for a in aliases):
                result[field] = index
    return result if NEEDED_COLUMNS <= result.keys() else None


def cell_text(value, max_length, context):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    value = str(value).strip()
    if len(value) > max_length:
        raise GrlsImportError(f"{context}: значение превышает {max_length} символов.")
    if any(ord(ch) < 32 and ch not in "\r\n\t" for ch in value):
        raise GrlsImportError(f"{context}: недопустимые символы.")
    return value


def parse_file_name(path):
    # A ZIP entry may be inside a folder; never extract its path to disk.
    name = PurePosixPath(path.replace("\\", "/")).name
    matched = FILE_RE.fullmatch(name)
    if not matched:
        raise GrlsImportError(
            f"Неизвестное имя книги «{name}». Нужны файлы grlsГГГГ-ММ-ДД-1-Статус.xlsx."
        )
    try:
        snapshot = date.fromisoformat(matched["stamp"])
    except ValueError as exc:
        raise GrlsImportError("Недопустимая дата в имени книги.") from exc
    if snapshot > date.today():
        raise GrlsImportError("Дата выгрузки находится в будущем; файл отклонён.")
    key = matched["status"].casefold().replace(" ", "_").replace("ё", "е")
    if key not in KNOWN_STATUSES:
        raise GrlsImportError(
            f"Незнакомый статус выгрузки «{matched['status']}». Не будем угадывать его значение."
        )
    return name, snapshot, KNOWN_STATUSES[key]


def check_inner_xlsx(blob, source_file):
    try:
        with ZipFile(BytesIO(blob)) as workbook_zip:
            total = sum(info.file_size for info in workbook_zip.infolist())
            if total > MAX_INNER_XML_SIZE:
                raise GrlsImportError(
                    f"Книга «{source_file}» слишком велика после распаковки XML."
                )
            for info in workbook_zip.infolist():
                if info.file_size < 0 or info.flag_bits & 1:
                    raise GrlsImportError(f"Некорректная или зашифрованная книга: {source_file}")
    except (BadZipFile, LargeZipFile, OSError, RuntimeError) as exc:
        raise GrlsImportError(f"Не удалось прочитать книгу «{source_file}».") from exc


def parse_grls_archive(raw):
    if not raw or len(raw) > MAX_ZIP_BYTES:
        raise GrlsImportError("Выберите ZIP-архив выгрузки ГРЛС размером не более 96 МБ.")
    digest = sha256(raw).hexdigest()
    entries = []
    status_counts = Counter()
    snapshots = set()
    source_files = []
    total_size = 0
    total_rows = 0
    seen_source = set()
    try:
        with ZipFile(BytesIO(raw)) as package:
            infos = [info for info in package.infolist() if not info.is_dir()]
            xlsx = [info for info in infos if info.filename.casefold().endswith(".xlsx")]
            # Never silently skip another spreadsheet, which could make the
            # displayed snapshot incomplete or hide an unsupported status.
            if len(xlsx) != len(infos) or not xlsx or len(xlsx) > MAX_BOOKS:
                raise GrlsImportError(
                    "Архив должен содержать 1–24 книги XLSX с описанными статусами ГРЛС, без посторонних файлов."
                )
            for info in xlsx:
                name, snapshot, status = parse_file_name(info.filename)
                key = name.casefold()
                if key in seen_source:
                    raise GrlsImportError("Повторяющееся имя книги в архиве.")
                seen_source.add(key)
                snapshots.add(snapshot)
                if len(snapshots) > 1:
                    raise GrlsImportError("В архиве разные даты выгрузок. Используйте один снимок реестра.")
                if info.flag_bits & 1 or info.file_size > MAX_EXTRACTED_XLSX:
                    raise GrlsImportError(f"Зашифрованная или слишком большая книга: {name}")
                total_size += info.file_size
                if total_size > MAX_EXTRACTED_XLSX:
                    raise GrlsImportError("Слишком большой распакованный архив XLSX.")
                workbook_bytes = package.read(info)
                check_inner_xlsx(workbook_bytes, name)
                try:
                    book = load_workbook(BytesIO(workbook_bytes), read_only=True, data_only=True)
                except Exception as exc:
                    raise GrlsImportError(f"Не удалось открыть книгу Excel «{name}».") from exc
                try:
                    if len(book.worksheets) != 1:
                        raise GrlsImportError(
                            f"Книга «{name}» содержит не один лист; проверьте формат выгрузки."
                        )
                    sheet = book.worksheets[0]
                    it = sheet.iter_rows(values_only=True)
                    columns = None
                    header_line = None
                    for row_no, values in enumerate(it, start=1):
                        columns = identify_header(values)
                        if columns:
                            header_line = row_no
                            break
                        if row_no >= MAX_HEADER_SCAN:
                            raise GrlsImportError(
                                f"В книге «{name}» не найдены документированные заголовки ГРЛС."
                            )
                    if not columns:
                        raise GrlsImportError(f"В книге «{name}» нет нужных столбцов ГРЛС.")
                    for source_row, values in enumerate(it, start=header_line + 1):
                        if source_row - header_line > MAX_PER_BOOK:
                            raise GrlsImportError(f"В книге «{name}» больше 150 000 строк.")
                        if not any(value is not None and str(value).strip() for value in values):
                            continue

                        def cell(field, max_len):
                            index = columns.get(field)
                            raw_value = values[index] if index is not None and index < len(values) else None
                            return cell_text(raw_value, max_len, f"{name}, строка {source_row}")

                        reg = cell("registration_number", 140)
                        trade = cell("trade_name", 350)
                        inn = cell("inn_raw", 1800)
                        forms = cell("release_forms_raw", 32767)
                        holder = cell("certificate_holder", 600)
                        # The real official XLSX contains a single-cell category
                        # banner immediately below its header, and a one-cell
                        # timestamp footer (e.g. 05.11.2026 05:00:00).
                        # They are not medicines and must not become false rows.
                        if not trade and not forms and (
                            source_row == header_line + 1
                            or re.fullmatch(r"\\d{2}\\.\\d{2}\\.\\d{4}\\s+\\d{2}:\\d{2}:\\d{2}", reg)
                        ):
                            continue
                        if not reg or not trade or not forms:
                            # Never invent missing registry identity fields.
                            raise GrlsImportError(
                                f"Книга «{name}», строка {source_row}: отсутствует номер регистрации, "
                                "торговое название либо формы выпуска."
                            )
                        entries.append({
                            "source_workbook": name, "source_row": source_row,
                            "status_from_filename": status,
                            "registration_number": reg,
                            "registration_key": normalize(reg),
                            "trade_name": trade, "trade_key": normalize(trade),
                            "inn_raw": inn, "release_forms_raw": forms,
                            "certificate_holder": holder,
                        })
                        total_rows += 1
                        status_counts[status] += 1
                        if total_rows > MAX_RECORDS:
                            raise GrlsImportError("В архиве более 250 000 товарных строк. Уменьшите объём выгрузки.")
                finally:
                    book.close()
                source_files.append(name)
    except (BadZipFile, LargeZipFile, OSError, RuntimeError) as exc:
        raise GrlsImportError("Архив ZIP повреждён или не читается.") from exc
    if not entries:
        raise GrlsImportError("В выгрузке отсутствуют регистрационные записи.")
    return {
        "sha256": digest, "snapshot_on": next(iter(snapshots)),
        "source_files": source_files, "status_counts": dict(status_counts),
        "count": total_rows, "sample": entries[:15], "rows": entries,
    }


def store_grls_batch(db, parsed, original_filename):
    current = db.scalar(select(GrlsImportBatch).where(GrlsImportBatch.sha256 == parsed["sha256"]))
    if current:
        return {"already_imported": True, "batch_id": current.id, "rows": current.row_count}
    from pathlib import PurePath
    safe_name = PurePath(original_filename or "grls.zip").name
    if len(safe_name) > 180:
        safe_name = safe_name[:180]
    batch = GrlsImportBatch(
        sha256=parsed["sha256"], archive_name=safe_name,
        snapshot_on=parsed["snapshot_on"], row_count=parsed["count"],
    )
    try:
        db.add(batch)
        db.flush()
        for number, row in enumerate(parsed["rows"], start=1):
            db.add(GrlsImportRow(batch_id=batch.id, **row))
            if number % 1000 == 0:
                db.flush()
        db.add(InventoryEvent(
            event_type="grls_snapshot_import",
            description=f"GRLS snapshot #{batch.id}: {parsed['count']} imported rows, source authenticity NOT checked."
        ))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"already_imported": False, "batch_id": batch.id, "rows": parsed["count"]}
