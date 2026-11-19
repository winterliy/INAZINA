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


if __name__ == "__main__":
    unittest.main()
