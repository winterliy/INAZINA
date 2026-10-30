"""A 법인 can list only part of its shares: the market never holds more than listed_shares."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from database import DatabaseManager
from database.db_manager import DBManager
from managers.admin_control_manager import AdminControlManager
from managers.order_book_manager import OrderBookManager
from managers.user_manager import UserManager


class PartialListingTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.system.set_setting("auto_target_enabled", "0")
        self.db.users.create_user("admin", "Admin", "낙원", 1000000, role="admin")
        self.db.users.create_user("alice", "Alice", "낙원", 10_000_000)
        self.control = AdminControlManager(SimpleNamespace(db=self.db))
        self.control.entity("admin", "stock", "create", "PART", name="Part", price="100", supply="1000",
                            extra={"exchange_id": "UNLISTED", "industry": "test", "capital": 100000})
        self.book = OrderBookManager(self.db, UserManager(self.db))

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def listing(self, **extra):
        return self.control.entity("admin", "stock", "edit", "PART", extra=extra)

    def company(self):
        with self.db.atomic(("company",)) as conn:
            return dict(conn.execute("SELECT exchange_id, listed_shares FROM company WHERE company_code='PART'").fetchone())

    def held(self):
        with self.db.atomic(("assets",)) as conn:
            return int(conn.execute("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE asset_code='PART'").fetchone()[0])

    def test_listing_30_percent_offers_only_300_shares(self):
        self.listing(exchange_id="EX", listed_shares=300)
        self.assertEqual(self.company(), {"exchange_id": "EX", "listed_shares": 300})
        ok, message = self.book.place_limit_order("alice", "PART", "buy", 250, 100)
        self.assertTrue(ok, message)
        self.assertEqual(self.held(), 250)
        # One holder can never own more than the listed shares.
        self.assertFalse(self.book.place_limit_order("alice", "PART", "buy", 100, 100)[0])
        self.assertTrue(self.book.place_limit_order("alice", "PART", "buy", 50, 100)[0])
        self.assertEqual(self.held(), 300)

    def test_listed_shares_must_fit_the_company_and_unlisting_clears_them(self):
        with self.assertRaises(ValueError):
            self.listing(exchange_id="EX", listed_shares=1001)
        self.listing(exchange_id="EX", listed_shares=500)
        self.listing(exchange_id="UNLISTED")
        self.assertEqual(self.company(), {"exchange_id": "UNLISTED", "listed_shares": None})

    def test_full_listing_keeps_every_share_available(self):
        self.listing(exchange_id="EX", listed_shares=None)
        self.assertTrue(self.book.place_limit_order("alice", "PART", "buy", 1000, 100)[0])
        self.assertEqual(self.held(), 1000)


if __name__ == "__main__":
    unittest.main()
