import tempfile
import unittest
import sqlite3
from contextlib import closing
from pathlib import Path

from database import DatabaseManager
from database.currency_db import CurrencyDB
from database.db_manager import DBManager
from database.user_db import TREASURY_USER_ID
from managers.currency_exchange_manager import CurrencyExchangeManager
from managers.currency_manager import CurrencyManager, SCALE
from managers.bank_manager import BankManager
from managers.user_manager import UserManager
from managers.compliance_manager import ComplianceManager


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

    def test_currency_settlement_history_is_append_only(self):
        inserts = (
            ("currency_transactions", "user_id,source_currency,target_currency,"
             "source_units,target_units,fee_nwx,recorded_epoch",
             "'us-user','USD','EUR',100,90,1,1"),
            ("currency_transfer_transactions", "sender_id,recipient_id,"
             "source_currency,target_currency,source_units,target_units,fee_nwx,recorded_epoch",
             "'us-user','recipient','USD','USD',100,100,1,1"),
            ("basic_income_payments", "batch_id,user_id,source_currency,target_currency,"
             "source_units,target_units,source_rate,target_rate,recorded_epoch",
             "'batch','us-user','NWX','USD',100,90,'1','1',1"),
        )
        for table, columns, values in inserts:
            self.db.currency.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({values})"
            )
            with self.db.atomic(("forex",)) as conn:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    conn.execute(f"UPDATE {table} SET recorded_epoch=2")
            with self.db.atomic(("forex",)) as conn:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    conn.execute(f"DELETE FROM {table}")

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
        self.db.accounts.execute(
            "UPDATE accounts SET balance=10.125 WHERE account_number=?", (account['account_number'],)
        )
        self.db.accounts.execute(
            "UPDATE accounts SET rate_type='floating',contract_rate=4.5,next_repricing_epoch=2000,"
            "benchmark_key='funding',benchmark_tenor_days=30 WHERE account_number=?",
            (account['account_number'],),
        )
        self.db.accounts.execute(
            "INSERT INTO contract_rate_reset_history(contract_type,contract_id,old_rate,new_rate,"
            "scheduled_epoch,observation_epoch,applied_epoch,benchmark_key,benchmark_tenor_days) "
            "VALUES ('account',?,4,4.5,2000,1900,2001,'funding',30)",
            (account['account_number'],),
        )
        portfolio = UserManager(self.db).get_total_assets('us-user', settle=False)
        self.assertEqual(portfolio['account_total'], 10.13)
        self.assertEqual(portfolio['accounts'][0]['balance'], 10.13)
        self.assertEqual(portfolio['accounts'][0]['currency_code'], 'USD')
        self.assertEqual(portfolio['accounts'][0]['decimal_places'], 2)
        self.assertEqual(portfolio['accounts'][0]['contract_rate'], 4.5)
        self.assertEqual(portfolio['accounts'][0]['next_repricing_epoch'], 2000)
        self.assertEqual(portfolio['accounts'][0]['benchmark_key'], 'funding')
        self.assertEqual(portfolio['accounts'][0]['rate_resets'][0]['new_rate'], 4.5)

    def test_pending_kyc_blocks_regular_and_foreign_account_opening(self):
        self.db.users.create_user('admin','관리자','미국',1000,'admin')
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,status,"
            "quote_currency,product_kind) VALUES ('SAVE_USD_KYC','KYC 예금',0,0,'active','USD','saving')"
        )
        with self.db.atomic() as conn:
            ComplianceManager.set_kyc_status(
                conn,'us-user','admin','pending',reason_code='review_started'
            )
        ok,message = BankManager(self.db).open_account(
            'us-user','SAVE_USD_KYC','1234',10
        )
        self.assertFalse(ok,message)
        self.assertIn('KYC 확인',message)
        with self.assertRaisesRegex(ValueError,'KYC 확인'):
            CurrencyExchangeManager(self.db).open_account(
                'us-user','FX_EUR','1234','3.25'
            )
        denials = self.db.system.execute(
            "SELECT event_type FROM kyc_profile_events WHERE user_id='us-user' "
            "AND event_type='deposit_open_denied' ORDER BY event_id"
        )
        self.assertEqual([row['event_type'] for row in denials],
                         ['deposit_open_denied','deposit_open_denied'])

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

    def test_foreign_account_opening_posts_both_currency_legs_to_bank_journal(self):
        from managers.bank_journal import BankJournal

        self.db.products.execute(
            "UPDATE saving SET issuer='FXBank' WHERE product_code='FX_EUR'"
        )
        number = CurrencyExchangeManager(self.db).open_account(
            'us-user', 'FX_EUR', password='1234', deposit_amount='3.25'
        )
        issuer = self.db.products.execute_one(
            "SELECT issuer FROM saving WHERE product_code='FX_EUR'"
        )[0]

        with self.db.atomic() as conn:
            report = BankJournal.reconcile_customer_deposits(conn, issuer)
            trial = BankJournal.trial_balance(conn, issuer)
            account = conn.execute(
                "SELECT balance_units,quote_currency FROM accounts WHERE account_number=?",
                (number,),
            ).fetchone()

        self.assertEqual((account['balance_units'], account['quote_currency']),
                         (325 * SCALE // 100, 'EUR'))
        self.assertEqual({row['currency_code']: row['status'] for row in report}['EUR'],
                         'matched')
        totals = {}
        for row in trial:
            totals[row['currency_code']] = totals.get(row['currency_code'], 0) + row['balance_units']
        self.assertIn('USD', totals)
        self.assertIn('EUR', totals)
        self.assertEqual(totals['USD'], 0)
        self.assertEqual(totals['EUR'], 0)

    def test_failed_bank_resolves_insured_fx_deposit_in_customer_home_currency(self):
        from managers.bank_journal import BankJournal

        self.db.products.execute(
            "UPDATE saving SET issuer='FXBank',deposit_insured=1 WHERE product_code='FX_EUR'"
        )
        self.db.currency.execute(
            "UPDATE financial_institutions SET nationality='미국',status='active',"
            "deposit_insurance_limit=2 WHERE issuer='FXBank'"
        )
        number = CurrencyExchangeManager(self.db).open_account(
            'us-user', 'FX_EUR', password='1234', deposit_amount='3.25'
        )
        cash_before = self.db.users.get_user('us-user')['cash']
        self.db.currency.execute(
            "UPDATE financial_institutions SET status='failed' WHERE issuer='FXBank'"
        )
        with self.db.atomic() as conn:
            result = BankManager.resolve_failed_institution(conn, 'FXBank', now=1_000)
            reconciliation = BankJournal.reconcile_customer_deposits(conn, 'FXBank')
            insurance_reconciliation = BankJournal.reconcile_deposit_insurance_claims(
                conn, 'FXBank'
            )
        account = self.db.accounts.execute_one(
            "SELECT balance_units,resolution_status,insurance_paid,uninsured_claim "
            "FROM accounts WHERE account_number=?", (number,),
        )
        self.assertEqual(result['accounts'], 1)
        self.assertEqual(account['balance_units'], 0)
        self.assertEqual(account['resolution_status'], 'resolved')
        self.assertGreater(account['insurance_paid'], 0)
        self.assertGreater(account['uninsured_claim'], 0)
        self.assertEqual(account['insurance_paid'], 1.87)
        self.assertEqual(account['uninsured_claim'], 1.38)
        self.assertAlmostEqual(result['covered'], 1.99)
        self.assertAlmostEqual(result['uninsured'], 1.47)
        self.assertGreater(self.db.users.get_user('us-user')['cash'], cash_before)
        self.assertEqual(reconciliation[0]['status'], 'matched')
        insurance_rows = {row['account_code']: row for row in insurance_reconciliation}
        self.assertEqual(insurance_rows['deposit_insurance_payable']['status'], 'matched')
        self.assertEqual(insurance_rows['deposit_insurance_payable']['claim_units'], 187)
        self.assertEqual(insurance_rows['deposit_insurance_payable']['journal_units'], 187)
        self.assertEqual(insurance_rows['uninsured_deposit_claim']['status'], 'matched')
        self.assertEqual(insurance_rows['uninsured_deposit_claim']['claim_units'], 138)
        self.assertEqual(insurance_rows['uninsured_deposit_claim']['journal_units'], 138)
        fx_record = self.db.currency.execute_one(
            "SELECT source_currency,target_currency FROM currency_transactions "
            "WHERE user_id='us-user' ORDER BY transaction_id DESC LIMIT 1"
        )
        self.assertEqual(tuple(fx_record), ('EUR', 'USD'))
        claims = UserManager(self.db).get_total_assets('us-user', settle=False)[
            'deposit_insurance_claims'
        ]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]['currency_code'], 'EUR')
        self.assertEqual(claims[0]['decimal_places'], 2)
        self.assertEqual(claims[0]['covered_amount'], 1.87)
        self.assertEqual(claims[0]['uninsured_amount'], 1.38)
        self.assertEqual(claims[0]['payout_currency'], 'USD')
        self.assertAlmostEqual(claims[0]['payout_amount'], 1.99)
        self.assertEqual(
            UserManager(self.db).get_total_assets('admin', settle=False)[
                'deposit_insurance_claims'
            ],
            [],
        )

    def test_insurance_claim_without_liability_journal_is_reported(self):
        from managers.bank_journal import BankJournal

        self.db.products.execute(
            "UPDATE saving SET issuer='FXBank' WHERE product_code='FX_EUR'"
        )
        number = CurrencyExchangeManager(self.db).open_account(
            'us-user', 'FX_EUR', password='1234', deposit_amount='1.25'
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "currency precision"):
            self.db.accounts.execute(
                "INSERT INTO deposit_insurance_claims "
                "(issuer,user_id,account_number,covered_amount,uninsured_amount,"
                "payout_currency,payout_amount,resolved_epoch) "
                "VALUES ('FXBank','us-user',?,1.25,0,'EUR',1.234,1)", (number,),
            )
        for payout_currency, payout_amount in ((None, 1.25), ('USD', None),
                                                ('UNKNOWN', 1.25)):
            with self.subTest(payout_currency=payout_currency, payout_amount=payout_amount):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "payout currency and amount"):
                    self.db.accounts.execute(
                        "INSERT INTO deposit_insurance_claims "
                        "(issuer,user_id,account_number,covered_amount,uninsured_amount,"
                        "payout_currency,payout_amount,resolved_epoch) VALUES (?,?,?,?,?,?,?,?)",
                        ('FXBank', 'us-user', number, 1.25, 0,
                         payout_currency, payout_amount, 1),
                    )
        self.db.accounts.execute(
            "INSERT INTO deposit_insurance_claims "
            "(issuer,user_id,account_number,covered_amount,uninsured_amount,resolved_epoch) "
            "VALUES ('FXBank','us-user',?,1.25,0,1)", (number,),
        )
        with self.db.atomic() as conn:
            reconciliation = BankJournal.reconcile_deposit_insurance_claims(conn, 'FXBank')
        insured = next(row for row in reconciliation
                       if row['account_code'] == 'deposit_insurance_payable')
        self.assertEqual(insured['currency_code'], 'EUR')
        self.assertEqual(insured['claim_units'], 125)
        self.assertEqual(insured['journal_units'], 0)
        self.assertEqual(insured['status'], 'missing_journal')
        with self.db.atomic() as conn:
            BankJournal.post(
                conn, 'FXBank', 'test', 'partial-insurance-claim', 'EUR',
                [('insurance_fund_receivable', 'debit', .50),
                 ('deposit_insurance_payable', 'credit', .50)],
            )
            reconciliation = BankJournal.reconcile_deposit_insurance_claims(
                conn, 'FXBank'
            )
        insured = next(row for row in reconciliation
                       if row['account_code'] == 'deposit_insurance_payable')
        self.assertEqual(insured['journal_units'], 50)
        self.assertEqual(insured['difference_units'], 75)
        self.assertEqual(insured['status'], 'mismatch')


