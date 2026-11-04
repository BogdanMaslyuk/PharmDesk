"""Pure progress calculations for the manual OCR review queue.

No database writes; counts are based solely on real review_records.
"""
from collections import defaultdict


def drawer_progress(records):
    stats = defaultdict(lambda: {
        "drawer": 0, "total": 0, "pending": 0,
        "priority": 0, "approved": 0, "excluded": 0,
        "done": 0, "percent": 0,
    })
    for record in records:
        number = record.drawer_number
        row = stats[number]
        row["drawer"] = number
        row["total"] += 1
        state = record.state
        if state == "approved":
            row["approved"] += 1
        elif state == "excluded":
            row["excluded"] += 1
        else:
            row["pending"] += 1
            if record.priority:
                row["priority"] += 1
    for row in stats.values():
        row["done"] = row["approved"] + row["excluded"]
        row["percent"] = round(100 * row["done"] / row["total"]) if row["total"] else 0
    return [stats[key] for key in sorted(stats)]
