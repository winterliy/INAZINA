import sqlite3
import unittest

import test_inazina as fixtures
from database import integrity


class IntegrityTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def test_seeded_database_has_no_dangling_references(self):
        self.seed()
        self.assertEqual(integrity.check(self.db.users.conn), [])

    def test_declared_foreign_keys_are_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.users.execute("INSERT INTO country_treasuries(nationality,user_id) VALUES ('낙원2','ghost')")

    def test_transaction_connections_enforce_foreign_keys(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.atomic() as conn:
                conn.execute("INSERT INTO country_treasuries(nationality,user_id) VALUES ('낙원2','ghost')")
        with self.db.atomic() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_undeclared_references_are_reported(self):
        self.seed()
        self.db.users.execute("UPDATE users SET nationality='없는나라' WHERE user_id='citizen'")
        self.db.users.execute("INSERT INTO accounts(account_number,user_id,product_code,balance) VALUES ('X-1','ghost',NULL,0)")
        found = {(table, column, value) for table, column, value, _ in integrity.check(self.db.users.conn)}
        self.assertIn(('users', 'nationality', '없는나라'), found)
        self.assertIn(('accounts', 'user_id', 'ghost'), found)

    def test_closed_orders_for_deleted_products_are_history_not_errors(self):
        self.seed()
        self.db.assets.execute("INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,remaining_quantity,status,created_epoch,updated_epoch) "
                               "VALUES ('citizen','GONE','buy',1,1,0,'cancelled',0,0)")
        self.assertEqual(integrity.check(self.db.users.conn), [])
