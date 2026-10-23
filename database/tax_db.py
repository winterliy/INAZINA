from .db_manager import DBManager


class TaxDB(DBManager):
    """세금 원장 접근 호환 계층. 전역 세율은 settings.tax_percent에 저장한다."""

    def __init__(self):
        super().__init__("tax")
        self._init_db()

    def _init_db(self):
        legacy = self.execute_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tax_settings'"
        )
        if legacy:
            row = self.execute_one("SELECT rate_percent FROM tax_settings WHERE id=1")
            if row is not None:
                self.execute(
                    "INSERT INTO settings(key,value) VALUES ('tax_percent',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(row['rate_percent']),),
                )
            self.execute("DROP TABLE tax_settings")
        self.execute(
            """CREATE TABLE IF NOT EXISTS tax_transactions (
                transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                source TEXT NOT NULL,
                gross_amount INTEGER NOT NULL,
                tax_amount INTEGER NOT NULL,
                net_amount INTEGER NOT NULL,
                currency_code TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        columns = {row[1] for row in self.execute("PRAGMA table_info(tax_transactions)")}
        if "currency_code" not in columns:
            self.execute(
                "ALTER TABLE tax_transactions ADD COLUMN currency_code TEXT"
            )
        self.enforce_integer_money(
            "tax_transactions",
            ("gross_amount", "tax_amount", "net_amount"),
            "transaction_id",
        )

    def get_rate_percent(self):
        row = self.execute_one("SELECT value FROM settings WHERE key='tax_percent'")
        try:
            value = float(row['value'] if row else 3)
        except (TypeError, ValueError):
            return 3.0
        return value if 0 <= value <= 100 else 3.0
