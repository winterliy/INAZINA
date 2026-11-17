import unittest

import test_inazina as fixtures


class ElsLegacyConflictTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_duplicate_product_aborts_before_overwriting_or_dropping_legacy(self):
        self.db.els.execute(
            "INSERT INTO els_products(product_code,issuer_code,face_value,annual_coupon,maturity_days,"
            "maturity_level,knock_in_level,subscription_end_epoch,created_epoch) "
            "VALUES ('LEGACY-ELS','Bank',100,3,30,0.9,0.5,1,1)"
        )
        self.db.els.execute(
            "INSERT INTO els_subscriptions(product_code,user_id,quantity,principal,coupon_reserved,created_epoch) "
            "VALUES ('LEGACY-ELS','citizen',1,100,0,1)"
        )
        self.db.els.execute("CREATE TABLE els_terms AS SELECT * FROM els_products")
        self.db.els.execute("UPDATE els_terms SET face_value=200 WHERE product_code='LEGACY-ELS'")
        with self.assertRaisesRegex(RuntimeError, "중복"):
            self.db.els._migrate_legacy_products()
        self.assertEqual(self.db.els.execute_one(
            "SELECT face_value FROM els_products WHERE product_code='LEGACY-ELS'"
        )[0], 100)
        self.assertEqual(self.db.els.execute_one(
            "SELECT face_value FROM els_terms WHERE product_code='LEGACY-ELS'"
        )[0], 200)
        self.assertEqual(self.db.els.execute_one(
            "SELECT COUNT(*) FROM els_subscriptions WHERE product_code='LEGACY-ELS'"
        )[0], 1)
