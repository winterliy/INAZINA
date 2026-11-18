"""Income taxed without a named currency is the citizen's own money, recorded as such."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.accounting_journal import AccountingJournal
from managers.user_manager import UserManager


class TaxIncomeCurrencyTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('SCD','남방','1/120',2)")
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code,tax_percent) VALUES ('United Southerns','SCD',10)")
        self.db.users.create_user('scd', '남방인', 'United Southerns', 1000)
        self.db.users.create_user('nwx', '시민', '낙원', 1000)
        self.users = UserManager(self.db)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def _tax_row(self, user):
        row = self.db.tax.execute_one(
            "SELECT gross_amount,tax_amount,net_amount,currency_code,tax_amount_minor_units "
            "FROM tax_transactions WHERE user_id=?", (user,))
        return (Decimal(str(row[0])), Decimal(str(row[1])), Decimal(str(row[2])), row[3], int(row[4]))

    def test_unnamed_income_is_recorded_in_the_citizens_currency_and_scale(self):
        with self.db.atomic() as conn:
            tax = UserManager._tax(conn, 'scd', 'test', Decimal('10.55'))
        # 10% of 10.55 SCD keeps its cents; it is not rounded as a whole-unit base amount.
        self.assertEqual(Decimal(str(tax)), Decimal('1.06'))
        self.assertEqual(self._tax_row('scd'), (Decimal('10.55'), Decimal('1.06'), Decimal('9.49'), 'SCD', 106))
        treasury = self.db.currency.execute_one(
            "SELECT u.cash_minor FROM country_treasuries t JOIN users.users u ON u.user_id=t.user_id "
            "WHERE t.nationality='United Southerns'")
        self.assertEqual(int(treasury[0]), 106)
        with self.db.readonly() as conn:
            check = next(c for c in AccountingJournal.reconcile_subledgers(conn)
                         if c['ledger_scope'] == 'tax_transactions')
        self.assertEqual(check['status'], 'matched', check)

    def test_base_currency_citizen_is_unchanged(self):
        with self.db.atomic() as conn:
            tax = UserManager._tax(conn, 'nwx', 'test', 100)
        self.assertEqual(self._tax_row('nwx')[3], 'NWX')
        self.assertEqual(self._tax_row('nwx')[0] - self._tax_row('nwx')[2], Decimal(str(tax)))

    def test_gambling_winnings_tax_carries_the_players_currency(self):
        self.db.system.set_setting("gamble_chance", "100")
        ok, message = self.users.gamble('scd', 100)
        self.assertTrue(ok, message)
        self.assertEqual(self._tax_row('scd')[3], 'SCD')


if __name__ == '__main__':
    unittest.main()
