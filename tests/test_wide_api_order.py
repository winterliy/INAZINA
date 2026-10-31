from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from web_api import server


def test_order_accepts_exact_string_price():
    book=Mock()
    book.place_limit_order.return_value=(True,'ok')
    with patch.object(server,'domain',return_value=SimpleNamespace(order_book=book)):
        assert server.execute_order('user',{'assetCode':'BIG','side':'buy','quantity':1,'limitPrice':str(10**18+1)})==(True,'ok')
    assert book.place_limit_order.call_args.args==('user','BIG','buy',1,10**18+1)


@pytest.mark.parametrize('price',[True,'NaN','Infinity','0','-1',str(10**24+1)])
def test_order_rejects_invalid_price(price):
    with pytest.raises((ValueError,ArithmeticError)):
        server.execute_order('user',{'assetCode':'BIG','side':'buy','quantity':1,'limitPrice':price})


def test_large_fractional_quote_keeps_minor_unit():
    from decimal import Decimal
    book=Mock();book.place_limit_order.return_value=(True,'ok')
    with patch.object(server,'domain',return_value=SimpleNamespace(order_book=book)):
        server.execute_order('user',{'assetCode':'BIG','side':'buy','quantity':1,'limitPrice':'1000000000000000.01'})
    assert book.place_limit_order.call_args.args[-1] == Decimal('1000000000000000.01')
