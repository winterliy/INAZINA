from .db_manager import DBManager


class LotteryDB(DBManager):
    def __init__(self):
        super().__init__('lottery')
        self.execute("""CREATE TABLE IF NOT EXISTS lottery_rounds (
            round_id INTEGER PRIMARY KEY AUTOINCREMENT, close_epoch INTEGER NOT NULL UNIQUE,
            draw_epoch INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'selling',
            rules_json TEXT NOT NULL, secret TEXT NOT NULL, commitment TEXT NOT NULL,
            entropy TEXT, winning_json TEXT, bonus INTEGER,
            sales_total INTEGER NOT NULL DEFAULT 0, pools_json TEXT NOT NULL,
            reserve_total INTEGER NOT NULL DEFAULT 0, treasury_total INTEGER NOT NULL DEFAULT 0,
            result_json TEXT, settled_epoch INTEGER)""")
        if 'admin_numbers_json' not in {r[1] for r in self.execute('PRAGMA table_info(lottery_rounds)')}:
            self.execute('ALTER TABLE lottery_rounds ADD COLUMN admin_numbers_json TEXT')
        self.execute("""CREATE TABLE IF NOT EXISTS lottery_tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER NOT NULL,
            user_id TEXT NOT NULL, numbers_json TEXT NOT NULL, price INTEGER NOT NULL,
            request_id TEXT NOT NULL, created_epoch INTEGER NOT NULL,
            rank INTEGER)""")
        self.execute('CREATE INDEX IF NOT EXISTS idx_lottery_tickets_user_round ON lottery_tickets(user_id,round_id,ticket_id)')
