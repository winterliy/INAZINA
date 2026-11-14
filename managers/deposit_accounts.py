"""Deposit accounts are one kind in every currency.

"외환통장" is only how an account looks to an owner whose national currency
differs from the account's. A person may hold several accounts in the same
currency; automatic flows (FX, foreign settlements, dividends) use the one
named by the caller, otherwise the oldest spendable account in that currency.
"""

from database.account_kind_terms import (  # noqa: F401  (re-exported for managers)
    ACCOUNT_KINDS, CONTRIBUTION_KINDS, DAY, FLEXIBLE_KINDS, INSTALLMENT_KINDS, KIND_TERMS, LOCKED_KINDS,
    ONE_PER_PERSON_KINDS)


def default_account_kind(product_kind, maturity_days):
    """The kind an unlabelled product gets: CMA stays CMA, else maturity decides."""
    if product_kind == "cma":
        return "cma"
    if product_kind in ("ima", "wrap"):
        return None
    return "time_deposit" if (maturity_days or 0) > 0 else "demand"


# SQL for "this user's primary account in this currency"; binds (user_id, currency).
SPENDABLE = ("COALESCE(account_kind,'demand') IN ('demand','parking','cma','combined')")
PRIMARY_NUMBER = ("(SELECT account_number FROM accounts WHERE user_id=? AND quote_currency=? "
                  f"AND account_type='saving' AND {SPENDABLE} ORDER BY created_at,rowid LIMIT 1)")

# SQL for the national currency of the row ``a``'s owner.
OWNER_HOME = ("(SELECT c.currency_code FROM country_currencies c JOIN users h ON h.nationality=c.nationality "
              "WHERE h.user_id=a.user_id)")

# Deposits raise the interest-bearing principal and withdrawals lower it, in
# every currency alike; binds the balance_units delta.
PRINCIPAL_DELTA = "principal=MAX(0,COALESCE(principal,0)+?/1000000.0)"


def rule_message(exc):
    """The Korean text of an account-kind rule the database refused, else None."""
    text = str(exc)
    return text if any("\uac00" <= ch <= "\ud7a3" for ch in text) else None


class rule_bypass:
    """Let closing/resolution code move money out of a locked account in this transaction."""

    def __init__(self, conn, account_number):
        self.conn, self.number = conn, str(account_number)

    def __enter__(self):
        self.conn.execute("INSERT OR IGNORE INTO account_rule_bypass(account_number) VALUES (?)", (self.number,))
        return self

    def __exit__(self, *exc):
        self.conn.execute("DELETE FROM account_rule_bypass WHERE account_number=?", (self.number,))
        return False


def account(conn, user_id, currency, account_number=None):
    """The named account if it matches user and currency, else the primary one."""
    if account_number:
        return conn.execute("SELECT * FROM accounts WHERE account_number=? AND user_id=? AND quote_currency=? "
                            "AND account_type='saving'",
                            (account_number, str(user_id), str(currency).upper())).fetchone()
    return conn.execute(f"SELECT * FROM accounts WHERE account_number={PRIMARY_NUMBER}",
                        (str(user_id), str(currency).upper())).fetchone()

