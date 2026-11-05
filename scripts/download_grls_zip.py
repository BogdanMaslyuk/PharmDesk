"""Download official public GRLS ZIP from the dynamic link on the ministry site.

GitHub Actions fallback when a user's Windows connection cannot access GRLS.
Only original public ZIP and SHA-256 manifest are archived, no PharmDesk stock.
"""
from datetime import datetime, timezone
from hashlib import sha256
import html
import json
import os
from pathlib import Path
import re
import urllib.request
import urllib.parse
import http.cookiejar
from zipfile import ZipFile, BadZipFile

SOURCE = "https://grls.minzdrav.gov.ru/grls.aspx"
MAX_BYTES = 96 * 1024 * 1024
OUT = Path("grls_download")
OUT.mkdir(exist_ok=True)

opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)
request = urllib.request.Request(SOURCE, headers={"User-Agent": "Mozilla/5.0"})
with opener.open(request, timeout=25) as page:
    if page.status != 200 or urllib.parse.urlsplit(page.geturl()).hostname != "grls.minzdrav.gov.ru":
        raise RuntimeError("Official GRLS page is unavailable")
    body = page.read(1_000_000).decode("utf-8", errors="replace")
target = re.search(
    r"GetGRLS\.ashx\?FileGUID=[0-9a-f-]+(?:&amp;|&)UserReq=\d+", body, re.I
)
if not target:
    raise RuntimeError("Official GRLS download control not found; do not invent a URL")
download = urllib.parse.urljoin(SOURCE, html.unescape(target.group(0)))
if urllib.parse.urlsplit(download).hostname != "grls.minzdrav.gov.ru":
    raise RuntimeError("Download would leave official host")
req = urllib.request.Request(download, headers={
    "User-Agent": "Mozilla/5.0", "Referer": SOURCE,
    "Accept": "application/zip,application/octet-stream,*/*"
})
output = OUT / "official-grls.zip"
total = 0
digest = sha256()
with opener.open(req, timeout=45) as response:
    if response.status != 200 or urllib.parse.urlsplit(response.geturl()).hostname != "grls.minzdrav.gov.ru":
        raise RuntimeError("Untrusted final download URL")
    with output.open("wb") as file:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_BYTES:
                raise RuntimeError("GRLS ZIP exceeded 96 MiB limit")
            file.write(block)
            digest.update(block)
if total < 1000 or output.open("rb").read(4) != b"PK\x03\x04":
    raise RuntimeError("GRLS official endpoint did not return a ZIP file")
try:
    with ZipFile(output) as package:
        files = [x.filename for x in package.infolist() if not x.is_dir()]
        xlsx = [x for x in files if x.lower().endswith(".xlsx")]
        if not xlsx:
            raise RuntimeError("No XLSX files inside GRLS ZIP")
        failed = package.testzip()
        if failed:
            raise RuntimeError(f"Damaged ZIP entry {failed}")
except BadZipFile as error:
    raise RuntimeError("GRLS did not provide a valid archive") from error

manifest = {
    "source": SOURCE,
    "direct_file_url": download,
    "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
    "bytes": total,
    "sha256": digest.hexdigest(),
    "xlsx_files": xlsx,
    "note": "Public ministry export; authenticity not digitally verified. User should confirm date and status.",
}
(OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print("SUCCESS", "bytes",total,"xlsx",len(xlsx),"sha256",digest.hexdigest(),flush=True)
for name in xlsx:
    print("XLSX",name,flush=True)
