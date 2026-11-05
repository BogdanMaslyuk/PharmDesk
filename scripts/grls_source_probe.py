"""Network reachability check, no registry content saved and no user data used."""
import ssl
import urllib.request
import urllib.error
from datetime import datetime
URLS = (
    "https://grls.minzdrav.gov.ru/grls.aspx",
    "https://esnsi.gosuslugi.ru/classifiers/26361/data?p=1",
    "https://grls.pharm-portal.ru/grls",
)
for url in URLS:
    print("CHECK", url, flush=True)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 PharmDesk data source connectivity",
            "Accept": "text/html,application/json,*/*",
        })
        with urllib.request.urlopen(req, timeout=14) as result:
            print("RESULT", result.status, result.geturl(),
                  result.headers.get("content-type", ""),
                  result.headers.get("content-length", ""), flush=True)
            print("SAMPLE", result.read(300).decode("utf-8", errors="replace").replace("\n", " ")[:300], flush=True)
    except Exception as exc:
        print("ERROR", type(exc).__name__, str(exc)[:350], flush=True)
