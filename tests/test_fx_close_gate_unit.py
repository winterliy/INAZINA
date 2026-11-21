from unittest.mock import patch

from managers.accounting_journal import AccountingJournal


class EmptyConnection:
    def execute(self, *args, **kwargs):
        return self

    def __iter__(self):
        return iter(())

    def fetchall(self):
        return []


def test_unavailable_fx_revaluation_blocks_financial_close():
    clean = {'unclassified': [], 'unconnected': []}
    with patch('functions.money.audit_amount_snapshots', return_value=[]), \
         patch('functions.money.audit_amount_field_coverage', return_value=clean), \
         patch.object(AccountingJournal, 'reconcile_subledgers', return_value=[{'ledger_scope': 'treasury_bond_cash', 'status': 'matched'}]), \
         patch.object(AccountingJournal, 'consolidated_trial_balance', return_value={'findings': []}), \
         patch.object(AccountingJournal, 'consumer_coverage', return_value=clean), \
         patch('managers.fx_position.FxPosition.preview', side_effect=ValueError('missing rate')):
        report = AccountingJournal.reconcile_all_financial_ledgers(EmptyConnection())
    assert report['fx_revaluation']['status'] == 'unavailable'
    assert report['status'] == 'mismatch'
    assert not report['close_ready']
    assert any(item['scope'] == 'fx_revaluation' for item in report['findings'])


def test_missing_treasury_check_blocks_financial_close():
    clean = {'unclassified': [], 'unconnected': []}
    with patch('functions.money.audit_amount_snapshots', return_value=[]), \
         patch('functions.money.audit_amount_field_coverage', return_value=clean), \
         patch.object(AccountingJournal, 'reconcile_subledgers', return_value=[]), \
         patch.object(AccountingJournal, 'consolidated_trial_balance', return_value={'findings': []}), \
         patch.object(AccountingJournal, 'consumer_coverage', return_value=clean), \
         patch('managers.fx_position.FxPosition.preview', return_value=[]), \
         patch('managers.fx_position.FxPosition.close_check', return_value={'status': 'matched', 'findings': []}):
        report = AccountingJournal.reconcile_all_financial_ledgers(EmptyConnection())
    assert report['treasury_bond_cash']['status'] == 'unavailable'
    assert not report['close_ready']
    assert any(item['scope'] == 'treasury_bond_cash' for item in report['findings'])


def test_pending_fx_posting_blocks_financial_close():
    clean = {'unclassified': [], 'unconnected': []}
    with patch('functions.money.audit_amount_snapshots', return_value=[]), \
         patch('functions.money.audit_amount_field_coverage', return_value=clean), \
         patch.object(AccountingJournal, 'reconcile_subledgers', return_value=[{'ledger_scope': 'treasury_bond_cash', 'status': 'matched'}]), \
         patch.object(AccountingJournal, 'consolidated_trial_balance', return_value={'findings': []}), \
         patch.object(AccountingJournal, 'consumer_coverage', return_value=clean), \
         patch('managers.fx_position.FxPosition.preview', return_value=[]), \
         patch('managers.fx_position.FxPosition.close_check', return_value={
             'status': 'mismatch', 'findings': [{'currency': 'USD', 'status': 'missing_run'}]}):
        report = AccountingJournal.reconcile_all_financial_ledgers(EmptyConnection())
    assert not report['close_ready']
    assert any(item['scope'] == 'fx_revaluation' and item['status'] == 'missing_run'
               for item in report['findings'])
