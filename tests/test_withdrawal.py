"""Withdrawal: everything the member owns passes to the national treasury, personal data is deleted and the ID is replaced."""
import re
import unittest

import test_inazina as fixtures
from managers.bank_manager import BankManager
from managers.currency_manager import CurrencyManager
from managers.withdrawal_manager import WithdrawalManager, COLUMN_BUCKETS, USER_COLUMN_PATTERN


class WithdrawalTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def prepare(self):
        self.seed()
        self.assertTrue(BankManager(self.db).open_account('citizen', 'INZSV001', 'pass1234', 15000)[0])
        self.db.assets.add_user_asset('citizen', 'INZST001', 3, unit_cost=100)
        with self.db.atomic() as conn:
            self.treasury = CurrencyManager.user_treasury_id(conn, 'citizen')

    def total_cash(self):
        """All money in NWX: cash plus the balances of bank accounts (closing an account turns its balance into cash)."""
        with self.db.atomic() as conn:
            cash = sum(int(row[0] or 0) for row in conn.execute("SELECT cash_minor FROM users"))
            accounts = sum(int(row[0] or 0) for row in conn.execute("SELECT balance_units FROM accounts WHERE quote_currency='NWX'"))
        return cash + accounts // 1_000_000

    def total_shares(self, code='INZST001'):
        return self.db.assets.execute_one("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE asset_code=?", (code,))[0]

    def owners(self, table, column, value):
        return self.db.assets.execute_one(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (value,))[0]

    def test_everything_passes_to_the_treasury_and_nothing_is_created_or_lost(self):
        self.prepare()
        from managers.interest_manager import InterestManager
        InterestManager(self.db).process_user_interests('citizen')  # the withdrawal settles interest first
        cash_before, shares_before = self.total_cash(), self.total_shares()
        ok, message = self.bot.order_book.place_limit_order('citizen', 'INZST001', 'buy', 2, 1)  # reserves cash; cancelling returns it
        self.assertTrue(ok, message)
        loan_before = self.db.accounts.execute_one("SELECT COALESCE(SUM(outstanding_balance),0) FROM loans")[0]
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,quote_currency) "
                         "VALUES ('citizen','X',100,100,'active','NWX')")

        result = WithdrawalManager(self.bot).withdraw('citizen')

        tomb = result['tombstoneId']
        self.assertTrue(re.fullmatch(r'w-[0-9a-f]{16}', tomb), tomb)
        self.assertIsNone(self.db.users.get_user('citizen'), 'the Discord ID is gone from the users table')
        row = self.db.users.get_user(tomb)
        self.assertEqual((row['status'], row['nickname']), ('withdrawn', '탈퇴한 사용자'))
        self.assertEqual(self.total_cash(), cash_before, 'cash is only moved, never created or destroyed')
        self.assertEqual(self.total_shares(), shares_before)
        self.assertEqual(self.owners('user_assets', 'user_id', 'citizen'), 0)
        self.assertGreaterEqual(self.db.assets.execute_one("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE user_id=? AND asset_code='INZST001'",
                                                           (self.treasury,))[0], 3)
        self.assertEqual(self.owners('accounts', 'user_id', 'citizen'), 0)
        self.assertEqual(self.db.accounts.execute_one("SELECT COUNT(*) FROM loans WHERE user_id=? AND product_code='X'", (self.treasury,))[0], 1,
                         'the contract now belongs to the treasury')
        self.assertEqual(self.db.accounts.execute_one("SELECT COALESCE(SUM(outstanding_balance),0) FROM loans")[0], loan_before + 100, 'no debt disappears')
        self.assertEqual(self.owners('market_orders', 'user_id', 'citizen'), 0)
        self.assertEqual(self.db.assets.execute_one(
            f"SELECT COUNT(*) FROM market_orders WHERE user_id=? AND status IN ('open','partial','scheduled')", (tomb,))[0], 0)
        record = self.db.system.execute_one("SELECT discord_user_id,tombstone_id,treasury_id FROM withdrawal_records WHERE tombstone_id=?", (tomb,))
        self.assertEqual(tuple(record), ('citizen', tomb, self.treasury))

    def test_personal_data_is_deleted_and_the_old_id_appears_nowhere(self):
        self.prepare()
        with self.db.atomic() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS web_auth_credentials (user_id TEXT PRIMARY KEY, salt TEXT, password_hash TEXT, created_epoch INTEGER, "
                         "updated_epoch INTEGER, auth_version INTEGER NOT NULL DEFAULT 0, hts_id TEXT)")
            conn.execute("INSERT INTO web_auth_credentials(user_id,salt,password_hash,created_epoch,updated_epoch,auth_version,hts_id) "
                         "VALUES ('citizen','s','h',1,1,0,'citizen-hts')")
            conn.execute("INSERT INTO notifications(user_id,kind,title,body,created_epoch) VALUES ('citizen','x','t','b',1)")
        WithdrawalManager(self.bot).withdraw('citizen')
        with self.db.atomic() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM web_auth_credentials WHERE user_id='citizen'").fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM notifications WHERE user_id='citizen'").fetchone())
            leftovers = []
            for table, columns in COLUMN_BUCKETS.items():
                for column in columns['columns']:
                    if columns['bucket'] == 'staff':
                        continue
                    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    if exists and conn.execute(f"SELECT 1 FROM {table} WHERE {column}='citizen' LIMIT 1").fetchone():
                        leftovers.append(f'{table}.{column}')
        self.assertEqual(leftovers, [])

    def test_open_positions_we_cannot_hand_over_block_the_withdrawal_and_nothing_moves(self):
        self.prepare()
        self.db.assets.execute("INSERT INTO futures_positions(user_id,product_code,quantity,settlement_price,collateral,net_pnl,updated_epoch) "
                               "VALUES ('citizen','F',1,1,1,0,0)")
        before = self.total_shares(), self.total_cash()
        with self.assertRaisesRegex(ValueError, '선물'):
            WithdrawalManager(self.bot).withdraw('citizen')
        self.assertIsNotNone(self.db.users.get_user('citizen'))
        self.assertEqual((self.total_shares(), self.total_cash()), before)

    def test_only_ordinary_members_can_withdraw(self):
        self.prepare()
        for role in ('admin', 'nation', 'company'):
            self.db.users.execute("UPDATE users SET role=? WHERE user_id='citizen'", (role,))
            with self.assertRaisesRegex(ValueError, '일반'):
                WithdrawalManager(self.bot).withdraw('citizen')
        self.db.users.execute("UPDATE users SET role='user' WHERE user_id='citizen'")
        with self.assertRaisesRegex(ValueError, '찾을 수 없'):
            WithdrawalManager(self.bot).withdraw('nobody')

    def test_dry_run_reports_the_moves_and_changes_nothing(self):
        self.prepare()
        before = self.total_cash(), self.total_shares()
        plan = WithdrawalManager(self.bot).plan('citizen')
        self.assertEqual(plan['treasuryId'], self.treasury)
        self.assertGreater(plan['holdings'], 0)
        self.assertEqual(plan['blockers'], [])
        self.assertIsNotNone(self.db.users.get_user('citizen'))
        self.assertEqual((self.total_cash(), self.total_shares()), before)

    def test_the_same_discord_id_can_register_again_as_a_new_member(self):
        self.prepare()
        WithdrawalManager(self.bot).withdraw('citizen')
        self.assertTrue(self.db.users.register_user('citizen', 'again', '낙원'))
        self.assertEqual(self.db.users.get_user('citizen')['nickname'], 'again')
        self.assertEqual(self.owners('user_assets', 'user_id', 'citizen'), 0, 'the new account starts empty')

    def test_every_column_that_holds_a_user_id_is_classified(self):
        """A new table with a user column must be put in a bucket, or this fails (so withdrawal never silently skips it)."""
        with self.db.atomic() as conn:
            found = {(row[0], row[1]) for row in conn.execute(
                "SELECT c.table_name, c.column_name FROM information_schema.columns c JOIN information_schema.tables t "
                "USING (table_schema, table_name) WHERE c.table_schema=current_schema() AND t.table_type='BASE TABLE'")
                if re.search(USER_COLUMN_PATTERN, row[1])}
        classified = {(table, column) for table, info in COLUMN_BUCKETS.items() for column in info['columns'] + info['staff']}
        self.assertEqual(sorted(found - classified), [], 'put these columns in COLUMN_BUCKETS')


if __name__ == '__main__':
    unittest.main()
