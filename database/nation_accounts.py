"""A country and its treasury are one account: the treasury's numeric id.

Older databases still carry a user named after the country (user_id = country name).
It is folded into the treasury, and only when it is empty: no balance, no holdings and no ledger history.
"""

from decimal import Decimal

# Tables whose rows for the leftover user are moved or discarded here.
_HANDLED = {"users", "web_auth_credentials", "credit_events"}  # credit_events is append-only history, never rewritten


def _tables(conn):
    return [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


def _owned_rows(conn, user_id):
    """Rows other than the account itself that still belong to this user id."""
    found = []
    for table in _tables(conn):
        if table in _HANDLED:
            continue
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if "user_id" in columns:
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE user_id=?', (user_id,)).fetchone()[0]
            if count:
                found.append((table, count))
    return found


def merge_legacy_nation_users(db):
    """Fold each leftover country-named user into its treasury; returns the merged country names."""
    with db.atomic() as conn:
        legacy = [(row["nationality"], row["treasury"]) for row in conn.execute(
            "SELECT t.nationality, t.user_id AS treasury, u.cash FROM country_treasuries t "
            "JOIN users u ON u.user_id=t.nationality WHERE t.user_id<>t.nationality AND u.role='nation'")
            if Decimal(str(row["cash"] or 0)) == 0]
    merged = []
    for nation, treasury in legacy:
        try:
            with db.atomic() as conn:
                if _owned_rows(conn, nation):
                    continue
                tables = set(_tables(conn))
                if "web_auth_credentials" in tables:
                    # The treasury's own login is the country's login; an unused duplicate goes away.
                    if conn.execute("SELECT 1 FROM web_auth_credentials WHERE user_id=?", (treasury,)).fetchone():
                        conn.execute("DELETE FROM web_auth_credentials WHERE user_id=?", (nation,))
                    else:
                        conn.execute("UPDATE web_auth_credentials SET user_id=? WHERE user_id=?", (treasury, nation))
                # credit_events is an append-only audit log without foreign keys: its rows stay as history.
                conn.execute("DELETE FROM users WHERE user_id=?", (nation,))
            merged.append(nation)
        except Exception:
            # Leave the leftover untouched rather than risk partial data.
            continue
    return merged
