from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from web_api import server


def test_general_ratio_endpoint_preserves_both_ratio_terms():
    manager=Mock()
    manager.adjust_face_value.return_value=(True,'ok')
    bot=SimpleNamespace(db=object())
    with patch.object(server,'domain',return_value=SimpleNamespace(bot=bot)), patch('managers.corporate_actions_manager.CorporateActionsManager',return_value=manager):
        assert server.admin_company_share_action('admin','CO',{'action':'execute','mode':'ratio_split','oldShares':'2','newShares':'3'})=='ok'
    manager.adjust_face_value.assert_called_once_with('admin','분할','CO',2,mode='ratio_split',resolution_id=None,old_shares='2',new_shares='3')


@pytest.mark.parametrize('payload',[{}, {'action':'execute','mode':'ratio_split','oldShares':'2'}, {'action':'execute','mode':'ratio_split','oldShares':'2','newShares':'3','unexpected':True}])
def test_ratio_endpoint_rejects_incomplete_or_unknown_fields(payload):
    with pytest.raises(ValueError):server.admin_company_share_action('admin','CO',payload)
