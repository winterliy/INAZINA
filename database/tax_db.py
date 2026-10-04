from .db_manager import DBManager


class TaxDB(DBManager):
    """세율과 소득별 원천징수 내역을 관리합니다. 세율은 퍼센트 단위입니다."""

    def __init__(self):
        super().__init__("tax")
        self._init_db()

    def _init_db(self):
        self.execute(
            """CREATE TABLE IF NOT EXISTS tax_settings (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                rate_percent REAL NOT NULL DEFAULT 3 CHECK(rate_percent >= 0 AND rate_percent <= 100)
            )"""
        )
        self.execute("INSERT OR IGNORE INTO tax_settings (id, rate_percent) VALUES (1, 3)")
        self.execute(
            """CREATE TABLE IF NOT EXISTS tax_transactions (
                transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                source TEXT NOT NULL,
                gross_amount INTEGER NOT NULL,
                tax_amount INTEGER NOT NULL,
                net_amount INTEGER NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        self.enforce_integer_money(
            "tax_transactions",
            ("gross_amount", "tax_amount", "net_amount"),
            "transaction_id",
        )

    def get_rate_percent(self):
        row = self.execute_one("SELECT rate_percent FROM tax_settings WHERE id = 1")
        return row["rate_percent"] if row else 3.0
