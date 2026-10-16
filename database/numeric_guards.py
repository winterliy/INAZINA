"""Database-level guards for numeric cells: no NaN, no infinity, no overflow.

Python's sqlite3 stores ``float('nan')`` as NULL and ``float('inf')`` as an
infinite REAL, and SQLite INTEGER is 64-bit. Input parsers already reject such
values, but computed values (price engines, interest, conversions) could still
reach a cell. These triggers refuse the write instead of letting a balance turn
into NULL (read back as 0), infinity, or a silently clamped integer.
"""

MONEY = 2**53 - 1                 # native amounts and prices (exact in a double)
MINOR = MONEY * 100               # cash in minor units (at most 2 decimals)
UNITS = 9_000_000_000_000_000_000  # x1,000,000 units, inside SQLite's int64

# (table, column, limit, nullable)
GUARDED = (
    ("users", "cash", MONEY, False),
    ("users", "cash_minor", MINOR, False),
    ("accounts", "balance_units", UNITS, False),
    ("market_orders", "limit_price", MONEY, True),
    ("market_orders", "reserved_cash", MONEY, True),
    ("market_orders", "quantity", MONEY, False),
    ("market_orders", "remaining_quantity", MONEY, False),
    ("user_assets", "quantity", MONEY, False),
    ("user_assets", "average_cost", MONEY, True),
    ("company", "share_price", MONEY, False),
    ("company", "total_shares", MONEY, False),
    ("company", "capital", MONEY, True),
    ("investment", "unit_price", MONEY, False),
    ("investment", "total_supply", MONEY, False),
    ("market_assets", "price", MONEY, False),
    ("market_assets", "total_supply", MONEY, True),
    ("treasury_bond_products", "market_price", MONEY, False),
    ("loans", "principal", MONEY, False),
    ("loans", "outstanding_balance", MONEY, False),
    ("issuer_cash_accounts", "balance", MONEY, False),
    ("issuer_cash_accounts", "locked_amount", MONEY, False),
    ("market_price_history", "price", MONEY, False),
    ("market_exchanges", "index_value", MONEY, True),
    ("futures_positions", "collateral", MONEY, False),
    ("fund_accounts", "cash", MONEY, False),
)


def install(db):
    """Create (or refresh) the guard triggers for every guarded column that exists."""
    for table, column, limit, nullable in GUARDED:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not exists or column not in {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}:
            continue
        null_check = "" if nullable else f"NEW.{column} IS NULL OR "
        # An infinite REAL is larger than any limit; NaN is stored as NULL.
        condition = (f"{null_check}(NEW.{column} IS NOT NULL AND (typeof(NEW.{column}) NOT IN ('integer','real') "
                     f"OR NEW.{column} > {limit} OR NEW.{column} < -{limit}))")
        message = f"숫자 저장 오류: {table}.{column} 값이 비었거나(NaN) 무한대·허용 범위를 넘었습니다."
        for event in ("INSERT", "UPDATE"):
            name = f"guard_{table}_{column}_{event.lower()}"
            when = f"BEFORE {event} OF {column} ON {table}" if event == "UPDATE" else f"BEFORE INSERT ON {table}"
            db.execute(f"DROP TRIGGER IF EXISTS {name}")
            db.execute(f"CREATE TRIGGER {name} {when} WHEN {condition} "
                       f"BEGIN SELECT RAISE(ABORT, '{message}'); END")
