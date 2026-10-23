import unittest

import test_inazina as fixtures
from functions.money import base_currency_code


class CurrencyDefaultConfigTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_default_currency_is_read_from_database_configuration(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('AAA','A currency',1,0)"
        )
        self.db.currency.execute(
            "UPDATE currency_system_settings SET base_currency_code='AAA' WHERE singleton_id=1"
        )
        with self.db.atomic() as conn:
            self.assertEqual(base_currency_code(conn), 'AAA')
            self.assertEqual(conn.execute("SELECT base_currency_code()").fetchone()[0], 'AAA')

    def test_missing_default_currency_setting_is_reported(self):
        self.db.currency.execute("DELETE FROM currency_system_settings")
        with self.db.atomic() as conn:
            with self.assertRaisesRegex(ValueError, '기본 통화'):
                base_currency_code(conn)

    def test_missing_row_currency_uses_configured_base(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('AAA','A currency',1,0)"
        )
        self.db.currency.execute(
            "UPDATE currency_system_settings SET base_currency_code='AAA' WHERE singleton_id=1"
        )
        self.db.accounts.execute("INSERT INTO accounts(account_number) VALUES ('TEST-CURRENCY')")
        row = self.db.accounts.execute_one(
            "SELECT quote_currency FROM accounts WHERE account_number='TEST-CURRENCY'"
        )
        self.assertEqual(row[0], 'AAA')

    def test_legacy_fixed_currency_default_is_removed_without_losing_rows(self):
        conn = self.db.currency.conn
        conn.execute("CREATE TABLE currency_migration_probe (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "quote_currency TEXT NOT NULL DEFAULT 'NWX', amount INTEGER NOT NULL)")
        conn.execute("CREATE INDEX currency_migration_probe_amount ON currency_migration_probe(amount)")
        conn.execute("INSERT INTO currency_migration_probe(amount) VALUES (7)")
        self.db.currency._remove_legacy_currency_defaults(
            {"currency_migration_probe": {"quote_currency"}}
        )
        column = next(row for row in conn.execute(
            "PRAGMA table_info(currency_migration_probe)"
        ) if row[1] == "quote_currency")
        self.assertIsNone(column[4])
        self.assertEqual(tuple(conn.execute(
            "SELECT quote_currency,amount FROM currency_migration_probe"
        ).fetchone()), ("NWX", 7))
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='currency_migration_probe_amount'"
        ).fetchone())
