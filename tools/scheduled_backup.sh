#!/bin/bash
# Production backup job: dump, optionally verify the restore, and check freshness. NOT installed anywhere by this repository;
# see docs/m8_backup_schedule.md for how a person can schedule it (the launchd plist there is an example only).
#
#   tools/scheduled_backup.sh             # dump + freshness check
#   tools/scheduled_backup.sh --verify    # also restore the new dump into a temporary database and check it
#
# The production database is spelled out below (rule 7: without INAZINA_PG_DSN the backup tool dumps the dev database).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
export INAZINA_PG_DSN="postgresql:///inazina"
mkdir -p "$ROOT/logs"
LOG="$ROOT/logs/backup.log"
cd "$ROOT"
{
  echo "== $(date '+%Y-%m-%d %H:%M:%S') backup start"
  DUMP="$("$PYTHON" -m database.backup | tail -1)"
  echo "dump: $DUMP"
  if [[ "${1:-}" == "--verify" ]]; then
    "$PYTHON" tools/backup_restore_drill.py "$DUMP" --require-table users --require-table accounts --expect-dbname inazina
  fi
  "$PYTHON" tools/backup_check.py
  echo "== done"
} >> "$LOG" 2>&1
