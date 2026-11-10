from decimal import Decimal
import tempfile
from decimal import Decimal
import unittest
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from database import DatabaseManager
from database.currency_db import CurrencyDB
from database.db_manager import DBManager
from database.user_db import TREASURY_USER_ID
from managers.currency_exchange_manager import CurrencyExchangeManager
from managers.currency_manager import CurrencyManager, SCALE
from managers.bank_manager import BankManager
from managers.user_manager import UserManager
from managers.compliance_manager import ComplianceManager
from managers.accounting_journal import AccountingJournal
from managers.treasury_bond_manager import TreasuryBondManager
from managers.order_book_manager import OrderBookManager
from functions.money import audit_amount_field_coverage, audit_amount_snapshots


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
                "VALUES (?,?,0,?,'saving','active')",
                (f'FX_{code}',f'{code} 외환통장',code),
            )

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def test_bank_journal_replay_rejects_a_different_amount_or_currency(self):
        from managers.bank_journal import BankJournal
        with self.db.atomic() as conn:
            lines = [('tax_payable', 'debit', 1), ('settlement_asset', 'credit', 1)]
            self.assertTrue(BankJournal.post(conn, 'Bank', 'test', 'replay', 'USD', lines, now=10))
            self.assertFalse(BankJournal.post(conn, 'Bank', 'test', 'replay', 'USD', lines, now=11))
            for currency, amount in (('USD', 2), ('EUR', 1)):
                with self.subTest(currency=currency, amount=amount), self.assertRaisesRegex(ValueError, '멱등성'):
                    BankJournal.post(conn, 'Bank', 'test', 'replay', currency,
                        [('tax_payable', 'debit', amount), ('settlement_asset', 'credit', amount)], now=12)

    def test_institution_reconciliation_rejects_wrong_common_entity_type(self):
        from managers.bank_journal import BankJournal
        with self.db.atomic() as conn:
            BankJournal.post(conn, 'Bank', 'test', 'wrong-entity', 'USD',
                [('tax_payable', 'debit', 1), ('settlement_asset', 'credit', 1)], now=10)
            conn.execute('DROP TRIGGER trg_accounting_entries_update_immutable')
            conn.execute("UPDATE accounting_entries SET entity_type='customer' WHERE ledger_scope='institution' AND source_key='test:wrong-entity'")
            check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                         if item['ledger_scope'] == 'institution')
            self.assertEqual(check['status'], 'mismatch', check)

    def test_bank_journal_common_post_failure_rolls_back_bank_entry(self):
        from managers.bank_journal import BankJournal
        with self.db.atomic() as conn:
            with patch.object(AccountingJournal, 'post', side_effect=RuntimeError('injected failure')):
                with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                    BankJournal.post(conn, 'Bank', 'test', 'failed-common-post', 'USD',
                        [('tax_payable', 'debit', 1), ('settlement_asset', 'credit', 1)], now=10)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_journal_entries WHERE reference_key='failed-common-post'").fetchone()[0], 0)

    def test_bank_journal_batches_mapping_schema_initialization(self):
        from managers.bank_journal import BankJournal
        with self.db.atomic() as conn:
            with patch.object(AccountingJournal, 'ensure_schema', wraps=AccountingJournal.ensure_schema) as ensure:
                BankJournal.post(conn, 'Bank', 'test', 'batch-mappings', 'USD',
                    [('tax_payable', 'debit', 1), ('settlement_asset', 'credit', 1)], now=10)
            self.assertLessEqual(ensure.call_count, 2)

    def test_cross_currency_tax_remittance_failure_rolls_back_both_legs(self):
        from managers.bank_journal import BankJournal
        with self.db.atomic() as conn:
            original = BankJournal.post
            calls = []
            def fail_second(*args, **kwargs):
                calls.append(args)
                if len(calls) == 2:
                    raise RuntimeError('injected settlement failure')
                return original(*args, **kwargs)
            with patch.object(BankJournal, 'post', side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, 'injected settlement failure'):
                    BankJournal.post_tax_remittance(conn, 'Bank', 'partial-tax', 'USD', 1, 'EUR', 1, now=10)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_journal_entries WHERE reference_key LIKE 'partial-tax:%'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounting_entries WHERE source_key LIKE 'tax_remittance:partial-tax:%'").fetchone()[0], 0)

    def test_tax_remittance_link_failure_rolls_back_partial_links(self):
        from fractions import Fraction
        from managers.bank_journal import BankJournal
        with self.db.atomic() as conn:
            BankJournal.post_tax_remittance(conn, 'Bank', 'link-failure', 'USD', 1, 'USD', 1, now=10)
            transaction_id = conn.execute("""INSERT INTO tax_transactions
                (user_id,source,gross_amount,tax_amount,net_amount,currency_code)
                VALUES ('us-user','fixture',2,1,1,'USD')""").lastrowid
            conn.execute("CREATE TEMP TRIGGER fail_tax_quote BEFORE INSERT ON tax_bank_remittance_quotes "
                         "BEGIN SELECT RAISE(ABORT,'injected quote failure'); END")
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected quote failure'):
                AccountingJournal.link_tax_bank_remittance(conn, transaction_id, 'Bank',
                    'link-failure', 'USD', 'USD', Fraction(1, 1))
            conn.execute('DROP TRIGGER fail_tax_quote')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tax_bank_remittance_links WHERE transaction_id=?',
                                          (transaction_id,)).fetchone()[0], 0)

    def test_tax_remittance_activation_cannot_be_replaced(self):
        with self.db.atomic() as conn:
            for table in ('tax_bank_remittance_activation', 'tax_bank_remittance_quote_activation'):
                with self.subTest(table=table), self.assertRaisesRegex(sqlite3.IntegrityError, 'activation is immutable'):
                    conn.execute(f'INSERT OR REPLACE INTO {table}(singleton_id,through_transaction_id) VALUES (1,999)')
        CurrencyDB().close()

    def test_financial_reconciliation_reports_dangling_declared_references(self):
        # A legacy or externally edited database can contain violations even
        # though application connections enable foreign keys today.
        with closing(sqlite3.connect(self.db.currency.db_path)) as raw:
            with raw:
                raw.execute('PRAGMA foreign_keys=OFF')
                raw.execute("INSERT INTO tax_bank_remittance_links(transaction_id,source_entry_id) VALUES (999999,999999)")
        with self.db.readonly() as conn:
            result = AccountingJournal.reconcile_all_financial_ledgers(conn)
        self.assertEqual(result['status'], 'mismatch', result)
        self.assertTrue(any(finding['scope'] == 'foreign_key_integrity'
                            and finding['table'] == 'tax_bank_remittance_links'
                            for finding in result['findings']), result)

    def test_financial_reconciliation_reports_orphan_loan_schedule(self):
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO loan_payment_schedule(loan_id,installment_number,due_epoch) VALUES (999999,1,1)")
            result = AccountingJournal.reconcile_all_financial_ledgers(conn)
        self.assertEqual(result['status'], 'mismatch', result)
        self.assertTrue(any(finding['scope'] == 'loan_schedule_references' and finding['loan_id'] == 999999
                            for finding in result['findings']), result)

    def test_financial_reconciliation_reports_orphan_loan_chargeoff(self):
        with self.db.atomic() as conn:
            conn.execute("""INSERT INTO loan_chargeoff_events
                (loan_id,issuer,event_key,event_type,amount_minor_units,decimal_places,currency_code,actor_id,created_epoch)
                VALUES (999999,'Bank','orphan-chargeoff','charge_off',1,2,'USD','admin',1)""")
            result = AccountingJournal.reconcile_all_financial_ledgers(conn)
        self.assertEqual(result['status'], 'mismatch', result)
        self.assertTrue(any(finding['scope'] == 'loan_related_references'
                            and finding['table'] == 'loan_chargeoff_events' and finding['loan_id'] == 999999
                            for finding in result['findings']), result)

    def test_financial_reconciliation_reports_orphan_ecl_and_commitment(self):
        with self.db.atomic() as conn:
            conn.execute("""INSERT INTO loan_ecl_assessments
                (loan_id,as_of_day,stage,pd_12m,lifetime_pd,lgd,ead,expected_loss,quote_currency,calculated_epoch)
                VALUES (999998,'2026-01-01',1,0,0,0,0,0,'USD',1)""")
            conn.execute("""INSERT INTO loan_future_commitments
                (loan_id,committed_limit_minor_units,credit_conversion_factor_bps,active,updated_epoch,updated_by)
                VALUES (999997,1,100,1,1,'admin')""")
            result = AccountingJournal.reconcile_all_financial_ledgers(conn)
        self.assertEqual(result['status'], 'mismatch', result)
        tables = {finding['table'] for finding in result['findings'] if finding['scope'] == 'loan_related_references'}
        self.assertTrue({'loan_ecl_assessments', 'loan_future_commitments'} <= tables, result)

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
        canonical = self.db.accounts.execute_one(
            'SELECT balance_minor_units,balance_decimal_places FROM accounts WHERE account_number=?',(number,)
        )
        self.assertEqual(tuple(canonical), (1137, 2))
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

    def test_account_balance_storage_enforces_currency_minor_units(self):
        number = CurrencyExchangeManager(self.db).open_account('us-user','FX_EUR')
        with self.assertRaisesRegex(sqlite3.IntegrityError,'currency minor units'):
            self.db.accounts.execute(
                "UPDATE accounts SET balance_units=1 WHERE account_number=?", (number,)
            )
        self.db.accounts.execute(
            "UPDATE accounts SET balance_units=1230000 WHERE account_number=?", (number,)
        )
        row = self.db.accounts.execute_one(
            "SELECT balance_units,balance_minor_units,balance_decimal_places FROM accounts WHERE account_number=?",
            (number,),
        )
        self.assertEqual(tuple(row),(1230000,123,2))

    def test_market_order_reservation_migration_backfills_and_is_idempotent(self):
        self.db.assets.execute(
            "INSERT INTO market_assets(code,name,exchange_id,price,type,quote_currency) "
            "VALUES ('LEGACY-USD','Legacy USD','FX',1,'stock','USD')"
        )
        for suffix in ("legacy", "legacy_update", "minor"):
            self.db.currency.execute(
                f"DROP TRIGGER IF EXISTS trg_market_order_reserved_{suffix}"
            )
        self.db.assets.execute(
            "INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,"
            "remaining_quantity,reserved_cash,quote_currency,status,created_epoch,updated_epoch) "
            "VALUES ('us-user','LEGACY-USD','buy',12,1,1,12.34,'USD','open',1,1)"
        )
        order_id = self.db.assets.execute_one(
            "SELECT order_id FROM market_orders WHERE asset_code='LEGACY-USD'"
        )[0]

        self.db.currency._ensure_market_order_reservation_units()
        self.assertEqual(self.db.assets.execute_one(
            "SELECT reserved_cash_minor_units FROM market_orders WHERE order_id=?",
            (order_id,),
        )[0], 1234)
        self.db.currency._ensure_market_order_reservation_units()
        self.assertEqual(tuple(Decimal(str(value)) if isinstance(value,(int,float,Decimal)) else value for value in self.db.assets.execute_one(
            "SELECT reserved_cash,reserved_cash_minor_units FROM market_orders WHERE order_id=?",
            (order_id,),
        )[:]), (Decimal('12.34'), 1234))
        self.db.assets.execute(
            "UPDATE market_orders SET reserved_cash_minor_units=1235 WHERE order_id=?",
            (order_id,),
        )
        self.assertEqual(Decimal(str(self.db.assets.execute_one(
            "SELECT reserved_cash FROM market_orders WHERE order_id=?", (order_id,),
        )[0])), Decimal('12.35'))

    def test_instrument_price_tables_use_quote_currency_minor_units(self):
        self.db.assets.execute(
            "INSERT INTO market_assets(code,name,exchange_id,price,type,quote_currency) "
            "VALUES ('USD-QUOTE','USD Quote','FX',1.23,'stock','USD')"
        )
        self.assertEqual(self.db.assets.execute_one(
            "SELECT price_minor_units FROM market_assets WHERE code='USD-QUOTE'"
        )[0], 123)
        self.db.assets.execute(
            "UPDATE market_assets SET price_minor_units=124 WHERE code='USD-QUOTE'"
        )
        self.assertEqual(tuple(Decimal(str(value)) if isinstance(value,(int,float,Decimal)) else value for value in self.db.assets.execute_one(
            "SELECT price,price_minor_units FROM market_assets WHERE code='USD-QUOTE'"
        )[:]), (Decimal('1.24'), 124))
        self.db.assets.execute(
            "UPDATE market_assets SET price=2.36 WHERE code='USD-QUOTE'"
        )
        self.assertEqual(self.db.assets.execute_one(
            "SELECT price_minor_units FROM market_assets WHERE code='USD-QUOTE'"
        )[0], 236)
        self.db.assets.execute(
            "INSERT INTO market_price_history(asset_code,price,recorded_epoch) "
            "VALUES ('USD-QUOTE',3.45,1)"
        )
        self.assertEqual(self.db.assets.execute_one(
            "SELECT price_minor_units FROM market_price_history WHERE asset_code='USD-QUOTE'"
        )[0], 345)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "instrument price minor units must be an integer"):
            self.db.assets.execute(
                "UPDATE market_assets SET price_minor_units=12.5 WHERE code='USD-QUOTE'"
            )
        self.db.companies.execute(
            "INSERT INTO company(company_code,name,industry,capital,total_shares,share_price,"
            "max_shareholder_id,exchange_id,quote_currency) "
            "VALUES ('USD-SHARE','USD share','test',500,5,1.23,'us-user','FX','USD')"
        )
        self.assertEqual(self.db.companies.execute_one(
            "SELECT share_price_minor_units FROM company WHERE company_code='USD-SHARE'"
        )[0], 123)
        self.db.products.execute(
            "INSERT INTO investment(product_code,name,product_type,total_supply,unit_price,quote_currency) "
            "VALUES ('USD-FUND','USD fund','mutual_fund',100,1.23,'USD')"
        )
        self.assertEqual(self.db.products.execute_one(
            "SELECT unit_price_minor_units FROM investment WHERE product_code='USD-FUND'"
        )[0], 123)

    def test_tax_transactions_use_balanced_currency_minor_units(self):
        self.db.tax.execute(
            "INSERT INTO tax_transactions(user_id,source,gross_amount,tax_amount,net_amount,"
            "currency_code) VALUES ('us-user','test',1.23,0.12,1.11,'USD')"
        )
        row = self.db.tax.execute_one(
            "SELECT gross_amount_minor_units,tax_amount_minor_units,net_amount_minor_units "
            "FROM tax_transactions WHERE user_id='us-user' AND source='test'"
        )
        self.assertEqual(tuple(row), (123, 12, 111))
        self.db.tax.execute(
            "UPDATE tax_transactions SET gross_amount_minor_units=124,"
            "tax_amount_minor_units=13,net_amount_minor_units=111 WHERE user_id='us-user' "
            "AND source='test'"
        )
        self.assertEqual(tuple(Decimal(str(value)) if isinstance(value,(int,float,Decimal)) else value for value in tuple(self.db.tax.execute_one(
            "SELECT gross_amount,tax_amount,net_amount FROM tax_transactions "
            "WHERE user_id='us-user' AND source='test'"
        ))), (Decimal('1.24'), Decimal('0.13'), Decimal('1.11')))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "tax transaction currency minor units"):
            self.db.tax.execute(
                "UPDATE tax_transactions SET net_amount=1.12 WHERE user_id='us-user' AND source='test'"
            )

    def test_market_order_limit_price_uses_quote_currency_minor_units(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('USD','Dollar',1300,2) ON CONFLICT(code) DO UPDATE SET decimal_places=2"
        )
        with self.db.atomic() as conn:
            order_id = conn.execute(
                "INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,"
                "remaining_quantity,status,created_epoch,updated_epoch,quote_currency) "
                "VALUES ('us-user','USD-PRICE','buy',1.23,2,2,'open',1,1,'USD')"
            ).lastrowid
            row = conn.execute("SELECT limit_price_minor_units,limit_price_decimal_places "
                               "FROM market_orders WHERE order_id=?", (order_id,)).fetchone()
            self.assertEqual(tuple(row), (123, 2))
            conn.execute("UPDATE market_orders SET limit_price_minor_units=124 WHERE order_id=?",
                         (order_id,))
            self.assertEqual(Decimal(str(conn.execute("SELECT limit_price FROM market_orders WHERE order_id=?",
                                          (order_id,)).fetchone()[0])), Decimal('1.24'))
            conn.execute("UPDATE market_orders SET limit_price=1.25 WHERE order_id=?", (order_id,))
            self.assertEqual(conn.execute("SELECT limit_price_minor_units FROM market_orders "
                                          "WHERE order_id=?", (order_id,)).fetchone()[0], 125)

    def test_market_order_limit_price_migration_backfills_and_is_idempotent(self):
        for suffix in ("legacy_insert", "legacy_update", "minor"):
            self.db.currency.execute(
                f"DROP TRIGGER IF EXISTS trg_market_order_limit_price_{suffix}"
            )
        self.db.assets.execute(
            "INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,"
            "remaining_quantity,quote_currency,status,created_epoch,updated_epoch) "
            "VALUES ('us-user','OLD-USD','buy',1.23,1,1,'USD','open',1,1)"
        )
        order_id = self.db.assets.execute_one(
            "SELECT order_id FROM market_orders WHERE asset_code='OLD-USD'"
        )[0]

        self.db.currency._ensure_market_order_price_units()
        self.db.currency._ensure_market_order_price_units()
        row = self.db.assets.execute_one(
            "SELECT limit_price,limit_price_minor_units,limit_price_decimal_places "
            "FROM market_orders WHERE order_id=?", (order_id,),
        )
        self.assertEqual(tuple(Decimal(str(value)) if isinstance(value,(int,float,Decimal)) else value for value in tuple(row)), (Decimal('1.23'), 123, 2))

    def test_market_order_reconciliation_flags_canonical_limit_price_corruption(self):
        self.db.assets.execute(
            "INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,"
            "remaining_quantity,reserved_cash,quote_currency,status,created_epoch,updated_epoch) "
            "VALUES ('us-user','BROKEN-USD','buy',1.23,2,2,2.46,'USD','open',1,1)"
        )
        order_id = self.db.assets.execute_one(
            "SELECT order_id FROM market_orders WHERE asset_code='BROKEN-USD'"
        )[0]
        self.assertEqual(self.db.assets.execute_one(
            "SELECT reserved_cash_minor_units FROM market_orders WHERE order_id=?",
            (order_id,),
        )[0], 246)
        self.db.currency.execute(
            "DROP TRIGGER IF EXISTS trg_market_order_limit_price_minor"
        )
        self.db.assets.execute(
            "UPDATE market_orders SET limit_price_minor_units=124 WHERE order_id=?",
            (order_id,),
        )

        with self.db.readonly() as conn:
            check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                         if item["ledger_scope"] == "market_order_reservations")
        self.assertEqual(check["status"], "mismatch")
        self.assertIn(124, [gap.get("actual_limit_price_minor_units")
                            for gap in check["reservation_gaps"]])

    def test_customer_wallet_changes_post_atomically_and_reconcile(self):
        with self.db.atomic() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM customer_wallet_ledger WHERE user_id='us-user'"
            ).fetchone()[0]
            conn.execute("UPDATE users SET cash_minor=cash_minor+100 WHERE user_id='us-user'")
            after = conn.execute(
                "SELECT COUNT(*) FROM customer_wallet_ledger WHERE user_id='us-user'"
            ).fetchone()[0]
            self.assertEqual(after,before+1)
            checks = AccountingJournal.reconcile_subledgers(conn)
            customer = next(item for item in checks if item['ledger_scope']=='customer')
            self.assertEqual(customer['status'],'matched',customer)
            self.assertEqual(customer['posting_gaps'],0)
            self.assertEqual(AccountingJournal.audit_unbalanced(conn,'customer'),[])

        number = CurrencyExchangeManager(self.db).open_account('us-user','FX_EUR')
        with self.db.atomic() as conn:
            conn.execute(
                "UPDATE accounts SET balance_minor_units=100 WHERE account_number=?",(number,)
            )
            checks = AccountingJournal.reconcile_subledgers(conn)
            self.assertEqual(next(item for item in checks if item['ledger_scope']=='customer')['status'],'matched')
            conn.execute("UPDATE accounting_account_mappings SET common_code='wrong_wallet_clearing' "
                         "WHERE ledger_scope='customer' AND local_code='wallet_clearing'")
            customer = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                            if item['ledger_scope']=='customer')
            self.assertEqual(customer['status'],'mismatch')
            self.assertGreater(customer['posting_gaps'],0)
            conn.execute("UPDATE accounting_account_mappings SET common_code='wallet_clearing' "
                         "WHERE ledger_scope='customer' AND local_code='wallet_clearing'")

        with self.assertRaisesRegex(RuntimeError,'rollback marker'):
            with self.db.atomic() as conn:
                conn.execute("UPDATE users SET cash_minor=cash_minor+100 WHERE user_id='us-user'")
                raise RuntimeError('rollback marker')
        with self.db.atomic() as conn:
            checks = AccountingJournal.reconcile_subledgers(conn)
            self.assertEqual(next(item for item in checks if item['ledger_scope']=='customer')['status'],'matched')

    def test_registered_currency_amount_snapshots_reconcile_to_source_rows(self):
        with self.db.readonly() as conn:
            findings = audit_amount_snapshots(conn)
            registered = conn.execute("""SELECT 1 FROM financial_amount_field_registry
                WHERE source_table='user_assets' AND amount_field='average_cost'""").fetchone()
            empty_field = conn.execute("""SELECT 1 FROM financial_amount_field_registry
                WHERE source_table='aml_alerts' AND amount_field='amount'""").fetchone()
            alert_count = conn.execute("SELECT COUNT(*) FROM aml_alerts").fetchone()[0]
        self.assertEqual(findings, [])
        self.assertIsNotNone(registered)
        self.assertEqual(alert_count, 0)
        self.assertIsNotNone(empty_field)
        row = self.db.currency.execute_one(
            "SELECT source_table,source_key,amount_field FROM financial_amount_snapshots LIMIT 1"
        )
        self.assertIsNotNone(row)
        self.db.currency.execute(
            "DELETE FROM financial_amount_snapshots WHERE source_table=? AND source_key=? AND amount_field=?",
            tuple(row),
        )
        with self.db.readonly() as conn:
            findings = audit_amount_snapshots(conn)
        self.assertTrue(any(item["status"] == "missing_snapshot" for item in findings), findings)

    def test_user_asset_average_cost_has_a_live_currency_minor_unit_value(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places,status) "
            "VALUES ('TST','Test Currency','1',2,'active')"
        )
        self.db.assets.execute(
            "INSERT INTO market_assets(code,name,price,type,quote_currency) "
            "VALUES ('COST-TST','Cost test',1,'stock','TST')"
        )
        self.db.assets.execute(
            "INSERT INTO user_assets(user_id,asset_code,quantity,average_cost) "
            "VALUES ('us-user','COST-TST',2,1.23)"
        )
        row = self.db.assets.execute_one(
            "SELECT average_cost,average_cost_minor_units FROM user_assets "
            "WHERE user_id='us-user' AND asset_code='COST-TST'"
        )
        self.assertEqual(tuple(Decimal(str(value)) if isinstance(value,(int,float,Decimal)) else value for value in (row[0], row[1])), (Decimal('1.23'), 123))
        self.db.assets.execute(
            "UPDATE user_assets SET average_cost_minor_units=456 "
            "WHERE user_id='us-user' AND asset_code='COST-TST'"
        )
        self.assertEqual(
            Decimal(str(self.db.assets.execute_one(
                "SELECT average_cost FROM user_assets "
                "WHERE user_id='us-user' AND asset_code='COST-TST'"
            )[0])),
            Decimal('4.56'),
        )

    def test_direct_security_buy_and_sell_preserve_fractional_home_currency_units(self):
        self.db.assets.execute(
            "INSERT INTO market_assets(code,name,price,type,quote_currency,total_supply) "
            "VALUES ('TRADE-USD','Fractional quote',1.23,'stock','USD',100)"
        )
        manager = UserManager(self.db)
        before = self.db.users.execute_one(
            "SELECT cash_minor FROM users WHERE user_id='us-user'"
        )[0]
        bought = manager.buy_asset("us-user", "TRADE-USD", 2)
        self.assertTrue(bought[0], bought[1])
        self.assertEqual(
            self.db.users.execute_one(
                "SELECT cash_minor FROM users WHERE user_id='us-user'"
            )[0],
            before-246,
        )
        holding = self.db.assets.execute_one(
            "SELECT average_cost_minor_units FROM user_assets "
            "WHERE user_id='us-user' AND asset_code='TRADE-USD'"
        )
        self.assertEqual(holding[0], 123)
        self.db.assets.execute(
            "UPDATE market_assets SET price=1.50 WHERE code='TRADE-USD'"
        )
        sold = manager.sell_asset("us-user", "TRADE-USD", 1)
        self.assertTrue(sold[0], sold[1])
        after = self.db.users.execute_one(
            "SELECT cash_minor FROM users WHERE user_id='us-user'"
        )[0]
        self.assertEqual(after, before-246+149)

    def test_late_initialized_financial_schemas_are_included_in_amount_registry(self):
        with self.db.readonly() as conn:
            registered = {
                (row["source_table"], row["amount_field"])
                for row in conn.execute("""SELECT source_table,amount_field
                    FROM financial_amount_field_registry
                    WHERE (source_table,amount_field) IN (
                      ('preferred_dividend_claims','gross_amount'),
                      ('els_products','face_value'),
                      ('lottery_purchases','price'))""")
            }
            findings = audit_amount_snapshots(conn)
        self.assertEqual(registered, {
            ("preferred_dividend_claims", "gross_amount"),
            ("els_products", "face_value"),
            ("lottery_purchases", "price"),
        })
        self.assertEqual(findings, [])

    def test_late_dividend_amount_field_uses_each_currency_scale(self):
        for distribution_id, asset_code, amount, currency in (
            (1, "PREF-USD", 12.34, "USD"),
            (2, "PREF-EUR", 56.78, "EUR"),
            (3, "PREF-NWX", 90, "NWX"),
        ):
            self.db.corporate.execute(
                "INSERT INTO preferred_dividend_claims "
                "(issuer_code,asset_code,distribution_id,user_id,quantity,gross_amount,"
                "quote_currency,funding_source,due_epoch,created_epoch) "
                "VALUES ('Issuer',?,?, 'us-user',1,?,?,'treasury',100,100)",
                (asset_code, distribution_id, amount, currency),
            )
        with self.db.readonly() as conn:
            snapshots = conn.execute("""SELECT s.amount_minor_units,s.decimal_places,s.currency_code
                FROM financial_amount_snapshots s
                JOIN preferred_dividend_claims p ON p.claim_id=CAST(s.source_key AS INTEGER)
                WHERE s.source_table='preferred_dividend_claims'
                  AND s.amount_field='gross_amount' ORDER BY p.distribution_id""").fetchall()
            findings = audit_amount_snapshots(conn)
        self.assertEqual([tuple(snapshot) for snapshot in snapshots], [
            (1234, 2, "USD"), (5678, 2, "EUR"), (90, 0, "NWX"),
        ])
        self.assertEqual(findings, [])

    def test_amount_audit_supports_rowid_keys_for_legacy_composite_tables(self):
        with self.db.atomic() as conn:
            conn.execute("CREATE TABLE rowid_money_fixture (amount REAL NOT NULL)")
            conn.execute("INSERT INTO rowid_money_fixture(amount) VALUES (1.23)")
            conn.execute("INSERT INTO financial_amount_field_registry "
                "(source_table,source_key_field,amount_field,currency_expression) "
                "VALUES ('rowid_money_fixture','rowid','amount','NWX')")
            findings = audit_amount_snapshots(conn)
        self.assertTrue(any(
            item["source_table"] == "rowid_money_fixture"
            and item["status"] == "missing_snapshot"
            for item in findings
        ), findings)

    def test_amount_field_coverage_flags_new_unregistered_money_columns(self):
        with self.db.readonly() as conn:
            before = audit_amount_field_coverage(conn)
        self.assertEqual(before["unclassified"], [])
        with self.db.atomic() as conn:
            conn.execute("CREATE TABLE new_money_source (record_id INTEGER PRIMARY KEY, gross_amount REAL)")
            after = audit_amount_field_coverage(conn)
        self.assertTrue(any(
            item["source_table"] == "new_money_source"
            and item["amount_field"] == "gross_amount"
            for item in after["unclassified"]
        ), after)

    def test_mode_dependent_price_event_value_is_not_blanket_exempted(self):
        with self.db.atomic() as conn:
            conn.execute("""INSERT INTO market_events
                (asset_code,event_type,severity,starts_epoch,ends_epoch,created_by,target_mode,target_value)
                VALUES ('USD_ASSET','good',1,1,2,'test','price',12.34)""")
            report = audit_amount_field_coverage(conn)
        self.assertTrue(any(
            item["source_table"] == "market_events"
            and item["amount_field"] == "target_value"
            for item in report["unclassified"]
        ), report)

    def test_market_event_target_snapshots_use_asset_code_and_rowid(self):
        self.db.assets.execute("""INSERT INTO market_assets
            (code,name,type,price,total_supply,status,exchange_id,quote_currency)
            VALUES ('USD_ASSET','Dollar Asset','stock',12.34,100,'active','NYSE','USD')""")
        self.db.currency.execute("""INSERT INTO system.market_event_targets
            (event_id,asset_code,start_price,target_price,start_epoch,end_epoch,last_tick_epoch,last_price)
            VALUES (1,'USD_ASSET',12.34,13.45,1,10,1,12.34)""")
        with self.db.readonly() as conn:
            rows = conn.execute("""SELECT amount_field,currency_code,decimal_places,amount_minor_units
                FROM financial_amount_snapshots WHERE source_table='market_event_targets'
                ORDER BY amount_field""").fetchall()
        self.assertEqual({row[0]: (row[1], row[2], row[3]) for row in rows}, {
            "last_price": ("USD", 2, 1234),
            "start_price": ("USD", 2, 1234),
            "target_price": ("USD", 2, 1345),
        })

    def test_amount_audit_checks_registered_schema_even_when_source_table_is_empty(self):
        self.db.currency.execute(
            "INSERT INTO financial_amount_field_registry "
            "(source_table,source_key_field,amount_field,currency_expression) "
            "VALUES ('aml_alerts','alert_id','missing_amount','NEW.currency_code')"
        )
        with self.db.readonly() as conn:
            findings = audit_amount_snapshots(conn)
        self.assertTrue(any(
            item["source_table"] == "aml_alerts"
            and item["amount_field"] == "missing_amount"
            and item["status"] == "missing_source_field"
            for item in findings
        ), findings)

    def test_full_financial_reconciliation_returns_each_implemented_ledger_scope(self):
        with self.db.readonly() as conn:
            report = AccountingJournal.reconcile_all_financial_ledgers(conn)
        self.assertEqual(report["status"], "matched", report)
        self.assertIn("amount_snapshot_registry", report["coverage"])
        self.assertIn("amount_schema_coverage", report["coverage"])
        self.assertIn("common_journals", report["coverage"])
        self.assertIn("institution_subledgers", report["coverage"])
        self.assertEqual(report["institution_count"], 0)
        self.assertEqual(report["amount_field_coverage"]["status"], "matched")


    def test_institution_ledger_minor_unit_upgrade_is_idempotent(self):
        self.db.currency.execute(
            "INSERT INTO financial_institution_ledger "
            "(issuer,event_type,amount,reference_key,created_epoch,currency_code,"
            "base_currency_code,base_amount) VALUES "
            "('LegacyBank','fee_income',1.005,'legacy-fee',123,'USD','USD',2.675)"
        )
        for _ in range(2):
            migrated = CurrencyDB()
            migrated.close()
        row = self.db.currency.execute_one(
            "SELECT amount,base_amount,amount_minor_units,base_amount_minor_units,"
            "amount_decimal_places,base_decimal_places FROM financial_institution_ledger "
            "WHERE issuer='LegacyBank' AND reference_key='legacy-fee'"
        )
        self.assertEqual(row["amount_minor_units"], 101)
        self.assertEqual(row["base_amount_minor_units"], 268)
        self.assertEqual((row["amount_decimal_places"], row["base_decimal_places"]),
                         (2, 2))
        self.assertEqual(tuple(Decimal(str(value)) if isinstance(value,(int,float,Decimal)) else value for value in (row["amount"], row["base_amount"])), (Decimal('1.005'), Decimal('2.675')))

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
                "WHERE user_id='us-user' AND account_type='saving'"
            )
        }
        self.assertEqual(balances,{'NWX':1000*SCALE,'EUR':3*SCALE})
        self.assertIsNone(self.db.currency.execute_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='currency_wallets'"
        ))

    def test_foreign_currency_savings_open_as_ordinary_savings(self):
        for code,currency in (('SAVE_USD','USD'),('SAVE_EUR','EUR')):
            self.db.products.execute(
                "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,status,"
                "quote_currency,product_kind) VALUES (?,?,0,0,'active',?,'saving')",
                (code,code,currency),
            )
        bank = BankManager(self.db)
        ok,message = bank.open_account('us-user','SAVE_EUR','1234',0)
        self.assertTrue(ok,message)
        self.assertIn('외환통장',message)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT account_type,quote_currency FROM accounts WHERE user_id='us-user' AND product_code='SAVE_EUR'"
        )[:],('saving','EUR'))
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
        self.assertEqual(Decimal(str(portfolio['account_total'])), Decimal('10.13'))
        self.assertEqual(Decimal(str(portfolio['accounts'][0]['balance'])), Decimal('10.13'))
        self.assertEqual(portfolio['accounts'][0]['currency_code'], 'USD')
        self.assertEqual(portfolio['accounts'][0]['decimal_places'], 2)
        self.assertEqual(Decimal(str(portfolio['accounts'][0]['contract_rate'])), Decimal('4.5'))
        self.assertEqual(portfolio['accounts'][0]['next_repricing_epoch'], 2000)
        self.assertEqual(portfolio['accounts'][0]['benchmark_key'], 'funding')
        self.assertEqual(Decimal(str(portfolio['accounts'][0]['rate_resets'][0]['new_rate'])), Decimal('4.5'))

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
        for invalid_epoch in (-1, float('nan'), float('inf'), 1e300, True, 'invalid'):
            with self.subTest(invalid_epoch=invalid_epoch):
                with self.assertRaisesRegex(ValueError, "valid UNIX epoch"):
                    with self.db.atomic() as conn:
                        BankManager.resolve_failed_institution(
                            conn, 'FXBank', now=invalid_epoch
                        )
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT resolution_status FROM accounts WHERE account_number=?", (number,)
        )[0], 'active')
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT COUNT(*) FROM deposit_insurance_claims WHERE issuer='FXBank'"
        )[0], 0)
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
        self.assertEqual(Decimal(str(account['insurance_paid'])), Decimal('1.87'))
        self.assertEqual(Decimal(str(account['uninsured_claim'])), Decimal('1.38'))
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
        payout_subledger = insurance_rows['deposit_insurance_payout_subledger']
        self.assertEqual(payout_subledger['currency_code'], 'USD')
        self.assertEqual(payout_subledger['claim_units'], 199)
        self.assertEqual(payout_subledger['unverifiable_claim_count'], 0)
        self.assertEqual(payout_subledger['status'], 'matched')
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
        self.assertEqual(Decimal(str(claims[0]['covered_amount'])), Decimal('1.87'))
        self.assertEqual(Decimal(str(claims[0]['uninsured_amount'])), Decimal('1.38'))
        self.assertEqual(claims[0]['payout_currency'], 'USD')
        self.assertAlmostEqual(claims[0]['payout_amount'], 1.99)
        claim_units = self.db.accounts.execute_one(
            "SELECT payout_amount_minor_units,payout_decimal_places "
            "FROM deposit_insurance_claims WHERE issuer='FXBank'"
        )
        self.assertEqual(tuple(claim_units), (199, 2))
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
        with self.assertRaisesRegex(sqlite3.IntegrityError, "insurance payout"):
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

    def test_insurance_payout_reconciler_reports_unverifiable_legacy_currency(self):
        from managers.bank_journal import BankJournal

        self.db.accounts.execute("DROP TRIGGER IF EXISTS trg_insurance_payout_precision_insert")
        self.db.accounts.execute("DROP TRIGGER IF EXISTS trg_insurance_payout_pair_insert")
        self.db.accounts.execute("DROP TRIGGER IF EXISTS trg_insurance_payout_minor_insert")
        self.db.accounts.execute(
            "INSERT INTO deposit_insurance_claims "
            "(issuer,user_id,account_number,payout_currency,payout_amount,resolved_epoch) "
            "VALUES ('LegacyBank','legacy-user','legacy-account','OLD',1.25,1)"
        )
        with self.db.atomic() as conn:
            findings = BankJournal.reconcile_deposit_insurance_claims(conn, 'LegacyBank')
        payout = next(row for row in findings
                      if row['account_code'] == 'deposit_insurance_payout_subledger')
        self.assertEqual(payout['currency_code'], 'OLD')
        self.assertIsNone(payout['decimal_places'])
        self.assertEqual(payout['unverifiable_claim_count'], 1)
        self.assertEqual(payout['status'], 'mismatch')

    def test_treasury_bond_offer_and_holding_use_currency_minor_units(self):
        self.db.products.execute(
            "INSERT INTO treasury_bond_products "
            "(product_code,name,annual_rate,maturity_days,face_value,exchange_id,"
            "market_price,total_supply,quote_currency) VALUES ('USD-BOND','USD bond',3,365,"
            "10.25,'BOND',10.50,1025,'USD')"
        )
        self.db.accounts.execute(
            "INSERT INTO treasury_bond_holdings "
            "(user_id,product_code,product_name,annual_rate,maturity_days,principal,"
            "face_value,acquisition_cost,purchased_epoch,maturity_epoch) "
            "VALUES ('us-user','USD-BOND','USD bond',3,365,20.50,10.25,20.75,1,2)"
        )
        offer = self.db.products.execute_one(
            "SELECT face_value_minor_units,market_price_minor_units,total_supply_minor_units,"
            "sold_amount_minor_units FROM treasury_bond_products WHERE product_code='USD-BOND'"
        )
        self.assertEqual(tuple(offer), (1025, 1050, 102500, 0))
        holding = self.db.accounts.execute_one(
            "SELECT principal_minor_units,face_value_minor_units,acquisition_cost_minor_units "
            "FROM treasury_bond_holdings WHERE product_code='USD-BOND'"
        )
        self.assertEqual(tuple(holding), (2050, 1025, 2075))
        self.db.products.execute(
            "UPDATE treasury_bond_products SET market_price_minor_units=1099 "
            "WHERE product_code='USD-BOND'"
        )
        self.assertEqual(Decimal(str(self.db.products.execute_one(
            "SELECT market_price FROM treasury_bond_products WHERE product_code='USD-BOND'"
        )[0])), Decimal('10.99'))
        self.db.accounts.execute(
            "UPDATE treasury_bond_holdings SET principal_minor_units=2010 "
            "WHERE product_code='USD-BOND'"
        )
        self.assertEqual(Decimal(str(self.db.accounts.execute_one(
            "SELECT principal FROM treasury_bond_holdings WHERE product_code='USD-BOND'"
        )[0])), Decimal('20.1'))

    def test_treasury_bond_primary_purchase_uses_quote_currency_minor_units(self):
        self.db.products.execute(
            "INSERT INTO treasury_bond_products "
            "(product_code,name,annual_rate,maturity_days,face_value,exchange_id,"
            "market_price,total_supply,quote_currency) "
            "VALUES ('USD-TRADE','USD trade bond',3,365,10.25,'EX',10.50,1025,'USD')"
        )
        book = OrderBookManager(self.db, UserManager(self.db))
        ok, message = book.place_limit_order('us-user', 'USD-TRADE', 'buy', 2, 10.50)
        self.assertTrue(ok, message)
        holding = self.db.accounts.execute_one(
            "SELECT principal_minor_units,face_value_minor_units,"
            "acquisition_cost_minor_units FROM treasury_bond_holdings "
            "WHERE user_id='us-user' AND product_code='USD-TRADE'"
        )
        self.assertEqual(tuple(holding), (2050, 1025, 2050))
        offer = self.db.products.execute_one(
            "SELECT sold_amount_minor_units FROM treasury_bond_products "
            "WHERE product_code='USD-TRADE'"
        )
        self.assertEqual(offer[0], 2050)

    def test_treasury_bond_secondary_transfer_uses_minor_unit_basis_and_price(self):
        self.db.products.execute(
            "INSERT INTO treasury_bond_products "
            "(product_code,name,annual_rate,maturity_days,face_value,exchange_id,"
            "market_price,total_supply,quote_currency) "
            "VALUES ('USD-SECONDARY','USD secondary bond',3,365,10.25,'EX',10.50,10.25,'USD')"
        )
        self.db.users.create_user('buyer','미국 매수자','미국',1000)
        book = OrderBookManager(self.db, UserManager(self.db))
        ok, message = book.place_limit_order('us-user', 'USD-SECONDARY', 'buy', 1, 10.50)
        self.assertTrue(ok, message)
        ok, message = book.place_limit_order('us-user', 'USD-SECONDARY', 'sell', 1, 10.50)
        self.assertTrue(ok, message)
        ok, message = book.place_limit_order('buyer', 'USD-SECONDARY', 'buy', 1, 10.50)
        self.assertTrue(ok, message)
        buyer_holding = self.db.accounts.execute_one(
            "SELECT principal_minor_units,face_value_minor_units,"
            "acquisition_cost_minor_units FROM treasury_bond_holdings "
            "WHERE user_id='buyer' AND product_code='USD-SECONDARY'"
        )
        self.assertEqual(tuple(buyer_holding), (1025, 1025, 1050))
        self.assertIsNone(self.db.accounts.execute_one(
            "SELECT 1 FROM treasury_bond_holdings "
            "WHERE user_id='us-user' AND product_code='USD-SECONDARY'"
        ))

    def test_usd_bond_maturity_posts_currency_minor_unit_interest_tax_and_credit(self):
        self.db.currency.execute(
            "INSERT INTO country_tax_rules(nationality,income_source,tax_percent,updated_by,updated_epoch) "
            "VALUES ('미국','treasury_bond_interest',20,'test',1)"
        )
        now = 2_000_000_000
        self.db.products.execute(
            "INSERT INTO treasury_bond_products "
            "(product_code,name,annual_rate,maturity_days,face_value,exchange_id,"
            "market_price,total_supply,quote_currency) VALUES ('USD-MATURE','USD maturity',10,365,"
            "1.25,'BOND',1.25,1.25,'USD')"
        )
        self.db.accounts.execute(
            "INSERT INTO treasury_bond_holdings "
            "(user_id,product_code,product_name,annual_rate,maturity_days,principal,face_value,"
            "acquisition_cost,purchased_epoch,maturity_epoch) "
            "VALUES ('us-user','USD-MATURE','USD maturity',10,365,1.25,1.25,1.25,"
            "?,?)", (now-365*86400, now-1),
        )
        redeemed = TreasuryBondManager(self.db).redeem_matured('us-user', now=now)
        self.assertEqual(len(redeemed), 1)
        self.assertEqual((redeemed[0]['principal'], redeemed[0]['interest'],
                          redeemed[0]['tax'], redeemed[0]['net']), tuple(Decimal(v) for v in ('1.25', '0.13', '0.03', '1.35')))
        user = self.db.users.get_user('us-user')
        self.assertAlmostEqual(user['cash'], 1001.35)
        tax = self.db.tax.execute_one(
            "SELECT gross_amount,tax_amount,net_amount,currency_code FROM tax_transactions "
            "WHERE user_id='us-user' AND source='treasury_bond_interest'"
        )
        self.assertEqual(tuple(Decimal(str(value)) if isinstance(value,(int,float,Decimal)) else value for value in tuple(tax)), (Decimal('0.13'), Decimal('0.03'), Decimal('0.1'), 'USD'))
        with self.db.readonly() as conn:
            bond_check = AccountingJournal.reconcile_treasury_bond_cash_events(conn)
        self.assertEqual(bond_check['status'], 'matched', bond_check)
        wallet_links = self.db.currency.execute_one(
            "SELECT COUNT(*) FROM treasury_bond_cash_wallet_links"
        )[0]
        self.assertEqual(wallet_links, 1)
        self.db.products.execute(
            "INSERT INTO treasury_bond_products "
            "(product_code,name,annual_rate,maturity_days,face_value,exchange_id,"
            "market_price,total_supply,quote_currency,coupon_frequency_days) "
            "VALUES ('USD-COUPON','USD coupon',10,365,1.25,'BOND',1.25,1.25,'USD',180)"
        )
        self.db.accounts.execute(
            "INSERT INTO treasury_bond_holdings "
            "(user_id,product_code,product_name,annual_rate,maturity_days,principal,face_value,"
            "acquisition_cost,purchased_epoch,maturity_epoch,coupon_frequency_days,"
            "day_count_convention,last_coupon_epoch) VALUES ('us-user','USD-COUPON','USD coupon',"
            "10,365,1.25,1.25,1.25,?,?,180,'ACT/365',?)",
            (now-180*86400, now+86400, now-180*86400),
        )
        paid = TreasuryBondManager(self.db).pay_due_coupons('us-user', now=now)
        self.assertEqual(len(paid), 1)
        self.assertEqual((paid[0]['coupon'], paid[0]['tax']), (Decimal('0.06'), Decimal('0.01')))
        self.assertAlmostEqual(self.db.users.get_user('us-user')['cash'], 1001.40)
        with self.db.readonly() as conn:
            bond_check = AccountingJournal.reconcile_treasury_bond_cash_events(conn)
        self.assertEqual(bond_check['status'], 'matched', bond_check)
        self.assertEqual(bond_check['event_count'], 2)
        with self.db.atomic() as conn:
            conn.execute("DROP TRIGGER trg_treasury_bond_cash_wallet_links_delete")
            conn.execute("DELETE FROM treasury_bond_cash_wallet_links WHERE event_key LIKE 'coupon:%'")
            bond_check = AccountingJournal.reconcile_treasury_bond_cash_events(conn)
            self.assertEqual(bond_check['status'], 'mismatch', bond_check)
            self.assertTrue(any(gap['status'] == 'missing_or_invalid_wallet_settlement_link'
                                for gap in bond_check['common_journal_gaps']), bond_check)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
            self.db.currency.execute(
                "UPDATE treasury_bond_cash_events SET net_minor_units=99 "
                "WHERE event_type='coupon'"
            )

    def test_bond_cash_reconciliation_finds_event_without_common_post(self):
        with self.db.atomic() as conn:
            AccountingJournal.ensure_schema(conn)
            conn.execute("""INSERT INTO treasury_bond_cash_events
                (event_key,event_type,holding_id,product_code,user_id,currency_code,decimal_places,
                 principal_minor_units,interest_minor_units,tax_minor_units,net_minor_units,recorded_epoch)
                VALUES ('missing-post','coupon',1,'B','us-user','USD',2,0,100,10,90,1)""")
            check = AccountingJournal.reconcile_treasury_bond_cash_events(conn)
        self.assertEqual(check['status'], 'mismatch')
        self.assertEqual(check['common_journal_gaps'][0]['event_key'], 'missing-post')


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
                    payout_currency TEXT,payout_amount REAL,
                    resolved_epoch REAL NOT NULL,UNIQUE(issuer,account_number))""")
                connection.execute(
                    "INSERT INTO deposit_insurance_claims "
                    "(issuer,user_id,account_number,covered_amount,uninsured_amount,"
                    "payout_currency,payout_amount,resolved_epoch) "
                    "VALUES ('Bank','u','acct',12,3,'NWX',12,1)"
                )
            db = DatabaseManager()
            try:
                columns = {row[1] for row in db.accounts.execute(
                    "PRAGMA table_info(deposit_insurance_claims)"
                )}
                self.assertTrue({'payout_currency', 'payout_amount',
                                 'payout_amount_minor_units', 'payout_decimal_places'} <= columns)
                claim = db.accounts.execute_one(
                    "SELECT covered_amount,uninsured_amount,payout_currency,payout_amount,"
                    "payout_amount_minor_units,payout_decimal_places "
                    "FROM deposit_insurance_claims WHERE issuer='Bank'"
                )
                self.assertEqual(tuple(Decimal(str(value)) if isinstance(value,(int,float,Decimal)) else value for value in tuple(claim)), (12, 3, 'NWX', Decimal('12.0'), 12, 0))
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
