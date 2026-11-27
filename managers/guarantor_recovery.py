"""Guarantor claims on charged-off loans (M7): an append-only guarantee record and recovery paid from the guarantor's own wallet.

A guarantee names a guarantor, the loan and the most the guarantor can be called for (integer minor units, the loan's currency). A claim
debits the guarantor's home-currency wallet and records the bank recovery through ``record_funded_recovery`` (bank journal, business cash
link, charged-off balance reduced) in one savepoint. The same key replays to the same result. Nothing is invented about other creditors.
"""
from managers.compliance_manager import ComplianceManager
from managers.currency_manager import CurrencyManager
from managers.funded_recovery_manager import record_funded_recovery

SAFE = 9007199254740991


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS loan_guarantees (
        guarantee_id INTEGER PRIMARY KEY AUTOINCREMENT, loan_id INTEGER NOT NULL, guarantor_user_id TEXT NOT NULL, currency_code TEXT NOT NULL,
        max_minor_units INTEGER NOT NULL CHECK(typeof(max_minor_units)='integer' AND max_minor_units>0),
        created_by TEXT NOT NULL, created_epoch INTEGER NOT NULL, UNIQUE(loan_id,guarantor_user_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS loan_guarantor_payments (
        event_key TEXT PRIMARY KEY, guarantee_id INTEGER NOT NULL, loan_id INTEGER NOT NULL, guarantor_user_id TEXT NOT NULL,
        amount_minor_units INTEGER NOT NULL CHECK(typeof(amount_minor_units)='integer' AND amount_minor_units>0),
        actor_id TEXT NOT NULL, created_epoch INTEGER NOT NULL)""")
    for table in ("loan_guarantees", "loan_guarantor_payments"):
        for operation in ("UPDATE", "DELETE"):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_{operation.lower()} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'{table} rows are append-only'); END""")


def register_guarantee(conn, *, loan_id, guarantor_id, max_minor_units, actor_id, now):
    if not conn.in_transaction:
        raise ValueError("caller must start a transaction")
    ComplianceManager._require_admin(conn, actor_id)
    guarantor = str(guarantor_id or "").strip()
    if (type(loan_id) is not int or loan_id <= 0 or type(max_minor_units) is not int or not 0 < max_minor_units <= SAFE or not guarantor
            or type(now) is not int):
        raise ValueError("invalid guarantee")
    ensure_tables(conn)
    loan = conn.execute("SELECT user_id,quote_currency FROM loans WHERE loan_id=?", (loan_id,)).fetchone()
    if not loan or not loan["quote_currency"] or guarantor == str(loan["user_id"]):
        raise ValueError("a guarantee needs an existing loan and a guarantor other than the borrower")
    currency = str(loan["quote_currency"]).upper()
    if not conn.execute("SELECT 1 FROM users WHERE user_id=?", (guarantor,)).fetchone() or CurrencyManager.home(conn, guarantor) != currency:
        raise ValueError("the guarantor must be a user whose home currency is the loan currency")
    if conn.execute("SELECT 1 FROM loan_guarantees WHERE loan_id=? AND guarantor_user_id=?", (loan_id, guarantor)).fetchone():
        raise ValueError("this guarantor already guarantees the loan")
    return conn.execute("INSERT INTO loan_guarantees(loan_id,guarantor_user_id,currency_code,max_minor_units,created_by,created_epoch) VALUES (?,?,?,?,?,?)",
                        (loan_id, guarantor, currency, max_minor_units, str(actor_id), now)).lastrowid


def claim_from_guarantor(conn, *, loan_id, guarantor_id, amount_minor_units, event_key, actor_id, now):
    if not conn.in_transaction:
        raise ValueError("caller must start a transaction")
    ComplianceManager._require_admin(conn, actor_id)
    key, guarantor = str(event_key or "").strip(), str(guarantor_id or "").strip()
    if (not 8 <= len(key) <= 100 or type(loan_id) is not int or loan_id <= 0 or type(amount_minor_units) is not int
            or not 0 < amount_minor_units <= SAFE or not guarantor or type(now) is not int):
        raise ValueError("invalid guarantor claim")
    ensure_tables(conn)
    prior = conn.execute("SELECT * FROM loan_guarantor_payments WHERE event_key=?", (key,)).fetchone()
    if prior:
        if (int(prior["loan_id"]), prior["guarantor_user_id"], int(prior["amount_minor_units"])) != (loan_id, guarantor, amount_minor_units):
            raise ValueError("guarantor claim key already used with different details")
        return {"duplicate": True, "amountMinorUnits": amount_minor_units}
    guarantee = conn.execute("SELECT * FROM loan_guarantees WHERE loan_id=? AND guarantor_user_id=?", (loan_id, guarantor)).fetchone()
    if not guarantee:
        raise ValueError("no guarantee from this guarantor on this loan")
    used = int(conn.execute("SELECT COALESCE(SUM(amount_minor_units),0) FROM loan_guarantor_payments WHERE guarantee_id=?", (guarantee["guarantee_id"],)).fetchone()[0])
    if amount_minor_units > int(guarantee["max_minor_units"]) - used:
        raise ValueError("the claim exceeds what is left of the guarantee")
    conn.execute("SAVEPOINT guarantor_claim")
    try:
        conn.execute("INSERT INTO loan_guarantor_payments(event_key,guarantee_id,loan_id,guarantor_user_id,amount_minor_units,actor_id,created_epoch) VALUES (?,?,?,?,?,?,?)",
                     (key, guarantee["guarantee_id"], loan_id, guarantor, amount_minor_units, str(actor_id), now))
        record_funded_recovery(conn, loan_id=loan_id, amount_minor_units=amount_minor_units, event_key=f"guarantor:{key}", actor_id=actor_id, now=now, payer_id=guarantor)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT guarantor_claim")
        conn.execute("RELEASE SAVEPOINT guarantor_claim")
        raise
    conn.execute("RELEASE SAVEPOINT guarantor_claim")
    return {"duplicate": False, "amountMinorUnits": amount_minor_units, "remainingGuaranteeMinorUnits": int(guarantee["max_minor_units"]) - used - amount_minor_units}


def reconcile(conn):
    """Guarantor payments against the bank recoveries they posted (by derived key)."""
    ensure_tables(conn)
    problems = []
    for row in conn.execute("SELECT event_key,amount_minor_units FROM loan_guarantor_payments").fetchall():
        posted = conn.execute("SELECT COALESCE(SUM(amount_minor_units),0) FROM loan_chargeoff_events WHERE event_key=? AND event_type='recovery'",
                              (f"guarantor:{row['event_key']}",)).fetchone()[0]
        if int(posted) != int(row["amount_minor_units"]):
            problems.append({"eventKey": row["event_key"], "payment": int(row["amount_minor_units"]), "posted": int(posted)})
    return problems
