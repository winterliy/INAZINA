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
        self.execute("""CREATE TABLE IF NOT EXISTS lottery_purchases (
            request_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, round_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL, price INTEGER NOT NULL, tickets_json TEXT NOT NULL)""")
        self.execute("""CREATE TABLE IF NOT EXISTS lottery_payouts (
            ticket_id INTEGER PRIMARY KEY, round_id INTEGER NOT NULL, user_id TEXT NOT NULL,
            rank INTEGER NOT NULL, gross INTEGER NOT NULL, tax INTEGER NOT NULL,
            net INTEGER NOT NULL, recorded_epoch INTEGER NOT NULL)""")
        self.execute("""CREATE TABLE IF NOT EXISTS lottery_pool_ledger (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER NOT NULL,
            kind TEXT NOT NULL, rank INTEGER NOT NULL DEFAULT 0,
            amount INTEGER NOT NULL, ticket_id INTEGER, recorded_epoch INTEGER NOT NULL)""")
        self.execute('CREATE INDEX IF NOT EXISTS idx_lottery_pool_round ON lottery_pool_ledger(round_id,entry_id)')
        # What a player whose own currency is not the lottery's actually paid or
        # received, with the exact rate (player currency per base unit) used.
        self.execute("""CREATE TABLE IF NOT EXISTS lottery_fx_settlements (
            kind TEXT NOT NULL CHECK(kind IN ('purchase','payout')), reference TEXT NOT NULL,
            user_id TEXT NOT NULL,
            base_minor_units INTEGER NOT NULL CHECK(typeof(base_minor_units)='integer' AND base_minor_units>0),
            currency TEXT NOT NULL, decimal_places INTEGER NOT NULL CHECK(decimal_places BETWEEN 0 AND 6),
            minor_units INTEGER NOT NULL CHECK(typeof(minor_units)='integer' AND minor_units>=0),
            rate_numerator TEXT NOT NULL, rate_denominator TEXT NOT NULL,
            recorded_epoch INTEGER NOT NULL, PRIMARY KEY(kind,reference))""")
        for operation in ('UPDATE', 'DELETE'):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_lottery_fx_settlements_{operation.lower()}
                BEFORE {operation} ON lottery_fx_settlements
                BEGIN SELECT RAISE(ABORT,'lottery fx settlements are append-only'); END""")
