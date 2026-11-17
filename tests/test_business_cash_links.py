"""Business records are tied to the exact wallet rows they moved (M4 cash attribution)."""
import tempfile
import unittest
from pathlib import Path

import test_liquidation_currency as liquidation
from database import DatabaseManager, pg_backend
from database.db_manager import DBManager
from managers.accounting_journal import AccountingJournal


def _check(conn):
    return next(item for item in AccountingJournal.reconcile_subledgers(conn)
                if item["ledger_scope"] == "business_cash")


class CashLinkPrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.users.create_user("a", "A", "낙원", 1000)
        self.db.users.create_user("b", "B", "낙원", 1000)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def test_link_takes_exactly_the_wallet_rows_made_since_the_mark(self):
        with self.db.atomic() as conn:
            conn.execute("UPDATE users SET cash_minor=cash_minor+1 WHERE user_id='a'")  # before the mark
            mark = AccountingJournal.cash_mark(conn)
            conn.execute("UPDATE users SET cash_minor=cash_minor-30 WHERE user_id='a'")
            conn.execute("UPDATE users SET cash_minor=cash_minor+30 WHERE user_id='b'")
            linked = AccountingJournal.link_cash(conn, "test_kind", "ref-1", mark, 10)
            self.assertEqual(linked, 2)
            rows = [tuple(r) for r in conn.execute(
                "SELECT w.user_id,w.delta_minor_units FROM business_cash_links l "
                "JOIN customer_wallet_ledger w ON w.entry_id=l.wallet_entry_id "
                "JOIN business_cash_events e ON e.event_id=l.event_id "
                "WHERE e.kind='test_kind' AND e.reference='ref-1' ORDER BY w.entry_id")]
            self.assertEqual(rows, [("a", -30), ("b", 30)])
            # The same record cannot be linked twice, and a wallet row belongs to one event only.
            with self.assertRaises(Exception):
                AccountingJournal.link_cash(conn, "test_kind", "ref-1", mark, 11)

    def test_a_later_event_does_not_take_rows_already_linked(self):
        with self.db.atomic() as conn:
            mark = AccountingJournal.cash_mark(conn)
            conn.execute("UPDATE users SET cash_minor=cash_minor+5 WHERE user_id='a'")
            self.assertEqual(AccountingJournal.link_cash(conn, "test_kind", "one", mark, 1), 1)
            conn.execute("UPDATE users SET cash_minor=cash_minor+7 WHERE user_id='a'")
            self.assertEqual(AccountingJournal.link_cash(conn, "test_kind", "two", mark, 2), 1)
            self.assertEqual(_check(conn)["status"], "matched", _check(conn))

    @unittest.skipIf(pg_backend.enabled(), "drops SQLite triggers to tamper")
    def test_links_and_events_are_append_only(self):
        with self.db.atomic() as conn:
            mark = AccountingJournal.cash_mark(conn)
            conn.execute("UPDATE users SET cash_minor=cash_minor+5 WHERE user_id='a'")
            AccountingJournal.link_cash(conn, "test_kind", "one", mark, 1)
        for statement in ("DELETE FROM business_cash_links", "UPDATE business_cash_events SET kind='x'",
                          "DELETE FROM business_cash_events"):
            with self.subTest(statement=statement), self.assertRaisesRegex(Exception, "append-only"):
                self.db.currency.execute(statement)


class LiquidationCashLinkTests(liquidation.LiquidationCurrencyTests):
    """Reuses the liquidation fixture; its inherited tests run again against the linked books."""

    def test_liquidation_payment_is_linked_to_the_recipients_wallet_row(self):
        self._foreign_holder()
        self._liquidate_with_preferred("scd")
        with self.db.readonly() as conn:
            check = _check(conn)
            payment = conn.execute(
                "SELECT rowid,paid_amount,tax_amount FROM corporate_liquidation_payments WHERE user_id='scd'").fetchone()
            linked = [tuple(r) for r in conn.execute(
                "SELECT w.user_id,w.currency_code,w.delta_minor_units FROM business_cash_links l "
                "JOIN customer_wallet_ledger w ON w.entry_id=l.wallet_entry_id "
                "JOIN business_cash_events e ON e.event_id=l.event_id "
                "WHERE e.kind='liquidation' AND e.reference=? AND w.user_id='scd'", (str(payment[0]),))]
            fx = conn.execute("SELECT minor_units FROM cash_fx_settlements WHERE kind='liquidation'").fetchone()
        self.assertEqual(check["status"], "matched", check)
        self.assertEqual(linked, [("scd", "SCD", int(fx[0]))])

    @unittest.skipIf(pg_backend.enabled(), "drops SQLite triggers to tamper")
    def test_reconciliation_reports_missing_events_and_wrong_amounts(self):
        self._foreign_holder()
        self._liquidate_with_preferred("scd")
        with self.assertRaises(_Undo), self.db.atomic() as conn:
            conn.execute("""INSERT INTO corporate_liquidation_payments
                (operation_key,issuer_code,asset_code,user_id,kind,quantity,claim_amount,paid_amount,tax_amount,recorded_epoch)
                VALUES ('late','ST','PR','scd','preferred',1,5,5,0,9)""")
            conn.execute("DROP TRIGGER trg_business_cash_links_delete")
            conn.execute("DELETE FROM business_cash_links WHERE wallet_entry_id IN ("
                         "SELECT entry_id FROM customer_wallet_ledger WHERE user_id='scd')")
            check = _check(conn)
            self.statuses = {gap["status"] for gap in check["gaps"]}
            raise _Undo()
        self.assertEqual(self.statuses, {"missing_cash_event", "cash_amount_mismatch"})


class _Undo(Exception):
    pass


if __name__ == "__main__":
    unittest.main()
