from .db_manager import DBManager


TREASURY_USER_ID = "__treasury__"


class UserDB(DBManager):
    def __init__(self):
        super().__init__("users")
        self._init_db()

    def _init_db(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                nickname TEXT,
                nationality TEXT,
                cash INTEGER DEFAULT 0,
                credit_score INTEGER DEFAULT 700,
                status TEXT DEFAULT 'active',
                role TEXT DEFAULT 'user'
            )
        ''')
        # 기존 테이블에 role 컬럼이 없을 경우를 대비해 추가 (Migration)
        try:
            self.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
        except:
            pass
        self.execute(
            "INSERT OR IGNORE INTO users "
            "(user_id, nickname, nationality, cash, status, role) "
            "VALUES (?, '국고', '낙원', 0, 'system', 'system')",
            (TREASURY_USER_ID,),
        )
        self.enforce_integer_money("users", ("cash",), "user_id")

    def create_user(self, user_id, nickname, nationality, cash=1000000, role='user'):
        self.execute("INSERT OR IGNORE INTO users (user_id, nickname, nationality, cash, role) VALUES (?, ?, ?, ?, ?)", 
                     (user_id, nickname, nationality, cash, role))

    def register_user(self, user_id, nickname, nationality="낙원", cash=1000000, role="user"):
        """Atomically add a self-registering citizen and report whether it was new."""
        with self.lock:
            with self.conn:
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO users "
                    "(user_id, nickname, nationality, cash, role) VALUES (?, ?, ?, ?, ?)",
                    (str(user_id), str(nickname), str(nationality), int(cash), str(role)),
                )
                return cursor.rowcount == 1

    def get_user(self, user_id):
        return self.execute_one("SELECT * FROM users WHERE user_id = ?", (user_id,))

    def update_nickname(self, user_id, nickname):
        if not nickname or str(user_id) == TREASURY_USER_ID:
            return
        self.execute(
            "UPDATE users SET nickname = ? WHERE user_id = ?",
            (str(nickname), str(user_id)),
        )

    def update_nicknames(self, nicknames):
        rows = [
            (str(nickname), str(user_id))
            for user_id, nickname in nicknames
            if nickname and str(user_id) != TREASURY_USER_ID
        ]
        if not rows:
            return 0
        with self.lock:
            with self.conn:
                cursor = self.conn.executemany(
                    "UPDATE users SET nickname = ? WHERE user_id = ?", rows
                )
                return cursor.rowcount

    def get_treasury_balance(self):
        row = self.execute_one(
            "SELECT cash FROM users WHERE user_id = ?", (TREASURY_USER_ID,)
        )
        return int(round(row["cash"] or 0)) if row else 0

    def update_cash(self, user_id, amount):
        self.execute("UPDATE users SET cash = cash + ? WHERE user_id = ?", (amount, user_id))

    def update_status(self, user_id, status):
        self.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))

    def update_role(self, user_id, role):
        self.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))
