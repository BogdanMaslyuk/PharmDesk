"""Bounded direct download of the public GRLS ZIP from its live official page.

Never accept arbitrary target URLs from callers. HTTPS+host is pinned; a
redirect to another host is a hard error. A public file downloaded from the
Ministry does not prove the current legal status of each individual record.
"""
from http.cookiejar import CookieJar
import html
import re
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, HTTPCookieProcessor, build_opener
from urllib.error import HTTPError, URLError
from zipfile import ZipFile, BadZipFile
from io import BytesIO

from .grls_import import MAX_ZIP_BYTES, GrlsImportError

OFFICIAL_HOST = "grls.minzdrav.gov.ru"
OFFICIAL_PAGE = "https://" + OFFICIAL_HOST + "/grls.aspx"
ARCHIVE_CONTROL = re.compile(
    r"GetGRLS\.ashx\?FileGUID=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:&amp;|&)UserReq=(\d{1,16})",
    re.IGNORECASE
)


def _is_official(url):
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == OFFICIAL_HOST
        and parsed.username is None and parsed.password is None
        and parsed.port is None
    )


def fetch_official_grls(*, opener=None):
    """Download public official archive into memory, with strict 96 MiB limit.

    Returns (ZIP bytes, metadata). Does not update an app database.
    """
    opener = opener or build_opener(HTTPCookieProcessor(CookieJar()))
    try:
        page_request = Request(OFFICIAL_PAGE, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(page_request, timeout=25) as response:
            if response.status != 200 or not _is_official(response.geturl()):
                raise GrlsImportError("Страница ГРЛС вернула неверный адрес или код ответа.")
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise GrlsImportError("Страница ГРЛС неожиданно велика. Автоматическая загрузка отменена.")
        control = ARCHIVE_CONTROL.search(body.decode("utf-8", errors="replace"))
        if not control:
            raise GrlsImportError(
                "Официальная страница не содержит ссылки на ZIP в ожидаемом формате. "
                "Возможно, Минздрав изменил интерфейс загрузки."
            )
        target = urljoin(
            OFFICIAL_PAGE, "GetGRLS.ashx?FileGUID=" + control.group(1)
            + "&UserReq=" + control.group(2)
        )
        if not _is_official(target):
            raise GrlsImportError("Адрес файла выходит за пределы официального сайта.")
        file_request = Request(target, headers={
            "User-Agent": "Mozilla/5.0", "Referer": OFFICIAL_PAGE,
            "Accept": "application/zip,application/octet-stream,*/*"
        })
        with opener.open(file_request, timeout=60) as response:
            if response.status != 200 or not _is_official(response.geturl()):
                raise GrlsImportError("Загрузка была перенаправлена на неподтверждённый адрес.")
            total = 0
            blocks = []
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > MAX_ZIP_BYTES:
                    raise GrlsImportError("Загружаемый ZIP превышает безопасный предел 96 МБ.")
                blocks.append(block)
        data = b"".join(blocks)
        if len(data) < 1000 or not data.startswith(b"PK\x03\x04"):
            raise GrlsImportError("Сайт вернул не ZIP-архив. Запись в базу не выполнялась.")
        try:
            with ZipFile(BytesIO(data)) as zip_file:
                names = [x.filename for x in zip_file.infolist() if not x.is_dir()]
                if not names or not all(x.lower().endswith(".xlsx") for x in names):
                    raise GrlsImportError("Полученный ZIP не содержит корректного набора XLSX ГРЛС.")
                # CRC is verified again while the parser reads the workbook.
                if zip_file.testzip() is not None:
                    raise GrlsImportError("Контроль целостности ZIP не прошёл.")
        except BadZipFile as exc:
            raise GrlsImportError("ZIP ГРЛС повреждён.") from exc
        return data, {"page": OFFICIAL_PAGE, "downloaded": True, "xlsx_files": len(names)}
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise GrlsImportError(
            "Не удалось получить ZIP с официального сайта ГРЛС. "
            "Возможна блокировка сети или временная недоступность портала. "
            "Используйте ручную загрузку архива, если он получен другим способом."
        ) from exc
