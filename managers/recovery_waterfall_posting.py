"""Real posting of a creditor-priority recovery across one borrower's charged-off loans (M7).

The proceeds come from the borrower's own home-currency wallet (same funding rule as ``funded_recovery_manager``).
The borrower's charged-off loans (possibly at different banks, one currency) are the competing claims: ranks decide the
order, equal ranks share pro rata (``allocate_recovery``). Each paid leg is posted through ``record_funded_recovery`` with a
key derived from the parent key, all inside one savepoint, so wallet debits, bank journals and business-cash links stay
reconciled and a failed leg rolls everything back. A parent record keeps the event key, so a replay changes nothing.
Other creditors' ledgers are not invented: only loans this system holds can be paid here.
"""
import json

from functions.recovery_waterfall import allocate_recovery
from managers.compliance_manager import ComplianceManager
from managers.currency_manager import CurrencyManager
from managers.funded_recovery_manager import record_funded_recovery

SAFE = 9007199254740991


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS recovery_waterfall_events (
        event_key TEXT PRIMARY KEY, user_id TEXT NOT NULL, currency_code TEXT NOT NULL,
        proceeds_minor_units INTEGER NOT NULL CHECK(typeof(proceeds_minor_units)='integer' AND proceeds_minor_units>0),
        allocated_minor_units INTEGER NOT NULL CHECK(typeof(allocated_minor_units)='integer' AND allocated_minor_units>=0),
        claims_json TEXT NOT NULL, allocations_json TEXT NOT NULL, actor_id TEXT NOT NULL, created_epoch INTEGER NOT NULL)""")
    for operation in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_recovery_waterfall_events_{operation.lower()}
            BEFORE {operation} ON recovery_waterfall_events
            BEGIN SELECT RAISE(ABORT,'recovery waterfall events are append-only'); END""")


def _leg_key(event_key, loan_id):
    return f"{event_key}:L{loan_id}"


def post_waterfall(conn, *, user_id, proceeds_minor_units, claims, event_key, actor_id, now):
    """``claims`` is a list of ``(loan_id, rank)``. Returns the parent record; a repeated key returns it unchanged."""
    if not conn.in_transaction:
        raise ValueError("caller must start a transaction")
    ComplianceManager._require_admin(conn, actor_id)
    key = str(event_key or "").strip()
    claims = [(c[0], c[1]) for c in (claims or ())]
    if (not 8 <= len(key) <= 120 or type(proceeds_minor_units) is not int or not 0 < proceeds_minor_units <= SAFE
            or not 1 <= len(claims) <= 50 or len({loan for loan, _ in claims}) != len(claims)
            or any(type(loan) is not int or loan <= 0 or type(rank) is not int or not 1 <= rank <= SAFE for loan, rank in claims)
            or not str(user_id or "").strip() or type(now) is not int):
        raise ValueError("invalid recovery waterfall")
    user_id = str(user_id).strip()
    ensure_tables(conn)
    claims_json = json.dumps(sorted(claims), separators=(",", ":"))
    prior = conn.execute("SELECT * FROM recovery_waterfall_events WHERE event_key=?", (key,)).fetchone()
    if prior:
        if (prior["user_id"], int(prior["proceeds_minor_units"]), prior["claims_json"]) != (user_id, proceeds_minor_units, claims_json):
            raise ValueError("recovery waterfall key already used with different details")
        return {"eventKey": key, "duplicate": True, "allocations": json.loads(prior["allocations_json"]),
                "allocatedMinorUnits": int(prior["allocated_minor_units"])}
    currency = CurrencyManager.home(conn, user_id)
    amounts = {}
    for loan_id, _ in claims:
        row = conn.execute("SELECT user_id,status,quote_currency,charged_off_balance_minor_units FROM loans WHERE loan_id=?", (loan_id,)).fetchone()
        if (not row or row["status"] != "charged_off" or str(row["user_id"]) != user_id or str(row["quote_currency"] or "").upper() != currency
                or row["charged_off_balance_minor_units"] is None or int(row["charged_off_balance_minor_units"]) <= 0):
            raise ValueError("every claim must be this borrower's charged-off loan in the home currency with a balance")
        amounts[loan_id] = int(row["charged_off_balance_minor_units"])
    result = allocate_recovery(proceeds_minor_units, [(str(loan), rank, amounts[loan]) for loan, rank in claims])
    paid = {int(loan): amount for loan, amount in result.allocations.items() if amount > 0}
    allocated = sum(paid.values())
    conn.execute("SAVEPOINT recovery_waterfall")
    try:
        conn.execute("INSERT INTO recovery_waterfall_events(event_key,user_id,currency_code,proceeds_minor_units,allocated_minor_units,"
                     "claims_json,allocations_json,actor_id,created_epoch) VALUES (?,?,?,?,?,?,?,?,?)",
                     (key, user_id, currency, proceeds_minor_units, allocated, claims_json,
                      json.dumps({str(k): v for k, v in sorted(paid.items())}, separators=(",", ":")), str(actor_id), now))
        for loan_id, amount in sorted(paid.items()):
            leg = record_funded_recovery(conn, loan_id=loan_id, amount_minor_units=amount, event_key=_leg_key(key, loan_id),
                                         actor_id=actor_id, now=now)
            if leg.get("duplicate"):
                raise ValueError("recovery leg already posted without its parent record")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT recovery_waterfall")
        conn.execute("RELEASE SAVEPOINT recovery_waterfall")
        raise
    conn.execute("RELEASE SAVEPOINT recovery_waterfall")
    return {"eventKey": key, "duplicate": False, "allocations": {str(k): v for k, v in sorted(paid.items())},
            "allocatedMinorUnits": allocated, "unallocatedMinorUnits": result.unallocated, "currencyCode": currency}


def reconcile(conn):
    """Parents whose posted legs (charge-off recovery events) do not add up to what the parent says was allocated."""
    ensure_tables(conn)
    problems = []
    for parent in conn.execute("SELECT event_key,allocations_json FROM recovery_waterfall_events").fetchall():
        for loan, expected in json.loads(parent["allocations_json"]).items():
            row = conn.execute("SELECT COALESCE(SUM(amount_minor_units),0) FROM loan_chargeoff_events WHERE event_key=? AND event_type='recovery'",
                               (_leg_key(parent["event_key"], loan),)).fetchone()
            if int(row[0]) != int(expected):
                problems.append({"eventKey": parent["event_key"], "loanId": int(loan), "expected": int(expected), "posted": int(row[0])})
    return problems
