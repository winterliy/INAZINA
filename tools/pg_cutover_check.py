"""Check a PostgreSQL database loaded by tools/sqlite_to_postgres.py against its SQLite source.

Usage: .venv/bin/python tools/pg_cutover_check.py SOURCE.db "postgresql:///inazina"

Compares row counts of every carried table, key money totals, runs the integrated ledger reconciliation and the amount
audit through the application on PostgreSQL. Read-only on both databases. Exit status 1 if anything differs.
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

TOTALS = (
    ("users.cash_minor", "SELECT SUM(cash_minor) FROM users"),
    ("accounts.balance_minor_units", "SELECT SUM(balance_minor_units) FROM accounts"),
    ("wallet ledger", "SELECT SUM(delta_minor_units) FROM customer_wallet_ledger"),
    ("bank journal credit", "SELECT SUM(credit_units) FROM bank_journal_lines"),
)


def main(source, dsn):
    import psycopg
    from build_postgres_schema import DROPPED, FOLDED, MERGED, RENAMED
    from database.unified import UnifiedConnection
    problems = []
    lite = sqlite3.connect(f"file:{source}?mode=ro", uri=True, factory=UnifiedConnection)
    pg = psycopg.connect(dsn)
    present = {row[0] for row in pg.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
    compared = 0
    for (table,) in lite.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if table in DROPPED or table in FOLDED or table in MERGED:
            continue
        target = RENAMED.get(table, table)
        if target not in present:
            problems.append(f"table missing in PostgreSQL: {table}")
            continue
        a = lite.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        b = pg.execute(f'SELECT COUNT(*) FROM "{target}"').fetchone()[0]
        compared += 1
        if a != b:
            problems.append(f"row count {table}: sqlite {a} vs postgres {b}")
    print(f"tables compared: {compared}")
    for label, query in TOTALS:
        try:
            a, b = lite.execute(query).fetchone()[0], pg.execute(query).fetchone()[0]
        except Exception as exc:
            problems.append(f"total {label}: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        a = str(a)[3:] if str(a).startswith(("~M~", "~R~")) else str(a)
        print(f"total {label}: sqlite {a} | postgres {b}")
        if str(a) != str(b):
            problems.append(f"total {label} differs")
    os.environ["INAZINA_DB_BACKEND"] = "postgres"
    os.environ["INAZINA_PG_DSN"] = dsn
    from database import DatabaseManager
    from functions.money import audit_amount_snapshots
    from managers.accounting_journal import AccountingJournal
    db = DatabaseManager()
    with db.readonly() as conn:
        report = AccountingJournal.reconcile_all_financial_ledgers(conn)
        audit = audit_amount_snapshots(conn)
    print(f"reconcile: {report['status']} close_ready={report['close_ready']} findings={len(report['findings'])}")
    problems += [f"reconcile finding: {finding}" for finding in report["findings"][:5]]
    problems += [f"amount audit: {finding}" for finding in audit[:5]]
    for item in problems:
        print("PROBLEM:", item)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
