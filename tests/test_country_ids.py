"""Countries have numeric ids: the treasury id, given by $국가생성 and accepted wherever a country is."""
import unittest

import test_inazina as fixtures
from managers.admin_commands_manager import AdminCommandsManager
from managers.currency_manager import CurrencyManager


class CountryIdTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown
    seed = fixtures.InazinaTests.seed

    def run_admin(self, command, args):
        return AdminCommandsManager(self.bot).execute('admin', command, args)

    def test_country_is_created_with_its_id_and_found_by_it(self):
        self.seed()
        self.assertIn('ID 5002', self.run_admin('국가생성', '5002 달러국 USD'))
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.treasury_id(conn, '달러국'), '5002')
            self.assertEqual(CurrencyManager.nationality_of(conn, '5002'), '달러국')
        self.run_admin('세율', '5002 7')
        rate = self.db.currency.execute_one("SELECT tax_percent FROM country_currencies WHERE nationality='달러국'")[0]
        self.assertEqual(float(rate), 7.0)

    def test_country_id_must_be_a_free_number(self):
        self.seed()
        with self.assertRaisesRegex(ValueError, '숫자'):
            self.run_admin('국가생성', 'abc 달러국 USD')
        with self.db.atomic() as conn:
            taken = CurrencyManager.treasury_id(conn, '낙원')
        with self.assertRaisesRegex(ValueError, '이미 사용 중'):
            self.run_admin('국가생성', f'{taken} 달러국 USD')

    def test_treasuries_made_on_demand_get_numeric_ids(self):
        self.seed()
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('엔국','USD')")
            self.assertTrue(CurrencyManager.treasury_id(conn, '엔국').isdecimal())
