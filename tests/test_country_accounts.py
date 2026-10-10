import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from database.db_manager import DBManager
from database.user_db import TREASURY_USER_ID, UserDB
from web_api import server


class CountryAccountTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_database_dir = DBManager.database_dir
        self.old_database = server.DATABASE
        root = Path(self.tempdir.name)
        DBManager.database_dir = root
        server.DATABASE = root / "inazina.db"
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            connection.executescript("""
                CREATE TABLE users (
                    user_id TEXT PRIMARY KEY, nickname TEXT, nationality TEXT,
                    cash INTEGER DEFAULT 0, credit_score INTEGER DEFAULT 700,
                    status TEXT DEFAULT 'active', role TEXT DEFAULT 'user');
                CREATE TABLE currencies (
                    code TEXT PRIMARY KEY, name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active');
                CREATE TABLE country_currencies (
                    nationality TEXT PRIMARY KEY, currency_code TEXT NOT NULL);
                CREATE TABLE admin_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL,
                    action TEXT NOT NULL, payload TEXT NOT NULL, recorded_epoch REAL NOT NULL);
                INSERT INTO users(user_id,nickname,role) VALUES ('admin-1','Admin','admin');
                INSERT INTO users(user_id,nickname,nationality,cash) VALUES ('discord-1','Discord user','Testland',2500);
                INSERT INTO currencies(code,name,status) VALUES ('TST','Test currency','active');
                INSERT INTO country_currencies(nationality,currency_code) VALUES ('Testland','TST');
            """)

    def tearDown(self):
        DBManager.database_dir = self.old_database_dir
        server.DATABASE = self.old_database
        self.tempdir.cleanup()

    def test_country_id_is_the_user_id_and_country_password_login_works(self):
        account = server.create_country_account("admin-1", "Testland", "CountryPass123")

        user = server.authenticate_country_password("Testland", "CountryPass123")
        self.assertEqual(account["userId"], "Testland")
        self.assertEqual(user["nationality"], "Testland")
        self.assertEqual(user["cash"], 0)
        self.assertEqual(user["role"], "nation")
        with closing(server.sqlite3.connect(server.DATABASE)) as connection:
            discord_cash = connection.execute(
                "SELECT cash FROM users WHERE user_id='discord-1'"
            ).fetchone()[0]
        self.assertEqual(discord_cash, 2500)
        self.assertIsNone(server.authenticate_country_password("Testland", "WrongPass123"))
        country_user = next(row for row in server.admin_users("") if row["userId"] == "Testland")
        self.assertEqual(country_user["accountType"], "country")
        with closing(server.sqlite3.connect(server.DATABASE)) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='country_accounts'"
            ).fetchone()
        self.assertIsNone(table)

    def test_existing_country_user_gets_password_without_resetting_balance(self):
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            connection.execute(
                "INSERT INTO users(user_id,nickname,nationality,cash,role) VALUES ('Testland','Existing','Testland',4200,'user')"
            )
        account = server.create_country_account("admin-1", "Testland", "CountryPass123")
        user = server.authenticate_country_password("Testland", "CountryPass123")
        self.assertEqual(account["cash"], 0)
        self.assertEqual(user["cash"], 0)

    def test_country_account_is_unique_per_country(self):
        server.create_country_account("admin-1", "Testland", "CountryPass123")
        with self.assertRaisesRegex(ValueError, "country_account_exists"):
            server.create_country_account("admin-1", "Testland", "AnotherPass123")

    def test_web_credentials_reference_and_follow_user_lifecycle(self):
        server.create_country_account("admin-1", "Testland", "CountryPass123")
        with server.connect_db() as connection:
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(web_auth_credentials)"
            ).fetchall()
            self.assertTrue(any(
                row[2] == "users" and row[3] == "user_id"
                and row[4] == "user_id" and row[6] == "CASCADE"
                for row in foreign_keys
            ))
            connection.execute("DELETE FROM users WHERE user_id='Testland'")
            remaining = connection.execute(
                "SELECT 1 FROM web_auth_credentials WHERE user_id='Testland'"
            ).fetchone()
        self.assertIsNone(remaining)

    def test_identity_rejects_active_user_without_web_credentials(self):
        server.create_country_account("admin-1", "Testland", "CountryPass123")
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            connection.execute(
                "INSERT INTO users(user_id,nickname,nationality) VALUES ('no-web-auth','No web auth','Testland')"
            )
        self.assertIsNone(server.identity("no-web-auth"))

    def test_hts_login_id_is_separate_and_password_confirmed(self):
        server.create_country_account("admin-1", "Testland", "CountryPass123")
        self.assertEqual(server.set_hts_login_id("Testland", "Test.User_1", "CountryPass123"), "test.user_1")
        self.assertEqual(server.get_hts_login_id("Testland"), "test.user_1")
        user, found = server.authenticate_login_identifier("TEST.USER_1", "CountryPass123")
        self.assertTrue(found)
        self.assertEqual(user["userId"], "Testland")
        with self.assertRaisesRegex(ValueError, "invalid_credentials"):
            server.set_hts_login_id("Testland", "another_id", "WrongPass123")

    def test_country_hts_identity_uses_national_treasury_balance(self):
        server.create_country_account("admin-1", "Testland", "CountryPass123")
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            connection.execute(
                "INSERT INTO users(user_id,nickname,nationality,cash,status,role) "
                "VALUES (?,'Treasury','Testland',123456,'system','nation')",
                (TREASURY_USER_ID,),
            )
        self.assertEqual(server.trading_user_id("Testland"), TREASURY_USER_ID)
        self.assertEqual(server.identity("Testland")["cash"], 123456)

    def test_non_admin_cannot_create_country_account(self):
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            connection.execute("INSERT INTO users(user_id,nickname,role) VALUES ('user-1','User','user')")
        with self.assertRaisesRegex(ValueError, "관리 권한"):
            server.create_country_account("user-1", "Testland", "CountryPass123")

    def test_internal_user_id_decodes_non_ascii_country_id(self):
        handler = SimpleNamespace(headers={"x-inazina-user-id": "%EB%82%99%EC%9B%90"})
        self.assertEqual(server.internal_user_id(handler), "낙원")

    def test_legacy_treasuries_merge_into_nation_account(self):
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            connection.execute(
                "INSERT INTO users(user_id,nickname,nationality,cash,status,role) "
                "VALUES('__treasury__','Old treasury','Testland',0,'system','system')"
            )
            connection.execute(
                "INSERT INTO users(user_id,nickname,nationality,cash,status,role) "
                "VALUES('20230219','Old country treasury','Testland',98765,'system','system')"
            )
            connection.execute("CREATE TABLE treasury_refs(user_id TEXT)")
            connection.execute("INSERT INTO treasury_refs VALUES('__treasury__')")

        users = UserDB()
        treasury = users.get_user(TREASURY_USER_ID)
        self.assertEqual(treasury["cash"], 98765)
        self.assertEqual(treasury["status"], "nation")
        self.assertEqual(treasury["role"], "nation")
        users.close()

        with closing(server.sqlite3.connect(server.DATABASE)) as connection:
            self.assertEqual(
                connection.execute("SELECT user_id FROM treasury_refs").fetchone()[0],
                TREASURY_USER_ID,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM users WHERE user_id IN ('__treasury__','20230219')"
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
