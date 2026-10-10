from .db_manager import DBManager


class PotDB(DBManager):
    """도박 승률. SQL Browser에서 probability.chance를 0~100으로 편집합니다."""

    def __init__(self):
        super().__init__("pot")
        self._init_db()

    def _init_db(self):
        self.execute(
            """CREATE TABLE IF NOT EXISTS probability (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                chance REAL NOT NULL DEFAULT 50 CHECK(chance >= 0 AND chance <= 100)
            )"""
        )
        self.execute("INSERT OR IGNORE INTO probability (id, chance) VALUES (1, 50)")

    def get_chance_percent(self):
        row = self.execute_one("SELECT chance FROM probability WHERE id = 1")
        return row["chance"] if row else 50.0

    def get_return_percent(self):
        row = self.execute_one("SELECT value FROM settings WHERE key = 'gamble_return'")
        return float(row["value"]) if row else 100.0
