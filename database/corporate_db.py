"""Issuer cash and enduring preferred-share entitlements."""
from .db_manager import DBManager


class CorporateDB(DBManager):
    def __init__(self):
        super().__init__('corporate')
        self._init_db()

    def _migrate_dr_dividend_cash_minor_units(self, existing_columns):
        """Add canonical DR withholding amounts without rewriting legacy mirrors."""
        required = {
            "decimal_places": "INTEGER NOT NULL DEFAULT 0 CHECK(decimal_places BETWEEN 0 AND 6)",
            "gross_minor_units": "INTEGER NOT NULL DEFAULT 0 CHECK(typeof(gross_minor_units)='integer' AND gross_minor_units>=0)",
            "tax_minor_units": "INTEGER NOT NULL DEFAULT 0 CHECK(typeof(tax_minor_units)='integer' AND tax_minor_units>=0)",
            "net_minor_units": "INTEGER NOT NULL DEFAULT 0 CHECK(typeof(net_minor_units)='integer' AND net_minor_units>=0)",
        }
        missing = {name: declaration for name, declaration in required.items()
                   if name not in existing_columns}
        if not missing:
            return
        from decimal import Decimal
        from functions.money import currency_precision, to_minor_units

        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DROP TRIGGER IF EXISTS dr_dividend_withholding_no_update")
                for name, declaration in missing.items():
                    self.conn.execute(
                        f"ALTER TABLE dr_dividend_withholding_events ADD COLUMN {name} {declaration}"
                    )
                rows = self.conn.execute(
                    "SELECT event_id,gross_amount,tax_amount,net_amount_units,currency_code "
                    "FROM dr_dividend_withholding_events ORDER BY event_id"
                ).fetchall()
                for row in rows:
                    places = currency_precision(self.conn, row["currency_code"])
                    gross = to_minor_units(row["gross_amount"], places)
                    tax = to_minor_units(row["tax_amount"], places)
                    net_major = Decimal(int(row["net_amount_units"] or 0)) / Decimal(1_000_000)
                    net = to_minor_units(net_major, places)
                    if gross != tax + net:
                        raise ValueError(
                            "기존 DR 원천징수 금액이 통화 최소단위에서 일치하지 않습니다: "
                            f"event_id={row['event_id']}"
                        )
                    self.conn.execute(
                        "UPDATE dr_dividend_withholding_events SET decimal_places=?,"
                        "gross_minor_units=?,tax_minor_units=?,net_minor_units=? WHERE event_id=?",
                        (places, gross, tax, net, int(row["event_id"])),
                    )
                self.conn.execute("""CREATE TRIGGER dr_dividend_withholding_no_update
                    BEFORE UPDATE ON dr_dividend_withholding_events BEGIN
                    SELECT RAISE(ABORT,'dr_withholding_events_are_immutable'); END""")
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

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
            quote_currency TEXT,
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
            tax_residency TEXT,
            PRIMARY KEY(register_id, user_id))''')
        register_entry_columns = {row[1] for row in self.execute("PRAGMA table_info('shareholder_register_entries')")}
        if 'tax_residency' not in register_entry_columns:
            self.execute('ALTER TABLE shareholder_register_entries ADD COLUMN tax_residency TEXT')
        self.execute('''CREATE TABLE IF NOT EXISTS shareholder_register_dr_entries (
            register_id INTEGER NOT NULL REFERENCES shareholder_registers(register_id),
            user_id TEXT NOT NULL,
            payout_currency TEXT NOT NULL,
            quantity REAL NOT NULL CHECK(quantity > 0),
            PRIMARY KEY(register_id,user_id,payout_currency))''')
        self.execute('''CREATE TABLE IF NOT EXISTS shareholder_register_dr_entitlements (
            register_id INTEGER NOT NULL REFERENCES shareholder_registers(register_id),
            user_id TEXT NOT NULL,
            dr_code TEXT NOT NULL,
            payout_currency TEXT NOT NULL,
            quantity REAL NOT NULL CHECK(quantity > 0),
            tax_residency TEXT,
            PRIMARY KEY(register_id,user_id,dr_code))''')
        dr_entitlement_columns = {row[1] for row in self.execute("PRAGMA table_info('shareholder_register_dr_entitlements')")}
        if 'tax_residency' not in dr_entitlement_columns:
            self.execute('ALTER TABLE shareholder_register_dr_entitlements ADD COLUMN tax_residency TEXT')
        self.execute('''CREATE TABLE IF NOT EXISTS user_tax_residency_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            old_residency TEXT,
            new_residency TEXT,
            source TEXT NOT NULL,
            recorded_epoch INTEGER NOT NULL)''')
        self.execute('''CREATE TRIGGER IF NOT EXISTS user_tax_residency_events_no_update
            BEFORE UPDATE ON user_tax_residency_events BEGIN
            SELECT RAISE(ABORT,'tax_residency_events_are_immutable'); END''')
        self.execute('''CREATE TRIGGER IF NOT EXISTS user_tax_residency_events_no_delete
            BEFORE DELETE ON user_tax_residency_events BEGIN
            SELECT RAISE(ABORT,'tax_residency_events_are_immutable'); END''')
        self.execute('CREATE INDEX IF NOT EXISTS idx_register_dr_entitlement_user ON shareholder_register_dr_entitlements(register_id,user_id,payout_currency)')
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
            allocation_key TEXT NOT NULL DEFAULT 'legacy',
            UNIQUE(distribution_id,user_id,quote_currency,allocation_key))''')
        self.execute('''CREATE TABLE IF NOT EXISTS dividend_payment_settlement_baseline_state (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
            through_payment_id INTEGER NOT NULL CHECK(through_payment_id>=0))''')
        self.execute('''INSERT OR IGNORE INTO dividend_payment_settlement_baseline_state
            (singleton_id,through_payment_id)
            SELECT 1,COALESCE(MAX(payment_id),0) FROM dividend_payments''')
        self.execute('''CREATE TABLE IF NOT EXISTS dividend_payment_wallet_links (
            payment_id INTEGER PRIMARY KEY REFERENCES dividend_payments(payment_id),
            wallet_entry_id INTEGER NOT NULL UNIQUE REFERENCES customer_wallet_ledger(entry_id))''')
        self.execute('''CREATE TABLE IF NOT EXISTS dividend_payment_fund_links (
            payment_id INTEGER PRIMARY KEY REFERENCES dividend_payments(payment_id),
            fund_entry_id INTEGER NOT NULL UNIQUE REFERENCES fund_cash_ledger(entry_id))''')
        for table in ("dividend_payment_settlement_baseline_state",
                      "dividend_payment_wallet_links", "dividend_payment_fund_links"):
            for operation in ("UPDATE", "DELETE"):
                self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_{operation.lower()}
                    BEFORE {operation} ON {table} BEGIN
                    SELECT RAISE(ABORT,'dividend payment settlement links are append-only'); END""")
        # Cross-border DR withholding is versioned: administrators append rules,
        # while paid receipts retain the exact rule and jurisdictions used.
        self.execute('''CREATE TABLE IF NOT EXISTS dr_dividend_tax_rules (
            rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_name TEXT NOT NULL,
            issuer_nationality TEXT NOT NULL,
            underlying_nationality TEXT NOT NULL,
            recipient_nationality TEXT NOT NULL DEFAULT '*',
            recipient_residency TEXT NOT NULL DEFAULT '*',
            product_type TEXT NOT NULL DEFAULT 'depositary_receipt',
            treaty_code TEXT,
            tax_rate_percent TEXT NOT NULL,
            tax_base TEXT NOT NULL DEFAULT 'gross' CHECK(tax_base='gross'),
            exempt INTEGER NOT NULL DEFAULT 0 CHECK(exempt IN (0,1)),
            effective_from INTEGER NOT NULL,
            effective_until INTEGER,
            created_by TEXT NOT NULL,
            created_epoch INTEGER NOT NULL,
            CHECK(effective_until IS NULL OR effective_until > effective_from))''')
        self.execute('CREATE INDEX IF NOT EXISTS idx_dr_tax_rule_lookup ON dr_dividend_tax_rules(issuer_nationality,underlying_nationality,recipient_nationality,recipient_residency,product_type,effective_from,effective_until)')
        self.execute('''CREATE TABLE IF NOT EXISTS dr_dividend_withholding_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id INTEGER NOT NULL UNIQUE,
            distribution_id INTEGER NOT NULL,
            rule_id INTEGER,
            dr_code TEXT NOT NULL,
            issuer_nationality TEXT NOT NULL,
            underlying_nationality TEXT NOT NULL,
            recipient_nationality TEXT NOT NULL,
            recipient_residency TEXT NOT NULL,
            product_type TEXT NOT NULL,
            contract_epoch INTEGER NOT NULL,
            treaty_code TEXT,
            tax_rate_percent TEXT NOT NULL,
            gross_amount INTEGER NOT NULL,
            tax_amount INTEGER NOT NULL,
            net_amount_units INTEGER NOT NULL,
            currency_code TEXT NOT NULL,
            remittance_treasury_id TEXT NOT NULL,
            decimal_places INTEGER NOT NULL DEFAULT 0 CHECK(decimal_places BETWEEN 0 AND 6),
            gross_minor_units INTEGER NOT NULL DEFAULT 0 CHECK(typeof(gross_minor_units)='integer' AND gross_minor_units>=0),
            tax_minor_units INTEGER NOT NULL DEFAULT 0 CHECK(typeof(tax_minor_units)='integer' AND tax_minor_units>=0),
            net_minor_units INTEGER NOT NULL DEFAULT 0 CHECK(typeof(net_minor_units)='integer' AND net_minor_units>=0),
            recorded_epoch INTEGER NOT NULL)''')
        self.execute('''CREATE TRIGGER IF NOT EXISTS dr_dividend_tax_rules_no_update
            BEFORE UPDATE ON dr_dividend_tax_rules BEGIN
            SELECT RAISE(ABORT,'dr_tax_rules_are_immutable'); END''')
        self.execute('''CREATE TRIGGER IF NOT EXISTS dr_dividend_tax_rules_no_delete
            BEFORE DELETE ON dr_dividend_tax_rules BEGIN
            SELECT RAISE(ABORT,'dr_tax_rules_are_immutable'); END''')
        self.execute('''CREATE TRIGGER IF NOT EXISTS dr_dividend_withholding_no_update
            BEFORE UPDATE ON dr_dividend_withholding_events BEGIN
            SELECT RAISE(ABORT,'dr_withholding_events_are_immutable'); END''')
        self.execute('''CREATE TRIGGER IF NOT EXISTS dr_dividend_withholding_no_delete
            BEFORE DELETE ON dr_dividend_withholding_events BEGIN
            SELECT RAISE(ABORT,'dr_withholding_events_are_immutable'); END''')
        payment_schema = self.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='dividend_payments'"
        )
        if payment_schema and 'UNIQUE(distribution_id,user_id))' in (payment_schema[0]['sql'] or '').replace(' ', ''):
            with self.lock:
                self.conn.execute('BEGIN IMMEDIATE')
                try:
                    self.conn.execute('''CREATE TABLE dividend_payments_v2 (
                        payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        distribution_id INTEGER NOT NULL REFERENCES dividend_distributions(distribution_id),
                        user_id TEXT NOT NULL,
                        quantity INTEGER NOT NULL CHECK(quantity > 0),
                        gross_amount INTEGER NOT NULL CHECK(gross_amount >= 0),
                        tax_amount INTEGER NOT NULL CHECK(tax_amount >= 0),
                        net_amount_units INTEGER NOT NULL CHECK(net_amount_units >= 0),
                        quote_currency TEXT NOT NULL,
                        created_epoch INTEGER NOT NULL,
                        UNIQUE(distribution_id,user_id,quote_currency))''')
                    self.conn.execute('''INSERT INTO dividend_payments_v2
                        SELECT payment_id,distribution_id,user_id,quantity,gross_amount,tax_amount,
                               net_amount_units,quote_currency,created_epoch FROM dividend_payments''')
                    self.conn.execute('DROP TABLE dividend_payments')
                    self.conn.execute('ALTER TABLE dividend_payments_v2 RENAME TO dividend_payments')
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
        payment_columns = {row[1] for row in self.execute("PRAGMA table_info('dividend_payments')")}
        if 'allocation_key' not in payment_columns:
            with self.lock:
                self.conn.execute('BEGIN IMMEDIATE')
                try:
                    self.conn.execute('''CREATE TABLE dividend_payments_v3 (
                        payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        distribution_id INTEGER NOT NULL REFERENCES dividend_distributions(distribution_id),
                        user_id TEXT NOT NULL,
                        quantity INTEGER NOT NULL CHECK(quantity > 0),
                        gross_amount INTEGER NOT NULL CHECK(gross_amount >= 0),
                        tax_amount INTEGER NOT NULL CHECK(tax_amount >= 0),
                        net_amount_units INTEGER NOT NULL CHECK(net_amount_units >= 0),
                        quote_currency TEXT NOT NULL,
                        created_epoch INTEGER NOT NULL,
                        allocation_key TEXT NOT NULL DEFAULT 'legacy',
                        UNIQUE(distribution_id,user_id,quote_currency,allocation_key))''')
                    self.conn.execute('''INSERT INTO dividend_payments_v3
                        (payment_id,distribution_id,user_id,quantity,gross_amount,tax_amount,
                         net_amount_units,quote_currency,created_epoch,allocation_key)
                        SELECT payment_id,distribution_id,user_id,quantity,gross_amount,tax_amount,
                               net_amount_units,quote_currency,created_epoch,'legacy'
                        FROM dividend_payments''')
                    self.conn.execute('DROP TABLE dividend_payments')
                    self.conn.execute('ALTER TABLE dividend_payments_v3 RENAME TO dividend_payments')
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
        withholding_columns = {row[1] for row in self.execute("PRAGMA table_info('dr_dividend_withholding_events')")}
        if 'dr_code' not in withholding_columns:
            self.execute("ALTER TABLE dr_dividend_withholding_events ADD COLUMN dr_code TEXT NOT NULL DEFAULT ''")
        self._migrate_dr_dividend_cash_minor_units(withholding_columns)
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
