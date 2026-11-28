#!/bin/zsh
# Separate MySQL instance for work.inazina.localhost. Own datadir (database/work-mysql), 127.0.0.1:3307 only, no brew service.
# Usage: tools/work_mysql.sh init|start|stop|status|shell
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN=/opt/homebrew/opt/mysql@8.4/bin
DATA="$ROOT/database/work-mysql/data"
RUN="$ROOT/database/work-mysql"
PORT="${WORK_DB_PORT:-3307}"
[[ -f "$ROOT/.env" ]] && { set -a; source "$ROOT/.env"; set +a; }
case "${1:-}" in
  init)
    [[ -d "$DATA/mysql" ]] && { echo "already initialised"; exit 0; }
    : "${WORK_DB_PASSWORD:?set WORK_DB_PASSWORD in .env first}" "${WORK_DB_ROOT_PASSWORD:?set WORK_DB_ROOT_PASSWORD in .env first}"
    "$BIN/mysqld" --no-defaults --initialize-insecure --datadir="$DATA" --basedir=/opt/homebrew/opt/mysql@8.4
    "$0" start
    "$BIN/mysql" --no-defaults -uroot -S "$RUN/mysql.sock" <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED BY '${WORK_DB_ROOT_PASSWORD}';
CREATE DATABASE IF NOT EXISTS inazina_work CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS 'inazina_work'@'127.0.0.1' IDENTIFIED BY '${WORK_DB_PASSWORD}';
GRANT ALL ON inazina_work.* TO 'inazina_work'@'127.0.0.1';
SQL
    ;;
  start)
    "$0" status >/dev/null 2>&1 && { echo "running"; exit 0; }
    nohup "$BIN/mysqld" --no-defaults --datadir="$DATA" --basedir=/opt/homebrew/opt/mysql@8.4 --bind-address=127.0.0.1 --port="$PORT" \
      --mysqlx=OFF --socket="$RUN/mysql.sock" --pid-file="$RUN/mysql.pid" --log-error="$RUN/error.log" \
      --local-infile=0 --skip-name-resolve --sql-mode=STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION >/dev/null 2>&1 &
    for i in {1..40}; do "$0" status >/dev/null 2>&1 && { echo "started on 127.0.0.1:$PORT"; exit 0; }; sleep 0.5; done
    echo "failed, see $RUN/error.log" >&2; exit 1 ;;
  withdraw)
    # withdraw <user_ref>: anonymise the work data of a withdrawn member (apps/work/db/withdraw.sql). A failure is kept in
    # database/work-mysql/pending_withdrawals.txt and retried by `withdraw-pending` (start_all runs it), so a withdrawal is never lost.
    ref="${2:?user_ref required}"
    [[ "$ref" =~ ^[A-Za-z0-9_.:-]{1,64}$ ]] || { echo "bad user_ref" >&2; exit 2; }
    # The drive's file contents are plain files outside MySQL (database/work-files): delete them first, then clear the rows.
    FILES="${WORK_FILES_DIR:-$ROOT/database/work-files}"
    keys=$(MYSQL_PWD="$WORK_DB_PASSWORD" "$BIN/mysql" --no-defaults -h127.0.0.1 -P"$PORT" -uinazina_work inazina_work -N -B -e "SELECT n.storage_key FROM drive_nodes n JOIN members m ON m.id=n.owner_id WHERE m.user_ref='$ref' AND n.storage_key IS NOT NULL" 2>/dev/null) || { echo "$ref" >> "$RUN/pending_withdrawals.txt"; echo "queued" >&2; exit 1; }
    for key in ${(f)keys}; do [[ "$key" =~ ^[0-9a-f]{32}$ ]] && rm -f "$FILES/$key"; done
    if ! MYSQL_PWD="$WORK_DB_PASSWORD" "$BIN/mysql" --no-defaults -h127.0.0.1 -P"$PORT" -uinazina_work inazina_work -e "CALL work_withdraw('$ref')" 2>/dev/null; then
      echo "$ref" >> "$RUN/pending_withdrawals.txt"; echo "queued" >&2; exit 1
    fi ;;
  withdraw-pending)
    [[ -s "$RUN/pending_withdrawals.txt" ]] || exit 0
    mv "$RUN/pending_withdrawals.txt" "$RUN/pending_withdrawals.work"
    while read -r ref; do [[ -n "$ref" ]] && "$0" withdraw "$ref" || true; done < "$RUN/pending_withdrawals.work"
    rm -f "$RUN/pending_withdrawals.work" ;;
  dump)
    # dump <file>: logical backup of the work database (used by tools/scheduled_backup.sh)
    out="${2:?file required}"
    MYSQL_PWD="$WORK_DB_PASSWORD" "$BIN/mysqldump" --no-defaults -h127.0.0.1 -P"$PORT" -uinazina_work --single-transaction --routines --no-tablespaces inazina_work > "$out.tmp" && mv "$out.tmp" "$out" ;;
  stop) [[ -f "$RUN/mysql.pid" ]] && kill "$(cat "$RUN/mysql.pid")" && echo stopped ;;
  status) "$BIN/mysqladmin" --no-defaults -S "$RUN/mysql.sock" -uroot ${WORK_DB_ROOT_PASSWORD:+-p"$WORK_DB_ROOT_PASSWORD"} ping ;;
  shell) exec "$BIN/mysql" --no-defaults -S "$RUN/mysql.sock" -uroot -p"${WORK_DB_ROOT_PASSWORD:-}" inazina_work ;;
  *) echo "usage: $0 init|start|stop|status|shell|withdraw <ref>|withdraw-pending|dump <file>"; exit 2 ;;
esac
