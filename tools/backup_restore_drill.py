#!/usr/bin/env python3
"""Restore a pg_dump backup (custom format) into a temporary database and inspect it; the source file is only read.

    python tools/backup_restore_drill.py database/backups/inazina-pg-<time>.dump [--require-table NAME]... [--reconcile]

The server comes from INAZINA_PG_DSN (host/user only: the drill creates and drops its own `inazina_drill_*` database there).
Prints one JSON report and exits 0 when ok, 1 otherwise. SQLite backups ended on 2026-11-20; the old SQLite version of this tool is
kept at the bottom of the file as a comment.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def server_dsns():
    """(admin DSN on the `postgres` database, function name -> DSN) for the server INAZINA_PG_DSN points at."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    base = conninfo_to_dict(os.environ.get("INAZINA_PG_DSN") or "postgresql:///postgres")
    return make_conninfo(**{**base, "dbname": "postgres"}), (lambda name: make_conninfo(**{**base, "dbname": name}))


def verify(dsn, required):
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        tables = {row[0] for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        unvalidated = conn.execute(
            "SELECT conrelid::regclass::text, conname FROM pg_constraint WHERE contype = 'f' AND NOT convalidated ORDER BY 1, 2").fetchall()
    missing = sorted(set(required) - tables)
    violations = [{"table": table.split(".")[-1].strip('"'), "constraint": name} for table, name in unvalidated]
    return {"ok": not missing and not violations, "tables": len(tables), "missing_tables": missing, "foreign_key_violations": violations}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("backup", type=Path, help="a pg_dump custom-format backup file")
    parser.add_argument("--require-table", action="append", default=[], metavar="NAME", help="require this table after restore (repeatable)")
    parser.add_argument("--reconcile", action="store_true", help="run the financial close checks on the restored copy")
    args = parser.parse_args()
    start = time.monotonic()
    name = f"inazina_drill_{os.getpid()}_{int(time.time())}"
    admin_dsn = restored_dsn = None
    created = False
    try:
        import psycopg
        from database.backup import pg_tool
        source = args.backup.resolve(strict=True)
        with source.open("rb") as handle:
            if handle.read(5) != b"PGDMP":
                raise ValueError("Not a pg_dump custom-format backup")
        admin_dsn, dsn_for = server_dsns()
        restored_dsn = dsn_for(name)
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{name}"')
        created = True
        result = subprocess.run([pg_tool("pg_restore"), "--no-owner", "--no-privileges", "--exit-on-error", "--dbname", restored_dsn, str(source)],
                                capture_output=True, text=True, timeout=1800)
        if result.returncode:
            raise RuntimeError(f"pg_restore failed: {result.stderr.strip()[:300]}")
        verification = verify(restored_dsn, args.require_table)
        report = {"ok": verification["ok"], "verification": verification}
        if args.reconcile and report["ok"]:
            os.environ["INAZINA_PG_DSN"] = restored_dsn
            os.environ["INAZINA_DB_BACKEND"] = "postgres"
            from contextlib import closing
            from database import pg_backend
            from managers.accounting_journal import AccountingJournal
            with closing(pg_backend.connect(readonly=True)) as conn:
                import sqlite3
                conn.row_factory = sqlite3.Row   # the managers read columns by name; on PostgreSQL this is just the marker for named rows
                check = AccountingJournal.reconcile_all_financial_ledgers(conn)
            report["reconciliation"] = {
                "status": check["status"], "close_ready": check["close_ready"], "coverage": check["coverage"],
                "finding_count": len(check["findings"]), "unconnected_tables": check["consumer_coverage"]["unconnected"]}
            report["ok"] = check["status"] == "matched" and check["close_ready"]
    except Exception as exc:  # a drill reports every failure as data, whatever its type
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if created:
            try:
                import psycopg
                with psycopg.connect(admin_dsn, autocommit=True) as admin:
                    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except Exception as exc:
                print(f"could not drop {name}: {exc}", file=sys.stderr)
    report["elapsed_seconds"] = round(time.monotonic() - start, 3)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------------------------------------------------------
# The SQLite version of this drill (retired 2026-11-20 with SQLite support), kept for reference. It restored a standalone SQLite
# backup file into a temporary directory and ran tools/verify_backup.py on it.
# ---------------------------------------------------------------------------------------------------------------------------
# #!/usr/bin/env python3
# """Restore a standalone SQLite backup into temporary storage and inspect it."""
# import argparse
# import json
# import sqlite3
# import subprocess
# import sys
# import tempfile
# import time
# from contextlib import closing
# from pathlib import Path
#
#
# def main():
#     parser = argparse.ArgumentParser(description=__doc__)
#     parser.add_argument('backup', type=Path, help='complete, standalone SQLite backup file')
#     parser.add_argument('--require-table', action='append', default=[], metavar='NAME',
#                         help='require this table after restore (repeatable)')
#     parser.add_argument('--reconcile', action='store_true',
#                         help='run implemented financial close checks on the restored copy')
#     args = parser.parse_args()
#     start = time.monotonic()
#     try:
#         source_path = args.backup.resolve(strict=True)
#         for suffix in ('-wal', '-journal'):
#             if Path(str(source_path) + suffix).exists():
#                 raise ValueError('A SQLite journal sidecar exists; supply a standalone backup')
#         with source_path.open('rb') as source_file:
#             if source_file.read(16) != b'SQLite format 3\x00':
#                 raise ValueError('Not a complete SQLite database file')
#         # immutable prevents SQLite from creating sidecars or changing the source.
#         # A live WAL database is not a standalone backup and must not be used here.
#         source_uri = source_path.as_uri() + '?mode=ro&immutable=1'
#         with tempfile.TemporaryDirectory(prefix='inazina-restore-drill-') as directory:
#             restored = Path(directory) / 'restored.db'
#             with closing(sqlite3.connect(source_uri, uri=True)) as source, \
#                     closing(sqlite3.connect(restored)) as target:
#                 source.backup(target)
#             command = [sys.executable, str(Path(__file__).with_name('verify_backup.py')), str(restored)]
#             for name in args.require_table:
#                 command.extend(['--require-table', name])
#             result = subprocess.run(
#                 command,
#                 capture_output=True, text=True, check=False,
#             )
#             if result.returncode not in (0, 1):
#                 raise RuntimeError('restored backup verification could not run')
#             verification = json.loads(result.stdout)
#             report = {'ok': result.returncode == 0 and verification.get('ok') is True,
#                       'verification': verification}
#             if args.reconcile and report['ok']:
#                 sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
#                 from database.unified import UnifiedConnection
#                 from managers.accounting_journal import AccountingJournal
#                 with closing(sqlite3.connect(restored.as_uri() + '?mode=ro', uri=True,
#                                              factory=UnifiedConnection)) as conn:
#                     conn.row_factory = sqlite3.Row
#                     conn.execute('PRAGMA query_only=ON')
#                     check = AccountingJournal.reconcile_all_financial_ledgers(conn)
#                 report['reconciliation'] = {
#                     'status': check['status'], 'close_ready': check['close_ready'],
#                     'coverage': check['coverage'], 'finding_count': len(check['findings']),
#                     'unconnected_tables': check['consumer_coverage']['unconnected'],
#                 }
#                 report['ok'] = check['status'] == 'matched' and check['close_ready']
#
#     except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
#         report = {'ok': False, 'error': str(exc)}
#     report['elapsed_seconds'] = round(time.monotonic() - start, 3)
#     print(json.dumps(report, ensure_ascii=False))
#     return 0 if report['ok'] else 1
#
#
# if __name__ == '__main__':
#     sys.exit(main())
