"""Dividends are paid to the shareholder register, closed before payment."""
import unittest
from unittest.mock import patch

import test_inazina as fixtures
from managers.corporate_actions_manager import CorporateActionsManager


class ShareholderRegisterTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def prepare(self, due=None):
        self.db.system.set_setting('tax_percent', 0)
        self.db.companies.create_company('CO', 'CO', '게임', 100000, 1000, 100, 'admin', 'INZ')
        if due:
            self.db.companies.execute("UPDATE company SET dividend_cycle_seconds=86400,dividend_per_share=5,"
                                      "dividend_next_epoch=? WHERE company_code='CO'", (due,))
        self.db.assets.add_user_asset('citizen', 'CO', 10)
        self.manager = CorporateActionsManager(self.bot)

    def cash(self, user):
        return self.db.users.get_user(user)['cash']

    def move_shares(self, quantity):
        self.db.assets.execute("UPDATE user_assets SET quantity=quantity-? WHERE user_id='citizen' AND asset_code='CO'", (quantity,))
        self.db.assets.add_user_asset('admin', 'CO', quantity)

    def test_regular_register_closes_ten_minutes_before_payment(self):
        due = 100_000
        self.prepare(due)
        self.assertEqual(self.manager.process_due(now=due - 601), [])
        self.assertIsNone(self.db.corporate.execute_one('SELECT 1 FROM shareholder_registers'))
        self.manager.process_due(now=due - 600)  # register closes; nothing is paid yet
        register = self.db.corporate.execute_one('SELECT * FROM shareholder_registers')
        self.assertEqual((register['dividend_kind'], register['status'], register['pay_epoch']), ('regular', 'closed', due))
        # Shares bought after the register closed do not carry this dividend.
        self.move_shares(4)
        citizen, admin = self.cash('citizen'), self.cash('admin')
        self.assertEqual(self.manager.process_due(now=due), [('CO', True, 50)])
        self.assertEqual(self.cash('citizen') - citizen, 50)
        self.assertEqual(self.cash('admin') - admin, 0)
        self.assertEqual(self.db.corporate.execute_one('SELECT status FROM shareholder_registers')[0], 'paid')

    def test_regular_dividend_without_a_closed_register_uses_holders_at_payment(self):
        due = 100_000
        self.prepare(due)
        citizen = self.cash('citizen')
        self.assertEqual(self.manager.process_due(now=due), [('CO', True, 50)])
        self.assertEqual(self.cash('citizen') - citizen, 50)

    def test_same_currency_dr_holders_receive_underlying_dividend_entitlement(self):
        due = 100_000
        self.prepare(due)
        self.db.assets.execute(
            "INSERT INTO depositary_receipts(dr_code,underlying_code,listing_exchange_id,"
            "quote_currency,underlying_per_receipt,receipt_supply,deposited_underlying_quantity) "
            "VALUES ('CO_DR','CO','LSE','NWX',2,100,200)"
        )
        self.db.assets.register_asset('CO_DR', 'CO DR', 'LSE', 200, 'depositary_receipt', 100)
        self.db.assets.add_user_asset('admin', 'CO_DR', 3)
        configured, reason = self.manager.configure_dividend('admin', 'CO_DR', 86400, 5)
        self.assertFalse(configured)
        self.assertIn('배당', reason)
        citizen_before, dr_holder_before = self.cash('citizen'), self.cash('admin')

        self.assertEqual(self.manager.process_due(now=due), [('CO', True, 80)])
        self.assertEqual(self.cash('citizen') - citizen_before, 50)
        self.assertEqual(self.cash('admin') - dr_holder_before, 30)
        payment = self.db.corporate.execute_one(
            "SELECT p.quantity,p.gross_amount FROM dividend_payments p "
            "JOIN dividend_distributions d USING(distribution_id) WHERE p.user_id='admin'"
        )
        self.assertEqual((payment['quantity'], payment['gross_amount']), (6, 30))

    def test_cross_currency_dr_is_not_silently_paid_in_underlying_currency(self):
        due = 100_000
        self.prepare(due)
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,decimal_places) VALUES ('USD','US Dollar',2)"
        )
        self.db.assets.execute(
            "INSERT INTO depositary_receipts(dr_code,underlying_code,listing_exchange_id,"
            "quote_currency,underlying_per_receipt,receipt_supply,deposited_underlying_quantity) "
            "VALUES ('CO_US_DR','CO','NYSE','USD',2,100,200)"
        )
        self.db.assets.register_asset('CO_US_DR', 'CO US DR', 'NYSE', 200,
                                      'depositary_receipt', 100)
        self.db.assets.add_user_asset('admin', 'CO_US_DR', 3)
        admin_before = self.cash('admin')

        self.assertEqual(self.manager.process_due(now=due), [('CO', True, 50)])
        self.assertEqual(self.cash('admin') - admin_before, 0)

    def test_suspended_dr_program_keeps_existing_dividend_rights(self):
        due = 100_000
        self.prepare(due)
        self.db.assets.execute(
            "INSERT INTO depositary_receipts(dr_code,underlying_code,listing_exchange_id,"
            "quote_currency,underlying_per_receipt,receipt_supply,deposited_underlying_quantity,"
            "program_status) VALUES ('CO_DR','CO','LSE','NWX',2,100,200,'suspended')"
        )
        self.db.assets.register_asset('CO_DR', 'CO DR', 'LSE', 200, 'depositary_receipt', 100)
        self.db.assets.execute("UPDATE market_assets SET status='inactive' WHERE code='CO_DR'")
        self.db.assets.add_user_asset('admin', 'CO_DR', 3)
        admin_before = self.cash('admin')

        self.assertEqual(self.manager.process_due(now=due), [('CO', True, 80)])
        self.assertEqual(self.cash('admin') - admin_before, 30)

    def test_special_dividend_closes_register_now_and_pays_ten_minutes_later(self):
        self.prepare()
        with patch('managers.corporate_actions_manager.time.time', return_value=1000):
            ok, message = self.manager.special_dividend('admin', 'CO', 3, 'special:one')
        self.assertTrue(ok, message)
        self.assertIn('주주명부', message)
        self.move_shares(9)  # sold after the record time: the seller still receives it
        citizen = self.cash('citizen')
        self.assertEqual(self.manager.process_due(now=1599), [])
        self.assertEqual(self.manager.process_due(now=1600), [('CO', True, 30)])
        self.assertEqual(self.cash('citizen') - citizen, 30)
        self.assertEqual(self.manager.process_due(now=1700), [])
        # Repeating the command with the same key neither re-registers nor re-pays.
        with patch('managers.corporate_actions_manager.time.time', return_value=1800):
            self.assertTrue(self.manager.special_dividend('admin', 'CO', 3, 'special:one')[0])
        self.assertEqual(self.manager.process_due(now=5000), [])
        self.assertEqual(self.db.corporate.execute_one('SELECT COUNT(*) FROM dividend_distributions')[0], 1)


if __name__ == "__main__":
    unittest.main()
