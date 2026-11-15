"""Savings product admission and transaction policies."""
import unittest

import test_inazina as fixtures


class ProductTradingPolicyTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def test_automatic_primary_account_skips_blocked_product(self):
        from managers.deposit_accounts import account
        self.seed()
        values = dict(interest_rate=0, deposit_amount=0, interest_cycle=86400,
                      compound_type=0, maturity_days=0, account_kind='demand')
        for code in ('POLICY_A', 'POLICY_B'):
            self.bot.admin_control.entity('admin', 'saving', 'create', code, code, '', '', values)
            self.assertTrue(self.bot.bank_mgr.open_account('citizen', code, 'pass1234', 0)[0])
        self.db.products.execute("UPDATE saving SET transaction_policy='blocked' WHERE product_code='POLICY_A'")
        with self.db.readonly() as conn:
            selected = account(conn, 'citizen', 'NWX')
        self.assertIsNotNone(selected, [dict(row) for row in self.db.accounts.execute(
            "SELECT account_number,product_code,quote_currency,account_kind FROM accounts WHERE user_id='citizen'")])
        self.assertEqual(selected['product_code'], 'POLICY_B')

    def test_blocked_product_still_credits_contract_interest(self):
        import time
        from managers.interest_manager import InterestManager
        self.seed()
        values = dict(interest_rate=1, deposit_amount=0, interest_cycle=86400,
                      compound_type=0, maturity_days=0, account_kind='demand')
        self.bot.admin_control.entity('admin', 'saving', 'create', 'POLICY_I', '이자 테스트', '', '', values)
        self.assertTrue(self.bot.bank_mgr.open_account('citizen', 'POLICY_I', 'pass1234', 100)[0])
        self.db.products.execute("UPDATE saving SET transaction_policy='blocked' WHERE product_code='POLICY_I'")
        with self.db.atomic() as conn:
            product = conn.execute("SELECT * FROM saving WHERE product_code='POLICY_I'").fetchone()
            row = conn.execute("SELECT * FROM accounts WHERE product_code='POLICY_I'").fetchone()
            InterestManager._credit_interest(conn, row, product, 10, time.time())
            after = conn.execute("SELECT balance_units FROM accounts WHERE account_number=?", (row['account_number'],)).fetchone()[0]
        self.assertGreater(after, row['balance_units'])

    def test_blocked_account_rejects_direct_balance_changes_but_allows_closing(self):
        self.seed()
        values = dict(interest_rate=0, deposit_amount=0, interest_cycle=86400,
                      compound_type=0, maturity_days=0, account_kind='demand')
        self.bot.admin_control.entity('admin', 'saving', 'create', 'POLICY2', '거래 제한 테스트', '', '', values)
        self.assertTrue(self.bot.bank_mgr.open_account('citizen', 'POLICY2', 'pass1234', 100)[0])
        number = self.db.accounts.execute_one(
            "SELECT account_number FROM accounts WHERE user_id=? AND product_code=?",
            ('citizen', 'POLICY2'))[0]
        self.db.products.execute("UPDATE saving SET transaction_policy='blocked' WHERE product_code=?", ('POLICY2',))
        for field, delta in (('balance_units', -1000000), ('balance_units', 1000000),
                             ('balance', 1), ('balance_minor_units', 1)):
            with self.subTest(field=field, delta=delta), self.assertRaises(Exception):
                with self.db.atomic() as conn:
                    conn.execute(f"UPDATE accounts SET {field}={field}+? WHERE account_number=?",
                                 (delta, number))
        ok, message = self.bot.bank_mgr.close_account('citizen', number, password='pass1234')
        self.assertTrue(ok, message)

    def test_blocking_new_accounts_preserves_existing_account_transactions(self):
        self.seed()
        values = dict(interest_rate=0, deposit_amount=0, interest_cycle=86400,
                      compound_type=0, maturity_days=0, account_kind='demand')
        self.bot.admin_control.entity('admin', 'saving', 'create', 'POLICY1', '정책 테스트', '', '', values)
        ok, message = self.bot.bank_mgr.open_account('citizen', 'POLICY1', 'pass1234', 0)
        self.assertTrue(ok, message)
        number = self.db.accounts.execute_one(
            "SELECT account_number FROM accounts WHERE user_id=? AND product_code=?",
            ('citizen', 'POLICY1'))[0]
        self.db.products.execute("UPDATE saving SET transaction_policy='no_new' WHERE product_code=?", ('POLICY1',))
        self.assertTrue(self.bot.user_mgr.transfer('citizen', number, 100)[0])
        self.assertTrue(self.bot.user_mgr.transfer('citizen', 'citizen', 50, number)[0])
        for kind in (None, 'demand', 'housing'):
            with self.subTest(kind=kind), self.assertRaisesRegex(Exception, '가입'):
                with self.db.atomic() as conn:
                    conn.execute("INSERT INTO accounts(account_number,user_id,product_code,account_type,account_kind) VALUES (?,?,?,?,?)",
                                 ('POLICY-DIRECT', 'admin', 'POLICY1', 'saving', kind))
