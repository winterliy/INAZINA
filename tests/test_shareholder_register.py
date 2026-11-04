"""Dividends are paid to the shareholder register, closed before payment."""
import unittest
from unittest.mock import patch

import test_inazina as fixtures
from managers.corporate_actions_manager import CorporateActionsManager


class ShareholderRegisterTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def prepare(self, due=None):
        self.db.system.set_setting('tax_percent', 0)
        self.db.companies.create_company('CO', 'CO', '게임', 100000, 1000, 100, 'admin', 'INZ')
        if due:
            self.db.companies.execute("UPDATE company SET dividend_cycle_seconds=86400,dividend_per_share=5,"
                                      "dividend_next_epoch=? WHERE company_code='CO'", (due,))
        self.db.assets.add_user_asset('citizen', 'CO', 10)
        self.manager = CorporateActionsManager(self.bot)
        # Dividends are always funded from the issuer's own cash account.
        self.assertTrue(self.manager.fund_issuer('admin', 'CO', 'add', 1000000)[0])

    def cash(self, user):
        return self.db.users.get_user(user)['cash']

    def test_legacy_dr_withholding_rows_migrate_to_currency_minor_units(self):
        from database.corporate_db import CorporateDB
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('USD','US Dollar','1500',2)"
        )
        conn = self.db.corporate.conn
        conn.execute("DROP TRIGGER IF EXISTS dr_dividend_withholding_no_update")
        conn.execute("DROP TRIGGER IF EXISTS dr_dividend_withholding_no_delete")
        conn.execute("DROP TABLE dr_dividend_withholding_events")
        conn.execute("""CREATE TABLE dr_dividend_withholding_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,payment_id INTEGER NOT NULL UNIQUE,
            distribution_id INTEGER NOT NULL,rule_id INTEGER,dr_code TEXT NOT NULL,
            issuer_nationality TEXT NOT NULL,underlying_nationality TEXT NOT NULL,
            recipient_nationality TEXT NOT NULL,recipient_residency TEXT NOT NULL,
            product_type TEXT NOT NULL,contract_epoch INTEGER NOT NULL,treaty_code TEXT,
            tax_rate_percent TEXT NOT NULL,gross_amount INTEGER NOT NULL,tax_amount INTEGER NOT NULL,
            net_amount_units INTEGER NOT NULL,currency_code TEXT NOT NULL,
            remittance_treasury_id TEXT NOT NULL,recorded_epoch INTEGER NOT NULL)""")
        conn.execute("""INSERT INTO dr_dividend_withholding_events
            (payment_id,distribution_id,rule_id,dr_code,issuer_nationality,
             underlying_nationality,recipient_nationality,recipient_residency,
             product_type,contract_epoch,treaty_code,tax_rate_percent,gross_amount,
             tax_amount,net_amount_units,currency_code,remittance_treasury_id,recorded_epoch)
            VALUES (1,1,NULL,'LEGACY','A','B','C','C','depositary_receipt',1,NULL,
                    '10',1.23,0.12,1110000,'USD','treasury',1)""")
        conn.commit()
        migrated = CorporateDB()
        try:
            event = migrated.execute_one(
                "SELECT decimal_places,gross_minor_units,tax_minor_units,net_minor_units "
                "FROM dr_dividend_withholding_events WHERE payment_id=1"
            )
            self.assertEqual(tuple(event), (2, 123, 12, 111))
            with self.assertRaisesRegex(Exception, 'dr_withholding_events_are_immutable'):
                migrated.execute(
                    "UPDATE dr_dividend_withholding_events SET gross_minor_units=124 "
                    "WHERE payment_id=1"
                )
        finally:
            migrated.close()

    def test_positive_dividend_tax_links_treasury_wallet_receipt(self):
        from managers.accounting_journal import AccountingJournal
        self.prepare()
        self.db.system.set_setting('tax_percent', 10)
        with self.db.atomic() as conn:
            tax, net, _context = self.manager._credit_dividend(
                conn, 'citizen', 'CO', 'stock', 50, 'NWX', 123
            )
            self.assertEqual((tax, net), (5, 45_000_000))
            check = next(item for item in AccountingJournal.reconcile_subledgers(conn)
                         if item['ledger_scope'] == 'tax_transactions')
            self.assertEqual(check['status'], 'matched', check)
            self.assertEqual(len(conn.execute(
                "SELECT * FROM tax_transaction_wallet_links"
            ).fetchall()), 1)

    def move_shares(self, quantity):
        self.db.assets.execute("UPDATE user_assets SET quantity=quantity-? WHERE user_id='citizen' AND asset_code='CO'", (quantity,))
        self.db.assets.add_user_asset('admin', 'CO', quantity)

    def test_regular_register_closes_ten_minutes_before_payment(self):
        due = 100_000
        self.prepare(due)
        self.assertEqual(self.manager.process_due(now=due - 601), [])
        self.assertIsNone(self.db.corporate.execute_one('SELECT 1 FROM shareholder_registers'))
        self.manager.process_due(now=due - 600)  # register closes; nothing is paid yet
        register = self.db.corporate.execute_one('SELECT * FROM shareholder_registers')
        self.assertEqual((register['dividend_kind'], register['status'], register['pay_epoch']), ('regular', 'closed', due))
        # Shares bought after the register closed do not carry this dividend.
        self.move_shares(4)
        citizen, admin = self.cash('citizen'), self.cash('admin')
        self.assertEqual(self.manager.process_due(now=due), [('CO', True, 50)])
        self.assertEqual(self.cash('citizen') - citizen, 50)
        self.assertEqual(self.cash('admin') - admin, 0)
        self.assertEqual(self.db.corporate.execute_one('SELECT status FROM shareholder_registers')[0], 'paid')

    def test_invalid_processing_epoch_cannot_close_register_or_pay_dividends(self):
        due = 100_000
        self.prepare(due)
        before_cash = self.cash('citizen')
        invalid_epochs = (-1, float('nan'), float('inf'), 1e300, True, 'invalid')
        for invalid_epoch in invalid_epochs:
            with self.subTest(invalid_epoch=invalid_epoch):
                with self.assertRaisesRegex(
                    ValueError, 'invalid_corporate_action_processing_epoch'
                ):
                    self.manager.process_due(now=invalid_epoch)
                self.assertIsNone(self.db.corporate.execute_one(
                    'SELECT 1 FROM shareholder_registers'
                ))
                self.assertEqual(self.cash('citizen'), before_cash)
                success, message = self.manager.liquidate(
                    'admin', 'CO', operation_key=f'bad-time:{invalid_epoch}',
                    now=invalid_epoch,
                )
                self.assertFalse(success)
                self.assertIn('invalid_corporate_action_liquidation_epoch', message)
        self.assertEqual(self.db.corporate.execute_one(
            "SELECT COUNT(*) FROM corporate_action_records WHERE action_kind='liquidation'"
        )[0], 0)

    def test_regular_dividend_without_a_closed_register_uses_holders_at_payment(self):
        from managers.accounting_journal import AccountingJournal
        due = 100_000
        self.prepare(due)
        citizen = self.cash('citizen')
        self.assertEqual(self.manager.process_due(now=due), [('CO', True, 50)])
        self.assertEqual(self.cash('citizen') - citizen, 50)
        with self.db.readonly() as conn:
            settlement = AccountingJournal.reconcile_dividend_payment_settlements(conn)
        self.assertEqual(settlement['status'], 'matched', settlement)
        self.assertEqual(settlement['payment_count'], 1)

    def test_dividend_payout_without_wallet_receipt_rolls_back_payment(self):
        due = 100_000
        self.prepare(due)
        citizen_before = self.cash('citizen')
        self.db.currency.execute("DROP TRIGGER trg_customer_cash_ledger_update")
        with self.assertRaisesRegex(RuntimeError, '배당 고객 지갑 입금 원장 항목이 없습니다'):
            self.manager.process_due(now=due)
        self.assertEqual(self.cash('citizen'), citizen_before)
        self.assertEqual(self.db.corporate.execute_one(
            'SELECT COUNT(*) FROM dividend_payments'
        )[0], 0)

    def test_legacy_dividend_payment_key_migrates_without_losing_rows(self):
        from database.corporate_db import CorporateDB

        self.prepare()
        self.db.corporate.execute('DROP TABLE dividend_payments')
        self.db.corporate.execute('''CREATE TABLE dividend_payments (
            payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            distribution_id INTEGER NOT NULL REFERENCES dividend_distributions(distribution_id),
            user_id TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            gross_amount INTEGER NOT NULL CHECK(gross_amount >= 0),
            tax_amount INTEGER NOT NULL CHECK(tax_amount >= 0),
            net_amount_units INTEGER NOT NULL CHECK(net_amount_units >= 0),
            quote_currency TEXT NOT NULL,
            created_epoch INTEGER NOT NULL,
            UNIQUE(distribution_id,user_id))''')
        self.db.corporate.execute('''INSERT INTO dividend_distributions
            (operation_key,dividend_kind,asset_code,due_epoch,per_share,dividend_mode,
             gross_amount,entitlement_amount,deferred_amount,quote_currency,funding_source,
             issuer_code,created_epoch) VALUES ('migration:payment','regular','CO',1,1,'fixed',
             1,1,0,'NWX','treasury','CO',1)''')
        distribution = self.db.corporate.execute_one(
            "SELECT distribution_id FROM dividend_distributions WHERE operation_key='migration:payment'"
        )[0]
        self.db.corporate.execute(
            "INSERT INTO dividend_payments(distribution_id,user_id,quantity,gross_amount,"
            "tax_amount,net_amount_units,quote_currency,created_epoch) VALUES (?,?,?,?,?,?,?,?)",
            (distribution, 'citizen', 1, 1, 0, 1_000_000, 'NWX', 1),
        )

        migrated_db = CorporateDB()

        payment = self.db.corporate.execute_one(
            "SELECT user_id,gross_amount,quote_currency FROM dividend_payments WHERE distribution_id=?",
            (distribution,),
        )
        self.assertEqual(tuple(payment), ('citizen', 1, 'NWX'))
        indexes = self.db.corporate.execute("PRAGMA index_list('dividend_payments')")
        unique = next(index for index in indexes if index['unique'])
        columns = self.db.corporate.execute(
            f"PRAGMA index_info('{unique['name']}')"
        )
        self.assertEqual([column['name'] for column in columns],
                         ['distribution_id', 'user_id', 'quote_currency', 'allocation_key'])
        migrated_payment = self.db.corporate.execute_one(
            'SELECT allocation_key FROM dividend_payments WHERE distribution_id=?', (distribution,)
        )
        self.assertEqual(migrated_payment['allocation_key'], 'legacy')
        migrated_db.close()

    def test_same_currency_dr_holders_receive_underlying_dividend_entitlement(self):
        due = 100_000
        self.prepare(due)
        self.db.assets.execute(
            "INSERT INTO depositary_receipts(dr_code,underlying_code,listing_exchange_id,"
            "quote_currency,underlying_per_receipt,receipt_supply,deposited_underlying_quantity) "
            "VALUES ('CO_DR','CO','LSE','NWX',2,100,200)"
        )
        self.db.assets.register_asset('CO_DR', 'CO DR', 'LSE', 200, 'depositary_receipt', 100)
        self.db.assets.add_user_asset('admin', 'CO_DR', 3)
        configured, reason = self.manager.configure_dividend('admin', 'CO_DR', 86400, 5)
        self.assertFalse(configured)
        self.assertIn('배당', reason)
        citizen_before, dr_holder_before = self.cash('citizen'), self.cash('admin')

        self.assertEqual(self.manager.process_due(now=due), [('CO', True, 80)])
        self.assertEqual(self.cash('citizen') - citizen_before, 50)
        self.assertEqual(self.cash('admin') - dr_holder_before, 30)
        payment = self.db.corporate.execute_one(
            "SELECT p.quantity,p.gross_amount FROM dividend_payments p "
            "JOIN dividend_distributions d USING(distribution_id) WHERE p.user_id='admin'"
        )
        self.assertEqual((payment['quantity'], payment['gross_amount']), (6, 30))

    def test_cross_currency_dr_dividend_is_converted_and_recorded(self):
        due = 100_000
        self.prepare(due)
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','US Dollar','1500',2)"
        )
        self.db.assets.execute(
            "INSERT INTO depositary_receipts(dr_code,underlying_code,listing_exchange_id,"
            "quote_currency,underlying_per_receipt,receipt_supply,deposited_underlying_quantity) "
            "VALUES ('CO_US_DR','CO','NYSE','USD',2,100,200)"
        )
        self.db.assets.register_asset('CO_US_DR', 'CO US DR', 'NYSE', 200,
                                      'depositary_receipt', 100)
        self.db.assets.add_user_asset('admin', 'CO_US_DR', 3)
        self.db.assets.add_user_asset('admin', 'CO', 1)
        self.db.accounts.execute(
            "INSERT INTO accounts(account_number,user_id,product_code,balance,principal,"
            "account_type,quote_currency,balance_units) "
            "VALUES ('ADMIN_USD','admin',NULL,0,0,'saving','USD',0)"
        )
        from database.currency_db import SCALE
        from managers.currency_manager import CurrencyManager
        expected_usd_units = CurrencyManager.convert(self.db.currency.conn, 30 * SCALE, 'NWX', 'USD')

        self.assertEqual(self.manager.process_due(now=due - 600), [])
        self.db.assets.execute("DELETE FROM user_assets WHERE user_id='admin' AND asset_code='CO_US_DR'")
        self.db.assets.add_user_asset('citizen', 'CO_US_DR', 3)
        self.assertEqual(self.manager.process_due(now=due), [('CO', True, 85)])
        paid = self.db.corporate.execute(
            "SELECT user_id,gross_amount,quote_currency FROM dividend_payments WHERE user_id='admin' "
            "ORDER BY quote_currency"
        )
        self.assertEqual([(row['user_id'], row['gross_amount'], row['quote_currency']) for row in paid],
                         [('admin', 5, 'NWX'), ('admin', expected_usd_units / SCALE, 'USD')])
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT balance_units FROM accounts WHERE account_number='ADMIN_USD'"
        )[0], expected_usd_units)
        fx = self.db.currency.execute_one(
            "SELECT source_currency,target_currency,source_units,target_units,fee_nwx "
            "FROM currency_transactions WHERE user_id='admin'"
        )
        self.assertEqual((fx['source_currency'], fx['target_currency'], fx['source_units'],
                          fx['target_units'], fx['fee_nwx']),
                         ('NWX', 'USD', 30 * SCALE, expected_usd_units, 0))
        dr_tax = self.db.corporate.execute_one(
            "SELECT currency_code,decimal_places,gross_minor_units,tax_minor_units,"
            "net_minor_units FROM dr_dividend_withholding_events WHERE currency_code='USD'"
        )
        self.assertIsNotNone(dr_tax)
        self.assertEqual((dr_tax['currency_code'], dr_tax['decimal_places'],
                          dr_tax['gross_minor_units'], dr_tax['tax_minor_units'],
                          dr_tax['net_minor_units']),
                         ('USD', 2, expected_usd_units // 10_000, 0,
                          expected_usd_units // 10_000))
        from managers.dr_dividend_tax_manager import DrDividendTaxManager
        self.assertEqual(DrDividendTaxManager.reconciliation(
            self.db.corporate.conn)['eventGaps'], [])
        distribution = self.db.corporate.execute_one(
            "SELECT gross_amount,entitlement_amount,quote_currency FROM dividend_distributions"
        )
        self.assertEqual((distribution['gross_amount'], distribution['entitlement_amount'],
                          distribution['quote_currency']), (85, 85, 'NWX'))

    def test_suspended_dr_program_keeps_existing_dividend_rights(self):
        due = 100_000
        self.prepare(due)
        self.db.assets.execute(
            "INSERT INTO depositary_receipts(dr_code,underlying_code,listing_exchange_id,"
            "quote_currency,underlying_per_receipt,receipt_supply,deposited_underlying_quantity,"
            "program_status) VALUES ('CO_DR','CO','LSE','NWX',2,100,200,'suspended')"
        )
        self.db.assets.register_asset('CO_DR', 'CO DR', 'LSE', 200, 'depositary_receipt', 100)
        self.db.assets.execute("UPDATE market_assets SET status='inactive' WHERE code='CO_DR'")
        self.db.assets.add_user_asset('admin', 'CO_DR', 3)
        admin_before = self.cash('admin')

        self.assertEqual(self.manager.process_due(now=due), [('CO', True, 80)])
        self.assertEqual(self.cash('admin') - admin_before, 30)

    def test_cross_border_dr_tax_uses_effective_rule_and_keeps_audit_snapshot(self):
        from managers.dr_dividend_tax_manager import DrDividendTaxManager
        due = 100_000
        self.prepare(due)
        self.db.assets.execute(
            "INSERT INTO depositary_receipts(dr_code,underlying_code,listing_exchange_id,"
            "quote_currency,underlying_per_receipt,receipt_supply,deposited_underlying_quantity) "
            "VALUES ('CO_DR_TAX','CO','LSE','NWX',2,100,200)"
        )
        self.db.assets.register_asset('CO_DR_TAX', 'CO DR TAX', 'LSE', 200,
                                      'depositary_receipt', 100)
        self.db.assets.add_user_asset('admin', 'CO_DR_TAX', 3)
        with self.db.corporate.atomic() as conn:
            old = DrDividendTaxManager.create_rule(conn, 'admin', {
                'rule_name': 'Expired 5 percent', 'issuer_nationality': '낙원',
                'underlying_nationality': '낙원', 'recipient_nationality': '*',
                'recipient_residency': '*', 'tax_rate_percent': '5',
                'effective_from': 1, 'effective_until': due,
            }, now=1)
            selected = DrDividendTaxManager.create_rule(conn, 'admin', {
                'rule_name': 'Treaty 20 percent', 'issuer_nationality': '낙원',
                'underlying_nationality': '낙원', 'recipient_nationality': '낙원',
                'recipient_residency': '낙원', 'tax_rate_percent': '20',
                'treaty_code': 'SIM-TEST', 'effective_from': due,
            }, now=due)
        before = self.cash('admin')
        self.manager.process_due(now=due)
        payment = self.db.corporate.execute_one(
            "SELECT p.gross_amount,p.tax_amount,p.net_amount_units,e.rule_id,e.treaty_code,"
            "e.tax_rate_percent,e.contract_epoch FROM dividend_payments p "
            "JOIN dr_dividend_withholding_events e USING(payment_id) WHERE p.user_id='admin'"
        )
        self.assertEqual((payment['gross_amount'], payment['tax_amount'],
                          payment['net_amount_units'], payment['rule_id'],
                          payment['treaty_code'], payment['tax_rate_percent'],
                          payment['contract_epoch']), (30, 6, 24 * 1_000_000,
                                                        selected, 'SIM-TEST', '20', due))
        self.assertNotEqual(payment['rule_id'], old)
        self.assertEqual(self.cash('admin') - before, 24)
        with self.assertRaisesRegex(Exception, 'dr_tax_rules_are_immutable'):
            self.db.corporate.execute("UPDATE dr_dividend_tax_rules SET tax_rate_percent='1' WHERE rule_id=?", (selected,))
        with self.assertRaisesRegex(Exception, 'dr_tax_rules_are_immutable'):
            self.db.corporate.execute("DELETE FROM dr_dividend_tax_rules WHERE rule_id=?", (selected,))

    def test_same_currency_dividend_keeps_each_dr_program_rule_and_journals_tax(self):
        from managers.bank_journal import BankJournal
        from managers.dr_dividend_tax_manager import DrDividendTaxManager
        due = 100_000
        self.prepare(due)
        for exchange, nationality, code, receipts in (
            ('EX_A', 'Aland', 'CO_DR_A', 2), ('EX_B', 'Boria', 'CO_DR_B', 3),
        ):
            self.db.system.execute(
                "INSERT INTO market_exchanges(exchange_id,status,updated_epoch,nationality) "
                "VALUES (?, 'open', 1, ?)", (exchange, nationality),
            )
            self.db.assets.execute(
                "INSERT INTO depositary_receipts(dr_code,underlying_code,listing_exchange_id,"
                "quote_currency,underlying_per_receipt,receipt_supply,deposited_underlying_quantity) "
                "VALUES (?, 'CO', ?, 'NWX', 2, 100, 200)", (code, exchange),
            )
            self.db.assets.register_asset(code, code, exchange, 200, 'depositary_receipt', 100)
            self.db.assets.add_user_asset('admin', code, receipts)
        self.db.assets.add_user_asset('admin', 'CO', 2)
        self.db.users.execute("UPDATE users SET tax_residency='OldRes' WHERE user_id='admin'")
        with self.db.corporate.atomic() as conn:
            rule_a = DrDividendTaxManager.create_rule(conn, 'admin', {
                'rule_name': 'Aland DR', 'issuer_nationality': 'Aland',
                'underlying_nationality': '낙원', 'recipient_nationality': '낙원',
                'recipient_residency': 'OldRes', 'tax_rate_percent': '10',
                'effective_from': 1,
            }, now=1)
            rule_b = DrDividendTaxManager.create_rule(conn, 'admin', {
                'rule_name': 'Boria DR', 'issuer_nationality': 'Boria',
                'underlying_nationality': '낙원', 'recipient_nationality': '낙원',
                'recipient_residency': 'OldRes', 'tax_rate_percent': '30',
                'effective_from': 1,
            }, now=1)

        before = self.cash('admin')
        self.manager.process_due(now=due - 600)
        self.db.users.execute("UPDATE users SET tax_residency='NewRes' WHERE user_id='admin'")
        self.manager.process_due(now=due)
        payments = self.db.corporate.execute(
            "SELECT p.allocation_key,p.quantity,p.gross_amount,p.tax_amount,p.net_amount_units,"
            "e.dr_code,e.rule_id FROM dividend_payments p LEFT JOIN dr_dividend_withholding_events e "
            "USING(payment_id) WHERE p.user_id='admin' ORDER BY p.allocation_key"
        )
        self.assertEqual([(row['allocation_key'], row['gross_amount'], row['tax_amount'],
                           row['rule_id']) for row in payments], [
            ('dr:CO_DR_A', 20, 2, rule_a), ('dr:CO_DR_B', 30, 9, rule_b),
            ('stock', 10, 0, None),
        ])
        self.assertEqual(self.cash('admin') - before, 49)
        self.assertEqual([row['dr_code'] for row in payments],
                         ['CO_DR_A', 'CO_DR_B', None])
        self.assertEqual(BankJournal.audit_unbalanced(self.db.corporate.conn), [])
        journal = self.db.corporate.execute_one(
            "SELECT COUNT(*) count FROM bank_journal_entries WHERE event_type='dr_dividend_payment'"
        )
        self.assertEqual(journal['count'], 2)  # One payment journal for each DR program.
        report = DrDividendTaxManager.reconciliation(self.db.corporate.conn)
        self.assertEqual(report['unloggedPaymentCount'], 0)
        self.assertEqual(report['currencies'][0]['difference_minor_units'], 0)
        self.assertEqual(report['eventGaps'], [])
        self.assertEqual(report['journalGaps'], [])
        self.assertEqual(report['settlements']['status'], 'matched', report['settlements'])
        payment_id = self.db.corporate.execute_one(
            "SELECT payment_id FROM dividend_payment_wallet_links ORDER BY payment_id LIMIT 1"
        )[0]
        self.db.corporate.execute(
            "DROP TRIGGER trg_dividend_payment_wallet_links_delete"
        )
        self.db.corporate.execute(
            "DELETE FROM dividend_payment_wallet_links WHERE payment_id=?", (payment_id,)
        )
        settlement = DrDividendTaxManager.reconciliation(self.db.corporate.conn)['settlements']
        self.assertEqual(settlement['status'], 'mismatch', settlement)
        self.assertTrue(any(gap.get('payment_id') == payment_id
                            and gap['status'] == 'missing_or_duplicate_settlement_link'
                            for gap in settlement['gaps']))
        entry = self.db.currency.execute_one(
            "SELECT entry_id FROM bank_journal_entries "
            "WHERE event_type='dr_dividend_payment' ORDER BY entry_id LIMIT 1"
        )
        self.db.currency.execute("DROP TRIGGER trg_bank_journal_lines_immutable_delete")
        self.db.currency.execute("DROP TRIGGER trg_bank_journal_entry_no_delete")
        self.db.currency.execute(
            "DELETE FROM bank_journal_lines WHERE entry_id=?", (entry['entry_id'],)
        )
        self.db.currency.execute(
            "DELETE FROM bank_journal_entries WHERE entry_id=?", (entry['entry_id'],)
        )
        broken = DrDividendTaxManager.reconciliation(self.db.corporate.conn)
        self.assertEqual(len(broken['journalGaps']), 1)
        self.assertEqual(broken['journalGaps'][0]['status'],
                         'missing_or_duplicate_payment_journal')

    def test_special_dividend_closes_register_now_and_pays_ten_minutes_later(self):
        self.prepare()
        with patch('managers.corporate_actions_manager.time.time', return_value=1000):
            ok, message = self.manager.special_dividend('admin', 'CO', 3, 'special:one')
        self.assertTrue(ok, message)
        self.assertIn('주주명부', message)
        self.move_shares(9)  # sold after the record time: the seller still receives it
        citizen = self.cash('citizen')
        self.assertEqual(self.manager.process_due(now=1599), [])
        self.assertEqual(self.manager.process_due(now=1600), [('CO', True, 30)])
        self.assertEqual(self.cash('citizen') - citizen, 30)
        self.assertEqual(self.manager.process_due(now=1700), [])
        # Repeating the command with the same key neither re-registers nor re-pays.
        with patch('managers.corporate_actions_manager.time.time', return_value=1800):
            self.assertTrue(self.manager.special_dividend('admin', 'CO', 3, 'special:one')[0])
        self.assertEqual(self.manager.process_due(now=5000), [])
        self.assertEqual(self.db.corporate.execute_one('SELECT COUNT(*) FROM dividend_distributions')[0], 1)


if __name__ == "__main__":
    unittest.main()
