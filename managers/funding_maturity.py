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


def process_opted_in_funding(conn, *, now, limit=100):
    """Repay matured funding of the banks whose jurisdiction switched ``funding_auto_maturity_flag`` on (default off).

    Repayment interest is an expense of the bank, so this never runs unless an administrator opted the jurisdiction in
    (``prudential_parameter_set``, which is confirmed and audited). Each bank is isolated: a failing bank or row is reported
    and retried on the next run. A run that repaid something leaves an ``admin_audit`` row (actor ``system:funding_maturity``).
    """
    from functions.admin_audit import record_admin_audit
    from managers.alm_manager import ALMManager

    if not conn.in_transaction:
        raise ValueError("caller must start a transaction before processing funding")
    when = int(BankJournal.validate_epoch(now))
    banks = conn.execute(
        "SELECT DISTINCT f.issuer,COALESCE(i.nationality,'') nationality FROM institution_funding f "
        "LEFT JOIN financial_institutions i ON i.issuer=f.issuer WHERE f.status='active' AND f.maturity_epoch<=? ORDER BY f.issuer",
        (when,)).fetchall()
    summary = {"skipped": [], "banks": []}
    for bank in banks:
        issuer = str(bank["issuer"])
        parameters, _ = ALMManager.parameters(conn, str(bank["nationality"]))
        if int(parameters["funding_auto_maturity_flag"]) != 1:
            summary["skipped"].append(issuer)
            continue
        conn.execute("SAVEPOINT funding_auto_bank")
        try:
            result = process_due_funding(conn, now=when, limit=limit, issuer=issuer)
            if result["repaid"]:
                record_admin_audit(conn, "system:funding_maturity", "funding.auto_process_due", {
                    "issuer": issuer, "repaid": [row["funding_id"] for row in result["repaid"]],
                    "failed": [row["funding_id"] for row in result["failed"]]}, when)
        except Exception as exc:
            conn.execute("ROLLBACK TO SAVEPOINT funding_auto_bank")
            result = {"repaid": [], "failed": [{"funding_id": None, "error": str(exc)[:500]}]}
        conn.execute("RELEASE SAVEPOINT funding_auto_bank")
        summary["banks"].append({"issuer": issuer, **result})
    return summary
