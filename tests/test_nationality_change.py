"""Changing nationality liquidates everything into the new home currency."""
import unittest

import test_inazina as fixtures
from database.user_db import TREASURY_USER_ID
from managers.bank_manager import BankManager
from managers.currency_manager import CurrencyManager, SCALE


class NationalityChangeTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def prepare(self):
        self.seed()  # citizen is 낙원 (NWX); INZCU001 is a USD account product
        with self.db.atomic() as conn:
            conn.execute("INSERT OR IGNORE INTO country_currencies(nationality,currency_code) VALUES ('달러국','USD')")
            CurrencyManager.treasury_id(conn, '달러국')
            conn.execute("UPDATE settings SET value='2' WHERE key='currency_exchange_fee_percent'")
        bank = BankManager(self.db)
        self.assertTrue(bank.open_account('citizen', 'INZSV001', 'pass1234', 15000)[0])
        self.assertTrue(bank.open_account('citizen', 'INZCU001', 'pass1234', '10')[0])

    def cash(self, user):
        with self.db.atomic() as conn:
            return CurrencyManager.balance(conn, user, CurrencyManager.home(conn, user))

    def test_everything_is_liquidated_exchanged_and_fee_goes_to_the_old_treasury(self):
        self.prepare()
        ok, message = self.bot.order_book.place_limit_order('citizen', 'INZST001', 'buy', 2, 1)
        self.assertTrue(ok, message)
        self.db.assets.add_user_asset('citizen', 'INZST001', 3, unit_cost=100)
        with self.db.atomic() as conn:
            old_treasury = CurrencyManager.user_treasury_id(conn, 'citizen')
        treasury_before = self.cash(old_treasury)

        message = self.bot.admin_control.user('admin', 'citizen', {'nationality': '달러국'})
        self.assertIn('달러국', message)

        user = self.db.users.get_user('citizen')
        self.assertEqual(user['nationality'], '달러국')
        self.assertEqual(self.db.accounts.execute_one("SELECT COUNT(*) FROM accounts WHERE user_id='citizen'")[0], 0)
        self.assertEqual(self.db.assets.execute_one("SELECT COUNT(*) FROM user_assets WHERE user_id='citizen'")[0], 0)
        self.assertEqual(self.db.assets.execute_one(
            "SELECT COUNT(*) FROM market_orders WHERE user_id='citizen' AND status IN ('open','partial','scheduled')")[0], 0)
        self.assertGreater(self.db.assets.execute_one(
            "SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE user_id=? AND asset_code='INZST001'", (old_treasury,))[0], 0)
        audit = self.db.system.execute_one("SELECT payload FROM admin_audit WHERE action='nationality.change' ORDER BY audit_id DESC LIMIT 1")[0]
        import json
        record = json.loads(audit)
        self.assertEqual((record['from_currency'], record['to_currency']), ('NWX', 'USD'))
        self.assertGreater(record['fee_units'], 0)
        self.assertEqual(record['received_units'], self.cash('citizen'))
        self.assertGreater(self.cash('citizen'), 0)
        # The fee stays with the treasury of the nationality that was left.
        self.assertGreater(self.cash(old_treasury), treasury_before - record['sold'])
        from managers.accounting_journal import AccountingJournal
        with self.db.readonly() as conn:
            check = next(item for item in AccountingJournal.reconcile_subledgers(conn) if item['ledger_scope'] == 'customer')
        self.assertEqual(check['orphan_balance_gaps'], 0, check)

    def test_obligations_block_the_change_and_nothing_moves(self):
        self.prepare()
        self.db.assets.execute("INSERT INTO futures_positions(user_id,product_code,quantity,settlement_price,collateral,net_pnl,updated_epoch) "
                               "VALUES ('citizen','F',1,1,1,0,0)")
        with self.assertRaisesRegex(ValueError, '선물 포지션'):
            self.bot.admin_control.user('admin', 'citizen', {'nationality': '달러국'})
        self.assertEqual(self.db.users.get_user('citizen')['nationality'], '낙원')
        self.assertEqual(self.db.accounts.execute_one("SELECT COUNT(*) FROM accounts WHERE user_id='citizen'")[0], 2)

    def test_loans_beyond_liquidated_cash_block_the_change(self):
        self.prepare()
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,quote_currency) "
                         "VALUES ('citizen','X',1,999999999,'active','NWX')")
        with self.assertRaisesRegex(ValueError, '대출을 갚기에 부족'):
            self.bot.admin_control.user('admin', 'citizen', {'nationality': '달러국'})
        self.assertEqual(self.db.users.get_user('citizen')['nationality'], '낙원')
        self.assertEqual(self.db.accounts.execute_one("SELECT COUNT(*) FROM accounts WHERE user_id='citizen'")[0], 2)


if __name__ == "__main__":
    unittest.main()
