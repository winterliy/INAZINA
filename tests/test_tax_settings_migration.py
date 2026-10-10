import unittest

import test_inazina as fixtures
from database.tax_db import TaxDB


class TaxSettingsMigrationTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_legacy_rate_is_moved_to_settings_then_table_is_removed(self):
        self.db.tax.execute(
            "CREATE TABLE tax_settings (id INTEGER PRIMARY KEY, rate_percent REAL NOT NULL)"
        )
        self.db.tax.execute("INSERT INTO tax_settings VALUES (1, 7.5)")

        tax = TaxDB()
        self.assertEqual(self.db.system.get_setting('tax_percent'), '7.5')
        self.assertIsNone(self.db.tax.execute_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tax_settings'"
        ))
        self.assertEqual(tax.get_rate_percent(), 7.5)
        tax.close()

    def test_admin_tax_setting_and_income_tax_reader_use_settings(self):
        self.assertIn('✅', self.bot.admin_control.settings('admin', {'tax_percent': 7}))
        self.assertEqual(self.db.system.get_setting('tax_percent'), '7')
        with self.db.atomic() as conn:
            self.assertEqual(self.bot.user_mgr._tax(conn, 'citizen', 'test', 100), 7)


if __name__ == '__main__':
    unittest.main()
