"""Non-citizen fund owners sharing the exchange's holdings/order infrastructure."""
import time

PREFIX = "__fund__:"
MAX_MONEY = 2**53 - 1


def account(conn, owner):
    if not str(owner).startswith(PREFIX):
        return None
    return conn.execute("SELECT * FROM fund_accounts WHERE owner_id=?", (str(owner),)).fetchone()


def change_cash(conn, owner, delta, event="settlement", reference=None):
    """Change available cash and append its audit entry in the caller's transaction."""
    if isinstance(delta, bool) or int(delta) != delta:
        raise ValueError("펀드 현금은 정수 NWX여야 합니다.")
    delta = int(delta)
    changed = conn.execute(
        "UPDATE fund_accounts SET cash=cash+? WHERE owner_id=? AND cash+? BETWEEN 0 AND ?",
        (delta, str(owner), delta, MAX_MONEY),
    )
    if changed.rowcount != 1:
        raise ValueError("펀드 사용 가능 현금이 부족하거나 잔액 한도를 초과합니다.")
    row = account(conn, owner)
    if delta:
        conn.execute(
            "INSERT INTO fund_cash_ledger(product_code,event,amount,balance,reference,recorded_epoch) "
            "VALUES (?,?,?,?,?,?)",
            (row["product_code"], event, delta, row["cash"], reference, int(time.time())),
        )


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS fund_accounts (
        product_code TEXT PRIMARY KEY REFERENCES investment(product_code),
        owner_id TEXT NOT NULL UNIQUE CHECK(owner_id='__fund__:' || product_code),
        cash INTEGER NOT NULL DEFAULT 0 CHECK(typeof(cash)='integer' AND cash BETWEEN 0 AND 9007199254740991),
        initial_nav INTEGER NOT NULL CHECK(initial_nav>0),
        created_epoch INTEGER NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS fund_cash_ledger (
        entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_code TEXT NOT NULL REFERENCES fund_accounts(product_code),
        event TEXT NOT NULL, amount INTEGER NOT NULL, balance INTEGER NOT NULL,
        reference TEXT, recorded_epoch INTEGER NOT NULL
    )""")
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
