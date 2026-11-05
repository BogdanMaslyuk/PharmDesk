"""Inspect public GRLS download controls, never save source content."""
import re
from html.parser import HTMLParser
from urllib.request import Request, urlopen

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
print("LINKS",len(parser.links))
for tag,attrs in parser.links:
    if tag in ("a","form","script","link"):
        print("NODE",tag," ".join(f"{k}={v}" for k,v in attrs.items())[:300])
for i,line in enumerate(page.splitlines()):
    if re.search(r"zip|download|export|xls|выгруз|скач|архив|GetFile|FileDownload|Reestr|Upload|File",line,re.I):
        print("RAW",i,line.strip()[:450])
