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
  # The work platform's own MySQL (docs/work-platform-elements.md): a separate dump next to the PostgreSQL one, same retention (newest 14).
  if [[ -d "$ROOT/database/work-mysql/data" ]] && "$ROOT/tools/work_mysql.sh" status >/dev/null 2>&1; then
    WORKDUMP="$ROOT/database/backups/work-mysql-$(date +%Y%m%d-%H%M%S).sql"
    "$ROOT/tools/work_mysql.sh" dump "$WORKDUMP"
    chmod 600 "$WORKDUMP"
    echo "work dump: $WORKDUMP ($(wc -c < "$WORKDUMP") bytes)"
    if [[ "${1:-}" == "--verify" ]]; then grep -q "CREATE TABLE \`members\`" "$WORKDUMP" && echo "work dump verified (members table present)"; fi
    # The drive's file contents are plain files outside MySQL: copy them next to the dump (changed files only; files deleted
    # by a withdrawal or a purge disappear from the copy too, so a withdrawn member's files do not live on in the backup).
    if [[ -d "$ROOT/database/work-files" ]]; then
      mkdir -p "$ROOT/database/backups/work-files"; chmod 700 "$ROOT/database/backups/work-files"
      rsync -a --delete "$ROOT/database/work-files/" "$ROOT/database/backups/work-files/"
      echo "work files copied: $(ls -1 "$ROOT/database/backups/work-files" | wc -l | tr -d ' ') files"
    fi
    ls -1t "$ROOT"/database/backups/work-mysql-*.sql 2>/dev/null | tail -n +15 | xargs -I{} rm -f {}
  else
    echo "work MySQL not running or not initialised: skipped"
  fi
  "$PYTHON" tools/backup_check.py
  echo "== done"
} >> "$LOG" 2>&1
