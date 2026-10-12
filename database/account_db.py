from .db_manager import DBManager
import hashlib
import os
import time
from datetime import datetime, timezone

class AccountDB(DBManager):
    def __init__(self):
        super().__init__("accounts")
        self._init_db()

    def _init_db(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                account_number TEXT PRIMARY KEY,
                user_id TEXT,
                product_code TEXT,
                balance INTEGER,
                password TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_payment DATETIME DEFAULT CURRENT_TIMESTAMP,
                quote_currency TEXT NOT NULL DEFAULT 'NWX',
                balance_units INTEGER NOT NULL DEFAULT 0,
                interest_remainder TEXT NOT NULL DEFAULT '0'
            )
        ''')
        try:
            self.execute("ALTER TABLE accounts ADD COLUMN last_payment DATETIME DEFAULT CURRENT_TIMESTAMP")
        except Exception:
            pass

        columns = {row[1] for row in self.execute("PRAGMA table_info(accounts)")}
        if "last_payment_epoch" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN last_payment_epoch REAL")
        if "principal" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN principal INTEGER NOT NULL DEFAULT 0")
        if "account_type" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN account_type TEXT NOT NULL DEFAULT 'saving'")
        if "quote_currency" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN quote_currency TEXT NOT NULL DEFAULT 'NWX'")
        if "balance_units" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN balance_units INTEGER NOT NULL DEFAULT 0")
        if "interest_remainder" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN interest_remainder TEXT NOT NULL DEFAULT '0'")
        now = time.time()
        for row in self.execute(
            "SELECT account_number, last_payment FROM accounts "
            "WHERE last_payment_epoch IS NULL AND last_payment IS NOT NULL"
        ):
            try:
                legacy_time = datetime.strptime(row["last_payment"], "%Y-%m-%d %H:%M:%S")
                epoch = legacy_time.replace(tzinfo=timezone.utc).timestamp()
                # Older versions wrote local-time strings. A timestamp far in the future
                # when read as UTC is a reliable signal; convert it as the old code did.
                if epoch > now + 300:
                    epoch = time.mktime(legacy_time.timetuple())
                self.execute(
                    "UPDATE accounts SET last_payment_epoch = ? WHERE account_number = ?",
                    (epoch, row["account_number"]),
                )
            except (TypeError, ValueError, OverflowError):
                continue
        # Repair rows migrated by a pre-fix build that interpreted a local timestamp as UTC.
        for row in self.execute(
            "SELECT account_number, last_payment, last_payment_epoch FROM accounts "
            "WHERE last_payment_epoch > ? AND last_payment IS NOT NULL",
            (now + 300,),
        ):
            try:
                legacy_time = datetime.strptime(row["last_payment"], "%Y-%m-%d %H:%M:%S")
                local_epoch = time.mktime(legacy_time.timetuple())
                if local_epoch <= now + 300:
                    self.execute(
                        "UPDATE accounts SET last_payment_epoch = ? WHERE account_number = ?",
                        (local_epoch, row["account_number"]),
                    )
            except (TypeError, ValueError, OverflowError):
                continue
        self.execute("UPDATE accounts SET last_payment_epoch = ? WHERE last_payment_epoch IS NULL", (time.time(),))
        self.execute("UPDATE accounts SET principal = balance WHERE principal = 0 AND balance > 0")
        self._migrate_plaintext_passwords()
        self.enforce_integer_money("accounts", ("balance", "principal"), "account_number")

        self.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_user_product_unique
            ON accounts (user_id, product_code)
        ''')
        self.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_user_currency_saving_unique
            ON accounts (user_id, quote_currency)
            WHERE account_type = 'currency_saving'
        ''')

        self.execute('''
            CREATE TABLE IF NOT EXISTS loans (
                loan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                product_code TEXT NOT NULL,
                principal INTEGER NOT NULL,
                outstanding_balance INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_payment_epoch REAL,
                last_repayment_epoch REAL,
                status TEXT NOT NULL DEFAULT 'active'
            )
        ''')
        loan_columns = {row[1] for row in self.execute("PRAGMA table_info(loans)")}
        for legacy_column in ("statutory_interest_remainder", "last_statutory_epoch"):
            if legacy_column in loan_columns:
                self.execute(f"ALTER TABLE loans DROP COLUMN {legacy_column}")
        loan_columns = {row[1] for row in self.execute("PRAGMA table_info(loans)")}
        for column, declaration in (
            ("maturity_epoch", "REAL"),
            ("extension_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_extended_epoch", "REAL"),
            ("extension_eligible_epoch", "REAL"),
        ):
            if column not in loan_columns:
                self.execute(f"ALTER TABLE loans ADD COLUMN {column} {declaration}")
        # Fix each contract's maturity rather than changing existing loans when
        # an administrator edits the product's duration.
        self.execute("UPDATE loans SET maturity_epoch = CAST(strftime('%s', created_at) AS REAL) + "
            "86400 * (SELECT maturity_days FROM liability p WHERE p.product_code=loans.product_code) "
            "WHERE maturity_epoch IS NULL AND (SELECT maturity_days FROM liability p "
            "WHERE p.product_code=loans.product_code)>0")
        if "last_payment_epoch" not in loan_columns:
            self.execute("ALTER TABLE loans ADD COLUMN last_payment_epoch REAL")
        if "status" not in loan_columns:
            self.execute("ALTER TABLE loans ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "last_repayment_epoch" not in loan_columns:
            self.execute("ALTER TABLE loans ADD COLUMN last_repayment_epoch REAL")
            # Existing loans start their first scheduled installment from migration time.
            self.execute(
                "UPDATE loans SET last_repayment_epoch = unixepoch('now') "
                "WHERE last_repayment_epoch IS NULL"
            )
        self.execute(
            "UPDATE loans SET last_payment_epoch = CAST(strftime('%s', created_at) AS REAL) "
            "WHERE last_payment_epoch IS NULL"
        )
        self.execute(
            "UPDATE loans SET last_repayment_epoch = COALESCE(last_repayment_epoch, "
            "CAST(strftime('%s', created_at) AS REAL), unixepoch('now')) "
            "WHERE last_repayment_epoch IS NULL"
        )
        self.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_loans_user_product_unique
            ON loans (user_id, product_code)
        ''')
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_loans_due "
            "ON loans (status, last_repayment_epoch)"
        )
        self.enforce_integer_money("loans", ("principal", "outstanding_balance"), "loan_id")

        self.execute(
            """CREATE TABLE IF NOT EXISTS treasury_bond_holdings (
                holding_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                product_code TEXT NOT NULL,
                product_name TEXT NOT NULL,
                annual_rate REAL NOT NULL,
                maturity_days INTEGER NOT NULL,
                principal INTEGER NOT NULL CHECK(principal > 0),
                face_value INTEGER NOT NULL DEFAULT 1 CHECK(face_value > 0),
                acquisition_cost INTEGER NOT NULL DEFAULT 0 CHECK(acquisition_cost >= 0),
                purchased_epoch INTEGER NOT NULL,
                maturity_epoch INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                redeemed_epoch INTEGER
            )"""
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_bond_holdings_due "
            "ON treasury_bond_holdings(status, maturity_epoch, user_id)"
        )
        bond_columns = {
            row[1] for row in self.execute("PRAGMA table_info(treasury_bond_holdings)")
        }
        for column, declaration in (
            ("product_name", "TEXT NOT NULL DEFAULT ''"),
            ("annual_rate", "REAL NOT NULL DEFAULT 0"),
            ("maturity_days", "INTEGER NOT NULL DEFAULT 1"),
            ("face_value", "INTEGER NOT NULL DEFAULT 1 CHECK(face_value > 0)"),
            ("acquisition_cost", "INTEGER NOT NULL DEFAULT 0 CHECK(acquisition_cost >= 0)"),
        ):
            if column not in bond_columns:
                self.execute(
                    f"ALTER TABLE treasury_bond_holdings ADD COLUMN {column} {declaration}"
                )
        self.enforce_integer_money(
            "treasury_bond_holdings",
            ("principal", "face_value", "acquisition_cost"),
            "holding_id",
        )

    def _migrate_plaintext_passwords(self):
        """기존 평문 비밀번호를 저장 즉시 PBKDF2-SHA256 형식으로 변환합니다."""
        rows = self.execute(
            "SELECT account_number, password FROM accounts "
            "WHERE password IS NOT NULL AND password NOT LIKE 'pbkdf2_sha256$%'"
        )
        for row in rows:
            salt = os.urandom(16)
            iterations = 240_000
            digest = hashlib.pbkdf2_hmac(
                "sha256", str(row["password"]).encode("utf-8"), salt, iterations
            )
            encoded = f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"
            self.execute(
                "UPDATE accounts SET password = ? WHERE account_number = ?",
                (encoded, row["account_number"]),
            )

    def create_account(self, account_number, user_id, product_code, balance, password):
        self.execute(
            "INSERT INTO accounts "
            "(account_number, user_id, product_code, balance, password, principal, last_payment_epoch, account_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'saving')",
            (account_number, user_id, product_code, balance, password, balance, time.time()),
        )

    def get_account(self, account_number):
        return self.execute_one("SELECT * FROM accounts WHERE account_number = ?", (account_number,))

    def get_user_accounts(self, user_id):
        return self.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,))

    def has_account_for_product(self, user_id, product_code):
        """사용자가 해당 저축상품에 이미 가입했는지 확인합니다."""
        return self.execute_one(
            "SELECT 1 FROM accounts WHERE user_id = ? AND product_code = ?",
            (user_id, product_code),
        ) is not None

    def has_loan_for_product(self, user_id, product_code):
        """사용자가 해당 대출상품을 이미 이용 중인지 확인합니다."""
        return self.execute_one(
            "SELECT 1 FROM loans WHERE user_id = ? AND product_code = ?",
            (user_id, product_code),
        ) is not None

    def create_loan(self, user_id, product_code, amount):
        now = time.time()
        self.execute(
            '''
            INSERT INTO loans
            (user_id, product_code, principal, outstanding_balance, last_payment_epoch, last_repayment_epoch, maturity_epoch)
            VALUES (?, ?, ?, ?, ?, ?, (SELECT CASE WHEN maturity_days>0 THEN ?+86400*maturity_days END
                                      FROM liability WHERE product_code=?))
            ''',
            (user_id, product_code, amount, amount, now, now, now, product_code),
        )

    def delete_account(self, account_number):
        self.execute("DELETE FROM accounts WHERE account_number = ?", (account_number,))

    def check_exists(self, account_number):
        return self.execute_one("SELECT 1 FROM accounts WHERE account_number = ?", (account_number,)) is not None

    def update_balance(self, account_number, amount):
        self.execute("UPDATE accounts SET balance = balance + ? WHERE account_number = ?", (amount, account_number))

    def update_last_payment(self, account_number, timestamp):
        self.execute("UPDATE accounts SET last_payment = ? WHERE account_number = ?", (timestamp, account_number))

    def apply_interest_updates(self, updates):
        """이자, UTC 지급시각, epoch를 한 트랜잭션으로 저장합니다.

        updates: (interest_amount, last_payment_timestamp, last_payment_epoch, account_number) 목록
        """
        if not updates:
            return
        with self.lock:
            with self.conn:
                self.conn.executemany(
                    "UPDATE accounts SET balance = balance + ?, last_payment = ?, "
                    "last_payment_epoch = ? WHERE account_number = ?",
                    updates,
                )

    def apply_loan_interest_updates(self, updates):
        """updates: (interest, last_payment_epoch, loan_id) 목록"""
        if not updates:
            return
        with self.lock:
            with self.conn:
                self.conn.executemany(
                    "UPDATE loans SET outstanding_balance = outstanding_balance + ?, "
                    "last_payment_epoch = ? WHERE loan_id = ? AND status = 'active'",
                    updates,
                )
