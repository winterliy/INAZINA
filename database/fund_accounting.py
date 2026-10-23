"""Fund cash accounts and their append-only, currency-aware subledger."""

import time
from decimal import Decimal

from functions.money import currency_precision, from_minor_units, to_minor_units

PREFIX = "__fund__:"
MAX_MONEY = 2**53 - 1


class FundCashLedger:
    """Transaction-bound service for fund cash balances and postings."""

    def __init__(self, conn):
        self.conn = conn

    def account(self, owner):
        if not str(owner).startswith(PREFIX):
            return None
        return self.conn.execute(
            "SELECT * FROM fund_accounts WHERE owner_id=?", (str(owner),)
        ).fetchone()

    def balance(self, owner, currency_code=None):
        fund = self.account(owner)
        if not fund:
            return 0
        if currency_code is None:
            currency_code = self.conn.execute(
                "SELECT quote_currency FROM investment WHERE product_code=?",
                (fund["product_code"],),
            ).fetchone()[0]
        row = self.conn.execute(
            "SELECT balance_minor_units FROM fund_cash_currency_balances "
            "WHERE product_code=? AND currency_code=?",
            (fund["product_code"], str(currency_code).upper()),
        ).fetchone()
        return int(row[0]) if row else 0

    def post(self, owner, amount, currency_code=None, event="settlement",
             reference=None, recorded_epoch=None):
        fund = self.account(owner)
        if not fund:
            raise ValueError("등록된 펀드 계정이 아닙니다.")
        if currency_code is None:
            currency_code = self.conn.execute(
                "SELECT quote_currency FROM investment WHERE product_code=?",
                (fund["product_code"],),
            ).fetchone()[0]
        currency = str(currency_code).upper()
        places = currency_precision(self.conn, currency)
        delta = to_minor_units(amount, places)
        old = self.balance(owner, currency)
        new = old + delta
        product_currency = str(self.conn.execute(
            "SELECT quote_currency FROM investment WHERE product_code=?",
            (fund["product_code"],),
        ).fetchone()[0]).upper()
        if not -MAX_MONEY <= new <= MAX_MONEY:
            raise ValueError("펀드 현금 잔액 한도를 초과합니다.")
        if new < 0:
            raise ValueError(f"펀드 {currency} 사용 가능 현금이 부족합니다.")
        if currency == product_currency and new > MAX_MONEY:
            raise ValueError("펀드 상품통화 현금 잔액 한도를 초과합니다.")
        now = int(time.time() if recorded_epoch is None else recorded_epoch)
        self.conn.execute(
            "INSERT INTO fund_cash_currency_balances "
            "(product_code,currency_code,decimal_places,balance_minor_units) "
            "VALUES (?,?,?,?) ON CONFLICT(product_code,currency_code) DO UPDATE SET "
            "decimal_places=excluded.decimal_places,balance_minor_units=excluded.balance_minor_units",
            (fund["product_code"], currency, places, new),
        )
        if currency == product_currency:
            self.conn.execute("UPDATE fund_accounts SET cash=? WHERE product_code=?",
                              (new, fund["product_code"]))
        major_delta = from_minor_units(delta, places)
        major_balance = from_minor_units(new, places)
        cur = self.conn.execute(
            "INSERT INTO fund_cash_ledger(product_code,event,amount,balance,reference,recorded_epoch,"
            "currency_code,decimal_places,amount_minor_units,balance_minor_units) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (fund["product_code"], str(event), float(major_delta), float(major_balance),
             reference, now, currency, places, delta, new),
        )
        return int(cur.lastrowid)

    def deposit(self, owner, amount, currency_code=None, event="settlement",
                reference=None, recorded_epoch=None):
        if Decimal(str(amount)) < 0:
            raise ValueError("입금액은 음수일 수 없습니다.")
        return self.post(owner, amount, currency_code, event, reference, recorded_epoch)

    def withdraw(self, owner, amount, currency_code=None, event="settlement",
                 reference=None, recorded_epoch=None):
        if Decimal(str(amount)) < 0:
            raise ValueError("출금액은 음수일 수 없습니다.")
        return self.post(owner, -Decimal(str(amount)), currency_code, event,
                         reference, recorded_epoch)


