#!/usr/bin/env python3
"""List what the schema sync (run at server start) would add to a PostgreSQL database, without adding it.

    INAZINA_PG_DSN=postgresql:///inazina_copy python tools/pg_schema_preview.py

Point it at a COPY (pg_restore of a pg_dump). It refuses the live `inazina` database unless --allow-live is given,
because even a dry run takes the sync's advisory lock. Prints every new table, column, index, view, constraint and
trigger name and exits 1 when the sync reports errors.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("INAZINA_DB_BACKEND", "sqlite-schema")      # only so the schema definition can be generated


def main():
    from psycopg.conninfo import conninfo_to_dict
    dsn = os.environ.get("INAZINA_PG_DSN", "")
    name = conninfo_to_dict(dsn).get("dbname", "") if dsn else ""
    if not dsn or (name == "inazina" and "--allow-live" not in sys.argv):
        raise SystemExit("INAZINA_PG_DSN must name a copy of the database (the live `inazina` needs --allow-live)")
    from database import pg_schema_sync
    summary = pg_schema_sync.ensure_current(dsn, dry_run=True, force=True) or {}
    print(json.dumps({kind: items for kind, items in summary.items()}, ensure_ascii=False, indent=2))
    return 1 if summary.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
