"""통장 종류별 계약 규칙 (managers/deposit_accounts.py KIND_TERMS).

Every account copies its product's kind and terms at opening. The rules that
must hold however money moves are SQLite triggers, so no payment path can skip
them: 정기예금 takes no further deposits, locked kinds release money only on
closing, and the deposit limits of 자유적금·청약·ISA·IRP are checked against the
contribution ledger. Code that legitimately moves locked money (closing,
deposit-insurance resolution, tax at closing) marks the account in
account_rule_bypass for the duration of its own transaction.
"""
import json

from .account_kind_terms import CONTRIBUTION_KINDS, KIND_TERMS, LOCKED_KINDS, ONE_PER_PERSON_KINDS

NOW = "CAST(strftime('%s','now') AS REAL)"
OPENED = "CAST(strftime('%s',NEW.created_at) AS REAL)"


def _kinds(kinds):
    return ",".join(f"'{kind}'" for kind in kinds)


def _term(name):
    return f"json_extract(NEW.kind_terms,'$.{name}')"


def _contributed(where):
    return ("(SELECT COALESCE(SUM(units),0) FROM account_contributions c "
            f"WHERE c.account_number=NEW.account_number{where})")


ACCOUNT_COLUMNS = {
    "account_kind": "TEXT",
    "kind_terms": "TEXT",
    # 마이너스통장: amount borrowed against the limit (balance never goes below 0).
    "overdraft_units": "INTEGER NOT NULL DEFAULT 0 CHECK(overdraft_units >= 0)",
    "overdraft_limit_units": "INTEGER NOT NULL DEFAULT 0 CHECK(overdraft_limit_units >= 0)",
    "overdraft_interest_units": "REAL NOT NULL DEFAULT 0",  # accrued (units), not yet charged
    "overdraft_epoch": "REAL",  # interest accrued up to here
    "overdraft_charged_epoch": "REAL",  # last monthly interest charge
    "overdraft_peak_units": "INTEGER NOT NULL DEFAULT 0",  # highest use in the review month
    "overdraft_review_epoch": "REAL",
    # 하루 중 최고 사용 금액 기준 이자: the KST day being accrued and its peak net use.
    "overdraft_day": "INTEGER",
    "overdraft_day_max_units": "INTEGER NOT NULL DEFAULT 0",
    # ISA·IRP 과세이연: interest earned untaxed until closing.
    "deferred_interest_units": "INTEGER NOT NULL DEFAULT 0",
    # IRP: contributions that received a 세액공제 refund (taxed again at early closing).
    "credited_contribution_units": "INTEGER NOT NULL DEFAULT 0",
}


def install(db):
    """Create the kind columns, ledgers and rule triggers (idempotent)."""
    with db.lock, db.conn:
        conn = db.conn
        columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
        for column, declaration in ACCOUNT_COLUMNS.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {declaration}")
        if "kind_terms" not in {row[1] for row in conn.execute("PRAGMA table_info(saving)")}:
            conn.execute("ALTER TABLE saving ADD COLUMN kind_terms TEXT")  # JSON overrides of KIND_TERMS
        conn.execute("CREATE TABLE IF NOT EXISTS account_kind_defaults (kind TEXT PRIMARY KEY, terms TEXT NOT NULL)")
        conn.execute("DELETE FROM account_kind_defaults")
        conn.executemany("INSERT INTO account_kind_defaults(kind,terms) VALUES (?,?)",
                         [(kind, json.dumps(terms)) for kind, terms in KIND_TERMS.items()])
        conn.execute("""CREATE TABLE IF NOT EXISTS account_contributions (
            contribution_id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_number TEXT NOT NULL, user_id TEXT NOT NULL, account_kind TEXT NOT NULL,
            units INTEGER NOT NULL CHECK(units > 0), epoch REAL NOT NULL,
            credit_units INTEGER NOT NULL DEFAULT 0, credited_epoch REAL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_account_contributions_account "
                     "ON account_contributions(account_number, epoch)")
        conn.execute("""CREATE TABLE IF NOT EXISTS overdraft_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, account_number TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('draw','repay','interest')),
            units INTEGER NOT NULL CHECK(units > 0), epoch REAL NOT NULL, posted INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("CREATE TABLE IF NOT EXISTS account_rule_bypass (account_number TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM account_rule_bypass")
        _install_triggers(conn)
        # Accounts opened before kinds existed take their product's kind now.
        conn.execute(f"UPDATE accounts SET {_snapshot_sql('accounts.product_code')} "
                     "WHERE account_kind IS NULL AND account_type='saving'")


