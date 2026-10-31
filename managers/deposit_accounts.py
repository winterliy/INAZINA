"""Deposit accounts are one kind in every currency.

"외환통장" is only how an account looks to an owner whose national currency
differs from the account's. A person may hold several accounts in the same
currency; automatic flows (FX, foreign settlements, dividends) use the one
named by the caller, otherwise the oldest account in that currency.
"""

# SQL for "this user's primary account in this currency"; binds (user_id, currency).
PRIMARY_NUMBER = ("(SELECT account_number FROM accounts WHERE user_id=? AND quote_currency=? "
                  "AND account_type='saving' ORDER BY created_at,rowid LIMIT 1)")

# SQL for the national currency of the row ``a``'s owner.
OWNER_HOME = ("(SELECT c.currency_code FROM country_currencies c JOIN users h ON h.nationality=c.nationality "
              "WHERE h.user_id=a.user_id)")

# Deposits raise the interest-bearing principal and withdrawals lower it, in
# every currency alike; binds the balance_units delta.
PRINCIPAL_DELTA = "principal=MAX(0,COALESCE(principal,0)+?/1000000.0)"


def account(conn, user_id, currency, account_number=None):
    """The named account if it matches user and currency, else the primary one."""
    if account_number:
        return conn.execute("SELECT * FROM accounts WHERE account_number=? AND user_id=? AND quote_currency=? "
                            "AND account_type='saving'",
                            (account_number, str(user_id), str(currency).upper())).fetchone()
    return conn.execute(f"SELECT * FROM accounts WHERE account_number={PRIMARY_NUMBER}",
                        (str(user_id), str(currency).upper())).fetchone()

