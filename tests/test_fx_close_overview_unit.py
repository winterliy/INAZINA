from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from web_api import server


def test_overview_marks_stale_fx_run_as_not_ready():
    conn = SimpleNamespace(execute=lambda *args: [])
    domain = SimpleNamespace(db=SimpleNamespace(readonly=lambda: nullcontext(conn)))
    stale = {'period': '2026-01-01', 'status': 'mismatch',
             'findings': [{'currency': 'USD', 'status': 'stale_or_invalid_run'}]}
    with patch.object(server, 'domain', return_value=domain), \
         patch('managers.fx_residual.FxResiduals.report', return_value=[]), \
         patch('managers.fx_residual.FxResiduals.history', return_value=[]), \
         patch('managers.baseline_report.BaselineReport.collect', return_value={}), \
         patch('managers.fx_revaluation.FxRevaluation.preview', return_value=[]), \
         patch('managers.fx_revaluation.FxRevaluation.history', return_value=[]), \
         patch('managers.fx_revaluation.FxRevaluation.close_check', return_value=stale):
        result = server.accounting_close_overview()
    gate = next(item for item in result['gates'] if item['key'] == 'fx_revaluation')
    assert gate['ok'] is False
    assert result['revaluation']['closeCheck'] == stale
