import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.currency_db import CurrencyDB
from database.db_manager import DBManager
from database.user_db import TREASURY_USER_ID
from managers.currency_exchange_manager import CurrencyExchangeManager
from managers.currency_manager import CurrencyManager, SCALE
from managers.bank_manager import BankManager


class CurrencyAccountStorageTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('USD','달러','1500',2),('EUR','유로','1600',2)"
        )
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code) VALUES ('미국','USD')"
        )
        self.db.users.create_user('us-user','미국인','미국',1000)
        for code in ('USD','EUR','NWX'):
            self.db.products.execute(
                "INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind,status) "
                "VALUES (?,?,0,?,'currency_saving','active')",
                (f'FX_{code}',f'{code} 외환통장',code),
            )

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def test_home_currency_uses_cash_and_foreign_currency_requires_account(self):
        manager = CurrencyExchangeManager(self.db)
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'us-user','USD'),1000*SCALE)
            with self.assertRaisesRegex(ValueError,'EUR 외환통장'):
                CurrencyManager.change(conn,'us-user','EUR',SCALE)
        with self.assertRaisesRegex(ValueError,'국적 통화'):
            manager.open_account('us-user','FX_USD')
        number = manager.open_account('us-user','FX_EUR')
        self.db.system.set_setting('currency_exchange_fee_percent','0')
        message = manager.exchange('us-user','USD','EUR','10')
        self.assertIn('10 USD',message)
        with self.db.atomic() as conn:
            CurrencyManager.change(conn,'us-user','EUR',2*SCALE)
            self.assertEqual(CurrencyManager.balance(conn,'us-user','USD'),990*SCALE)
        self.assertEqual(
            self.db.accounts.execute_one(
                'SELECT balance_units FROM accounts WHERE account_number=?',(number,)
            )[0], 11_370_000,
        )
        before = self.db.users.get_treasury_balance()
        manager.transfer('us-user',TREASURY_USER_ID,'10')
        self.assertEqual(self.db.users.get_user('us-user')['cash'],980)
        self.assertEqual(self.db.users.get_treasury_balance()-before,15_000)
        self.assertIsNone(self.db.currency.execute_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='currency_wallets'"
        ))

    def test_legacy_wallets_move_to_home_cash_and_foreign_accounts(self):
        self.db.currency.execute("CREATE TABLE currency_wallets(user_id TEXT,currency_code TEXT,balance_units INTEGER,PRIMARY KEY(user_id,currency_code))")
        self.db.currency.execute(
            "INSERT INTO currency_wallets VALUES ('us-user','USD',?),('us-user','EUR',?)",
            (200*SCALE,3*SCALE),
        )
        migrated = CurrencyDB()
        migrated.close()
        self.assertEqual(self.db.users.get_user('us-user')['cash'],200)
        balances = {
            row['quote_currency']:row['balance_units']
            for row in self.db.accounts.execute(
                "SELECT quote_currency,balance_units FROM accounts "
                "WHERE user_id='us-user' AND account_type='currency_saving'"
            )
        }
        self.assertEqual(balances,{'NWX':1000*SCALE,'EUR':3*SCALE})
        self.assertIsNone(self.db.currency.execute_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='currency_wallets'"
        ))

    def test_regular_bank_products_must_match_the_users_home_currency(self):
        for code,currency in (('SAVE_USD','USD'),('SAVE_EUR','EUR')):
            self.db.products.execute(
                "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,status,"
                "quote_currency,product_kind) VALUES (?,?,0,0,'active',?,'saving')",
                (code,code,currency),
            )
        bank = BankManager(self.db)
        ok,message = bank.open_account('us-user','SAVE_EUR','1234',0)
        self.assertFalse(ok,message)
        self.assertIn('국적 통화 USD',message)
        ok,message = bank.open_account('us-user','SAVE_USD','1234',10)
        self.assertTrue(ok,message)
        account = self.db.accounts.execute_one(
            "SELECT * FROM accounts WHERE user_id='us-user' AND product_code='SAVE_USD'"
        )
        self.assertEqual((account['quote_currency'],account['balance']),('USD',10))
        self.assertEqual(self.db.users.get_user('us-user')['cash'],990)

    def test_transfer_to_own_foreign_account_uses_destination_currency_amount(self):
        manager = CurrencyExchangeManager(self.db)
        number = manager.open_account('us-user', 'FX_EUR')
        self.db.system.set_setting('currency_exchange_fee_percent', '0')

        message = manager.transfer('us-user', number, '3')

        self.assertIn('환전 기준 3 EUR', message)
        self.assertIn('3.2 USD → 3 EUR', message)
        self.assertAlmostEqual(self.db.users.get_user('us-user')['cash'], 996.8)
        self.assertEqual(
            self.db.accounts.execute_one(
                'SELECT balance_units FROM accounts WHERE account_number=?', (number,)
            )[0],
            3 * SCALE,
        )


if __name__ == '__main__':
    unittest.main()
