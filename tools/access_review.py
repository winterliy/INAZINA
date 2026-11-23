#!/usr/bin/env python3
"""Print an access-review record (admin list, audit summary) from a read-only look at the database.

    python tools/access_review.py [--dsn postgresql:///inazina]        # prints one JSON object
    python tools/access_review.py --append                              # also appends it to docs/m8_records/drills.json

Read-only: it only SELECTs. --append edits the records file in the repository, never the database.
Procedure: docs/m8_access_review_procedure.md.
"""
import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RECORDS = ROOT / "docs" / "m8_records" / "drills.json"


def collect(dsn, today=None):
    import psycopg
    from web_api.server import bootstrap_admin_ids
    bootstrap = bootstrap_admin_ids()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.read_only = True
        admins = [row[0] for row in conn.execute("SELECT user_id FROM users WHERE role='admin' ORDER BY user_id")]
        total, first, last = conn.execute("SELECT count(*), min(recorded_epoch), max(recorded_epoch) FROM admin_audit").fetchone()
        top = conn.execute("SELECT action, count(*) FROM admin_audit WHERE recorded_epoch > %s GROUP BY 1 ORDER BY 2 DESC LIMIT 8",
                           (time.time() - 30 * 86400,)).fetchall()
    day = lambda epoch: time.strftime("%Y-%m-%d", time.localtime(epoch)) if epoch else None
    return {"date": (today or date.today()).isoformat(), "scope": "운영 DB 읽기 전용 조회",
            "admins": [{"user_id_tail": str(user)[-4:], "role": "admin", "bootstrap_id": str(user) in bootstrap} for user in admins],
            "admin_count": len(admins), "audit_rows": int(total), "audit_range": f"{day(first)} ~ {day(last)}",
            "top_actions_30d": {action: int(count) for action, count in top},
            "findings": ["검토자가 채운다: 더 이상 필요 없는 관리자, 예상 밖 행위자, 감사 공백."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dsn", default=os.environ.get("INAZINA_PG_DSN", "postgresql:///inazina"))
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args()
    record = collect(args.dsn)
    if args.append:
        data = json.loads(RECORDS.read_text(encoding="utf-8"))
        data["access_reviews"].append(record)
        RECORDS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