def _snapshot_sql(product):
    kind = f"(SELECT s.account_kind FROM saving s WHERE s.product_code={product})"
    terms = (f"json_patch(json_patch(COALESCE((SELECT d.terms FROM account_kind_defaults d WHERE d.kind={kind}),'{{}}'),"
             f"COALESCE((SELECT s.kind_terms FROM saving s WHERE s.product_code={product}),'{{}}')),"
             f"(SELECT json_object('installment_amount',COALESCE(s.installment_amount,0),"
             f"'installment_cycle_seconds',COALESCE(s.installment_cycle_seconds,0)) "
             f"FROM saving s WHERE s.product_code={product}))")
    return f"account_kind={kind}, kind_terms={terms}"


def _install_triggers(conn):
    for name in ("trg_account_kind_open_rules", "trg_account_kind_snapshot", "trg_account_kind_terms_effects",
                 "trg_account_kind_contribution", "trg_account_kind_inflow", "trg_account_kind_outflow",
                 "trg_account_kind_overdraft_repay", "trg_account_kind_overdraft_use"):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    product_kind = "(SELECT s.account_kind FROM saving s WHERE s.product_code=NEW.product_code)"
    conn.execute(f"""CREATE TRIGGER trg_account_kind_open_rules BEFORE INSERT ON accounts
        WHEN NEW.account_type='saving' BEGIN
        SELECT RAISE(ABORT,'주택청약·ISA는 1인 1계좌만 가입할 수 있습니다. 이미 가입한 계좌가 있습니다.')
         WHERE {product_kind} IN ({_kinds(ONE_PER_PERSON_KINDS)})
           AND EXISTS (SELECT 1 FROM accounts o WHERE o.user_id=NEW.user_id AND o.account_kind={product_kind});
        SELECT RAISE(ABORT,'종합통장(마이너스통장)은 신용점수가 상품 기준 이상이어야 개설할 수 있습니다.')
         WHERE {product_kind}='combined'
           AND COALESCE((SELECT credit_score FROM users WHERE user_id=NEW.user_id),0) <
               COALESCE(json_extract(json_patch(
                   COALESCE((SELECT terms FROM account_kind_defaults WHERE kind='combined'),'{{}}'),
                   COALESCE((SELECT kind_terms FROM saving WHERE product_code=NEW.product_code),'{{}}')),
                 '$.min_credit_score'),0);
        END""")
    # The opening deposit is the first contribution and must fit the same limits.
    kind_of = "(SELECT r.account_kind FROM accounts r WHERE r.rowid=NEW.rowid)"
    opening = "CAST(ROUND(COALESCE(NEW.principal,0)*1000000) AS INTEGER)"

    def opened_term(name):
        return f"CAST(ROUND(COALESCE(json_extract((SELECT r.kind_terms FROM accounts r WHERE r.rowid=NEW.rowid),'$.{name}'),0)*1000000) AS INTEGER)"

    conn.execute(f"""CREATE TRIGGER trg_account_kind_snapshot AFTER INSERT ON accounts
        WHEN NEW.account_type='saving' AND NEW.account_kind IS NULL BEGIN
        UPDATE accounts SET {_snapshot_sql('NEW.product_code')} WHERE rowid=NEW.rowid;
        SELECT RAISE(ABORT,'정기적금의 첫 납입은 약정한 납입 금액이어야 합니다.')
         WHERE {kind_of}='fixed_installment' AND {opening} > 0 AND {opening} <> {opened_term('installment_amount')};
        SELECT RAISE(ABORT,'자유적금의 납입 기간 한도를 넘습니다.')
         WHERE {kind_of}='free_installment' AND {opened_term('period_limit')} > 0 AND {opening} > {opened_term('period_limit')};
        SELECT RAISE(ABORT,'주택청약은 1회 최소 납입 금액 이상 입금해야 합니다.')
         WHERE {kind_of}='housing' AND {opening} > 0 AND {opening} < {opened_term('min_payment')};
        SELECT RAISE(ABORT,'주택청약은 한 달(납입 기간)에 최대 납입 금액까지만 넣을 수 있습니다.')
         WHERE {kind_of}='housing' AND {opening} > {opened_term('max_payment')};
        SELECT RAISE(ABORT,'ISA 연간 납입한도를 넘습니다.')
         WHERE {kind_of}='isa' AND ({opening} > {opened_term('annual_limit')} OR {opening} > {opened_term('total_limit')});
        SELECT RAISE(ABORT,'IRP 연간 납입한도를 넘습니다.')
         WHERE {kind_of}='irp' AND {opening} > {opened_term('annual_limit')};
        INSERT INTO account_contributions(account_number,user_id,account_kind,units,epoch)
        SELECT NEW.account_number,NEW.user_id,{kind_of},{opening},{NOW}
         WHERE {opening} > 0 AND {kind_of} IN ({_kinds(CONTRIBUTION_KINDS)});
        END""")
    # Terms that change the account itself: CMA is outside deposit insurance, and
    # a 마이너스통장 starts with its full limit.
    conn.execute(f"""CREATE TRIGGER trg_account_kind_terms_effects AFTER UPDATE OF kind_terms ON accounts
        WHEN NEW.kind_terms IS NOT NULL AND OLD.kind_terms IS NULL BEGIN
        UPDATE accounts SET
          deposit_insured=CASE WHEN {_term('deposit_insured')} IS NOT NULL THEN {_term('deposit_insured')} ELSE deposit_insured END,
          overdraft_limit_units=CASE WHEN NEW.account_kind='combined'
              THEN CAST(ROUND(COALESCE({_term('overdraft_limit')},0)*1000000) AS INTEGER) ELSE 0 END,
          overdraft_epoch={NOW}, overdraft_charged_epoch={NOW}, overdraft_review_epoch={NOW}
        WHERE rowid=NEW.rowid;
        END""")
    bypass = "NOT EXISTS (SELECT 1 FROM account_rule_bypass b WHERE b.account_number=NEW.account_number)"
    delta = "CAST(ROUND((NEW.principal-OLD.principal)*1000000) AS INTEGER)"
    period = (f"CAST(({NOW}-{OPENED})/(COALESCE({_term('period_days')},30)*86400) AS INTEGER)")
    period_start = f"({OPENED}+{period}*COALESCE({_term('period_days')},30)*86400)"
    year_start = f"({OPENED}+CAST(({NOW}-{OPENED})/31536000 AS INTEGER)*31536000)"
    years_open = f"(CAST(({NOW}-{OPENED})/31536000 AS INTEGER)+1)"
    maturity = ("(SELECT COALESCE(s.maturity_days,0) FROM saving s WHERE s.product_code=NEW.product_code)")
    conn.execute(f"""CREATE TRIGGER trg_account_kind_inflow BEFORE UPDATE OF principal ON accounts
        WHEN NEW.principal > OLD.principal AND NEW.account_kind IN ({_kinds(CONTRIBUTION_KINDS)}) AND {bypass} BEGIN
        SELECT RAISE(ABORT,'정기예금은 가입할 때 한 번만 예치합니다. 추가 입금할 수 없습니다.')
         WHERE NEW.account_kind='time_deposit';
        SELECT RAISE(ABORT,'정기적금은 약정한 납입 금액만 입금할 수 있습니다.')
         WHERE NEW.account_kind='fixed_installment'
           AND {delta} <> CAST(ROUND(COALESCE({_term('installment_amount')},0)*1000000) AS INTEGER);
        SELECT RAISE(ABORT,'만기가 지난 적금에는 입금할 수 없습니다.')
         WHERE NEW.account_kind IN ('fixed_installment','free_installment') AND {maturity} > 0
           AND {NOW} >= {OPENED}+{maturity}*86400;
        SELECT RAISE(ABORT,'자유적금의 이번 납입 기간 한도를 넘습니다.')
         WHERE NEW.account_kind='free_installment' AND COALESCE({_term('period_limit')},0) > 0
           AND {_contributed(f' AND c.epoch>={period_start}')}+{delta}
               > CAST(ROUND({_term('period_limit')}*1000000) AS INTEGER);
        SELECT RAISE(ABORT,'주택청약은 1회 최소 납입 금액 이상 입금해야 합니다.')
         WHERE NEW.account_kind='housing' AND {delta} < CAST(ROUND(COALESCE({_term('min_payment')},0)*1000000) AS INTEGER);
        SELECT RAISE(ABORT,'주택청약은 한 달(납입 기간)에 최대 납입 금액까지만 넣을 수 있습니다.')
         WHERE NEW.account_kind='housing'
           AND {_contributed(f' AND c.epoch>={period_start}')}+{delta}
               > CAST(ROUND(COALESCE({_term('max_payment')},0)*1000000) AS INTEGER);
        SELECT RAISE(ABORT,'ISA 총 납입한도를 넘습니다.')
         WHERE NEW.account_kind='isa'
           AND {_contributed('')}+{delta} > CAST(ROUND(COALESCE({_term('total_limit')},0)*1000000) AS INTEGER);
        SELECT RAISE(ABORT,'ISA 연간 납입한도(이월분 포함)를 넘습니다.')
         WHERE NEW.account_kind='isa' AND COALESCE({_term('carry_over')},1)=1
           AND {_contributed('')}+{delta}
               > CAST(ROUND(COALESCE({_term('annual_limit')},0)*{years_open}*1000000) AS INTEGER);
        SELECT RAISE(ABORT,'ISA 연간 납입한도를 넘습니다.')
         WHERE NEW.account_kind='isa' AND COALESCE({_term('carry_over')},1)=0
           AND {_contributed(f' AND c.epoch>={year_start}')}+{delta}
               > CAST(ROUND(COALESCE({_term('annual_limit')},0)*1000000) AS INTEGER);
        SELECT RAISE(ABORT,'IRP 연간 납입한도를 넘습니다.')
         WHERE NEW.account_kind='irp'
           AND {_contributed(f' AND c.epoch>={year_start}')}+{delta}
               > CAST(ROUND(COALESCE({_term('annual_limit')},0)*1000000) AS INTEGER);
        END""")
    conn.execute(f"""CREATE TRIGGER trg_account_kind_contribution AFTER UPDATE OF principal ON accounts
        WHEN NEW.principal > OLD.principal AND NEW.account_kind IN ({_kinds(CONTRIBUTION_KINDS)}) AND {bypass} BEGIN
        INSERT INTO account_contributions(account_number,user_id,account_kind,units,epoch)
        VALUES (NEW.account_number,NEW.user_id,NEW.account_kind,{delta},{NOW});
        END""")
    # Money leaves a locked account only by closing. ISA lets the owner take out
    # up to the paid-in principal (원금 범위 내 중도인출) but never the earnings.
    conn.execute(f"""CREATE TRIGGER trg_account_kind_outflow BEFORE UPDATE OF balance_units ON accounts
        WHEN NEW.balance_units < OLD.balance_units AND {bypass} BEGIN
        SELECT RAISE(ABORT,'이 통장은 만기·해지 전에는 일부 출금할 수 없습니다. 해지로만 찾을 수 있습니다.')
         WHERE NEW.account_kind IN ({_kinds(LOCKED_KINDS)});
        SELECT RAISE(ABORT,'ISA는 납입 원금 범위 안에서만 중도인출할 수 있습니다.')
         WHERE NEW.account_kind='isa'
           AND OLD.balance_units-NEW.balance_units > CAST(ROUND((OLD.principal-NEW.principal)*1000000) AS INTEGER)+1;
        END""")
    # 마이너스통장 interest is charged on the highest net use of each day
    # (빌린 금액 − 잔액). Whenever the balance or the debt changes, finished
    # days are accrued and today's peak is updated. Repayment itself is a
    # bank job (managers/overdraft.py), so payments are never altered midway.
    use_old = "MAX(0,OLD.overdraft_units-OLD.balance_units)"
    use_new = "MAX(0,NEW.overdraft_units-NEW.balance_units)"
    today = "CAST((CAST(strftime('%s','now') AS REAL)+32400)/86400 AS INTEGER)"
    rate = (f"((COALESCE({_term('overdraft_rate')},0)+CASE WHEN OLD.overdraft_units>OLD.overdraft_limit_units "
            f"THEN COALESCE({_term('late_spread')},0) ELSE 0 END)/100.0/365.0)")
    conn.execute(f"""CREATE TRIGGER trg_account_kind_overdraft_use AFTER UPDATE OF balance_units, overdraft_units, overdraft_epoch
        ON accounts WHEN NEW.account_kind='combined' BEGIN
        UPDATE accounts SET
          overdraft_interest_units=overdraft_interest_units+CASE
            WHEN OLD.overdraft_day IS NULL OR {today}<=OLD.overdraft_day THEN 0
            ELSE (OLD.overdraft_day_max_units+({today}-OLD.overdraft_day-1)*{use_old})*{rate} END,
          overdraft_day_max_units=CASE
            WHEN OLD.overdraft_day IS NULL OR {today}>OLD.overdraft_day THEN MAX({use_old},{use_new})
            ELSE MAX(OLD.overdraft_day_max_units,{use_new}) END,
          overdraft_day={today}
        WHERE rowid=NEW.rowid;
        END""")
