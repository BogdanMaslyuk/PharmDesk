"""Inspect public GRLS page for official downloadable dataset links (no file saved)."""
import html
import re
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from urllib.parse import urljoin

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("a", "form", "script", "link", "button", "input"):
            self.links.append((tag, a))

url = "https://grls.minzdrav.gov.ru/grls.aspx"
response = urlopen(Request(url, headers={"User-Agent":"Mozilla/5.0"}), timeout=16)
page = response.read().decode("utf-8", "replace")
print("PAGE", response.status, len(page), "bytes")
parser = Links()
parser.feed(page)
for tag, attrs in parser.links:
    string = " ".join(str(x) for x in attrs.values())
    if re.search(r"zip|download|export|xls|выгруз|скач|архив|GetFile|FileDownload", string, re.I):
        print("NODE",tag," ".join(f"{k}={v}" for k,v in attrs.items())[:550])
for i, line in enumerate(page.splitlines()):
    if re.search(r"zip|download|export|xls|выгруз|скач|архив|GetFile|FileDownload", line, re.I):
        print("TEXT",i,re.sub(r"\s+"," ",html.unescape(re.sub(r"<[^>]*>"," ",line)))[:550])
