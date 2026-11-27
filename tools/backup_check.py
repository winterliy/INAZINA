#!/usr/bin/env python3
"""Exit 1 when the newest backup in database/backups is older than N hours (default 48) or missing. Read-only: never makes a backup.

    python tools/backup_check.py [--max-hours 48]

For the periodic check in docs/m8_prod_migration_runbook.md. The repository cannot make backups happen on a schedule; this only
tells you when one has not.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-hours", type=float, default=None)
    args = parser.parse_args()
    from web_api.controls_overview import backup_status
    status = backup_status(max_age_hours=args.max_hours)
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
