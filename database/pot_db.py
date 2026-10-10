from .db_manager import DBManager


class PotDB(DBManager):
    """도박 설정 접근자."""

    def __init__(self):
        super().__init__("pot")
        self._init_db()

    def _init_db(self):
        pass

    def get_chance_percent(self):
        row = self.execute_one("SELECT value FROM settings WHERE key = 'gamble_chance'")
        return float(row["value"]) if row else 50.0

    def get_return_percent(self):
        row = self.execute_one("SELECT value FROM settings WHERE key = 'gamble_return'")
        return float(row["value"]) if row else 100.0
