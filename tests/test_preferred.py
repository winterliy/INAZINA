"""Preferred entitlements, issuer cash conservation and liquidation boundaries."""
import unittest
from unittest.mock import patch

import test_inazina as fixtures
from database.user_db import TREASURY_USER_ID
from managers.corporate_actions_manager import CorporateActionsManager


class PreferredTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def company(self, code, preferred=False, parent='CO', cycle=0, dividend=0, cumulative=0, priority=None, cap=0):
        self.db.companies.create_company(code,code,'게임',100000,1000,100,'admin','INZ')
        self.db.companies.execute('UPDATE company SET share_class=?,parent_company_code=?,dividend_funding=\'issuer\',dividend_priority=?,cumulative_dividend=?,liquidation_value_per_share=?,dividend_cycle_seconds=?,dividend_per_share=?,dividend_next_epoch=? WHERE company_code=?',
                                 ('preferred' if preferred else 'common',parent if preferred else None,priority if priority is not None else int(preferred),cumulative,cap,cycle,dividend,100 if cycle else None,code))

    def setup_company(self, cumulative=1, funding=0):
        self.db.system.set_setting('tax_percent',0)
        self.company('CO',cycle=10,dividend=1)
        self.company('PR',True,cycle=10,dividend=10,cumulative=cumulative,cap=50)
        self.db.assets.add_user_asset('citizen','CO',10)
        self.db.assets.add_user_asset('admin','PR',2)
        self.manager = CorporateActionsManager(self.bot)
        if funding:
            self.assertTrue(self.manager.fund_issuer('admin','CO','add',funding)[0])

    def test_partial_cumulative_dividend_preserves_record_date_owner_and_is_idempotent(self):
        self.setup_company(funding=5)
        admin_cash = self.db.users.get_user('admin')['cash']
        citizen_cash = self.db.users.get_user('citizen')['cash']
        result = self.manager.process_due(now=100)
        self.assertIn(('PR',True,5),result)
        self.assertEqual(self.db.users.get_user('admin')['cash']-admin_cash,5)
        self.assertEqual(self.db.users.get_user('citizen')['cash'],citizen_cash)
        claim = self.db.corporate.execute_one('SELECT * FROM preferred_dividend_claims')
        self.assertEqual((claim['user_id'],claim['quantity'],claim['gross_amount'],claim['paid_amount']),('admin',2,15,0))
        self.manager.process_due(now=100)
        self.assertEqual(self.db.corporate.execute_one('SELECT COUNT(*) FROM preferred_dividend_claims')[0],1)
        # Changing the holdings cannot transfer the already-fixed receivable.
        self.db.assets.execute("UPDATE user_assets SET user_id='citizen' WHERE asset_code='PR'")
        self.manager.fund_issuer('admin','CO','add',25)
        self.manager.process_due(now=100)
        self.assertEqual(self.db.users.get_user('admin')['cash']-admin_cash,20)
        self.assertEqual(self.db.users.get_user('citizen')['cash']-citizen_cash,10)
        self.assertEqual(self.db.corporate.execute_one('SELECT status FROM preferred_dividend_claims')[0],'paid')
        self.manager.process_due(now=100)
        self.assertEqual(self.db.users.get_user('admin')['cash']-admin_cash,20)

    def test_noncumulative_shortfall_stays_due_and_blocks_common(self):
        self.setup_company(cumulative=0,funding=15)
        balances = [self.db.users.get_user(user)['cash'] for user in ('admin','citizen')]
        results = self.manager.process_due(now=100)
        self.assertEqual(sum(bool(success) for _,success,_ in results),0)
        self.assertEqual(self.db.companies.get_company('PR')['dividend_next_epoch'],100)
        self.assertEqual(self.db.corporate.execute_one('SELECT COUNT(*) FROM preferred_dividend_claims')[0],0)
        self.assertEqual([self.db.users.get_user(user)['cash'] for user in ('admin','citizen')],balances)
        self.manager.fund_issuer('admin','CO','add',15)
        results = self.manager.process_due(now=100)
        self.assertEqual(results,[('PR',True,20),('CO',True,10)])

    def test_special_common_dividend_respects_due_preference_and_duplicate_key(self):
        self.setup_company(funding=40)
        admin_cash = self.db.users.get_user('admin')['cash']
        citizen_cash = self.db.users.get_user('citizen')['cash']
        with patch('managers.corporate_actions_manager.time.time',return_value=100):
            self.assertTrue(self.manager.special_dividend('admin','CO',2,'special:test', delay=0)[0])
            self.assertTrue(self.manager.special_dividend('admin','CO',2,'special:test', delay=0)[0])
        self.assertEqual(self.db.users.get_user('admin')['cash']-admin_cash,20)
        self.assertEqual(self.db.users.get_user('citizen')['cash']-citizen_cash,20)
        self.assertEqual(self.db.corporate.execute_one('SELECT balance FROM issuer_cash_accounts')[0],0)

    def test_switch_off_does_not_erase_fixed_receivable(self):
        self.setup_company()
        self.manager.process_due(now=100)
        self.manager.set_dividend_active('admin','PR',0)
        self.manager.fund_issuer('admin','CO','add',20)
        self.manager.process_due(now=100)
        self.assertEqual(self.db.corporate.execute_one('SELECT status FROM preferred_dividend_claims')[0],'paid')
        self.assertEqual(self.db.companies.get_company('PR')['dividend_is_active'],0)

    def test_cash_transfer_reauthorizes_and_preserves_locked_reserve(self):
        self.setup_company(funding=100)
        self.db.corporate.execute('UPDATE issuer_cash_accounts SET locked_amount=200')
        treasury = self.db.users.get_user(TREASURY_USER_ID)['cash']
        self.assertFalse(self.manager.fund_issuer('citizen','CO','add',10)[0])
        self.assertFalse(self.manager.fund_issuer('admin','CO','del',101)[0])
        self.assertTrue(self.manager.fund_issuer('admin','PR','modify',50)[0])
        account = self.db.corporate.execute_one('SELECT * FROM issuer_cash_accounts')
        self.assertEqual((account['company_code'],account['balance'],account['locked_amount']),('CO',50,200))
        self.assertEqual(self.db.users.get_user(TREASURY_USER_ID)['cash']-treasury,50)

    def add_bond(self, code='CB', user='citizen', quantity=1):
        self.db.products.register_investment(code,code,'corporate_bond',100,100,30,'CO','BOND')
        self.db.products.execute('UPDATE investment SET issuer_code=\'CO\',face_value=100,annual_rate=0,maturity_epoch=2592000 WHERE product_code=?',(code,))
        self.db.assets.add_user_asset(user,code,quantity)

    def test_liquidation_waterfall_debt_arrears_preference_then_common(self):
        self.setup_company()
        self.manager.process_due(now=100)
        self.add_bond()
        self.manager.fund_issuer('admin','CO','add',250)
        before = {user:self.db.users.get_user(user)['cash'] for user in ('admin','citizen')}
        result = self.manager.liquidate('admin','CO','liquidate:test',now=101)
        self.assertTrue(result[0],result[1])
        payments = {row['kind']:row['paid_amount'] for row in self.db.corporate.execute('SELECT * FROM corporate_liquidation_payments')}
        self.assertEqual(payments,{'debt':100,'arrears':20,'preferred':100,'common':30})
        self.assertEqual(self.db.users.get_user('admin')['cash']-before['admin'],120)
        self.assertEqual(self.db.users.get_user('citizen')['cash']-before['citizen'],130)
        self.assertEqual(self.db.assets.execute_one('SELECT COUNT(*) FROM user_assets')[0],0)
        self.assertEqual(self.db.companies.get_company('CO')['status'],'inactive')
        self.assertEqual(self.db.companies.get_company('PR')['status'],'inactive')
        self.assertEqual(self.db.products.get_investment('CB')['status'],'liquidated')
        self.assertTrue(self.manager.liquidate('admin','CO','liquidate:test',now=102)[0])
        self.assertEqual(self.db.users.get_user('admin')['cash']-before['admin'],120)

    def test_liquidation_insufficient_equal_rank_debt_is_proportional(self):
        self.setup_company()
        self.add_bond(user='admin')
        self.db.assets.add_user_asset('citizen','CB',1)
        self.manager.fund_issuer('admin','CO','add',101)
        self.assertTrue(self.manager.liquidate('admin','CO','liquidate:short',now=100)[0])
        debt = self.db.corporate.execute("SELECT user_id,paid_amount FROM corporate_liquidation_payments WHERE kind='debt' ORDER BY user_id")
        self.assertEqual([(row['user_id'],row['paid_amount']) for row in debt],[('admin',51),('citizen',50)])
        self.assertEqual(self.db.corporate.execute_one("SELECT SUM(paid_amount) FROM corporate_liquidation_payments WHERE kind IN ('preferred','common')")[0],0)

    def test_liquidation_refunds_orders_and_rolls_back_failed_payment(self):
        self.setup_company(funding=100)
        now=100
        self.db.assets.execute("INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,remaining_quantity,reserved_cash,order_kind,status,created_epoch,updated_epoch) VALUES ('citizen','CO','buy',100,1,1,100,'asset','open',?,?)",(now,now))
        citizen = self.db.users.get_user('citizen')['cash']
        # Payout cannot credit a disappeared preferred claimant: all cancellations
        # and debt deductions must roll back along with the failed payout.
        self.db.assets.execute("UPDATE user_assets SET user_id='missing' WHERE asset_code='PR'")
        self.assertFalse(self.manager.liquidate('admin','CO','bad',now=now)[0])
        self.assertEqual(self.db.users.get_user('citizen')['cash'],citizen)
        self.assertEqual(self.db.assets.execute_one('SELECT status FROM market_orders')[0],'open')
        self.assertEqual(self.db.corporate.execute_one('SELECT balance FROM issuer_cash_accounts')[0],100)
        self.db.assets.execute("UPDATE user_assets SET user_id='admin' WHERE asset_code='PR'")
        self.assertTrue(self.manager.liquidate('admin','CO','good',now=now)[0])
        self.assertEqual(self.db.users.get_user('citizen')['cash']-citizen,100)
        self.assertEqual(self.db.assets.execute_one('SELECT status FROM market_orders')[0],'cancelled')

    def test_liquidation_blocks_active_derivative_and_reserved_issuer_cash(self):
        self.setup_company(funding=100)
        self.db.corporate.execute('UPDATE issuer_cash_accounts SET locked_amount=1')
        self.assertFalse(self.manager.liquidate('admin','CO','locked',now=100)[0])
        self.db.corporate.execute('UPDATE issuer_cash_accounts SET locked_amount=0')
        self.db.assets.execute("INSERT INTO option_contracts(contract_code,writer_id,underlying_code,option_type,strike_price,expiration_epoch,implied_volatility,issued_contracts,created_epoch) VALUES ('OP','admin','PR','call',100,200,0.2,1,100)")
        self.assertFalse(self.manager.liquidate('admin','CO','option',now=100)[0])
        self.assertEqual(self.db.companies.get_company('CO')['status'],'active')
        self.assertEqual(self.db.corporate.execute_one('SELECT balance FROM issuer_cash_accounts')[0],100)


if __name__ == '__main__':
    unittest.main()
