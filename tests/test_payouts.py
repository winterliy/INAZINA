"""A receiver with no account in the payment's currency chooses: exchange now or a temporary account."""
import time
import unittest

from managers.accounting_journal import AccountingJournal
from managers.currency_exchange_manager import CurrencyExchangeManager
from managers.currency_manager import CurrencyManager
from managers.notification_manager import NotificationManager
from managers.payout_manager import DAY, HOLD_DAYS, PayoutManager
from tests import test_market_features as fixtures


class PayoutChoiceTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown
    issue = fixtures.MarketFeatureTests.issue
    issue_future = fixtures.MarketFeatureTests.issue_future
    _foreign_future_trader = fixtures.MarketFeatureTests._foreign_future_trader
    _nwx_units = fixtures.MarketFeatureTests._nwx_units

    USER = "foreign-future"

    def _futures_check(self):
        with self.db.readonly() as conn:
            return next(item for item in AccountingJournal.reconcile_subledgers(conn)
                        if item["ledger_scope"] == "futures_cash")

    def _close_position_without_an_nwx_account(self):
        """Open a position funded from an NWX account, drop the account, then let the position mature."""
        number = self._foreign_future_trader(5000)
        self.assertTrue(self.book.place_limit_order(self.USER, "FUT", "buy", 2, 100)[0])
        self.assertEqual(self._nwx_units(number), 4960)
        with self.db.atomic() as conn:     # the account is gone before the position pays out
            conn.execute("UPDATE accounts SET balance_units=0,balance_minor_units=0,balance=0 WHERE account_number=?",
                         (number,))
            conn.execute("DELETE FROM accounts WHERE account_number=?", (number,))
        wallet = self.db.users.get_user(self.USER)["cash"]
        now = int(time.time())
        self.db.products.execute("UPDATE investment SET maturity_epoch=? WHERE product_code='FUT'", (now - 1,))
        self.futures.settle_positions(now=now)               # maturity pays the collateral out
        return wallet

    def _pending(self):
        with self.db.readonly() as conn:
            return PayoutManager.open_for(conn, self.USER)

    def test_a_payout_without_an_account_waits_for_the_receivers_choice_and_notifies_them(self):
        wallet = self._close_position_without_an_nwx_account()
        offers = self._pending()
        self.assertEqual(len(offers), 1)
        self.assertEqual((offers[0]["status"], offers[0]["currency"]), ("pending", "NWX"))
        self.assertEqual(self.db.users.get_user(self.USER)["cash"], wallet)          # nothing was credited or exchanged
        with self.db.readonly() as conn:
            notes = NotificationManager.list(conn, self.USER)
            self.assertEqual(NotificationManager.unread_count(conn, self.USER), len(notes))
        choice = [n for n in notes if n["kind"] == "payout_choice"]
        self.assertEqual(len(choice), 1)
        self.assertEqual(choice[0]["action_path"], f"/payouts/{offers[0]['payout_id']}")
        check = self._futures_check()
        self.assertEqual(check["status"], "matched", check)

    def test_choosing_exchange_pays_in_the_receivers_own_currency_at_the_live_rate(self):
        wallet = self._close_position_without_an_nwx_account()
        payout = self._pending()[0]
        with self.db.atomic() as conn:
            done = PayoutManager.convert(conn, self.USER, payout["payout_id"], now=payout["created_epoch"] + 10)
        self.assertEqual(done["status"], "converted")
        self.assertGreater(self.db.users.get_user(self.USER)["cash"], wallet)         # USD arrived, at 1 NWX = 1/1500 USD
        self.assertEqual(self._pending(), [])
        check = self._futures_check()
        self.assertEqual(check["status"], "matched", check)
        with self.assertRaisesRegex(ValueError, "이미 처리"):
            with self.db.atomic() as conn:
                PayoutManager.convert(conn, self.USER, payout["payout_id"], now=payout["created_epoch"] + 11)

    def test_a_temporary_account_only_withdraws_into_the_receivers_own_account_less_the_fee(self):
        self._close_position_without_an_nwx_account()
        payout = self._pending()[0]
        opened = payout["created_epoch"] + 10
        with self.db.atomic() as conn:
            temp = PayoutManager.open_temp(conn, self.USER, payout["payout_id"], now=opened)
        self.assertEqual((temp["status"], temp["temp_number"]), ("temp_open", f"TMP{payout['payout_id']:08d}"))
        with self.db.readonly() as conn:
            note = next(n for n in NotificationManager.list(conn, self.USER) if n["kind"] == "payout_temp_account")
        fee_text = f"(수수료: {int(temp['withdraw_fee']):,} NWX)"
        self.assertIn("3%가 차감됩니다 " + fee_text, note["body"])           # the real amount, not an example
        self.assertEqual(temp["expires_epoch"], opened + HOLD_DAYS * DAY)
        with self.assertRaisesRegex(ValueError, "NWX 통장을 먼저"):                   # no NWX account yet
            with self.db.atomic() as conn:
                PayoutManager.withdraw(conn, self.USER, payout["payout_id"], now=opened + 100)
        self.db.products.execute(
            "INSERT OR IGNORE INTO saving(product_code,name,interest_rate,quote_currency,product_kind,status) "
            "VALUES ('FX_NWX','NWX 외환통장',0,'NWX','saving','active')")
        number = CurrencyExchangeManager(self.db).open_account(self.USER, "FX_NWX")
        amount = int(payout["amount"])
        with self.db.atomic() as conn:
            done = PayoutManager.withdraw(conn, self.USER, payout["payout_id"], now=opened + 200)
        fee = PayoutManager.fee_minor_units(amount)
        self.assertEqual((done["status"], self._nwx_units(number)), ("withdrawn", amount - fee))
        self.assertGreater(fee, 0)
        check = self._futures_check()
        self.assertEqual(check["status"], "matched", check)

    def test_unchosen_and_unwithdrawn_amounts_return_to_the_payer_after_a_month(self):
        self._close_position_without_an_nwx_account()
        payout = self._pending()[0]
        with self.db.atomic() as conn:
            self.assertEqual(PayoutManager.expire_due(conn, now=payout["created_epoch"] + HOLD_DAYS * DAY - 1), [])
            returned = PayoutManager.expire_due(conn, now=payout["created_epoch"] + HOLD_DAYS * DAY)
        self.assertEqual([item["status"] for item in returned], ["forfeited"])
        self.assertEqual(self._pending(), [])
        with self.db.readonly() as conn:
            kinds = [n["kind"] for n in NotificationManager.list(conn, self.USER)]
        self.assertIn("payout_expired", kinds)
        check = self._futures_check()
        self.assertEqual(check["status"], "matched", check)


class FillAndMarginNotificationTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown
    issue = fixtures.MarketFeatureTests.issue
    issue_future = fixtures.MarketFeatureTests.issue_future

    def _kinds(self, user):
        with self.db.readonly() as conn:
            return [n["kind"] for n in NotificationManager.list(conn, user)]

    def test_both_sides_of_a_fill_are_notified(self):
        self.db.assets.add_user_asset("alice", "CO", 5, unit_cost=100)
        self.assertTrue(self.book.place_limit_order("alice", "CO", "sell", 1, 100)[0])
        self.assertEqual(self._kinds("alice"), [])
        self.assertTrue(self.book.place_limit_order("bob", "CO", "buy", 1, 100)[0])
        self.assertEqual(self._kinds("alice"), ["order_fill"])
        self.assertEqual(self._kinds("bob"), ["order_fill"])

    def test_a_futures_position_close_to_liquidation_is_warned_once_a_day(self):
        self.issue_future()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 2, 100)[0])
        self.db.companies.update_share_price("CO", 95)         # collateral 30 against a maintenance margin of about 29
        self.futures.settle_positions()
        position = self.db.assets.execute_one("SELECT * FROM futures_positions WHERE user_id='alice'")
        self.assertIsNotNone(position, "the position must survive the fall to be warned about")
        self.futures.settle_positions()
        warnings = [kind for kind in self._kinds("alice") if kind == "futures_margin"]
        self.assertEqual(warnings, ["futures_margin"])


if __name__ == "__main__":
    unittest.main()
