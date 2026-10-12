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
        self.execute('''CREATE TABLE IF NOT EXISTS corporate_action_records (
            record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_key TEXT NOT NULL UNIQUE,
            action_kind TEXT NOT NULL,
            source_code TEXT NOT NULL,
            target_code TEXT,
            payload_json TEXT NOT NULL,
            created_epoch INTEGER NOT NULL)''')
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
        self.enforce_integer_money('issuer_cash_accounts', ('balance','locked_amount'), 'company_code')
