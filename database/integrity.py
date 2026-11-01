"""Referential integrity check for the single Inazina database.

Only a few tables declare SQLite foreign keys; most references (user IDs,
currencies, countries, products) are plain text columns. ``check`` verifies the
declared keys with ``PRAGMA foreign_key_check`` and the undeclared ones with the
rules below, and returns every orphan it finds.

Run ``python -m database.integrity`` to check the live database.
"""
import sqlite3
import sys
from pathlib import Path

USER_REFERENCES = {
    "user_id": ("accounts", "basic_income_payments", "chart_preferences", "corporate_liquidation_payments",
                "currency_transactions", "deposit_insurance_claims", "dividend_payments", "els_settlements",
                "els_subscriptions", "futures_positions", "loan_collateral_assets", "loans", "lottery_payouts",
                "lottery_purchases", "lottery_tickets", "market_orders", "option_positions",
                "preferred_dividend_claims", "product_settlements", "tax_transactions", "treasury_bond_holdings",
                "user_assets", "web_auth_credentials", "web_chart_drawings"),
    "writer_id": ("option_collateral_cash_lots", "option_collateral_lots", "option_contracts"),
}
CURRENCY_REFERENCES = {
    "quote_currency": ("accounts", "company", "dividend_distributions", "dividend_payments", "investment",
                       "liability", "loans", "market_assets", "market_orders", "option_contracts",
                       "preferred_dividend_claims", "saving", "treasury_bond_products"),
    "source_currency": ("basic_income_payments", "currency_transactions", "currency_transfer_transactions"),
    "target_currency": ("basic_income_payments", "currency_transactions", "currency_transfer_transactions"),
    "currency_code": ("financial_institution_ledger", "tax_transactions"),
}
COUNTRY_REFERENCES = {
    "nationality": ("users", "country_rate_history", "financial_holidays", "financial_institutions",
                    "market_exchanges", "market_rate_curve", "market_rate_curve_history",
                    "market_rate_pressure", "market_rate_pressure_history"),
    "rate_nationality": ("accounts", "liability", "loans", "saving"),
}
# exchange_id is not checked: market_exchanges rows are created lazily and
# MarketSessionManager.normalize_exchange accepts aliases.
# Every tradable or contractable code lives in exactly one of these catalogs.
PRODUCT_CATALOG = ("SELECT code FROM market_assets UNION SELECT company_code FROM company "
                   "UNION SELECT product_code FROM investment UNION SELECT product_code FROM saving "
                   "UNION SELECT product_code FROM liability UNION SELECT product_code FROM treasury_bond_products "
                   "UNION SELECT product_code FROM els_products UNION SELECT contract_code FROM option_contracts "
                   "UNION SELECT asset_code FROM currencies "
                   "WHERE asset_code IS NOT NULL")
PRODUCT_REFERENCES = {
    "product_code": ("accounts", "loans", "treasury_bond_holdings", "els_subscriptions", "futures_positions"),
    "asset_code": ("user_assets", "market_last_trade", "dividend_distributions",
                   "preferred_dividend_claims", "loan_collateral_assets"),
}
# Closed orders are history and may name products deleted or expired since.
OPEN_ORDERS = "status NOT IN ('cancelled','filled')"
OTHER_REFERENCES = (
    ("market_orders", "asset_code", PRODUCT_CATALOG, OPEN_ORDERS),
    ("loan_collateral_assets", "loan_id", "SELECT loan_id FROM loans"),
    ("loan_payment_schedule", "loan_id", "SELECT loan_id FROM loans"),
    ("deposit_insurance_claims", "account_number", "SELECT account_number FROM accounts"),
    ("els_subscriptions", "account_number", "SELECT account_number FROM accounts"),
)


def _rules():
    for column, tables in USER_REFERENCES.items():
        for table in tables:
            yield table, column, "SELECT user_id FROM users", None
    for column, tables in CURRENCY_REFERENCES.items():
        for table in tables:
            yield table, column, "SELECT code FROM currencies", None
    for column, tables in COUNTRY_REFERENCES.items():
        for table in tables:
            yield table, column, "SELECT nationality FROM country_currencies", None
    for column, tables in PRODUCT_REFERENCES.items():
        for table in tables:
            yield table, column, PRODUCT_CATALOG, None
    for rule in OTHER_REFERENCES:
        yield rule if len(rule) == 4 else (*rule, None)


def check(conn):
    """Return ``[(table, column, value, row_count), ...]`` for every dangling reference."""
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    problems = []
    for table, _rowid, parent, key in conn.execute("PRAGMA foreign_key_check"):
        problems.append((table, f"FK→{parent}#{key}", None, 1))
    for table, column, parents, where in _rules():
        if table not in existing:
            continue
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if column not in columns:
            continue
        rows = conn.execute(
            f'SELECT "{column}", COUNT(*) FROM "{table}" WHERE "{column}" IS NOT NULL AND "{column}"<>\'\' '
            f'AND "{column}" NOT IN ({parents}){f" AND {where}" if where else ""} GROUP BY "{column}"').fetchall()
        problems.extend((table, column, value, count) for value, count in rows)
    return problems


def main(path=None):
    path = Path(path or Path(__file__).resolve().parent / "inazina.db")
    from .unified import UnifiedConnection
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, factory=UnifiedConnection)
    conn.legacy_aliases=False
    try:
        problems = check(conn)
    finally:
        conn.close()
    for table, column, value, count in problems:
        print(f"{table}.{column} = {value!r} → 참조 대상 없음 ({count}행)")
    print(f"무결성 위반 {len(problems)}건")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
