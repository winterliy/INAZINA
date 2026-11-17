import sqlite3
import unittest

import test_inazina as fixtures


class ProductRegistrationConflictTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_other_bank_kind_cannot_overwrite_and_policy_survives_update(self):
        self.db.products.register_saving("SHARED", "저축", 1, 0, 30, 3600, 0, "Bank")
        self.db.products.execute("UPDATE saving SET transaction_policy='blocked' WHERE product_code=?", ("SHARED",))
        self.db.products.register_saving("SHARED", "수정", 2, 0, 30, 3600, 0, "Bank")
        self.assertEqual(self.db.products.get_saving("SHARED")["transaction_policy"], "blocked")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.products.register_liability("SHARED", "대출", 1, 0, 30, 3600, 0, "Bank")
        self.assertEqual(self.db.products.get_saving("SHARED")["name"], "수정")
        self.assertIsNone(self.db.products.get_liability("SHARED"))

    def test_saving_cannot_overwrite_existing_loan(self):
        self.db.products.register_liability("SHARED", "대출", 1, 0, 30, 3600, 0, "Bank")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.products.register_saving("SHARED", "저축", 1, 0, 30, 3600, 0, "Bank")
        self.assertEqual(self.db.products.get_liability("SHARED")["name"], "대출")

    def test_asset_and_investment_reregistration_keeps_other_fields(self):
        self.db.assets.register_asset("KEEP", "기존", "INZ", 10, "stock")
        self.db.assets.register_asset("KEEP", "변경", "INZ", 11, "stock")
        self.assertEqual(self.db.assets.execute_one("SELECT name FROM market_assets WHERE code='KEEP'")[0], "변경")
        self.db.products.register_investment("FUND", "기존", "investment", 10, 10, 30, "admin", "INZ")
        self.db.products.register_investment("FUND", "변경", "investment", 10, 11, 30, "admin", "INZ")
        self.assertEqual(self.db.products.get_investment("FUND")["name"], "변경")
