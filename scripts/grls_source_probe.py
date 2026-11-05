"""Probe official GRLS dynamically generated ZIP link, read only first bytes."""
import http.cookiejar
import re
import urllib.request
from urllib.parse import urljoin

page_url="https://grls.minzdrav.gov.ru/grls.aspx"
opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
page=opener.open(urllib.request.Request(page_url,headers={"User-Agent":"Mozilla/5.0"}),timeout=16).read().decode("utf-8","replace")
result=re.search(r"GetGRLS\.ashx\?FileGUID=[0-9a-f-]+&amp;UserReq=\d+|GetGRLS\.ashx\?FileGUID=[0-9a-f-]+&UserReq=\d+",page,re.I)
print("OFFICIAL_LINK_FOUND",bool(result),flush=True)
if result:
 url=urljoin(page_url,result.group().replace("&amp;","&"))
 print("OFFICIAL_ENDPOINT",url,flush=True)
 req=urllib.request.Request(url,headers={
   "User-Agent":"Mozilla/5.0",
   "Referer":page_url,
   "Accept":"application/zip,application/octet-stream,*/*"
 })
 try:
  with opener.open(req,timeout=18) as response:
   head=response.read(32)
   print("ARCHIVE_STATUS",response.status,"MIME",response.headers.get("Content-Type"),
      "BYTES",response.headers.get("Content-Length"),
      "MAGIC",head[:8].hex(), "ZIP",head.startswith(b"PK"),flush=True)
 except Exception as e:
  print("ARCHIVE_ERROR",type(e).__name__,str(e)[:400],flush=True)
