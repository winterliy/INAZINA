"""Explicit maturity processing for the simulator wholesale funding ledger.

The caller owns the surrounding transaction and commits it. This helper does not
establish that the institution has cash available to meet its obligations.
"""
from managers.bank_journal import BankJournal
from managers.funding_manager import FundingManager


def process_due_funding(conn, *, now, limit=100, issuer=None):
    """Repay due rows in maturity order; isolate failures for a later retry. ``issuer`` limits it to one institution."""
    if not conn.in_transaction:
        raise ValueError("caller must start a transaction before processing funding")
    when = int(BankJournal.validate_epoch(now))
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer between 1 and 500")
    ids = [int(row[0]) for row in conn.execute(
        "SELECT funding_id FROM institution_funding WHERE status='active' AND maturity_epoch<=? "
        + ("AND issuer=? " if issuer is not None else "") + "ORDER BY maturity_epoch, funding_id LIMIT ?",
        (when, *([str(issuer)] if issuer is not None else []), limit))]
    repaid, failed = [], []
    for funding_id in ids:
        conn.execute("SAVEPOINT funding_maturity_row")
        try:
            amounts = FundingManager.repay(conn, funding_id, now=when)
        except Exception as exc:
            conn.execute("ROLLBACK TO SAVEPOINT funding_maturity_row")
            conn.execute("RELEASE SAVEPOINT funding_maturity_row")
            failed.append({"funding_id": funding_id, "error": str(exc)[:500]})
        else:
            conn.execute("RELEASE SAVEPOINT funding_maturity_row")
            repaid.append({"funding_id": funding_id, **amounts})
    return {"repaid": repaid, "failed": failed}
