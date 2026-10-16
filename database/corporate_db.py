"""Issuer cash and enduring preferred-share entitlements."""
from .db_manager import DBManager


class CorporateDB(DBManager):
    def __init__(self):
        super().__init__('corporate')
        self._init_db()

    def _init_db(self):
        # balance is spendable cash; locked_amount is a separate reserve, not a
        # deduction from balance. Capital is deliberately never copied here.
        self.execute('''CREATE TABLE IF NOT EXISTS issuer_cash_accounts (
            company_code TEXT PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
            locked_amount INTEGER NOT NULL DEFAULT 0 CHECK(locked_amount >= 0))''')
        self.execute('''CREATE TABLE IF NOT EXISTS issuer_cash_transfers (
            transfer_id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_code TEXT NOT NULL, actor_id TEXT NOT NULL,
            old_balance INTEGER NOT NULL, new_balance INTEGER NOT NULL,
            delta INTEGER NOT NULL, recorded_epoch INTEGER NOT NULL)''')
        self.execute('''CREATE TABLE IF NOT EXISTS preferred_dividend_claims (
            claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
            issuer_code TEXT NOT NULL, asset_code TEXT NOT NULL,
            distribution_id INTEGER NOT NULL, user_id TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            gross_amount INTEGER NOT NULL CHECK(gross_amount > 0),
            paid_amount INTEGER NOT NULL DEFAULT 0
                CHECK(paid_amount >= 0 AND paid_amount <= gross_amount),
            quote_currency TEXT NOT NULL DEFAULT 'NWX',
            funding_source TEXT NOT NULL CHECK(funding_source IN ('treasury','issuer')),
            due_epoch INTEGER NOT NULL, created_epoch INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'unpaid'
                CHECK(status IN ('unpaid','partial','paid','written_off')),
            UNIQUE(distribution_id,user_id))''')
        self.execute('''CREATE INDEX IF NOT EXISTS idx_preferred_claims_issuer
            ON preferred_dividend_claims(issuer_code,status,due_epoch,claim_id)''')
        self.execute('''CREATE INDEX IF NOT EXISTS idx_preferred_claims_user
            ON preferred_dividend_claims(user_id,status)''')
        self.execute('''CREATE TABLE IF NOT EXISTS corporate_liquidation_payments (
            payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_key TEXT NOT NULL, issuer_code TEXT NOT NULL,
            asset_code TEXT NOT NULL, user_id TEXT NOT NULL, kind TEXT NOT NULL,
            quantity INTEGER NOT NULL, claim_amount INTEGER NOT NULL,
            paid_amount INTEGER NOT NULL, tax_amount INTEGER NOT NULL DEFAULT 0,
            recorded_epoch INTEGER NOT NULL)''')
        self.execute('''CREATE INDEX IF NOT EXISTS idx_liquidation_payments_user
            ON corporate_liquidation_payments(user_id,recorded_epoch)''')
        self.execute('''CREATE TABLE IF NOT EXISTS corporate_action_records (
            record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_key TEXT NOT NULL UNIQUE,
            action_kind TEXT NOT NULL,
            source_code TEXT NOT NULL,
            target_code TEXT,
            payload_json TEXT NOT NULL,
            created_epoch INTEGER NOT NULL)''')
        # A company's capital account (company:<code>) shares one cash balance with
        # its issuer cash ledger; each side mirrors the other's changes.
        self.execute('''CREATE TABLE IF NOT EXISTS company_accounts (
            company_code TEXT PRIMARY KEY,
            user_id TEXT NOT NULL UNIQUE)''')
        self.execute('''CREATE TRIGGER IF NOT EXISTS company_cash_to_issuer
            AFTER UPDATE OF cash ON users
            WHEN NEW.cash IS NOT OLD.cash AND EXISTS (SELECT 1 FROM company_accounts WHERE user_id=NEW.user_id)
            BEGIN
                UPDATE issuer_cash_accounts SET balance=CAST(ROUND(NEW.cash) AS INTEGER)
                WHERE company_code=(SELECT company_code FROM company_accounts WHERE user_id=NEW.user_id)
                AND balance<>CAST(ROUND(NEW.cash) AS INTEGER);
            END''')
        self.execute('''CREATE TRIGGER IF NOT EXISTS issuer_cash_to_company
            AFTER UPDATE OF balance ON issuer_cash_accounts
            WHEN NEW.balance IS NOT OLD.balance
            BEGIN
                UPDATE users SET cash=NEW.balance
                WHERE user_id=(SELECT user_id FROM company_accounts WHERE company_code=NEW.company_code)
                AND cash<>NEW.balance;
            END''')
        # Income a company account received that its books must classify: dividends
        # (영업외수익) and intra-group transfers, kept for consolidation eliminations.
        legacy = self.execute("SELECT sql FROM sqlite_master WHERE name='company_income_log'")
        if legacy and 'overdue_interest' not in (legacy[0]['sql'] or ''):
            # Earlier versions did not list every kind; rebuild with the full set.
            self.execute("ALTER TABLE company_income_log RENAME TO company_income_log_v1")
        self.execute('''CREATE TABLE IF NOT EXISTS company_income_log (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_code TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('dividend','transfer_in','transfer_out','interest_income','interest_expense','overdue_interest')),
            counterparty TEXT,
            gross_nwx INTEGER NOT NULL,
            tax_nwx INTEGER NOT NULL DEFAULT 0,
            booked INTEGER NOT NULL DEFAULT 0,
            recorded_epoch INTEGER NOT NULL)''')
        if self.execute("SELECT 1 FROM sqlite_master WHERE name='company_income_log_v1'"):
            self.execute("INSERT INTO company_income_log SELECT * FROM company_income_log_v1")
            self.execute("DROP TABLE company_income_log_v1")
        # Shareholder register (주주명부): who is entitled is fixed when the register
        # closes, before the payment. Regular dividends close 10 minutes before the
        # due time; a special dividend closes at the command and pays 10 minutes later.
        self.execute('''CREATE TABLE IF NOT EXISTS shareholder_registers (
            register_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_key TEXT NOT NULL UNIQUE,
            asset_code TEXT NOT NULL,
            dividend_kind TEXT NOT NULL CHECK(dividend_kind IN ('regular','special')),
            record_epoch INTEGER NOT NULL,
            pay_epoch INTEGER NOT NULL,
            per_share INTEGER NOT NULL DEFAULT 0 CHECK(per_share >= 0),
            status TEXT NOT NULL DEFAULT 'closed' CHECK(status IN ('closed','paid','failed')),
            actor_id TEXT,
            note TEXT,
            created_epoch INTEGER NOT NULL)''')
        self.execute('''CREATE TABLE IF NOT EXISTS shareholder_register_entries (
            register_id INTEGER NOT NULL REFERENCES shareholder_registers(register_id),
            user_id TEXT NOT NULL,
            quantity REAL NOT NULL CHECK(quantity > 0),
            PRIMARY KEY(register_id, user_id))''')
        self.execute('CREATE INDEX IF NOT EXISTS idx_registers_due ON shareholder_registers(status, pay_epoch)')
        self.execute('''CREATE TABLE IF NOT EXISTS dividend_distributions (
            distribution_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_key TEXT NOT NULL UNIQUE,
            dividend_kind TEXT NOT NULL CHECK(dividend_kind IN ('regular','special')),
            asset_code TEXT NOT NULL,
            due_epoch INTEGER NOT NULL,
            per_share INTEGER NOT NULL DEFAULT 0 CHECK(per_share >= 0),
            dividend_mode TEXT NOT NULL CHECK(dividend_mode IN ('fixed','percent')),
            dividend_percent REAL,
            reference_price INTEGER,
            gross_amount INTEGER NOT NULL CHECK(gross_amount >= 0),
            entitlement_amount INTEGER NOT NULL CHECK(entitlement_amount >= 0),
            deferred_amount INTEGER NOT NULL DEFAULT 0 CHECK(deferred_amount >= 0),
            quote_currency TEXT NOT NULL,
            funding_source TEXT NOT NULL CHECK(funding_source IN ('treasury','issuer')),
            issuer_code TEXT NOT NULL,
            created_epoch INTEGER NOT NULL)''')
        self.execute('''CREATE TABLE IF NOT EXISTS dividend_payments (
            payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            distribution_id INTEGER NOT NULL REFERENCES dividend_distributions(distribution_id),
            user_id TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            gross_amount INTEGER NOT NULL CHECK(gross_amount >= 0),
            tax_amount INTEGER NOT NULL CHECK(tax_amount >= 0),
            net_amount_units INTEGER NOT NULL CHECK(net_amount_units >= 0),
            quote_currency TEXT NOT NULL,
            created_epoch INTEGER NOT NULL,
            UNIQUE(distribution_id,user_id))''')
        self.execute('''CREATE INDEX IF NOT EXISTS idx_dividend_distributions_asset
            ON dividend_distributions(asset_code,created_epoch)''')
        self.execute('''CREATE INDEX IF NOT EXISTS idx_dividend_payments_user
            ON dividend_payments(user_id,distribution_id)''')
        self.enforce_currency_money(
            'issuer_cash_accounts', ('balance','locked_amount'), 'company_code',
            "SELECT quote_currency FROM company WHERE company_code=NEW.company_code",
        )
        self.enforce_currency_money(
            'preferred_dividend_claims', ('gross_amount','paid_amount'), 'claim_id',
            'NEW.quote_currency',
        )
        self.enforce_currency_money(
            'dividend_distributions',
            ('per_share','reference_price','gross_amount','entitlement_amount','deferred_amount'),
            'distribution_id', 'NEW.quote_currency',
        )
        self.enforce_currency_money(
            'dividend_payments', ('gross_amount','tax_amount'), 'payment_id',
            'NEW.quote_currency',
        )
