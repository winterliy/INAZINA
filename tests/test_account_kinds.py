"""통장 종류별 현실 규칙 (managers/deposit_accounts.py KIND_TERMS, database/account_kind_rules.py)."""
import json
import unittest

import test_inazina as fixtures


class AccountKindRuleTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    SAVING = dict(interest_rate=0, deposit_amount=0, interest_cycle=86400, compound_type=0)

    def product(self, code, kind, maturity_days=0, terms=None, **extra):
        values = {**self.SAVING, 'maturity_days': maturity_days, 'account_kind': kind, **extra}
        if terms is not None:
            values['kind_terms'] = json.dumps(terms)
        return self.bot.admin_control.entity('admin', 'saving', 'create', code, code, '', '', values)

    def open(self, code, deposit=0, user='citizen'):
        ok, message = self.bot.bank_mgr.open_account(user, code, 'pass1234', deposit)
        self.assertTrue(ok, message)
        return self.db.accounts.execute_one(
            "SELECT account_number FROM accounts WHERE product_code=? AND user_id=?", (code, user))[0]

    def pay(self, account, amount, source=None, user='citizen'):
        return self.bot.user_mgr.transfer(user, account, amount, source)

    def balance(self, account):
        return self.db.accounts.execute_one("SELECT balance FROM accounts WHERE account_number=?", (account,))[0]

    def test_contract_copies_kind_and_terms_at_opening(self):
        self.seed()
        self.product('ISA1', 'isa', terms={'annual_limit': 500})
        number = self.open('ISA1')
        row = self.db.accounts.execute_one("SELECT account_kind,kind_terms FROM accounts WHERE account_number=?", (number,))
        terms = json.loads(row['kind_terms'])
        self.assertEqual(row['account_kind'], 'isa')
        self.assertEqual(terms['annual_limit'], 500)
        self.assertEqual(terms['total_limit'], 100000000)  # default kept
        # Editing the product later does not change the signed contract.
        self.bot.admin_control.entity('admin', 'saving', 'edit', 'ISA1', None, None, None,
                                      {'kind_terms': json.dumps({'annual_limit': 9})})
        terms = json.loads(self.db.accounts.execute_one(
            "SELECT kind_terms FROM accounts WHERE account_number=?", (number,))[0])
        self.assertEqual(terms['annual_limit'], 500)

    def test_time_deposit_takes_one_deposit_and_releases_money_only_on_closing(self):
        self.seed()
        self.product('TD1', 'time_deposit', maturity_days=365)
        number = self.open('TD1', 1000)
        ok, message = self.pay(number, 100)
        self.assertFalse(ok)
        self.assertIn('추가 입금', message)
        ok, message = self.pay('citizen', 100, source=number)
        self.assertFalse(ok)
        self.assertIn('해지', message)
        self.assertEqual(self.balance(number), 1000)
        ok, message = self.bot.bank_mgr.close_account('citizen', number, password='pass1234')
        self.assertTrue(ok, message)

    def test_free_installment_period_limit_and_no_withdrawal(self):
        self.seed()
        self.product('FR1', 'free_installment', maturity_days=365, terms={'period_limit': 300})
        number = self.open('FR1')
        self.assertTrue(self.pay(number, 200)[0])
        ok, message = self.pay(number, 150)
        self.assertFalse(ok)
        self.assertIn('한도', message)
        self.assertTrue(self.pay(number, 100)[0])
        self.assertFalse(self.pay('citizen', 50, source=number)[0])
        self.assertEqual(self.balance(number), 300)

    def test_housing_payment_range_and_one_account_per_person(self):
        self.seed()
        self.product('HS1', 'housing', terms={'min_payment': 20, 'max_payment': 50})
        self.product('HS2', 'housing')
        number = self.open('HS1')
        ok, message = self.pay(number, 10)
        self.assertFalse(ok)
        self.assertIn('최소', message)
        self.assertTrue(self.pay(number, 30)[0])
        ok, message = self.pay(number, 30)
        self.assertFalse(ok)
        self.assertIn('최대', message)
        ok, message = self.bot.bank_mgr.open_account('citizen', 'HS2', 'pass1234', 0)
        self.assertFalse(ok)
        self.assertIn('1인 1계좌', message)

    def test_isa_limits_and_principal_only_withdrawal(self):
        self.seed()
        self.product('ISA1', 'isa', terms={'annual_limit': 500, 'total_limit': 2000})
        number = self.open('ISA1')
        self.assertTrue(self.pay(number, 400)[0])
        ok, message = self.pay(number, 200)
        self.assertFalse(ok)
        self.assertIn('ISA', message)
        # Earnings stay in the account; only paid-in principal comes out early.
        self.db.accounts.execute("UPDATE accounts SET balance_units=balance_units+50000000 WHERE account_number=?", (number,))
        self.assertTrue(self.pay('citizen', 100, source=number)[0])
        ok, message = self.pay('citizen', 340, source=number)
        self.assertFalse(ok)
        self.assertIn('원금', message)

    def test_irp_annual_limit_and_no_partial_withdrawal(self):
        self.seed()
        self.product('IRP1', 'irp', terms={'annual_limit': 500})
        number = self.open('IRP1')
        self.assertTrue(self.pay(number, 500)[0])
        self.assertFalse(self.pay(number, 1)[0])
        ok, message = self.pay('citizen', 10, source=number)
        self.assertFalse(ok)
        self.assertIn('해지', message)

    def test_automatic_flows_skip_locked_accounts(self):
        self.seed()
        self.product('TD1', 'time_deposit', maturity_days=365)
        self.product('DEM1', 'demand')
        locked = self.open('TD1', 1000)
        spendable = self.open('DEM1', 0)
        from managers.deposit_accounts import account
        with self.db.readonly() as conn:
            home = conn.execute("SELECT quote_currency FROM accounts WHERE account_number=?", (locked,)).fetchone()[0]
            self.assertEqual(account(conn, 'citizen', home)['account_number'], spendable)
        # A wallet payment larger than the wallet never dips into the 정기예금.
        cash = self.db.users.get_user('citizen')['cash']
        ok, _ = self.pay('treasury', cash + 500)
        self.assertFalse(ok)
        self.assertEqual(self.balance(locked), 1000)


    def settle(self, now, user='citizen'):
        from managers.interest_manager import InterestManager
        InterestManager(self.db).process_user_interests(user, now=now)

    def opened(self, account):
        from datetime import datetime, timezone
        created = self.db.accounts.execute_one("SELECT created_at FROM accounts WHERE account_number=?", (account,))[0]
        return datetime.strptime(created, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()

    def test_parking_pays_preferred_rate_only_up_to_the_limit(self):
        self.seed()
        self.product('PK1', 'parking', interest_rate=10, interest_cycle=31536000,
                     terms={'preferred_limit': 1000, 'excess_rate': 0})
        number = self.open('PK1', 5000)
        self.settle(self.opened(number) + 31536000 + 10)
        # 10% on the first 1,000 only (before tax).
        gross = self.db.tax.execute_one(
            "SELECT SUM(gross_amount) FROM tax_transactions WHERE user_id='citizen' AND source='saving_interest'")[0]
        self.assertEqual(gross, 100)

    def test_post_maturity_rate_steps_down(self):
        self.seed()
        self.product('TD2', 'time_deposit', maturity_days=365, interest_rate=10, interest_cycle=31536000,
                     terms={'post_maturity': [{'days': 365, 'percent': 50}, {'rate': 0}]})
        number = self.open('TD2', 10000)
        opened = self.opened(number)
        gross = lambda: self.db.tax.execute_one(
            "SELECT COALESCE(SUM(gross_amount),0) FROM tax_transactions WHERE user_id='citizen' "
            "AND source='saving_interest'")[0]
        self.settle(opened + 365 * 86400 + 1)
        self.assertEqual(gross(), 1000)  # the full term, paid at maturity
        matured_balance = self.balance(number)
        # Then 50% of the contract rate for a year after maturity, then nothing.
        self.settle(opened + 3 * 365 * 86400)
        self.assertAlmostEqual(gross() - 1000, round(matured_balance * 0.05), delta=1)

    def test_isa_interest_is_credited_untaxed_and_remembered(self):
        self.seed()
        self.product('ISA2', 'isa', interest_rate=10, interest_cycle=31536000)
        number = self.open('ISA2', 1000)
        self.settle(self.opened(number) + 31536000 + 10)
        row = self.db.accounts.execute_one(
            "SELECT balance,deferred_interest_units FROM accounts WHERE account_number=?", (number,))
        self.assertEqual(row['balance'], 1100)
        self.assertEqual(row['deferred_interest_units'], 100 * 1000000)

    def test_housing_pays_no_interest_while_saving(self):
        self.seed()
        self.product('HS3', 'housing', interest_rate=10, interest_cycle=86400)
        number = self.open('HS3', 20000)
        self.settle(self.opened(number) + 400 * 86400)
        self.assertEqual(self.balance(number), 20000)

    def test_opening_deposit_counts_toward_limits(self):
        self.seed()
        self.product('ISA3', 'isa', terms={'annual_limit': 500})
        ok, message = self.bot.bank_mgr.open_account('citizen', 'ISA3', 'pass1234', 600)
        self.assertFalse(ok)
        self.assertIn('ISA', message)
        number = self.open('ISA3', 400)
        self.assertFalse(self.pay(number, 200)[0])
        self.assertTrue(self.pay(number, 100)[0])

    def age(self, account, days):
        """Pretend the account (and its payments) started ``days`` ago."""
        self.db.accounts.execute(
            "UPDATE accounts SET created_at=datetime(created_at, ?) WHERE account_number=?", (f'-{days} days', account))
        self.db.accounts.execute(
            "UPDATE account_contributions SET epoch=epoch-? WHERE account_number=?", (days * 86400, account))

    def closing_tax(self, kind):
        return self.db.tax.execute_one(
            "SELECT gross_amount,tax_amount,net_amount FROM tax_transactions WHERE source=? ORDER BY rowid DESC",
            (f'{kind}_closing',))

    def test_isa_closing_tax_free_after_lock_and_ordinary_tax_before(self):
        from managers.interest_manager import InterestManager
        self.seed()
        self.product('ISA4', 'isa', terms={'tax_free': 30, 'separate_tax_percent': 10, 'lock_days': 1095})
        early = self.open('ISA4', 100)
        self.db.accounts.execute("UPDATE accounts SET deferred_interest_units=50000000 WHERE account_number=?", (early,))
        ok, message = self.bot.bank_mgr.close_account('citizen', early, password='pass1234')
        self.assertTrue(ok, message)
        with self.db.readonly() as conn:
            ordinary = InterestManager._tax_rate(conn, 'citizen', 'saving_interest')
        self.assertAlmostEqual(float(self.closing_tax('isa')['tax_amount']), round(50 * float(ordinary) / 100), delta=1)
        later = self.open('ISA4', 100)
        self.db.accounts.execute("UPDATE accounts SET deferred_interest_units=50000000 WHERE account_number=?", (later,))
        self.age(later, 1100)
        self.assertTrue(self.bot.bank_mgr.close_account('citizen', later, password='pass1234')[0])
        self.assertEqual(self.closing_tax('isa')['tax_amount'], 2)  # (50 - 30 tax-free) x 10%

    def test_housing_closing_pays_holding_period_rate(self):
        self.seed()
        self.product('HS4', 'housing', interest_rate=0,
                     terms={'rate_tiers': [{'days': 30, 'rate': 0}, {'days': 730, 'rate': 10}, {'rate': 20}]})
        number = self.open('HS4', 100000)
        self.age(number, 365)
        ok, message = self.bot.bank_mgr.close_account('citizen', number, password='pass1234')
        self.assertTrue(ok, message)
        self.assertIn('주택청약', message)
        self.assertEqual(self.closing_tax('housing')['gross_amount'], 10000)  # 100,000 x 10% x 1 year

    def test_irp_year_end_credit_then_early_closing_tax(self):
        from managers.account_kind_settlement import settle_irp_tax_credits
        self.seed()
        self.product('IRP2', 'irp', terms={'credit_limit': 600, 'credit_percent': 10,
                                            'credit_percent_low_income': 20, 'income_threshold': 0,
                                            'early_tax_percent': 50})
        number = self.open('IRP2', 1000)
        cash = self.db.users.get_user('citizen')['cash']
        with self.db.atomic(('users', 'accounts', 'tax')) as conn:
            self.assertEqual(settle_irp_tax_credits(conn), [])  # this year is not over
        self.age(number, 400)
        self.db.users.execute("UPDATE users SET annual_income=1000 WHERE user_id='citizen'")
        with self.db.atomic(('users', 'accounts', 'tax')) as conn:
            notices = settle_irp_tax_credits(conn)
        self.assertEqual(len(notices), 1)
        self.assertEqual(self.db.users.get_user('citizen')['cash'], cash + 60)  # 600 x 10%
        ok, message = self.bot.bank_mgr.close_account('citizen', number, password='pass1234')
        self.assertTrue(ok, message)
        self.assertEqual(self.closing_tax('irp')['tax_amount'], 300)  # credited 600 x 50%

    def overdraft(self, account):
        return self.db.accounts.execute_one(
            "SELECT balance_units,overdraft_units,overdraft_limit_units,overdraft_interest_units,overdraft_day,"
            "overdraft_day_max_units FROM accounts WHERE account_number=?", (account,))

    def test_combined_account_borrows_the_shortfall_and_repays_from_deposits(self):
        self.seed()
        self.product('CB1', 'combined', terms={'overdraft_limit': 1000, 'overdraft_rate': 36.5, 'min_credit_score': 0})
        number = self.open('CB1', 100)
        ok, message = self.pay('treasury', 300, source=number)
        self.assertTrue(ok, message)
        row = self.overdraft(number)
        self.assertEqual((row['balance_units'], row['overdraft_units']), (0, 200 * 1000000))
        # Beyond balance + remaining limit is refused.
        ok, _ = self.pay('treasury', 900, source=number)
        self.assertFalse(ok)
        ok, message = self.bot.bank_mgr.close_account('citizen', number, password='pass1234')
        self.assertFalse(ok)
        self.assertIn('마이너스', message)
        # A deposit is used to repay by the bank job.
        self.assertTrue(self.pay(number, 150)[0])
        self.bot.bank_mgr.process_account_kind_jobs()
        row = self.overdraft(number)
        self.assertEqual((row['balance_units'], row['overdraft_units']), (0, 50 * 1000000))

    def test_combined_interest_uses_each_days_peak_and_is_added_monthly(self):
        self.seed()
        self.product('CB2', 'combined', terms={'overdraft_limit': 1000, 'overdraft_rate': 36.5, 'min_credit_score': 0,
                                                'late_spread': 0})
        number = self.open('CB2', 0)
        self.assertTrue(self.pay('treasury', 100, source=number)[0])
        # Pretend today's peak (100) was 10 days ago and the debt stayed since.
        self.db.accounts.execute("UPDATE accounts SET overdraft_day=overdraft_day-10 WHERE account_number=?", (number,))
        self.bot.bank_mgr.process_account_kind_jobs()
        # 10 days x 100 x 36.5% / 365 = 1.0
        self.assertAlmostEqual(self.overdraft(number)['overdraft_interest_units'] / 1000000, 1.0, places=4)
        self.db.accounts.execute("UPDATE accounts SET overdraft_charged_epoch=overdraft_charged_epoch-2600000 "
                                 "WHERE account_number=?", (number,))
        self.bot.bank_mgr.process_account_kind_jobs()
        self.assertEqual(self.overdraft(number)['overdraft_units'], 101 * 1000000)

    def test_combined_limit_shrinks_when_unused_after_review_period(self):
        self.seed()
        self.product('CB3', 'combined', terms={'overdraft_limit': 1000, 'min_credit_score': 0, 'review_after_days': 90,
                                                'usage_floor_percent': 50, 'reduce_percent': 10, 'min_limit_percent': 50})
        number = self.open('CB3', 0)
        self.age(number, 120)
        self.db.accounts.execute("UPDATE accounts SET overdraft_review_epoch=overdraft_review_epoch-2600000 "
                                 "WHERE account_number=?", (number,))
        self.bot.bank_mgr.process_account_kind_jobs()
        self.assertEqual(self.overdraft(number)['overdraft_limit_units'], 900 * 1000000)

    def test_combined_requires_credit_score_and_cma_is_uninsured(self):
        self.seed()
        self.product('CB4', 'combined', terms={'min_credit_score': 2000})
        ok, message = self.bot.bank_mgr.open_account('citizen', 'CB4', 'pass1234', 0)
        self.assertFalse(ok)
        self.assertIn('신용점수', message)
        self.product('CM1', 'cma')
        number = self.open('CM1', 10)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT deposit_insured FROM accounts WHERE account_number=?", (number,))[0], 0)

if __name__ == '__main__':
    unittest.main()
