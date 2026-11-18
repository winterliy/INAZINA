import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.game_funding import game_funds
from managers.user_manager import UserManager
from managers.lottery_manager import LotteryManager


class _Undo(Exception):
    """Leaves an atomic block so the tampering is rolled back."""


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

    def _scd_player(self, cash):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('SCD','남방','1/120',2)")
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code,tax_percent) VALUES ('United Southerns','SCD',10)")
        self.db.users.create_user('scd-player', '남방인', 'United Southerns', cash)

    def _cash_minor(self, user):
        return int(self.db.users.execute_one("SELECT cash_minor FROM users WHERE user_id=?", (user,))[0])

    def test_anyone_can_buy_the_lottery_but_pays_its_base_currency_price_exchanged(self):
        # The operator is a 낙원 company: the ticket costs 1,000 base units for everyone.
        self._scd_player(1_000_000)
        before = self._cash_minor('scd-player')
        bought = self.lottery.purchase('scd-player', 2, 'auto', 'scd-request', source='__wallet__')
        self.assertEqual((bought['price'], len(bought['tickets'])), (1000, 2))
        with self.db.readonly() as conn:
            fx = dict(conn.execute("SELECT * FROM lottery_fx_settlements WHERE kind='purchase'").fetchone())
            round_row = conn.execute("SELECT sales_total,treasury_total FROM lottery_rounds").fetchone()
            pool = conn.execute("SELECT COALESCE(SUM(amount),0) FROM lottery_pool_ledger").fetchone()[0]
            operator = conn.execute(
                "SELECT u.cash_minor FROM country_treasuries t JOIN users u ON u.user_id=t.user_id "
                "WHERE t.nationality='낙원'").fetchone()
        self.assertEqual((fx['reference'], fx['user_id'], fx['base_minor_units'], fx['currency'],
                          fx['decimal_places']), ('scd-request', 'scd-player', 2000, 'SCD', 2))
        # 2,000 base units at 1 SCD = 1/120 base is about 240,000.00 SCD, rounded up to a cent.
        paid = before - self._cash_minor('scd-player')
        self.assertEqual(paid, fx['minor_units'])
        numerator, denominator = int(fx['rate_numerator']), int(fx['rate_denominator'])
        self.assertEqual(paid, -(-2000 * numerator * 100 // denominator))
        self.assertTrue(20_000_000 < paid < 28_000_000, paid)
        # The pool and the operator country's levy stay in base units.
        self.assertEqual((round_row['sales_total'], pool), (2000, 2000))
        self.assertEqual(int(operator[0]), round_row['treasury_total'])
        # Replaying the request neither charges nor converts again.
        self.assertEqual(self.lottery.purchase('scd-player', 2, 'auto', 'scd-request', source='__wallet__'), bought)
        self.assertEqual(before - self._cash_minor('scd-player'), paid)

    def _base_account(self, units):
        from managers.currency_exchange_manager import CurrencyExchangeManager
        from managers.currency_manager import CurrencyManager
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind,status) "
            "VALUES ('FX_BASE','기준통화 외환통장',0,?,'saving','active')", (self.home,))
        number = CurrencyExchangeManager(self.db).open_account('scd-player', 'FX_BASE')
        if units:
            with self.db.atomic() as conn:
                CurrencyManager.change(conn, 'scd-player', self.home, units * 1_000_000)
        return number

    def _account_minor(self, number):
        return int(self.db.accounts.execute_one(
            "SELECT balance_minor_units FROM accounts WHERE account_number=?", (number,))[0])

    def _fx_count(self):
        return self.db.lottery.execute_one("SELECT COUNT(*) FROM lottery_fx_settlements")[0]

    def test_foreign_player_pays_from_a_base_currency_account_when_it_covers_the_price(self):
        self._scd_player(1_000_000)
        number = self._base_account(2500)
        cash = self._cash_minor('scd-player')
        self.lottery.purchase('scd-player', 2, 'auto', 'from-account')
        # Paid as 2,000 base units from the account: no exchange, no home cash.
        self.assertEqual(self._account_minor(number), 500)
        self.assertEqual(self._cash_minor('scd-player'), cash)
        self.assertEqual(self._fx_count(), 0)
        # 500 left does not cover another ticket, so this one is exchanged from home money.
        self.lottery.purchase('scd-player', 1, 'auto', 'from-cash')
        self.assertEqual(self._account_minor(number), 500)
        self.assertLess(self._cash_minor('scd-player'), cash)
        self.assertEqual(self._fx_count(), 1)

    def test_foreign_player_can_name_the_base_account_or_insist_on_home_money(self):
        self._scd_player(1_000_000)
        number = self._base_account(5000)
        cash = self._cash_minor('scd-player')
        self.lottery.purchase('scd-player', 1, 'auto', 'named-account', source=number)
        self.assertEqual((self._account_minor(number), self._cash_minor('scd-player')), (4000, cash))
        self.lottery.purchase('scd-player', 1, 'auto', 'named-wallet', source='__wallet__')
        self.assertEqual(self._account_minor(number), 4000)
        self.assertLess(self._cash_minor('scd-player'), cash)
        # A named account that cannot cover the price is refused rather than silently swapped.
        import json
        with self.db.atomic() as conn:
            rules = json.loads(conn.execute("SELECT rules_json FROM lottery_rounds").fetchone()[0])
            conn.execute("UPDATE lottery_rounds SET rules_json=?", (json.dumps({**rules, 'max_tickets': 1000}),))
        with self.assertRaisesRegex(ValueError, '부족'):
            self.lottery.purchase('scd-player', 5, 'auto', 'named-short', source=number)
        self.assertEqual(self._account_minor(number), 4000)

    def test_foreign_player_without_enough_home_money_is_refused_untouched(self):
        self._scd_player(1000)  # worth about 8 base units
        with self.assertRaisesRegex(ValueError, '부족'):
            self.lottery.purchase('scd-player', 1, 'auto', 'poor-request', source='__wallet__')
        self.assertEqual(self._cash_minor('scd-player'), 100_000)
        with self.db.readonly() as conn:
            for table in ('lottery_tickets', 'lottery_pool_ledger', 'lottery_fx_settlements'):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_foreign_winner_is_paid_the_base_currency_prize_exchanged(self):
        self._scd_player(1_000_000)
        bought = self.lottery.purchase('scd-player', 1, '1 2 3 4 5 6', 'win-request', source='__wallet__')
        after_purchase = self._cash_minor('scd-player')
        with self.db.atomic() as conn:
            conn.execute("UPDATE lottery_rounds SET close_epoch=1,draw_epoch=2,"
                         "admin_numbers_json='{\"winning\":[1,2,3,4,5,6],\"bonus\":7}'")
        self.assertIsNotNone(self.lottery.process_due())
        with self.db.readonly() as conn:
            payout = conn.execute("SELECT gross,tax,net FROM lottery_payouts WHERE ticket_id=?",
                                  (bought['tickets'][0]['ticket_id'],)).fetchone()
            fx = dict(conn.execute("SELECT * FROM lottery_fx_settlements WHERE kind='payout'").fetchone())
            tax_row = conn.execute("SELECT currency_code,tax_amount_minor_units FROM tax_transactions "
                                   "WHERE user_id='scd-player' AND source='lottery_winnings'").fetchone()
        self.assertGreater(payout['gross'], 0)
        self.assertEqual(payout['gross'], payout['tax'] + payout['net'])
        # Prize, tax and net are base units; only what reaches the wallet is exchanged.
        self.assertEqual(tuple(tax_row), (self.home, payout['tax']))
        self.assertEqual((fx['base_minor_units'], fx['currency']), (payout['net'], 'SCD'))
        received = self._cash_minor('scd-player') - after_purchase
        self.assertEqual(received, fx['minor_units'])
        self.assertEqual(received, payout['net'] * int(fx['rate_numerator']) * 100 // int(fx['rate_denominator']))
        self.assertNotEqual(received, payout['net'] * 100)

    def _lottery_check(self):
        from managers.accounting_journal import AccountingJournal
        with self.db.readonly() as conn:
            return next(item for item in AccountingJournal.reconcile_subledgers(conn)
                        if item['ledger_scope'] == 'lottery')

    def test_lottery_books_reconcile_through_sale_exchange_draw_and_payout(self):
        self._scd_player(1_000_000)
        # Fixed losing numbers: a random pick could match the draw and add a payout.
        self.lottery.purchase('player', 2, '40 41 42 43 44 45', 'base-sale', source='GAME')
        self.lottery.purchase('scd-player', 1, '1 2 3 4 5 6', 'fx-sale', source='__wallet__')
        self.assertEqual(self._lottery_check()['status'], 'matched', self._lottery_check())
        with self.db.atomic() as conn:
            conn.execute("UPDATE lottery_rounds SET close_epoch=1,draw_epoch=2,"
                         "admin_numbers_json='{\"winning\":[1,2,3,4,5,6],\"bonus\":7}'")
        self.assertIsNotNone(self.lottery.process_due())
        check = self._lottery_check()
        self.assertEqual(check['status'], 'matched', check)
        self.assertEqual((check['payout_count'], check['fx_settlement_count']), (1, 2))
        self.assertGreaterEqual(check['round_count'], 2)

    def test_lottery_reconciliation_reports_each_kind_of_break(self):
        self._scd_player(1_000_000)
        self.lottery.purchase('scd-player', 1, '1 2 3 4 5 6', 'fx-sale', source='__wallet__')
        with self.db.atomic() as conn:
            conn.execute("UPDATE lottery_rounds SET close_epoch=1,draw_epoch=2,"
                         "admin_numbers_json='{\"winning\":[1,2,3,4,5,6],\"bonus\":7}'")
        self.lottery.process_due()
        from managers.accounting_journal import AccountingJournal
        breaks = {
            'sales_allocation_mismatch': "UPDATE lottery_rounds SET sales_total=sales_total+1 WHERE round_id=1",
            'ticket_sales_mismatch': "UPDATE lottery_tickets SET price=price+1",
            'pool_balance_mismatch': "UPDATE lottery_pool_ledger SET amount=amount+1 WHERE entry_id="
                                     "(SELECT MIN(entry_id) FROM lottery_pool_ledger WHERE kind='sale' AND rank=2)",
            'payout_amount_mismatch': "UPDATE lottery_payouts SET net=net+1",
            'payout_without_pool_entry': "DELETE FROM lottery_pool_ledger WHERE kind='payout'",
        }
        for status, statement in breaks.items():
            with self.subTest(status=status):
                try:
                    with self.db.atomic() as conn:
                        conn.execute(statement)
                        check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                                     if item['ledger_scope'] == 'lottery')
                        raise _Undo()
                except _Undo:
                    pass
                self.assertIn(status, {gap['status'] for gap in check['gaps']}, check)
        # An exchange record that no longer matches its own rate is reported too.
        try:
            with self.db.atomic() as conn:
                for operation in ('update', 'delete'):
                    conn.execute(f"DROP TRIGGER trg_lottery_fx_settlements_{operation}")
                conn.execute("UPDATE lottery_fx_settlements SET minor_units=minor_units+1 WHERE kind='payout'")
                conn.execute("DELETE FROM lottery_fx_settlements WHERE kind='purchase'")
                check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                             if item['ledger_scope'] == 'lottery')
                raise _Undo()
        except _Undo:
            pass
        self.assertIn('fx_settlement_mismatch', {gap['status'] for gap in check['gaps']}, check)
        self.assertEqual(self._lottery_check()['status'], 'matched')

    def test_lottery_fx_settlements_are_append_only(self):
        self._scd_player(1_000_000)
        self.lottery.purchase('scd-player', 1, 'auto', 'immutable-request', source='__wallet__')
        for statement in ("UPDATE lottery_fx_settlements SET minor_units=1", "DELETE FROM lottery_fx_settlements"):
            with self.subTest(statement=statement), self.assertRaisesRegex(Exception, 'append-only'):
                self.db.lottery.execute(statement)

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
