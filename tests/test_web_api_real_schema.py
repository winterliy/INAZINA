"""Web API reads that test_web_api.py covers only on SQLite (it builds reduced SQLite tables by hand), run on
the real schema so they also run on PostgreSQL, which is what production uses."""
import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.company_account_manager import CompanyAccounts
from web_api import server


class RealSchemaWebApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_dir, self.old_database = DBManager.database_dir, server.DATABASE
        DBManager.database_dir = Path(self.tempdir.name)
        server.DATABASE = Path(self.tempdir.name) / "inazina.db"
        self.db = DatabaseManager()

    def tearDown(self):
        self.db.close()
        DBManager.database_dir, server.DATABASE = self.old_dir, self.old_database
        self.tempdir.cleanup()

    def test_admin_users_separates_people_from_company_accounts(self):
        self.db.users.create_user("person-1", "시민", "낙원", 1000)
        with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
            conn.execute(
                "INSERT INTO company(company_code,name,industry,capital,total_shares,share_price,max_shareholder_id,"
                "exchange_id,nationality,legal_form) VALUES ('SELO','셀로','금융',1000,1000,1,'person-1',NULL,'낙원','limited_company')")
            account = CompanyAccounts.account_id(conn, "SELO")
        # (a search term keeps this independent of how many accounts the template database already has: the list is capped at 100)
        people = {user["userId"]: user["accountType"] for user in server.admin_users("person-1")}
        companies = {user["userId"]: user["companyCode"] for user in server.admin_users("SELO", "company")}
        self.assertEqual(people, {"person-1": "discord"})
        self.assertEqual(server.admin_users(account), [])                   # a company account is not listed among people
        self.assertEqual(companies, {account: "SELO"})

    def test_audit_and_events_are_read_from_the_database(self):
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES ('admin-1','settings','{\"a\":1}',5)")
            conn.execute("INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES ('admin-1','legacy','not json',6)")
            conn.execute("INSERT INTO market_events(asset_code,event_type,severity,starts_epoch,ends_epoch,created_by) "
                         "VALUES ('AAA','good',5,1,2,'admin-1')")
        audit = {entry["action"]: entry for entry in server.admin_audit()}
        self.assertEqual(audit["legacy"]["payload"], {"raw": "not json"})
        self.assertEqual(audit["legacy"]["time"], 6.0)
        self.assertEqual(audit["settings"]["payload"], {"a": 1})
        self.assertLess(list(audit).index("legacy"), list(audit).index("settings"))      # newest first
        event = next(row for row in server.admin_events() if row["asset_code"] == "AAA")
        self.assertIn(event["target_mode"], (None, "legacy"))      # the real column defaults to "legacy"
        self.assertFalse(event["active"])

    def test_market_instruments_report_24h_value_trades_and_volume(self):
        from unittest.mock import patch
        now = server.time.time()
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO market_assets(code,name,type,price,exchange_id,quote_currency,status) "
                         "VALUES ('GOLD','Gold','commodity',100,NULL,'NWX','active')")
            for side, quantity, price, age in (("buy", 3, 100, 60), ("sell", 2, 110, 30), ("buy", 50, 90, 3 * 86400)):
                conn.execute("INSERT INTO market_trade_history(asset_code,side,quantity,price,recorded_epoch) VALUES (?,?,?,?,?)",
                             ("GOLD", side, quantity, price, now - age))
        with patch.object(server, "domain", return_value=None):
            gold = next(row for row in server.instruments() if row["code"] == "GOLD")
        self.assertEqual((gold["tradeValue24h"], gold["trades24h"], gold["volume24h"]), (520, 2, 5))

    def test_market_instruments_include_bond_rate_risk_metrics(self):
        from unittest.mock import patch
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO market_exchanges(exchange_id,status,updated_epoch,nationality) VALUES ('BOND','open',0,'낙원')")
            for tenor, rate in ((1, 3), (3650, 4)):
                conn.execute("INSERT INTO market_rate_curve(nationality,benchmark_key,tenor_days,annual_rate) VALUES ('낙원','treasury',?,?) "
                             "ON CONFLICT(nationality,benchmark_key,tenor_days) DO UPDATE SET annual_rate=excluded.annual_rate", (tenor, rate))
            conn.execute("INSERT INTO treasury_bond_products(product_code,name,market_price,exchange_id,quote_currency,status,face_value,"
                         "annual_rate,maturity_days,coupon_frequency_days,day_count_convention,credit_spread,liquidity_spread,total_supply) "
                         "VALUES ('TB1','Test Treasury',1000,'BOND','NWX','active',1000,5,365,180,'ACT/365',0.2,0.1,1000)")
        with patch.object(server, "domain", return_value=None):
            bond = next(row for row in server.instruments() if row["code"] == "TB1")
        self.assertGreater(bond["bondRisk"]["modified_duration"], 0)
        self.assertGreater(bond["bondRisk"]["dv01"], 0)
        self.assertNotIn("warning", bond)

    def test_savings_and_loan_products_are_found_under_their_old_table_names(self):
        # On PostgreSQL `saving` and `liability` are rows of bank_products; the "does the table exist" guard in front of
        # the product lists asked sqlite_master for the old names and answered no, so the lists came back empty.
        with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
            conn.execute("INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,issuer,status) "
                         "VALUES ('SV1','저축 시험',0.03,1000,30,'은행','active')")
            conn.execute("INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,issuer,status) "
                         "VALUES ('LN1','대출 시험',0.05,100000,90,'은행','active')")
        with server.connect_readonly() as connection:
            self.assertTrue(server._table_exists(connection, "saving"))
            self.assertTrue(server._table_exists(connection, "liability"))
            self.assertFalse(server._table_exists(connection, "no_such_table"))
        found = server.user_lookups({"userId": "1", "role": "user", "nationality": "낙원"})
        self.assertEqual([row["code"] for row in found["savingProducts"]], ["SV1"])
        self.assertEqual([row["code"] for row in found["loanProducts"]], ["LN1"])

    def test_admin_overview_lists_the_stocks_lifted_to_the_price_floor(self):
        import time
        empty = server.admin_overview()
        self.assertEqual(empty["priceFloor"], {"floor": 100.0, "actions": []})
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO price_floor_events(asset_code,name,from_price,to_price,created_epoch) VALUES ('TSB','The South Bank',1,100,?)", (int(time.time()),))
            conn.execute("INSERT INTO price_floor_events(asset_code,name,from_price,to_price,created_epoch) VALUES ('OLD','오래된',2,100,?)", (int(time.time()) - 30 * 86400,))
        overview = server.admin_overview()
        self.assertEqual([item["code"] for item in overview["priceFloor"]["actions"]], ["TSB"], "only the last 7 days")
        item = overview["priceFloor"]["actions"][0]
        self.assertEqual((item["name"], item["from"], item["to"]), ("The South Bank", 1.0, 100.0))

    def test_a_company_shows_the_companies_it_holds_even_with_one_share(self):
        # The ownership chart follows a company's shareholders. A company with none of its own (a holding company
        # before anyone bought in, a liquidity firm) used to open an empty chart although it owns other companies.
        from web_api import organizations
        with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
            for code in ("HOLDA", "HELDB"):
                conn.execute(
                    "INSERT INTO company(company_code,name,industry,capital,total_shares,share_price,max_shareholder_id,"
                    "exchange_id,nationality,legal_form) VALUES (?,?,'금융',1000,1000,1,NULL,NULL,'낙원','limited_company')",
                    (code, code))
            holder = CompanyAccounts.account_id(conn, "HOLDA")
            conn.execute("INSERT INTO user_assets(user_id,asset_code,quantity) VALUES (?,?,1)", (holder, "HELDB"))
        with server.connect_readonly() as connection:
            graph = organizations.ownership_graph(connection, "HOLDA", min_percent=0)["graph"]
            self.assertEqual([item["code"] for item in graph["holdings"]], ["HELDB"])
            self.assertEqual(graph["holdings"][0]["shares"], "1")
            self.assertEqual(organizations.ownership_graph(connection, "HELDB", min_percent=0)["graph"]["holdings"], [])

    def test_chart_shows_any_company_holder_but_only_strong_person_holders(self):
        from web_api import organizations
        self.db.users.create_user("small-1", "소액", "낙원", 1000)
        self.db.users.create_user("big-1", "대주주", "낙원", 1000)
        with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
            for code in ("TARGET", "OWNERCO"):
                conn.execute(
                    "INSERT INTO company(company_code,name,industry,capital,total_shares,share_price,max_shareholder_id,"
                    "exchange_id,nationality,legal_form) VALUES (?,?,'금융',1000,1000,1,NULL,NULL,'낙원','limited_company')",
                    (code, code))
            owner = CompanyAccounts.account_id(conn, "OWNERCO")
            for holder, quantity in ((owner, 1), ("small-1", 3), ("big-1", 600)):
                conn.execute("INSERT INTO user_assets(user_id,asset_code,quantity) VALUES (?,?,?)", (holder, "TARGET", quantity))
        with server.connect_readonly() as connection:
            graph = organizations.ownership_graph(connection, "TARGET", min_percent=0)["graph"]
        sources = {edge["from"] for edge in graph["edges"]}
        self.assertIn("company:OWNERCO", sources)      # one share is enough for a company
        self.assertIn("user:big-1", sources)            # a major shareholder is shown
        self.assertNotIn("user:small-1", sources)       # a small private holder is not


if __name__ == "__main__":
    unittest.main()
