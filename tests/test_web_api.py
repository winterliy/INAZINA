import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from database.db_manager import DBManager
from web_api import server


class WebApiTests(unittest.TestCase):
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
                CREATE TABLE admin_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL,
                    action TEXT NOT NULL, payload TEXT NOT NULL, recorded_epoch REAL NOT NULL);
                CREATE TABLE web_auth_credentials (
                    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL, created_epoch INTEGER NOT NULL,
                    updated_epoch INTEGER NOT NULL, auth_version INTEGER NOT NULL DEFAULT 0, hts_id TEXT);
                CREATE TABLE country_currencies (nationality TEXT PRIMARY KEY, currency_code TEXT NOT NULL);
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TRIGGER settings_rate_check BEFORE UPDATE ON settings
                    WHEN NEW.key='base_rate' AND CAST(NEW.value AS REAL) NOT BETWEEN -100 AND 100
                    BEGIN SELECT RAISE(ABORT, 'Rate is outside its permitted range'); END;
                CREATE TABLE market_price_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT, asset_code TEXT NOT NULL,
                    price REAL NOT NULL, recorded_epoch INTEGER NOT NULL,
                    UNIQUE(asset_code, recorded_epoch));
                CREATE TABLE market_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, asset_code TEXT, event_type TEXT NOT NULL,
                    severity INTEGER NOT NULL, starts_epoch INTEGER NOT NULL, ends_epoch INTEGER NOT NULL,
                    created_by TEXT NOT NULL);
                INSERT INTO users(user_id,nickname,nationality,role) VALUES ('admin-1','Admin','낙원','admin');
                INSERT INTO settings(key,value) VALUES ('base_rate','3');
            """)

    def tearDown(self):
        DBManager.database_dir = self.old_database_dir
        server.DATABASE = self.old_database
        self.tempdir.cleanup()

    def execute(self, sql, params=()):
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            return connection.execute(sql, params).fetchall()

    def test_dr_reference_value_and_deviation_alert(self):
        ordinary = server.dr_reference_metrics(22, 10, 2, 1)
        self.assertEqual(ordinary["referenceValue"], 20)
        self.assertAlmostEqual(ordinary["referencePremiumPercent"], 10)
        self.assertTrue(ordinary["referenceDeviationAlert"])
        self.assertFalse(server.dr_reference_metrics(21.99, 10, 2, 1)["referenceDeviationAlert"])
        self.assertTrue(server.dr_reference_metrics(18, 10, 2, 1)["referenceDeviationAlert"])
        self.assertEqual(server.dr_reference_metrics(float("nan"), 10, 2, 1), {})

    def test_user_loan_schedules_only_returns_own_unpaid_installments(self):
        connection = server.sqlite3.connect(":memory:")
        connection.row_factory = server.sqlite3.Row
        connection.executescript("""
            CREATE TABLE currencies(code TEXT PRIMARY KEY, decimal_places INTEGER);
            INSERT INTO currencies VALUES ('NWX',0);
            CREATE TABLE loans(loan_id INTEGER PRIMARY KEY,user_id TEXT,product_code TEXT,
                quote_currency TEXT,outstanding_balance REAL,status TEXT,collection_status TEXT,
                maturity_epoch REAL,underwriting_dsr REAL,underwriting_stress_dsr REAL);
            CREATE TABLE loan_payment_schedule(schedule_id INTEGER PRIMARY KEY,loan_id INTEGER,
                installment_number INTEGER,due_epoch REAL,principal_due REAL,interest_due REAL,
                fee_due REAL,principal_paid REAL,interest_paid REAL,fee_paid REAL,amount_paid REAL,status TEXT);
            CREATE TABLE contract_rate_reset_history(reset_id INTEGER PRIMARY KEY,contract_type TEXT,
                contract_id TEXT,old_rate REAL,new_rate REAL,scheduled_epoch REAL,applied_epoch REAL,
                benchmark_key TEXT,benchmark_tenor_days INTEGER);
            INSERT INTO loans VALUES (1,'alice','L1','NWX',900,'active','current',3000,20,25);
            INSERT INTO loans VALUES (2,'bob','L2','NWX',500,'active','current',3000,30,35);
            INSERT INTO loan_payment_schedule VALUES (11,1,1,1000,800,80,20,0,0,0,0,'scheduled');
            INSERT INTO loan_payment_schedule VALUES (12,1,2,2000,700,70,10,700,70,10,780,'paid');
            INSERT INTO loan_payment_schedule VALUES (21,2,1,1000,500,50,0,0,0,0,0,'scheduled');
            INSERT INTO contract_rate_reset_history VALUES (1,'loan','1',5,6,900,1000,'bank_bond',90);
            INSERT INTO contract_rate_reset_history VALUES (2,'loan','2',5,6,900,1000,'bank_bond',90);
        """)
        with patch.object(server, "domain"), patch.object(
            server, "connect_readonly", return_value=closing(connection)
        ):
            result = server.user_loan_schedules("alice")
        self.assertEqual([loan["loan_id"] for loan in result["loans"]], [1])
        self.assertEqual([row["schedule_id"] for row in result["loans"][0]["installments"]], [11])
        self.assertEqual(result["loans"][0]["decimal_places"], 0)
        self.assertEqual(result["loans"][0]["rate_resets"][0]["new_rate"], 6)
        self.assertEqual(result["loans"][0]["underwriting_stress_dsr"], 25)

    def test_candles_aggregate_open_high_low_close_per_bucket(self):
        ticks = [(0, 100), (10, 120), (30, 90), (59, 110), (60, 111), (119, 105)]
        for epoch, price in ticks:
            self.execute("INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES ('AAA',?,?)",
                         (price, 600 + epoch))
        self.execute("INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES ('BBB',999,610)")
        with server.connect_readonly() as connection:
            result = server.candles(connection, "AAA", 0, 60)
        self.assertEqual(result, [
            {"time": 600, "open": 100.0, "high": 120.0, "low": 90.0, "close": 110.0, "ticks": 4},
            {"time": 660, "open": 111.0, "high": 111.0, "low": 105.0, "close": 105.0, "ticks": 2},
        ])

    def test_chart_rejects_unknown_ranges_and_intervals(self):
        with patch.object(server, "instruments", return_value=[{"code": "AAA"}]):
            with self.assertRaisesRegex(ValueError, "unsupported_chart_range"):
                server.chart("AAA", "2y")
            with self.assertRaisesRegex(ValueError, "unsupported_candle_interval"):
                server.chart("AAA", "1d", 7)
            result = server.chart("AAA", "1w")
        self.assertEqual(result["interval"], 3600)
        self.assertEqual(result["candles"], [])

    def test_drawings_round_trip_per_user_and_instrument(self):
        drawings = [
            {"id": "a", "type": "trend", "anchors": [{"time": 1, "price": 10}, {"time": 2, "price": 12.5}], "extra": 1},
            {"id": "b", "type": "horizontal", "anchors": [{"time": 3, "price": 11}]},
        ]
        saved = server.save_drawings("admin-1", "AAA", drawings)
        self.assertNotIn("extra", saved[0])
        self.assertEqual(server.get_drawings("admin-1", "AAA"), saved)
        self.assertEqual(server.get_drawings("admin-1", "BBB"), [])
        self.assertEqual(server.get_drawings("other", "AAA"), [])
        server.save_drawings("admin-1", "AAA", [])
        self.assertEqual(self.execute("SELECT COUNT(*) FROM web_chart_drawings")[0][0], 0)

    def test_drawings_reject_invalid_shapes(self):
        invalid = [
            "not a list",
            [{"id": "a", "type": "circle", "anchors": [{"time": 1, "price": 1}]}],
            [{"id": "a", "type": "trend", "anchors": [{"time": 1, "price": 1}]}],
            [{"id": "a", "type": "horizontal", "anchors": [{"time": 1, "price": 0}]}],
            [{"id": "a", "type": "horizontal", "anchors": [{"time": float("nan"), "price": 1}]}],
            [{"id": "", "type": "horizontal", "anchors": [{"time": 1, "price": 1}]}],
            [{"id": str(index), "type": "horizontal", "anchors": [{"time": 1, "price": 1}]}
             for index in range(server.MAX_DRAWINGS + 1)],
        ]
        for value in invalid:
            with self.subTest(value=str(value)[:40]), self.assertRaisesRegex(ValueError, "invalid_drawing"):
                server.normalize_drawings(value)

    def test_audit_and_events_are_read_from_the_database(self):
        self.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES ('admin-1','settings','{\"a\":1}',5)")
        self.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES ('admin-1','legacy','not json',6)")
        self.execute("INSERT INTO market_events(asset_code,event_type,severity,starts_epoch,ends_epoch,created_by) "
                     "VALUES ('AAA','good',5,1,2,'admin-1')")
        audit = server.admin_audit()
        self.assertEqual(audit[0], {"auditId": 2, "actorId": "admin-1", "action": "legacy", "payload": {"raw": "not json"}, "time": 6.0})
        self.assertEqual(audit[1]["payload"], {"a": 1})
        events = server.admin_events()
        self.assertEqual(events[0]["asset_code"], "AAA")
        self.assertIsNone(events[0]["target_mode"])
        self.assertFalse(events[0]["active"])

    def test_db_editor_hides_credentials_and_audit(self):
        names = {table["name"] for table in server.db_tables()}
        self.assertIn("settings", names)
        self.assertTrue(names.isdisjoint({"web_auth_credentials", "admin_audit"}))
        for table in ("web_auth_credentials", "admin_audit", "sqlite_master"):
            with self.subTest(table=table), self.assertRaisesRegex(ValueError, "table_not_found"):
                server.db_rows(table)

    def test_db_editor_update_insert_delete_are_audited(self):
        page = server.db_rows("settings", search="base")
        self.assertEqual(page["total"], 1)
        rowid = page["rows"][0]["__rowid__"]
        updated = server.db_update("admin-1", "settings", rowid, {"value": "4.5"})
        self.assertEqual(updated["value"], "4.5")
        inserted = server.db_insert("admin-1", "settings", {"key": "garch_alpha", "value": "0.1"})
        server.db_delete("admin-1", "settings", inserted["__rowid__"])
        actions = [(row[0], json.loads(row[1])) for row in self.execute(
            "SELECT action,payload FROM admin_audit ORDER BY audit_id")]
        self.assertEqual([action for action, _ in actions], ["db.update", "db.insert", "db.delete"])
        self.assertEqual(actions[0][1]["before"], {"value": "3"})
        self.assertEqual(actions[0][1]["after"], {"value": "4.5"})
        self.assertEqual(self.execute("SELECT COUNT(*) FROM settings")[0][0], 1)

    def test_db_editor_keeps_database_triggers_and_validates_columns(self):
        rowid = server.db_rows("settings")["rows"][0]["__rowid__"]
        with self.assertRaises(server.sqlite3.IntegrityError):
            server.db_update("admin-1", "settings", rowid, {"value": "500"})
        self.assertEqual(self.execute("SELECT value FROM settings")[0][0], "3")
        self.assertEqual(self.execute("SELECT COUNT(*) FROM admin_audit")[0][0], 0)
        for values in ({}, {"missing": 1}, {"value": ["list"]}, {"value": float("inf")}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                server.db_update("admin-1", "settings", rowid, values)
        with self.assertRaisesRegex(ValueError, "row_not_found"):
            server.db_update("admin-1", "settings", 999, {"value": "1"})

    def test_admin_command_catalog_lists_usage(self):
        catalog = server.admin_command_catalog()
        names = {item["name"] for item in catalog["commands"]}
        self.assertIn("잔고수정", names)
        self.assertTrue(all("usage" in item for item in catalog["commands"]))
        with self.assertRaisesRegex(ValueError, "알 수 없는"):
            server.run_admin_command("admin-1", {"command": "rm", "arguments": ""})

    def test_national_commands_cover_discord_treasury_commands(self):
        from functions.admin_prefix import NATIONAL_COMMANDS
        catalog = server.admin_command_catalog()
        national = {item["name"] for item in catalog["national"]}
        self.assertEqual(national | set(catalog["discordOnly"]), NATIONAL_COMMANDS)
        self.assertIn("매수", national)
        transfer = next(item for item in catalog["national"] if item["name"] == "송금")
        self.assertEqual([field["required"] for field in transfer["fields"]], [True, True, False])

    def test_national_commands_validate_before_running(self):
        cases = [
            ({"command": "도움말", "values": []}, "알 수 없는"),
            ({"command": "매수", "values": ["A"]}, "입력값"),
            ({"command": "매수", "values": ["A", " ", "1"]}, "수량"),
            ({"command": "매수", "values": ["A", 1, "1"]}, "입력값"),
        ]
        for payload, error in cases:
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, error):
                server.run_national_command("admin-1", payload)

    def test_db_editor_masks_password_columns(self):
        self.execute("CREATE TABLE accounts (account_number TEXT PRIMARY KEY, user_id TEXT, password TEXT, balance INTEGER)")
        self.execute("INSERT INTO accounts VALUES ('A1','admin-1','secret',5)")
        row = server.db_rows("accounts")["rows"][0]
        self.assertEqual(row["password"], server.HIDDEN_VALUE)
        updated = server.db_update("admin-1", "accounts", row["__rowid__"],
                                   {"password": server.HIDDEN_VALUE, "balance": 7})
        self.assertEqual(updated["balance"], 7)
        self.assertEqual(self.execute("SELECT password FROM accounts")[0][0], "secret")
        payload = self.execute("SELECT payload FROM admin_audit")[0][0]
        self.assertNotIn("secret", payload)

    def test_admin_web_password_and_login_actions(self):
        message = server.admin_set_web_password("admin-1", "admin-1", "ResetPass1234")
        self.assertIn("비밀번호", message)
        self.assertIsNotNone(server.authenticate_password("admin-1", "ResetPass1234"))
        server.admin_web_login_action("admin-1", "admin-1", "hts_id", "Admin.Web")
        user, found = server.authenticate_login_identifier("admin.web", "ResetPass1234")
        self.assertTrue(found)
        self.assertEqual(user["userId"], "admin-1")
        version = self.execute("SELECT auth_version FROM web_auth_credentials")[0][0]
        server.admin_web_login_action("admin-1", "admin-1", "logout")
        self.assertEqual(self.execute("SELECT auth_version FROM web_auth_credentials")[0][0], version + 1)
        with self.assertRaisesRegex(ValueError, "password_policy"):
            server.admin_set_web_password("admin-1", "admin-1", "short")
        audit = [row[0] for row in self.execute("SELECT payload FROM admin_audit")]
        self.assertFalse(any("ResetPass1234" in payload for payload in audit))

    def test_shareholder_meetings_are_queued_for_the_bot(self):
        message = server.queue_shareholder_meeting("admin-1", {"code": "AAA", "place": "회의실"})
        self.assertIn("#1", message)
        jobs = server.bot_jobs()
        self.assertEqual((jobs[0]["kind"], jobs[0]["status"]), ("shareholder_meeting", "pending"))
        self.assertEqual(json.loads(jobs[0]["payload"]), {"code": "AAA", "place": "회의실"})
        with self.assertRaisesRegex(ValueError, "주식 코드"):
            server.queue_shareholder_meeting("admin-1", {"code": "", "place": "x"})

    def test_help_pages_match_the_discord_help_view(self):
        from functions.admin_help_view import AdminHelpView
        pages = server.admin_help_pages()
        self.assertEqual([page["label"] for page in pages], [label for _key, label in AdminHelpView.PAGES])
        self.assertTrue(any("$상품생성" in field["value"] for page in pages for field in page["fields"]))

    def test_product_catalog_covers_every_creatable_kind(self):
        from managers.admin_commands_manager import CREATE_ARGS
        with closing(server.sqlite3.connect(server.DATABASE)) as connection, connection:
            for table, key in {("company", "company_code"), ("market_assets", "code"), ("investment", "product_code"),
                               ("treasury_bond_products", "product_code"), ("saving", "product_code"),
                               ("liability", "product_code"), ("option_contracts", "contract_code"), ("currencies", "code")}:
                connection.execute(f"CREATE TABLE {table} ({key} TEXT PRIMARY KEY, name TEXT, status TEXT, extra_setting REAL)")
        kinds = {item["kind"]: item for item in server.product_catalog()["kinds"]}
        self.assertEqual(set(kinds), set(CREATE_ARGS))
        self.assertEqual({kinds[name]["group"] for name in ("saving", "loan", "currency", "currencyproduct")}, {"finance"})
        self.assertIn("extra_setting", {field["key"] for field in kinds["saving"]["advanced"]})
        self.assertNotIn("status", {field["key"] for field in kinds["saving"]["advanced"]})

    def test_product_form_values_follow_the_positional_parser(self):
        fields = server._convert_product_fields(
            "stock", {"name": "회사", "capital": "1000", "dividend_cycle_seconds": "1주",
                      "dividend_per_share": "5%", "exchange_id": "INZ", "industry": ""},
            {"liquidity": "0.5"}, {"liquidity": "REAL"})
        self.assertEqual(fields["capital"], 1000)
        self.assertEqual(fields["dividend_cycle_seconds"], 604800)
        self.assertEqual((fields["dividend_mode"], fields["dividend_percent"]), ("percent", 5.0))
        self.assertEqual(fields["liquidity"], 0.5)
        self.assertNotIn("industry", fields)
        fund = server._convert_product_fields("fund", {"components": "A:0.6, B:0.4"}, {}, {})
        self.assertEqual(fund["components"], {"A": 0.6, "B": 0.4})
        for kind, values, advanced in (("stock", {"unknown": "1"}, {}), ("stock", {}, {"status": "x"}),
                                       ("fund", {"components": "A0.6"}, {})):
            with self.subTest(values=values, advanced=advanced), self.assertRaises(ValueError):
                server._convert_product_fields(kind, values, advanced, {"status": "TEXT"})

    def test_products_are_purged_only_when_stopped_and_unused(self):
        self.execute("CREATE TABLE market_assets (code TEXT PRIMARY KEY, name TEXT, status TEXT, quote_currency TEXT)")
        self.execute("CREATE TABLE user_assets (user_id TEXT, asset_code TEXT, quantity REAL)")
        self.execute("CREATE TABLE market_orders (order_id INTEGER PRIMARY KEY, asset_code TEXT, status TEXT)")
        self.execute("INSERT INTO market_assets VALUES ('AAA','에이','active','NWX'), ('BBB','비','inactive','NWX')")
        self.execute("INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES ('AAA',1,1), ('BBB',1,1)")
        self.execute("INSERT INTO user_assets VALUES ('u','BBB',1)")
        self.execute("INSERT INTO market_orders VALUES (1,'BBB','filled')")
        with self.assertRaisesRegex(ValueError, "먼저 중지"):
            server.product_delete("admin-1", "AAA")
        with self.assertRaisesRegex(ValueError, "보유 중인 사용자"):
            server.product_delete("admin-1", "BBB")
        self.execute("DELETE FROM user_assets")
        self.assertIn("완전히 삭제", server.product_delete("admin-1", "BBB"))
        self.assertEqual(self.execute("SELECT code FROM market_assets"), [("AAA",)])
        self.assertEqual(self.execute("SELECT asset_code FROM market_price_history"), [("AAA",)])
        self.assertEqual(self.execute("SELECT COUNT(*) FROM market_orders")[0][0], 1)
        self.assertEqual(self.execute("SELECT action FROM admin_audit")[-1][0], "product.purge")

    def test_savings_and_fx_accounts_share_one_form(self):
        self.assertEqual(server._form_fields("saving"), server._form_fields("currencyproduct"))
        fields = server._convert_product_fields("currencyproduct", {"name": "달러", "quote_currency": "usd", "interest_rate": "2"},
                                                {}, {})
        self.assertEqual(fields, {"name": "달러", "quote_currency": "USD", "interest_rate": 2})


if __name__ == "__main__":
    unittest.main()
