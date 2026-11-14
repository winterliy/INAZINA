"""CB/BW, ETN, asset-backed paper, fund of funds, swaps, CMA, IMA and wrap accounts."""
import time
import unittest

import test_inazina as fixtures
from database.user_db import TREASURY_USER_ID
from managers.capital_markets_manager import CapitalMarketsManager
from managers.admin_commands_manager import AdminCommandsManager


class CapitalMarketsTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def prepare(self):
        self.seed()
        self.bot.capital_markets = CapitalMarketsManager(self.db, self.bot.user_mgr, self.bot.order_book)
        return self.bot.admin_control

    def create(self, category, code, name, price, supply, **extra):
        return self.bot.admin_control.entity('admin', category, 'create', code, name, price, supply, extra)

    def give_bonds(self, code, quantity, user='citizen'):
        self.db.assets.execute("INSERT INTO user_assets(user_id,asset_code,quantity,average_cost) VALUES (?,?,?,1000)",
                               (user, code, quantity))

    def shares(self, user, code):
        return self.db.assets.execute_one("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE user_id=? AND asset_code=?",
                                          (user, code))[0]

    def test_variants_are_created_with_their_feature_and_located_by_category(self):
        self.prepare()
        self.create('convertiblebond', 'CB1', '전환사채', 1000, 100, issuer_code='INZST001', face_value=1000,
                    annual_rate=1, maturity_days=365, conversion_price=500, exchange_id='BOND')
        self.create('abcp', 'CP1', 'ABCP', 1000, 100, issuer_code='INZST001', face_value=1000,
                    annual_rate=3, maturity_days=90, collateral_note='매출채권', exchange_id='BOND')
        self.create('etn', 'ETN1', 'ETN', 1000, 100, issuer_code='INZST001', underlying_exchange_id='INZ',
                    leverage_multiplier=-1, exchange_id='INZ')
        with self.assertRaisesRegex(ValueError, '365일 이하'):
            self.create('abcp', 'CP2', 'ABCP', 1000, 100, issuer_code='INZST001', face_value=1000,
                        annual_rate=3, maturity_days=400, collateral_note='x', exchange_id='BOND')
        with self.assertRaisesRegex(ValueError, '전환가격'):
            self.create('convertiblebond', 'CB2', 'CB', 1000, 100, issuer_code='INZST001', face_value=1000,
                        annual_rate=1, maturity_days=365, exchange_id='BOND')
        commands = AdminCommandsManager(self.bot)
        with self.db.atomic() as conn:
            self.assertEqual(commands.locate(conn, 'CB1')[0], 'convertiblebond')
            self.assertEqual(commands.locate(conn, 'CP1')[0], 'abcp')
            self.assertEqual(commands.locate(conn, 'ETN1')[0], 'etn')
            self.assertEqual(commands.locate(conn, 'INZCB001')[0], 'corporatebond')

    def test_convertible_bond_becomes_shares_and_books_move_from_debt_to_equity(self):
        self.prepare()
        self.create('convertiblebond', 'CB1', '전환사채', 1000, 100, issuer_code='INZST001', face_value=1000,
                    annual_rate=1, maturity_days=365, conversion_price=400, exchange_id='BOND')
        self.give_bonds('CB1', 3)
        before = self.db.companies.execute_one("SELECT total_shares FROM company WHERE company_code='INZST001'")[0]
        self.db.corporate.execute("INSERT INTO issuer_cash_accounts(company_code,balance) VALUES ('INZST001',1000)")
        cash = self.db.users.get_user('citizen')['cash']
        ok, message = self.bot.capital_markets.convert_bond('citizen', 'CB1', 3)
        self.assertTrue(ok, message)
        # 3,000 face / 400 = 7 shares, 200 paid in cash by the issuer.
        self.assertEqual(self.shares('citizen', 'INZST001'), 7)
        self.assertEqual(self.shares('citizen', 'CB1'), 0)
        self.assertEqual(self.db.users.get_user('citizen')['cash'], cash + 200)
        self.assertEqual(self.db.companies.execute_one("SELECT total_shares FROM company WHERE company_code='INZST001'")[0], before + 7)
        self.assertFalse(self.bot.capital_markets.convert_bond('citizen', 'CB1', 1)[0])

    def test_warrant_is_exercised_once_per_bond_unit_and_pays_the_issuer(self):
        self.prepare()
        self.create('warrantbond', 'BW1', '신주인수권부사채', 1000, 100, issuer_code='INZST001', face_value=1000,
                    annual_rate=1, maturity_days=365, warrant_exercise_price=500, exchange_id='BOND')
        self.give_bonds('BW1', 2)
        ok, message = self.bot.capital_markets.exercise_warrant('citizen', 'BW1', 2)
        self.assertTrue(ok, message)
        self.assertEqual(self.shares('citizen', 'INZST001'), 4)
        self.assertEqual(self.shares('citizen', 'BW1'), 2)  # the bond itself remains
        self.assertEqual(self.db.corporate.execute_one(
            "SELECT balance FROM issuer_cash_accounts WHERE company_code='INZST001'")[0], 2000)
        self.assertFalse(self.bot.capital_markets.exercise_warrant('citizen', 'BW1', 1)[0])

    def test_fund_of_funds_accepts_funds_and_rejects_itself(self):
        self.prepare()
        funds = [r[0] for r in self.db.products.execute(
            "SELECT product_code FROM investment WHERE product_type='mutual_fund' LIMIT 2")]
        self.create('fund', 'FOF1', '재간접', 10000, 100, components={funds[0]: .5, funds[1]: .5}, exchange_id='INZ')
        self.assertEqual(self.db.products.execute_one("SELECT COUNT(*) FROM fund_components WHERE product_code='FOF1'")[0], 2)
        with self.assertRaisesRegex(ValueError, '자기 자신'):
            self.bot.admin_control.entity('admin', 'fund', 'edit', 'FOF1', extra={'components': {'FOF1': 1}})

    def test_swap_settles_daily_against_the_nationality_treasury_and_closes(self):
        self.prepare()
        markets = self.bot.capital_markets
        ok, message = markets.open_swap('citizen', 1_000_000, '고정지급', 365)
        self.assertTrue(ok, message)
        swap = self.db.accounts.execute_one("SELECT * FROM swap_contracts")
        self.assertEqual(swap['margin'], 50_000)
        # Policy rate jumps above the fixed rate: the fixed payer gains.
        self.db.currency.execute("UPDATE country_currencies SET base_rate=base_rate+5 WHERE nationality='낙원'")
        cash = self.db.users.get_user('citizen')['cash']
        markets.process_due(now=swap['start_epoch'] + 10 * 86400)
        self.assertGreater(self.db.users.get_user('citizen')['cash'], cash)
        ok, message = markets.close_swap('citizen', swap['swap_id'])
        self.assertTrue(ok, message)
        self.assertEqual(self.db.accounts.execute_one("SELECT status,margin FROM swap_contracts")[:], ('closed', 0))

    def test_due_processing_rejects_invalid_epoch_before_swap_settlement(self):
        self.prepare()
        markets = self.bot.capital_markets
        ok, message = markets.open_swap('citizen', 1_000_000, '고정지급', 365)
        self.assertTrue(ok, message)
        swap_before = tuple(self.db.accounts.execute_one(
            "SELECT margin,realized,last_settle_epoch,status FROM swap_contracts"
        ))
        cash_before = self.db.users.get_user('citizen')['cash']
        for invalid_epoch in (-1, float('nan'), float('inf'), 1e300, True, 'invalid'):
            with self.subTest(invalid_epoch=invalid_epoch):
                with self.assertRaisesRegex(ValueError, 'invalid_capital_markets_processing_epoch'):
                    markets.process_due(now=invalid_epoch)
                self.assertEqual(tuple(self.db.accounts.execute_one(
                    "SELECT margin,realized,last_settle_epoch,status FROM swap_contracts"
                )), swap_before)
                self.assertEqual(self.db.users.get_user('citizen')['cash'], cash_before)

    def test_account_kind_labels_saving_products(self):
        self.prepare()
        kind = lambda code: self.db.products.execute_one(
            "SELECT account_kind FROM saving WHERE product_code=?", (code,))[0]
        saving = dict(interest_rate=3, deposit_amount=0, interest_cycle=86400, compound_type=0, issuer='')
        self.create('saving', 'DEM1', '입출금', '', '', maturity_days=0, **saving)
        self.create('saving', 'TD1', '정기예금', '', '', maturity_days=30, **saving)
        self.create('saving', 'ISA1', 'ISA', '', '', maturity_days=0, account_kind='isa', **saving)
        self.create('cma', 'CMA1', 'CMA', '', '', interest_rate=2, deposit_amount=0)
        self.assertEqual([kind(c) for c in ('DEM1', 'TD1', 'ISA1', 'CMA1')], ['demand', 'time_deposit', 'isa', 'cma'])
        self.bot.admin_control.entity('admin', 'saving', 'edit', 'DEM1', None, None, None, {'account_kind': 'parking'})
        self.assertEqual(kind('DEM1'), 'parking')
        with self.assertRaisesRegex(ValueError, '통장 종류'):
            self.bot.admin_control.entity('admin', 'saving', 'edit', 'DEM1', None, None, None, {'account_kind': 'bogus'})
        with self.assertRaisesRegex(ValueError, 'cma'):
            self.bot.admin_control.entity('admin', 'cma', 'edit', 'CMA1', None, None, None, {'account_kind': 'demand'})

    def test_installment_products_collect_on_schedule_until_maturity(self):
        import time as _time
        self.prepare()
        bank = self.bot.bank_mgr
        saving = dict(interest_rate=0, deposit_amount=0, interest_cycle=86400, compound_type=0, issuer='')
        for kind, days in (('demand', 0), ('time_deposit', 5)):  # 정기예금 is funded once at opening
            with self.assertRaisesRegex(ValueError, '정기적금'):
                self.create('saving', 'BAD1', '잘못', '', '', maturity_days=days, account_kind=kind,
                            installment_amount=100, installment_cycle_seconds=86400, **saving)
        self.create('saving', 'FI1', '정기적금', '', '', maturity_days=5, account_kind='fixed_installment',
                    installment_amount=100, installment_cycle_seconds=86400, **saving)
        self.create('saving', 'DEM2', '입출금', '', '', maturity_days=0, account_kind='demand', **saving)
        self.assertTrue(bank.open_account('citizen', 'FI1', 'pass1234', 0)[0])
        number = self.db.accounts.execute_one("SELECT account_number FROM accounts WHERE product_code='FI1'")[0]
        balance = lambda n: self.db.accounts.execute_one("SELECT balance FROM accounts WHERE account_number=?", (n,))[0]
        progress = lambda: tuple(self.db.accounts.execute_one(
            "SELECT installments_paid,installments_missed FROM accounts WHERE account_number=?", (number,)))
        cash = lambda: self.db.users.get_user('citizen')['cash']
        start = _time.time()
        before = cash()
        notices = bank.process_scheduled_deposits(now=start + 2 * 86400 + 60)
        self.assertEqual(progress(), (2, 0))
        self.assertEqual(balance(number), 200)
        self.assertEqual(cash(), before - 200)
        self.assertEqual(notices[0]['paid_count'], 2)
        self.assertEqual(bank.process_scheduled_deposits(now=start + 2 * 86400 + 60), [])
        # No wallet cash: the spendable 입출금통장 pays instead.
        self.assertTrue(bank.open_account('citizen', 'DEM2', 'pass1234', 150)[0])
        demand = self.db.accounts.execute_one("SELECT account_number FROM accounts WHERE product_code='DEM2'")[0]
        self.db.users.execute("UPDATE users SET cash=0 WHERE user_id='citizen'")
        bank.process_scheduled_deposits(now=start + 3 * 86400 + 60)
        self.assertEqual(progress(), (3, 0))
        self.assertEqual(balance(demand), 50)
        # Nothing left anywhere: missed, and nothing is taken after maturity (day 5).
        bank.process_scheduled_deposits(now=start + 30 * 86400)
        self.assertEqual(progress(), (3, 1))
        self.assertEqual(balance(number), 300)

    def test_cma_and_wrap_accounts(self):
        self.prepare()
        self.create('cma', 'CMA1', 'CMA', '', '', interest_rate=2, deposit_amount=0)
        self.assertTrue(self.bot.bank_mgr.open_account('citizen', 'CMA1', 'pass1234', 5000)[0])
        self.db.users.create_user('manager', '운용역', '낙원', 0)
        self.create('wrap', 'WRAP1', '랩', '', '', manager_id='manager', management_fee_percent=1, deposit_amount=0)
        ok, message = self.bot.bank_mgr.open_account('citizen', 'WRAP1', 'pass1234', 100000)
        self.assertTrue(ok, message)
        account = self.db.accounts.execute_one("SELECT account_number,managed_by FROM accounts WHERE product_code='WRAP1'")
        self.assertEqual(account['managed_by'], 'manager')
        self.assertFalse(self.bot.capital_markets.wrap_order('citizen', account['account_number'], 'INZST001', 'buy', 1, 1000)[0])
        price = int(self.db.companies.execute_one("SELECT share_price FROM company WHERE company_code='INZST001'")[0])
        ok, message = self.bot.capital_markets.wrap_order('manager', account['account_number'], 'INZST001', 'buy', 2, price * 2)
        self.assertTrue(ok, message)
        # The owner's ordinary orders never draw on the wrap account.
        with self.db.atomic() as conn:
            funds = self.bot.user_mgr._choose_funds(conn, 'citizen')
        self.assertNotEqual(funds[1], account['account_number'])

    def test_ima_pays_the_reference_gain_at_maturity_from_the_bank(self):
        self.prepare()
        self.create('ima', 'IMA1', 'IMA', '', '', reference_code='INZST001', participation_percent=50,
                    deposit_amount=0, maturity_days=1, interest_rate=0, issuer='Inazina')
        self.assertTrue(self.bot.bank_mgr.open_account('citizen', 'IMA1', 'pass1234', 100000)[0])
        number = self.db.accounts.execute_one("SELECT account_number FROM accounts WHERE product_code='IMA1'")[0]
        reference = self.db.accounts.execute_one(
            "SELECT reference_price,reference_price_minor_units FROM accounts WHERE account_number=?",
            (number,),
        )
        self.assertEqual(reference['reference_price_minor_units'], reference['reference_price'])
        self.db.companies.execute("UPDATE company SET share_price=share_price*2 WHERE company_code='INZST001'")
        self.db.accounts.execute("UPDATE accounts SET created_at=datetime('now','-2 days') WHERE account_number=?", (number,))
        cash = self.db.users.get_user('citizen')['cash']
        ok, message = self.bot.bank_mgr.close_account('citizen', number, password='pass1234')
        self.assertTrue(ok, message)
        # 100% gain shared at 50% on 100,000 principal.
        self.assertEqual(self.db.users.get_user('citizen')['cash'] - cash, 150000)
        self.assertIn('IMA', message)
        # The head office paid the excess and fell into deficit: administrators are warned once.
        alerts = self.bot.capital_markets.bank_deficit_alerts()
        self.assertEqual([alert['issuer'] for alert in alerts], ['Inazina'])
        self.assertEqual(alerts[0]['cash_minor_units'], alerts[0]['cash'])  # NWX has zero decimals.
        saved_alert = self.db.currency.execute_one(
            "SELECT cash,cash_minor_units,cash_decimal_places,cash_currency_code "
            "FROM bank_deficit_alerts WHERE issuer='Inazina'")
        self.assertEqual(tuple(saved_alert), (alerts[0]['cash'], alerts[0]['cash_minor_units'], 0, 'NWX'))
        self.assertEqual(self.bot.capital_markets.bank_deficit_alerts(), [])
        with self.db.atomic() as conn:
            self.assertEqual(len(CapitalMarketsManager.bank_deficits(conn)), 1)


if __name__ == "__main__":
    unittest.main()