def account(conn, owner):
    return FundCashLedger(conn).account(owner)


def change_cash(conn, owner, delta, event="settlement", reference=None):
    """Compatibility boundary for existing integer fund cash call sites."""
    if isinstance(delta, bool) or int(delta) != delta:
        raise ValueError("기존 펀드 결제 경로는 정수 기준 통화만 허용합니다.")
    product_code = str(owner)[len(PREFIX):]
    currency = conn.execute("SELECT quote_currency FROM investment WHERE product_code=?",
                            (product_code,)).fetchone()
    if not currency:
        raise ValueError("펀드 상품 통화를 찾을 수 없습니다.")
    return FundCashLedger(conn).post(owner, int(delta), currency[0], event, reference)


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS fund_accounts (
        product_code TEXT PRIMARY KEY REFERENCES investment(product_code),
        owner_id TEXT NOT NULL UNIQUE CHECK(owner_id='__fund__:' || product_code),
        cash INTEGER NOT NULL DEFAULT 0 CHECK(typeof(cash)='integer' AND cash BETWEEN 0 AND 9007199254740991),
        initial_nav INTEGER NOT NULL CHECK(initial_nav>0),
        created_epoch INTEGER NOT NULL
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS fund_cash_ledger (
        entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_code TEXT NOT NULL REFERENCES fund_accounts(product_code),
        event TEXT NOT NULL, amount INTEGER NOT NULL, balance INTEGER NOT NULL,
        reference TEXT, recorded_epoch INTEGER NOT NULL,
        currency_code TEXT NOT NULL,
        decimal_places INTEGER NOT NULL DEFAULT 0,
        amount_minor_units INTEGER, balance_minor_units INTEGER
    )""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(fund_cash_ledger)")}
    for name, declaration in (
        ("currency_code", "TEXT"),
        ("decimal_places", "INTEGER NOT NULL DEFAULT 0"),
        ("amount_minor_units", "INTEGER"),
        ("balance_minor_units", "INTEGER"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE fund_cash_ledger ADD COLUMN {name} {declaration}")
    db.execute("""CREATE TABLE IF NOT EXISTS fund_cash_currency_balances (
        product_code TEXT NOT NULL,
        currency_code TEXT NOT NULL,
        decimal_places INTEGER NOT NULL CHECK(decimal_places BETWEEN 0 AND 6),
        balance_minor_units INTEGER NOT NULL CHECK(typeof(balance_minor_units)='integer'),
        PRIMARY KEY(product_code,currency_code)
    )""")
    db.execute("UPDATE fund_cash_ledger SET amount_minor_units=amount,balance_minor_units=balance "
               "WHERE amount_minor_units IS NULL OR balance_minor_units IS NULL")
    db.execute("""INSERT OR IGNORE INTO fund_cash_currency_balances
        (product_code,currency_code,decimal_places,balance_minor_units)
        SELECT f.product_code,i.quote_currency,0,f.cash
        FROM fund_accounts f JOIN investment i USING(product_code)""")
    db.execute("""INSERT OR IGNORE INTO fund_cash_currency_balances
        (product_code,currency_code,decimal_places,balance_minor_units)
        SELECT l.product_code,l.currency_code,l.decimal_places,l.balance_minor_units
        FROM fund_cash_ledger l WHERE l.entry_id=(
            SELECT MAX(l2.entry_id) FROM fund_cash_ledger l2
            WHERE l2.product_code=l.product_code AND l2.currency_code=l.currency_code)""")
    db.execute("CREATE INDEX IF NOT EXISTS fund_ledger_product ON fund_cash_ledger(product_code,entry_id)")
    for operation in ("UPDATE", "DELETE"):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS fund_ledger_no_{operation.lower()}
            BEFORE {operation} ON fund_cash_ledger BEGIN
            SELECT RAISE(ABORT,'Fund cash ledger is append-only'); END""")
    # Never allow a fund identifier to acquire citizenship, login or basic income.
    for operation in ("INSERT", "UPDATE OF user_id"):
        suffix = operation.split()[0].lower()
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS users_no_fund_{suffix}
            BEFORE {operation} ON users WHEN substr(NEW.user_id,1,9)='__fund__:'
            BEGIN SELECT RAISE(ABORT,'Fund owners are not citizens'); END""")
