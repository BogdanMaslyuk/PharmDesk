"""Live acceptance check of a real GRLS ZIP on the official site.

No PharmDesk user data, database or downloaded archive is persisted.
"""
from app.grls_fetch import fetch_official_grls
from app.grls_import import parse_grls_archive
raw, meta = fetch_official_grls()
parsed = parse_grls_archive(raw)
print("OFFICIAL_ARCHIVE_DATE", parsed["snapshot_on"], flush=True)
print("RECORDS", parsed["count"], "MISSING_FORMS", parsed["missing_forms"], flush=True)
print("FILES", len(parsed["source_files"]), flush=True)
for status, count in parsed["status_counts"].items():
    print("STATUS",status,count,flush=True)
assert parsed["count"] >= 100000, "Incomplete official GRLS export"
assert 7 <= len(parsed["source_files"]) <= 24, "Unexpected archive categories"
assert parsed["missing_forms"] >= 0
print("LIVE_GRLS_IMPORT_VALIDATED",flush=True)
