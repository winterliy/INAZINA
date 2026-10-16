from .db_manager import DBManager


TREASURY_USER_ID = "20230216"
LEGACY_TREASURY_USER_IDS = ("__treasury__", "20230219")


def citizen_filter(alias="users"):
    """SQL condition for real citizens: not a treasury and not a country login account.

    Country accounts (user_id equals the country name, role 'nation') only log in
    to trade with their treasury, so they never count as or receive anything as citizens.
    """
    return (f"{alias}.user_id<>'{TREASURY_USER_ID}' "
            f"AND {alias}.user_id NOT IN (SELECT user_id FROM country_treasuries) "
            f"AND {alias}.user_id NOT IN (SELECT nationality FROM country_currencies) "
            f"AND COALESCE({alias}.role,'user')<>'nation'")


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
        user_columns = {row[1] for row in self.execute("PRAGMA table_info(users)")}
        for column, declaration in (
            ("annual_income", "INTEGER NOT NULL DEFAULT 0 CHECK(annual_income >= 0)"),
            ("income_stability", "REAL NOT NULL DEFAULT 1 CHECK(income_stability BETWEEN 0 AND 1)"),
            # Server where the citizen registered; it decides their nationality.
            ("registration_guild_id", "TEXT"),
        ):
            if column not in user_columns:
                self.execute(f"ALTER TABLE users ADD COLUMN {column} {declaration}")
        with self.lock, self.conn:
            legacy_rows = self.conn.execute(
                "SELECT user_id,cash FROM users WHERE user_id IN (?,?)",
                LEGACY_TREASURY_USER_IDS,
            ).fetchall()
            existing = self.conn.execute(
                "SELECT cash FROM users WHERE user_id=?", (TREASURY_USER_ID,)
            ).fetchone()
            merged_cash = sum(int(row["cash"] or 0) for row in legacy_rows)
            if existing:
                merged_cash += int(existing["cash"] or 0)
            self.conn.execute(
                "INSERT INTO users(user_id,nickname,nationality,cash,status,role) "
                "VALUES (?, '낙원국고', '낙원', ?, 'active', 'nation') "
                "ON CONFLICT(user_id) DO UPDATE SET cash=excluded.cash, "
                "nationality='낙원', status='active', role='nation'",
                (TREASURY_USER_ID, merged_cash),
            )
            old_ids = (*LEGACY_TREASURY_USER_IDS,)
            if legacy_rows:
                tables = self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                for (table,) in tables:
                    columns = self.conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                    foreign_keys = self.conn.execute(
                        f'PRAGMA foreign_key_list("{table}")'
                    ).fetchall()
                    for column in columns:
                        name = column[1]
                        is_user_ref = any(
                            fk[2] == "users" and fk[3] == name and fk[4] == "user_id"
                            for fk in foreign_keys
                        ) or name in {"user_id", "writer_id", "max_shareholder_id"}
                        if table == "users" and name == "user_id":
                            continue
                        if is_user_ref:
                            self.conn.execute(
                                f'UPDATE "{table}" SET "{name}"=? WHERE "{name}" IN (?,?)',
                                (TREASURY_USER_ID, *old_ids),
                            )
                self.conn.execute(
                    "DELETE FROM users WHERE user_id IN (?,?)", LEGACY_TREASURY_USER_IDS
                )
        self.enforce_integer_money("users", ("cash", "annual_income"), "user_id")

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
                    (str(user_id), str(nickname), str(nationality), cash, str(role)),
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
