"""One-time ledger repair for the November 2026 reconciliation findings.

    python tools/repair_ledger.py <data-dir> [--apply] [--actor NAME]

<data-dir> is a folder holding inazina.db (normally the live database/ folder).
Without --apply this is a true preview: the database is copied to a temporary folder and
everything (start-up included, which creates the new tables/triggers and prunes orphan
snapshots) runs on that copy inside a transaction that is rolled back. The live database
is only read. With --apply the live database is first copied to <data-dir>/backups/, then
the steps run on it and commit.

Steps, in order (each is idempotent):
  1. rebuild_from_subledgers      company/bank entries missing from the common journal
  2. repair_duplicated_lines      exact repeated journal lines, one audit row per entry
  3. rebaseline_customer_wallets  wallet/ledger gaps restated as labelled baseline rows
  4. deposit liability correction interest later repaid into a loan from the same bank's deposit
  5. primary recipient note       market-fill:79 paid the treasury before SELOA had a cash account
  6. clear customer_funds_payable loan proceeds booked as payable to customers but never settled
  7. retire explained snapshots  snapshots of deleted rows, each retired only when evidence shows nothing was lost
Stop the bot and the web API before running with --apply.
"""
import argparse
import collections
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEPOSIT_CORRECTION = {
    "issuer": "낙원시민은행", "currency": "NWX", "units": 5611,
    "reference_key": "correction:deposit-interest-repaid:7428377058",
    "description": "Deposit interest of account 7428377058 was repaid into loan 1; "
                   "the repayment debited settlement cash instead of the deposit liability",
}
PRIMARY_RECIPIENT = {
    "execution_key": "market-fill:79:primary:1500000", "recipient_id": "20230216",
    "reason": "SELOA's issuer cash account did not exist when this fill was made; "
              "the proceeds correctly went to the treasury (낙원국고)",
}


def summarize(report):
    counts = collections.Counter((f["scope"], f.get("status") or "") for f in report["findings"])
    return dict(sorted(counts.items()))