class LegacyCurrencySchemaMigrationTests(unittest.TestCase):
    def test_legacy_deposit_insurance_claims_gain_nullable_payout_fields(self):
        old_root = DBManager.database_dir
        temp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(temp.name)
        database_path = Path(temp.name) / 'inazina.db'
        try:
            with closing(sqlite3.connect(database_path)) as connection, connection:
                connection.execute("""CREATE TABLE deposit_insurance_claims (
                    claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issuer TEXT NOT NULL,user_id TEXT NOT NULL,account_number TEXT NOT NULL,
                    covered_amount INTEGER NOT NULL DEFAULT 0,
                    uninsured_amount INTEGER NOT NULL DEFAULT 0,
                    resolved_epoch REAL NOT NULL,UNIQUE(issuer,account_number))""")
                connection.execute(
                    "INSERT INTO deposit_insurance_claims "
                    "(issuer,user_id,account_number,covered_amount,uninsured_amount,resolved_epoch) "
                    "VALUES ('Bank','u','acct',12,3,1)"
                )
            db = DatabaseManager()
            try:
                columns = {row[1] for row in db.accounts.execute(
                    "PRAGMA table_info(deposit_insurance_claims)"
                )}
                self.assertTrue({'payout_currency', 'payout_amount'} <= columns)
                claim = db.accounts.execute_one(
                    "SELECT covered_amount,uninsured_amount,payout_currency,payout_amount "
                    "FROM deposit_insurance_claims WHERE issuer='Bank'"
                )
                self.assertEqual(tuple(claim), (12, 3, None, None))
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    db.accounts.execute(
                        "UPDATE deposit_insurance_claims SET payout_currency='EUR' "
                        "WHERE issuer='Bank'"
                    )
            finally:
                db.close()
        finally:
            DBManager.database_dir = old_root
            temp.cleanup()

    def test_source_tax_rule_fk_survives_legacy_negative_rate_table_rebuild(self):
        old_root = DBManager.database_dir
        temp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(temp.name)
        database_path = Path(temp.name) / 'inazina.db'
        try:
            with closing(sqlite3.connect(database_path)) as connection, connection:
                connection.executescript("""
                    CREATE TABLE currencies (
                        code TEXT PRIMARY KEY,name TEXT NOT NULL,asset_code TEXT UNIQUE,
                        status TEXT NOT NULL DEFAULT 'active',reference_rate TEXT,
                        rate_multiplier TEXT NOT NULL DEFAULT '1',
                        decimal_places INTEGER NOT NULL DEFAULT 6
                    );
                    INSERT INTO currencies(code,name,decimal_places) VALUES ('NWX','NWX',0);
                    CREATE TABLE country_currencies (
                        nationality TEXT PRIMARY KEY,
                        currency_code TEXT NOT NULL REFERENCES currencies(code),
                        base_rate REAL NOT NULL DEFAULT 3 CHECK(base_rate BETWEEN 0 AND 100),
                        legal_max_rate REAL NOT NULL DEFAULT 20 CHECK(legal_max_rate BETWEEN 0 AND 100),
                        tax_percent REAL NOT NULL DEFAULT 3 CHECK(tax_percent BETWEEN 0 AND 100)
                    );
                    INSERT INTO country_currencies VALUES ('낙원','NWX',3,20,3);
                """)

            db = DatabaseManager()
            try:
                definition = db.currency.execute_one(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='country_tax_rules'"
                )[0]
                foreign_keys = db.currency.execute("PRAGMA foreign_key_list('country_tax_rules')")
                self.assertIn('country_currencies', definition)
                self.assertEqual(foreign_keys[0]['table'], 'country_currencies')
                db.currency.execute(
                    "INSERT INTO country_tax_rules(nationality,income_source,tax_percent,updated_by,updated_epoch) "
                    "VALUES ('낙원','lottery_winnings',12,'admin',1)"
                )
                db.currency.execute("""CREATE TRIGGER keep_rate_history_schema_marker
                    BEFORE UPDATE ON country_rate_history
                    BEGIN SELECT RAISE(ABORT,'schema marker must survive'); END""")
                reopened_currency = CurrencyDB()
                try:
                    self.assertIsNotNone(db.currency.execute_one(
                        "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                        "AND name='keep_rate_history_schema_marker'"
                    ))
                finally:
                    reopened_currency.close()
            finally:
                db.close()
        finally:
            DBManager.database_dir = old_root
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
