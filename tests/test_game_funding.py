import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.game_funding import game_funds
from managers.user_manager import UserManager
from managers.lottery_manager import LotteryManager


class GameFundingTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.users.create_user('player', 'Player', '낙원', 0)
        self.db.users.create_user('other', 'Other', '낙원', 0)
        self.user = UserManager(self.db)
        self.lottery = LotteryManager(self.db)
        with self.db.atomic() as conn:
            self.home = conn.execute("SELECT currency_code FROM country_currencies WHERE nationality='낙원'").fetchone()[0]
            conn.execute("INSERT INTO accounts(account_number,user_id,balance,quote_currency) VALUES ('GAME','player',10000,?)", (self.home,))
            conn.execute("INSERT INTO accounts(account_number,user_id,balance,quote_currency) VALUES ('OTHER','other',10000,?)", (self.home,))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def balance(self):
        with self.db.readonly() as conn:
            return conn.execute("SELECT balance FROM accounts WHERE account_number='GAME'").fetchone()[0]

    def test_auto_falls_back_to_account_and_explicit_wallet_does_not(self):
        with self.db.atomic() as conn:
            self.assertEqual(game_funds(conn, 'player'), (10000, 'GAME'))
            self.assertEqual(game_funds(conn, 'player', '__wallet__'), (0, None))
        self.db.system.set_setting('gamble_chance', '0')
        self.assertIn('꽝', self.user.gamble('player', 100)[1])
        self.assertEqual(self.balance(), 9900)
        self.assertIn('부족', self.user.gamble('player', 100, '__wallet__')[1])
        self.assertEqual(self.balance(), 9900)

    def test_rejects_foreign_other_closed_and_managed_accounts(self):
        for account in ('OTHER', 'missing'):
            self.assertFalse(self.user.gamble('player', 10, account)[0])
        for column, value in [('quote_currency', 'USD'), ('resolution_status', 'closed'), ('managed_by', 'other')]:
            with self.db.atomic() as conn:
                conn.execute(f'UPDATE accounts SET {column}=? WHERE account_number=?', (value, 'GAME'))
                with self.assertRaises(ValueError):
                    game_funds(conn, 'player', 'GAME')
                conn.execute("UPDATE accounts SET quote_currency=?,resolution_status='active',managed_by=NULL WHERE account_number='GAME'", (self.home,))
        self.assertEqual(self.balance(), 10000)

    def test_win_credits_selected_account(self):
        self.db.system.set_setting('gamble_chance', '100')
        success, message = self.user.gamble('player', 100, 'GAME')
        self.assertTrue(success, message)
        self.assertGreater(self.balance(), 10000)
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute("SELECT cash FROM users WHERE user_id='player'").fetchone()[0], 0)

    def test_lottery_account_debit_is_idempotent_and_failure_rolls_back(self):
        current = self.lottery.current()
        price = current['rules']['ticket_price']
        first = self.lottery.purchase('player', 1, 'auto', 'game-request', source='GAME')
        self.assertEqual(self.balance(), 10000-price)
        self.assertEqual(self.lottery.purchase('player', 1, 'auto', 'game-request', source='GAME'), first)
        self.assertEqual(self.balance(), 10000-price)
        for source in ('OTHER', '__wallet__'):
            with self.assertRaises(ValueError):
                self.lottery.purchase('player', 1, 'auto', 'failed-'+source, source=source)
        self.assertEqual(self.balance(), 10000-price)
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lottery_tickets WHERE user_id='player'").fetchone()[0], 1)


    def test_lottery_interest_failure_does_not_debit_account(self):
        from unittest.mock import patch
        with patch.object(UserManager, '_settle_user_interest', return_value='이자 정산 실패'):
            with self.assertRaisesRegex(ValueError, '이자 정산 실패'):
                self.lottery.purchase('player', 1, 'auto', 'interest-failure', source='GAME')
        self.assertEqual(self.balance(), 10000)
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM lottery_tickets').fetchone()[0], 0)