def explain_deleted_row(conn, table, key, field, snapshot_units):
    """Evidence that a deleted row's snapshot lost no money, or None (the finding stays)."""
    def ledger_net(where, value):
        rows = conn.execute(f"SELECT currency_code,SUM(delta_minor_units) FROM customer_wallet_ledger WHERE {where} GROUP BY currency_code",
                            (value,)).fetchall()
        return all(int(row[1] or 0) == 0 for row in rows)
    if table == "accounts" and ledger_net("source_type='account' AND source_account=?", key) and snapshot_units == 0:
        return "계좌가 해지됨: 지갑 원장 순합계 0, 스냅샷 금액 0"
    if table == "users" and snapshot_units == 0 and ledger_net("user_id=?", key) and not conn.execute(
            "SELECT 1 FROM users WHERE user_id=?", (key,)).fetchone():
        return "삭제된 사용자(국가·시스템 잔재 포함): 지갑 원장 순합계 0, 스냅샷 금액 0"
    if table == "financial_institutions":
        renamed = conn.execute("SELECT 1 FROM bank_journal_entries WHERE event_type='opening_balance_transfer' "
                               "AND reference_key LIKE 'issuer-rename:%' AND description LIKE ?", (f"%{key}%",)).fetchone()
        carried = conn.execute(f"SELECT 1 FROM financial_institutions WHERE {field}=?", (snapshot_units,)).fetchone() \
            if field in {"equity_capital", "deposit_insurance_limit"} else None
        if renamed and (snapshot_units == 0 or carried):
            return "금융기관 이름 변경: 잔액이 새 이름의 개시 이관 분개로 옮겨졌고 같은 값이 현재 기관에 있음"
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("data_dir")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor", default="ledger-repair-2026-11")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    db_path = data_dir / "inazina.db"
    if not db_path.exists():
        raise SystemExit(f"{db_path} not found")

    scratch = None
    if args.apply:
        # Back up before anything else: start-up below already changes the live database.
        backups = data_dir / "backups"
        backups.mkdir(exist_ok=True)
        target = backups / f"inazina-before-ledger-repair-{time.strftime('%Y%m%d-%H%M%S')}.db"
        with sqlite3.connect(db_path) as source, sqlite3.connect(target) as copy:
            source.backup(copy)
        print(f"backup: {target}")
    else:
        import tempfile
        scratch = tempfile.TemporaryDirectory(prefix="repair-ledger-preview-")
        data_dir = Path(scratch.name)
        copy_path = data_dir / "inazina.db"
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as source, sqlite3.connect(copy_path) as copy:
            source.backup(copy)
        db_path = copy_path
        print(f"preview on a copy: {copy_path}")

    from database.db_manager import DBManager
    DBManager.database_dir = data_dir
    from database import DatabaseManager
    DatabaseManager().close()                      # creates tables/triggers the new code needs
    from database.unified import UnifiedConnection
    from managers.accounting_journal import AccountingJournal
    from managers.bank_journal import BankJournal

    conn = sqlite3.connect(db_path, factory=UnifiedConnection, isolation_level=None)
    conn.row_factory = sqlite3.Row
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        print("before:", summarize(AccountingJournal.reconcile_all_financial_ledgers(conn)))
        print("1 rebuild_from_subledgers:", AccountingJournal.rebuild_from_subledgers(conn))
        print("2 repair_duplicated_lines:", AccountingJournal.repair_duplicated_lines(
            conn, actor_id=args.actor, now=now))
        print("3 rebaseline_customer_wallets:", AccountingJournal.rebaseline_customer_wallets(
            conn, actor_id=args.actor, now=now,
            reason="wallet balances without matching ledger history, restated as baseline (2026-11 repair)"))
        c = DEPOSIT_CORRECTION
        if conn.execute("SELECT 1 FROM bank_journal_entries WHERE issuer=? AND reference_key=?",
                        (c["issuer"], c["reference_key"])).fetchone():
            print("4 deposit liability correction: already recorded")
        else:
            BankJournal.post(conn, c["issuer"], "deposit_repayment_correction", c["reference_key"], c["currency"],
                             [("customer_deposits_payable", "debit", c["units"]),
                              ("settlement_asset", "credit", c["units"])],
                             description=c["description"], now=now)
            print("4 deposit liability correction: posted", c["units"], c["currency"])
        p = PRIMARY_RECIPIENT
        if conn.execute("SELECT 1 FROM market_execution_primary_recipient_overrides WHERE execution_key=?",
                        (p["execution_key"],)).fetchone():
            print("5 primary recipient note: already recorded")
        else:
            AccountingJournal.record_primary_recipient_override(
                conn, execution_key=p["execution_key"], recipient_id=p["recipient_id"],
                reason=p["reason"], actor_id=args.actor, now=now)
            print("5 primary recipient note: recorded")
        print("6 clear customer_funds_payable:", BankJournal.clear_customer_funds_payable(
            conn, actor_id=args.actor, now=now))
        from functions.money import audit_amount_snapshots
        retired, kept = 0, []
        for finding in audit_amount_snapshots(conn):
            if finding.get("status") != "missing_source_row":
                continue
            units = conn.execute("SELECT amount_minor_units FROM financial_amount_snapshots WHERE source_table=? "
                                 "AND source_key=? AND amount_field=?", (finding["source_table"], finding["source_key"],
                                                                         finding["amount_field"])).fetchone()
            evidence = explain_deleted_row(conn, finding["source_table"], finding["source_key"],
                                           finding["amount_field"], int(units[0]))
            if evidence and AccountingJournal.retire_snapshot(
                    conn, source_table=finding["source_table"], source_key=finding["source_key"],
                    amount_field=finding["amount_field"], reason=evidence, actor_id=args.actor, now=now):
                retired += 1
            elif not evidence:
                kept.append((finding["source_table"], finding["source_key"], finding["amount_field"]))
        print("7 retire explained snapshots:", retired, "retired;", len(kept), "left as findings", kept[:5])
        report = AccountingJournal.reconcile_all_financial_ledgers(conn)
        print("after: ", summarize(report))
        for finding in report["findings"][:10]:
            print("  remaining:", {k: v for k, v in finding.items() if k != "orphan_balances"})
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if args.apply:
        conn.execute("COMMIT")
        print("committed")
    else:
        conn.execute("ROLLBACK")
        print("preview only: the live database was not changed (use --apply to run it for real)")
    conn.close()
    if scratch is not None:
        scratch.cleanup()


if __name__ == "__main__":
    main()
