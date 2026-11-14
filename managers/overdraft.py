"""종합통장(마이너스통장): a credit line attached to a spendable account.

The balance never goes below zero. When a payment needs more than the
balance, the shortfall is lent into the account first (a loan disbursement),
then the payment debits as usual, so every wallet ledger stays exact. A bank
job later repays the debt from any balance and, once a month, adds the
accrued interest (charged on each day's highest net use, see
database/account_kind_rules.py) to the debt. Using more than the limit (after
interest or a limit cut) is 연체 and costs the late spread. From
review_after_days on, a month whose peak use stays under usage_floor_percent
of the limit cuts the limit by reduce_percent of the original, down to
min_limit_percent.
"""
import json
import time

from functions.exact_money import ExactDecimal
from managers.bank_journal import BankJournal

SCALE = 1000000
MONTH = 30 * 86400


def _issuer(conn, account_number):
    row = conn.execute("SELECT s.issuer FROM accounts a JOIN saving s ON s.product_code=a.product_code "
                       "WHERE a.account_number=?", (account_number,)).fetchone()
    return row[0] if row else None


def _post(conn, account_number, currency, kind, units, now):
    amount = ExactDecimal(units) / SCALE
    lines = {"draw": [("loans_receivable", "debit", amount), ("customer_deposits_payable", "credit", amount)],
             "repay": [("customer_deposits_payable", "debit", amount), ("loans_receivable", "credit", amount)],
             "interest": [("loans_receivable", "debit", amount), ("interest_income", "credit", amount)]}[kind]
    event = conn.execute("INSERT INTO overdraft_events(account_number,kind,units,epoch,posted) VALUES (?,?,?,?,1)",
                         (account_number, kind, int(units), now))
    issuer = _issuer(conn, account_number)
    if issuer:
        BankJournal.post(conn, issuer, f"overdraft_{kind}", f"overdraft:{account_number}:{event.lastrowid}",
                         currency, lines, description=f"마이너스통장 {kind} {account_number}", now=now)


def headroom(conn, account_number):
    """Units still available to borrow (0 for any other kind)."""
    row = conn.execute("SELECT account_kind,overdraft_units,overdraft_limit_units FROM accounts WHERE account_number=?",
                       (account_number,)).fetchone()
    if not row or row["account_kind"] != "combined":
        return 0
    return max(0, int(row["overdraft_limit_units"]) - int(row["overdraft_units"]))


def draw(conn, account_number, need_units, now=None):
    """Lend up to ``need_units`` into a 마이너스통장 so a payment can debit it; returns units lent."""
    need_units = int(need_units)
    if need_units <= 0:
        return 0
    lend = min(need_units, headroom(conn, account_number))
    if lend <= 0:
        return 0
    now = time.time() if now is None else now
    currency = conn.execute("SELECT quote_currency FROM accounts WHERE account_number=?", (account_number,)).fetchone()[0]
    conn.execute("UPDATE accounts SET balance_units=balance_units+?,overdraft_units=overdraft_units+?,"
                 "overdraft_peak_units=MAX(overdraft_peak_units,overdraft_units+?) WHERE account_number=?",
                 (lend, lend, lend, account_number))
    _post(conn, account_number, currency, "draw", lend, now)
    return lend


def cover(conn, account_number, payment_units, now=None):
    """Borrow just enough for ``payment_units`` to debit; False when even the limit is short."""
    row = conn.execute("SELECT account_kind,balance_units FROM accounts WHERE account_number=?",
                       (account_number,)).fetchone()
    if not row or row["account_kind"] != "combined":
        return False
    short = int(payment_units) - int(row["balance_units"])
    if short <= 0:
        return True
    if short > headroom(conn, account_number):
        return False
    return draw(conn, account_number, short, now) == short


def process(conn, now=None):
    """Bank job: repay from balances, charge monthly interest, review limits. Returns notices."""
    now = time.time() if now is None else now
    notices = []
    rows = conn.execute("SELECT account_number,user_id,quote_currency,kind_terms,created_at,balance_units,"
                        "overdraft_units,overdraft_limit_units,overdraft_interest_units,overdraft_charged_epoch,"
                        "overdraft_review_epoch,overdraft_peak_units FROM accounts WHERE account_kind='combined'").fetchall()
    for row in rows:
        number, terms = row["account_number"], json.loads(row["kind_terms"] or "{}")
        # Accrue finished days up to now (the use trigger fires on this column).
        conn.execute("UPDATE accounts SET overdraft_epoch=? WHERE account_number=?", (now, number))
        current = conn.execute("SELECT balance_units,overdraft_units,overdraft_interest_units FROM accounts "
                               "WHERE account_number=?", (number,)).fetchone()
        repaid = min(int(current["balance_units"]), int(current["overdraft_units"]))
        if repaid > 0:
            conn.execute("UPDATE accounts SET balance_units=balance_units-?,overdraft_units=overdraft_units-? "
                         "WHERE account_number=?", (repaid, repaid, number))
            _post(conn, number, row["quote_currency"], "repay", repaid, now)
        charged = 0
        if now - float(row["overdraft_charged_epoch"] or now) >= MONTH:
            charged = int(float(current["overdraft_interest_units"]))
            conn.execute("UPDATE accounts SET overdraft_units=overdraft_units+?,"
                         "overdraft_interest_units=overdraft_interest_units-?,overdraft_charged_epoch=? "
                         "WHERE account_number=?", (charged, charged, now, number))
            if charged:
                _post(conn, number, row["quote_currency"], "interest", charged, now)
        cut = 0
        original = int(round(ExactDecimal(terms.get("overdraft_limit", 0)) * SCALE))
        from managers.account_kind_settlement import _opened
        review_from = _opened(row) + int(terms.get("review_after_days", 0)) * 86400
        if (original and now >= review_from and now - float(row["overdraft_review_epoch"] or now) >= MONTH):
            limit = int(row["overdraft_limit_units"])
            if int(row["overdraft_peak_units"]) < limit * float(terms.get("usage_floor_percent", 0)) / 100:
                floor = int(original * float(terms.get("min_limit_percent", 0)) / 100)
                new_limit = max(floor, limit - int(original * float(terms.get("reduce_percent", 0)) / 100))
                cut = limit - new_limit
            after = conn.execute("SELECT overdraft_units FROM accounts WHERE account_number=?", (number,)).fetchone()[0]
            conn.execute("UPDATE accounts SET overdraft_limit_units=overdraft_limit_units-?,overdraft_review_epoch=?,"
                         "overdraft_peak_units=? WHERE account_number=?", (cut, now, int(after), number))
        if repaid or charged or cut:
            notices.append({"user_id": str(row["user_id"]), "account_number": number,
                            "currency": row["quote_currency"], "repaid": ExactDecimal(repaid) / SCALE,
                            "interest": ExactDecimal(charged) / SCALE, "limit_cut": ExactDecimal(cut) / SCALE})
    return notices
