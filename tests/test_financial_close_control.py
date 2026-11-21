"""M4 close control: one trial balance over every common-ledger scope, and an honest coverage inventory."""
import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager, pg_backend
from database.db_manager import DBManager
from managers.accounting_journal import AccountingJournal


class _Rollback(Exception):
    pass


class FinancialCloseControlTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','달러','1500',2)")

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def _post(self, conn, scope, key, currency, places, units, entity='one', kind='entity'):
        for account in ('cash', 'revenue'):
            AccountingJournal.register_mapping(conn, scope, account, account)
        return AccountingJournal.post(
            conn, ledger_scope=scope, entity_type=kind, entity_id=entity, source_key=key,
            currency_code=currency, decimal_places=places, recorded_epoch=10,
            lines=[('cash', 'debit', units), ('revenue', 'credit', units)])

    def _trial(self, conn):
        return AccountingJournal.consolidated_trial_balance(conn)

    def test_trial_balance_is_grouped_by_scope_currency_and_scale(self):
        with self.db.atomic() as conn:
            self._post(conn, 'close_a', 'k1', 'NWX', 0, 100)
            self._post(conn, 'close_a', 'k2', 'NWX', 0, 50, entity='two')
            self._post(conn, 'close_a', 'k3', 'USD', 2, 123)
            self._post(conn, 'close_b', 'k1', 'USD', 2, 7)
            trial = self._trial(conn)
        rows = {(r['ledger_scope'], r['currency_code'], r['decimal_places']): r for r in trial['totals']
                if r['ledger_scope'].startswith('close_')}
        self.assertEqual(set(rows), {('close_a', 'NWX', 0), ('close_a', 'USD', 2), ('close_b', 'USD', 2)})
        nwx = rows[('close_a', 'NWX', 0)]
        self.assertEqual((nwx['entry_count'], nwx['entity_count'], nwx['debit_units'],
                          nwx['credit_units'], nwx['difference_units']), (2, 2, 150, 150, 0))
        accounts = {(r['ledger_scope'], r['currency_code'], r['account_code']): r['balance_units']
                    for r in trial['accounts'] if r['ledger_scope'].startswith('close_')}
        self.assertEqual(accounts[('close_a', 'NWX', 'cash')], 150)
        self.assertEqual(accounts[('close_a', 'NWX', 'revenue')], -150)
        self.assertEqual(accounts[('close_a', 'USD', 'cash')], 123)
        self.assertEqual(accounts[('close_b', 'USD', 'revenue')], -7)
        self.assertEqual(trial['status'], 'matched', trial['findings'])

    def test_unbalanced_and_lineless_entries_in_any_scope_fail_the_close(self):
        with self.db.atomic() as conn:
            self._post(conn, 'close_a', 'ok', 'NWX', 0, 100)
            conn.execute("""INSERT INTO accounting_entries
                (ledger_scope,entity_type,entity_id,source_key,currency_code,decimal_places,recorded_epoch)
                VALUES ('close_z','entity','one','empty','NWX',0,10),('close_z','entity','one','lopsided','NWX',0,10)""")
            conn.execute("""INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
                SELECT entry_id,'cash',9,0 FROM accounting_entries WHERE source_key='lopsided'""")
            trial = self._trial(conn)
            report = AccountingJournal.reconcile_all_financial_ledgers(conn)
        statuses = sorted(f['status'] for f in trial['findings'] if f.get('ledger_scope') == 'close_z')
        self.assertEqual(statuses.count('unbalanced_entry'), 2, trial['findings'])
        self.assertEqual(trial['status'], 'mismatch')
        self.assertEqual(report['status'], 'mismatch')
        self.assertTrue(any(f['scope'] == 'common_trial_balance' and f['status'] == 'unbalanced_entry'
                            for f in report['findings']), report['findings'])

    def test_scale_that_disagrees_with_the_currency_or_is_mixed_is_reported(self):
        with self.db.atomic() as conn:
            self._post(conn, 'close_a', 'ok', 'USD', 2, 100)
            conn.execute("""INSERT INTO accounting_entries
                (ledger_scope,entity_type,entity_id,source_key,currency_code,decimal_places,recorded_epoch)
                VALUES ('close_a','entity','one','wrong-scale','USD',0,10),
                       ('close_a','entity','one','unknown-currency','ZZZ',2,10)""")
            for key in ('wrong-scale', 'unknown-currency'):
                conn.execute("""INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
                    SELECT entry_id,'cash',5,0 FROM accounting_entries WHERE source_key=?""", (key,))
                conn.execute("""INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
                    SELECT entry_id,'revenue',0,5 FROM accounting_entries WHERE source_key=?""", (key,))
            trial = self._trial(conn)
        found = {(f['status'], f.get('currency_code')) for f in trial['findings']}
        self.assertIn(('scale_mismatch', 'USD'), found, trial['findings'])
        self.assertIn(('unknown_currency', 'ZZZ'), found, trial['findings'])
        self.assertIn(('mixed_scale', 'USD'), found, trial['findings'])

    def test_orphan_lines_unmapped_accounts_and_duplicate_sources_are_reported(self):
        with self.assertRaises(_Rollback), self.db.atomic() as conn:
            self._post(conn, 'close_a', 'dup', 'NWX', 0, 100, kind='entity')
            self._post(conn, 'close_a', 'dup', 'NWX', 0, 100, kind='other_kind')
            entry = self._post(conn, 'close_a', 'unmapped', 'NWX', 0, 5)
            conn.execute("INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units) "
                         "VALUES (?,'ghost_account',3,0),(?,'ghost_account',0,3)", (entry, entry))
            # A line without its entry can only come from an externally edited database;
            # defer the foreign key so the report can be read before this rolls back.
            if pg_backend.enabled():
                # PostgreSQL checks the key at once; skip it (and triggers) for this transaction only.
                conn.execute("SET LOCAL session_replication_role = replica")
            else:
                conn.execute("PRAGMA defer_foreign_keys=ON")
            conn.execute("INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units) "
                         "VALUES (987654321,'cash',1,0)")
            trial = self._trial(conn)
            raise _Rollback()
        statuses = {f['status'] for f in trial['findings']}
        self.assertTrue({'duplicate_source_key', 'unmapped_account', 'orphan_line'} <= statuses, trial['findings'])
        duplicate = next(f for f in trial['findings'] if f['status'] == 'duplicate_source_key')
        self.assertEqual((duplicate['ledger_scope'], duplicate['source_key'], duplicate['entry_count']),
                         ('close_a', 'dup', 2))

    def _close(self, conn, scope, currency='NWX'):
        return AccountingJournal.close_period(
            conn, ledger_scope=scope, entity_id='one', currency_code=currency,
            period_start_epoch=0, period_end_epoch=100, actor_id='admin', closed_epoch=101)

    def test_period_close_is_refused_while_the_scope_has_integrity_findings(self):
        with self.db.atomic() as conn:
            self._post(conn, 'close_ok', 'k1', 'NWX', 0, 100)
            self.assertGreater(self._close(conn, 'close_ok'), 0)
            # An account the scope has no mapping for.
            entry = self._post(conn, 'close_bad', 'k1', 'NWX', 0, 100)
            conn.execute("INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units) "
                         "VALUES (?,'ghost_account',3,0),(?,'ghost_account',0,3)", (entry, entry))
            with self.assertRaisesRegex(ValueError, 'unmapped_account'):
                self._close(conn, 'close_bad')
            # The same source posted under two entity types.
            self._post(conn, 'close_dup', 'k1', 'NWX', 0, 100, kind='entity')
            self._post(conn, 'close_dup', 'k1', 'NWX', 0, 100, kind='other_kind')
            with self.assertRaisesRegex(ValueError, 'duplicate_source_key'):
                self._close(conn, 'close_dup')
            # A scale that disagrees with the currency's configured minor unit.
            conn.execute("""INSERT INTO accounting_entries
                (ledger_scope,entity_type,entity_id,source_key,currency_code,decimal_places,recorded_epoch)
                VALUES ('close_scale','entity','one','k1','USD',0,10)""")
            for account in ('cash', 'revenue'):
                AccountingJournal.register_mapping(conn, 'close_scale', account, account)
            conn.execute("""INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
                SELECT entry_id,'cash',5,0 FROM accounting_entries WHERE ledger_scope='close_scale'""")
            conn.execute("""INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
                SELECT entry_id,'revenue',0,5 FROM accounting_entries WHERE ledger_scope='close_scale'""")
            with self.assertRaisesRegex(ValueError, 'scale_mismatch'):
                self._close(conn, 'close_scale', 'USD')
            # Findings in another scope do not block a clean one, and a replayed close is unchanged.
            self._post(conn, 'close_ok2', 'k1', 'NWX', 0, 100)
            first = self._close(conn, 'close_ok2')
            self.assertEqual(self._close(conn, 'close_ok2'), first)

    def _duplicate_lines(self, conn, entry, copies):
        """Repeat an entry's lines the way the damaged production rows look."""
        for _ in range(copies):
            conn.execute("""INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
                SELECT entry_id,account_code,debit_units,credit_units FROM accounting_lines
                WHERE entry_id=? AND line_id IN (SELECT MIN(line_id) FROM accounting_lines
                                                WHERE entry_id=? GROUP BY account_code)""", (entry, entry))

    def test_repeated_lines_inside_one_entry_are_reported_even_though_they_balance(self):
        with self.db.atomic() as conn:
            clean = self._post(conn, 'close_a', 'clean', 'NWX', 0, 100)
            damaged = self._post(conn, 'close_a', 'damaged', 'NWX', 0, 40)
            self._duplicate_lines(conn, damaged, 3)
            trial = self._trial(conn)
            duplicates = [f for f in trial['findings'] if f['status'] == 'duplicate_line']
            self.assertEqual([(f['entry_id'], f['line_count'], f['account_count']) for f in duplicates],
                             [(damaged, 8, 2)])
            self.assertNotIn(clean, [f['entry_id'] for f in duplicates])
            with self.assertRaisesRegex(ValueError, 'duplicate_line'):
                self._close(conn, 'close_a')

    def test_repair_removes_exact_repeats_only_and_keeps_an_audit_row(self):
        with self.db.atomic() as conn:
            damaged = self._post(conn, 'close_a', 'damaged', 'NWX', 0, 40)
            self._duplicate_lines(conn, damaged, 3)
            uneven = self._post(conn, 'close_a', 'uneven', 'NWX', 0, 9)
            conn.execute("INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units) "
                         "VALUES (?,'cash',9,0)", (uneven,))          # cannot be explained as a whole repeat
            preview = AccountingJournal.repair_duplicated_lines(conn, actor_id='admin', now=50, dry_run=True)
            self.assertEqual((preview['repairable_entries'], preview['lines_to_remove'],
                              preview['skipped_entries']), (1, 6, [uneven]))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounting_lines WHERE entry_id=?",
                                          (damaged,)).fetchone()[0], 8)   # a dry run changes nothing
            done = AccountingJournal.repair_duplicated_lines(conn, actor_id='admin', now=50)
            self.assertEqual((done['repaired_entries'], done['lines_removed']), (1, 6))
            lines = sorted(tuple(row) for row in conn.execute(
                "SELECT account_code,debit_units,credit_units FROM accounting_lines WHERE entry_id=?", (damaged,)))
            self.assertEqual(lines, [('cash', 40, 0), ('revenue', 0, 40)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounting_lines WHERE entry_id=?",
                                          (uneven,)).fetchone()[0], 3)    # left for a person to judge
            audit = conn.execute("SELECT entry_id,lines_before,lines_after,actor_id FROM accounting_line_repairs").fetchall()
            self.assertEqual([tuple(row) for row in audit], [(damaged, 8, 2, 'admin')])
            # Running it again finds nothing left to do, and the ledger is append-only again.
            again = AccountingJournal.repair_duplicated_lines(conn, actor_id='admin', now=51)
            self.assertEqual((again['repaired_entries'], again['lines_removed']), (0, 0))
            with self.assertRaisesRegex(Exception, 'append-only'):
                conn.execute("DELETE FROM accounting_lines WHERE entry_id=?", (damaged,))

    def test_repair_refuses_entries_inside_a_closed_period(self):
        with self.db.atomic() as conn:
            self._post(conn, 'close_ok', 'k1', 'NWX', 0, 100)
            self._close(conn, 'close_ok')
            entry = conn.execute("SELECT entry_id FROM accounting_entries WHERE ledger_scope='close_ok'").fetchone()[0]
            conn.execute("DROP TRIGGER trg_accounting_lines_closed_insert")
            self._duplicate_lines(conn, entry, 1)
            result = AccountingJournal.repair_duplicated_lines(conn, actor_id='admin', now=200)
        self.assertEqual((result['repaired_entries'], result['skipped_entries']), (0, [entry]))

    def _customer_check(self, conn):
        return next(item for item in AccountingJournal.reconcile_subledgers(conn)
                    if item['ledger_scope'] == 'customer')

    def test_rebaseline_labels_unexplained_wallet_differences_without_inventing_history(self):
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code) VALUES ('미국','USD')")
        self.db.users.create_user('mover', 'Mover', '낙원', 500)
        self.db.users.create_user('steady', 'Steady', '낙원', 70)
        with self.db.atomic() as conn:
            self.assertEqual(self._customer_check(conn)['status'], 'matched')
            # A direct nationality edit: the same cash is now read in another currency.
            conn.execute("UPDATE users SET nationality='미국' WHERE user_id='mover'")
            broken = self._customer_check(conn)
            self.assertEqual((broken['balance_gaps'], broken['orphan_balance_gaps']), (1, 1))
            preview = AccountingJournal.rebaseline_customer_wallets(
                conn, actor_id='admin', now=900, reason='direct nationality edit', dry_run=True)
            self.assertEqual((preview['adjustments'], preview['orphans']), (2, 1))
            self.assertEqual(self._customer_check(conn)['balance_gaps'], 1)      # a dry run changes nothing
            done = AccountingJournal.rebaseline_customer_wallets(
                conn, actor_id='admin', now=900, reason='direct nationality edit')
            self.assertEqual((done['adjustments'], done['orphans']), (2, 1))
            fixed = self._customer_check(conn)
            self.assertEqual((fixed['status'], fixed['balance_gaps'], fixed['orphan_balance_gaps']),
                             ('matched', 0, 0), fixed)
            self.assertEqual(fixed['baseline_adjustments'], 2)
            rows = sorted(tuple(row) for row in conn.execute(
                "SELECT user_id,currency_code,ledger_before_minor_units,balance_minor_units,"
                "difference_minor_units,orphan,actor_id,reason FROM customer_wallet_baseline_adjustments"))
            self.assertEqual(rows, [
                ('mover', 'NWX', 500, 0, -500, 1, 'admin', 'direct nationality edit'),
                # The edit rescaled 500 whole units into 50,000 cents, logging only the 49,500 increase.
                ('mover', 'USD', 49500, 50000, 500, 0, 'admin', 'direct nationality edit')])
            # Nothing about the untouched wallet changed, the balances themselves never moved,
            # and a second run has nothing to do.
            self.assertEqual(conn.execute("SELECT cash_minor FROM users WHERE user_id='mover'").fetchone()[0], 50000)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM customer_wallet_ledger WHERE user_id='steady'").fetchone()[0], 1)
            again = AccountingJournal.rebaseline_customer_wallets(conn, actor_id='admin', now=901, reason='again')
            self.assertEqual(again['adjustments'], 0)
            self.assertEqual(AccountingJournal.audit_unbalanced(conn, 'customer'), [])
            with self.assertRaisesRegex(Exception, 'append-only'):
                conn.execute("DELETE FROM customer_wallet_baseline_adjustments")

    def test_rebaseline_needs_a_reason(self):
        with self.db.atomic() as conn, self.assertRaisesRegex(ValueError, '사유'):
            AccountingJournal.rebaseline_customer_wallets(conn, actor_id='admin', now=1, reason=' ')

    def _records_check(self, conn):
        return next(item for item in AccountingJournal.reconcile_subledgers(conn)
                    if item['ledger_scope'] == 'settlement_records')

    def _settlement(self, conn, user, product, kind, gross, tax, epoch=5):
        conn.execute("INSERT INTO product_settlements(user_id,product_code,kind,quantity,gross_amount,tax_amount,"
                     "recorded_epoch) VALUES (?,?,?,?,?,?,?)", (user, product, kind, 1, gross, tax, epoch))

    def test_settlement_log_must_mirror_the_payment_records_it_restates(self):
        with self.db.atomic() as conn:
            conn.execute("""INSERT INTO corporate_liquidation_payments
                (operation_key,issuer_code,asset_code,user_id,kind,quantity,claim_amount,paid_amount,tax_amount,recorded_epoch)
                VALUES ('op','CO','PR','u1','preferred',1,100,100,3,5)""")
            self._settlement(conn, 'u1', 'PR', 'liquidation:preferred', 100, 3)
            conn.execute("""INSERT INTO els_settlements
                (product_code,sequence,subscription_id,user_id,quantity,principal,gross_amount,tax_amount,net_amount,recorded_epoch)
                VALUES ('ELS',1,1,'u1',1,90,100,2,98,5)""")
            self._settlement(conn, 'u1', 'ELS', 'els_early', 100, 2)
            self._settlement(conn, 'u1', 'CO', 'special_dividend:NWX', 50, 1)   # not mirrored, only counted
            check = self._records_check(conn)
            self.assertEqual(check['status'], 'matched', check)
            self.assertEqual((check['mirrored_settlements'], check['unverified_settlements']), (2, 1))
            # A log row with no payment behind it, and one whose amounts differ.
            self._settlement(conn, 'u2', 'PR', 'liquidation:preferred', 7, 0)
            self._settlement(conn, 'u1', 'ELS', 'els_early', 100, 9)
            self._settlement(conn, 'u1', 'CO', 'special_dividend:NWX', 5, 6)
            check = self._records_check(conn)
        self.assertEqual(sorted(gap['status'] for gap in check['gaps']),
                         ['settlement_tax_exceeds_gross', 'settlement_without_payment', 'settlement_without_payment'])

    def test_preferred_arrears_claims_must_be_explained_by_their_payments(self):
        with self.db.atomic() as conn:
            conn.execute("""INSERT INTO preferred_dividend_claims
                (issuer_code,asset_code,distribution_id,user_id,quantity,gross_amount,paid_amount,quote_currency,
                 funding_source,due_epoch,created_epoch,status)
                VALUES ('CO','PR',1,'u1',1,100,60,'NWX','issuer',1,1,'partial')""")
            claim = conn.execute("SELECT claim_id FROM preferred_dividend_claims").fetchone()[0]
            self._settlement(conn, 'u1', 'PR', f'preferred_arrears:{claim}:NWX', 40, 0)
            conn.execute("""INSERT INTO corporate_liquidation_payments
                (operation_key,issuer_code,asset_code,user_id,kind,quantity,claim_amount,paid_amount,tax_amount,recorded_epoch)
                VALUES ('op','CO','PR','u1','arrears',1,60,20,0,6)""")
            self._settlement(conn, 'u1', 'PR', 'liquidation:arrears', 20, 0, epoch=6)
            self.assertEqual(self._records_check(conn)['status'], 'matched', self._records_check(conn))
            conn.execute("UPDATE preferred_dividend_claims SET paid_amount=75")
            check = self._records_check(conn)
            self.assertEqual([gap['status'] for gap in check['gaps']], ['claim_paid_amount_unexplained'])
            conn.execute("UPDATE preferred_dividend_claims SET paid_amount=60,status='paid'")
            check = self._records_check(conn)
        self.assertEqual([gap['status'] for gap in check['gaps']], ['claim_status_mismatch'])

    def test_company_income_rows_posted_one_by_one_must_match_their_entry(self):
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO financial_entities(company_code,currency,decimal_places,opened_epoch) "
                         "VALUES ('CO','NWX',0,1)")
            for kind, gross, tax, booked in (('dividend', 100, 10, 1), ('interest_expense', 30, 0, 1),
                                             ('transfer_in', 5, 0, 0), ('dividend', 8, 1, 1)):
                conn.execute("INSERT INTO company_income_log(company_code,kind,gross_nwx,tax_nwx,booked,recorded_epoch) "
                             "VALUES ('CO',?,?,?,?,1)", (kind, gross, tax, booked))
            ids = [row[0] for row in conn.execute("SELECT entry_id FROM company_income_log ORDER BY entry_id")]

            def post(log_id, lines):
                entry = conn.execute("INSERT INTO financial_entries(company_code,period,recorded_epoch,source_key,"
                                     "description,cash_flow,status) VALUES ('CO','2026-01',1,?,'x','operating','draft')",
                                     (f'income-log:{log_id}',)).lastrowid
                conn.executemany("INSERT INTO financial_lines(entry_id,account,amount) VALUES (?,?,?)",
                                 [(entry, account, amount) for account, amount in lines])
                conn.execute("UPDATE financial_entries SET status='posted' WHERE entry_id=?", (entry,))
            post(ids[0], [('cash', 90), ('other_income', -100), ('income_tax', 10)])
            post(ids[1], [('cash', -30), ('interest_expense', 30)])
            check = self._records_check(conn)
            self.assertEqual(check['status'], 'matched', check)
            # Pending rows wait; a booked row without its own entry was booked in aggregate.
            self.assertEqual((check['income_rows_matched'], check['income_rows_pending'],
                              check['income_rows_booked_in_aggregate']), (2, 1, 1))
            post(ids[3], [('cash', 99), ('other_income', -100), ('income_tax', 1)])
            check = self._records_check(conn)
        self.assertEqual([gap['status'] for gap in check['gaps']], ['income_entry_mismatch'])

    def _issuer_check(self, conn):
        return next(item for item in AccountingJournal.reconcile_subledgers(conn)
                    if item['ledger_scope'] == 'issuer_cash')

    def test_issuer_cash_reconciles_to_the_company_account_wallet_and_its_transfer_log(self):
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO users(user_id,nickname,nationality,cash,status,role) "
                         "VALUES ('__company__:CO','Co','낙원',0,'active','company')")
            conn.execute("INSERT INTO company_accounts(company_code,user_id) VALUES ('CO','__company__:CO')")
            conn.execute("INSERT INTO issuer_cash_accounts(company_code) VALUES ('CO'),('LONE')")
            conn.execute("UPDATE issuer_cash_accounts SET balance=700 WHERE company_code='CO'")
            conn.execute("INSERT INTO issuer_cash_transfers(company_code,actor_id,old_balance,new_balance,delta,"
                         "recorded_epoch) VALUES ('CO','admin',0,700,700,1)")
            check = self._issuer_check(conn)
            self.assertEqual(check['status'], 'matched', check)
            # The trigger mirrored the balance into the company's wallet; LONE has no account to mirror to.
            self.assertEqual((check['account_count'], check['without_company_account']), (2, 1))
            # Balance and wallet are kept in step by triggers; only a bypass can separate them.
            conn.execute("DROP TRIGGER issuer_cash_to_company")
            conn.execute("UPDATE issuer_cash_accounts SET balance=balance+5 WHERE company_code='CO'")
            conn.execute("INSERT INTO issuer_cash_transfers(company_code,actor_id,old_balance,new_balance,delta,"
                         "recorded_epoch) VALUES ('CO','admin',700,900,100,2)")
            check = self._issuer_check(conn)
        self.assertEqual({gap['status'] for gap in check['gaps']},
                         {'company_account_cash_mismatch', 'transfer_arithmetic_mismatch'}, check)

    def test_every_registered_amount_table_is_classified_and_unconnected_ones_block_close(self):
        with self.db.readonly() as conn:
            coverage = AccountingJournal.consumer_coverage(conn)
            report = AccountingJournal.reconcile_all_financial_ledgers(conn)
        self.assertEqual(coverage['unclassified'], [])
        self.assertIn('users', {row['source_table'] for row in coverage['connected']})
        self.assertIn('market_price_history', {row['source_table'] for row in coverage['reference']})
        unconnected = {row['source_table']: row['reason'] for row in coverage['unconnected']}
        self.assertEqual(unconnected, {})
        self.assertNotIn('hybrid_bond_events', unconnected)
        self.assertNotIn('futures_positions', unconnected)
        self.assertNotIn('els_settlements', unconnected)
        self.assertTrue(all(unconnected.values()))
        # Every connected table names a check that the report really runs.
        ran = set(report['coverage']) | {row['ledger_scope'] for row in report['common_journal_checks']}
        for row in coverage['connected']:
            self.assertTrue(set(row['checks']) <= ran, row)
        # PostgreSQL has no amount-snapshot inventory yet and reports that as a standing finding.
        self.assertEqual([f for f in report['findings'] if f.get('status') != 'missing_inventory'], [])
        # Every table is connected now, so readiness follows the findings alone.
        self.assertIs(report['close_ready'], not report['findings'])
        self.assertEqual(report['consumer_coverage'], coverage)
        self.assertIn('common_trial_balance', report['coverage'])
        self.assertIn('treasury_bond_cash', report['coverage'])
        self.assertIn('fx_revaluation', report['coverage'])
        self.assertEqual(report['treasury_bond_cash']['status'], 'matched')
        self.assertEqual(report['fx_revaluation']['status'], 'preview')
        self.assertIn('consumer_coverage', report['coverage'])

    def test_a_new_registered_amount_table_must_be_classified(self):
        from unittest import mock
        with self.db.atomic() as conn:
            if pg_backend.enabled():
                # PostgreSQL has no registry table; the code registry it is built from stands in.
                from database.money_columns import REGISTERED
                with mock.patch.dict(REGISTERED, {'brand_new_money_table': ('amount',)}):
                    coverage = AccountingJournal.consumer_coverage(conn)
            else:
                conn.execute("""INSERT INTO financial_amount_field_registry
                    (source_table,source_key_field,amount_field,currency_expression)
                    VALUES ('brand_new_money_table','id','amount','''NWX''')""")
                coverage = AccountingJournal.consumer_coverage(conn)
        self.assertEqual(coverage['unclassified'], ['brand_new_money_table'])


if __name__ == '__main__':
    unittest.main()
