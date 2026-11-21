"""Web login ids: lower-cased, 3-32 safe characters, never digits only (that shape is a Discord id)."""
import pytest

from web_api import server


@pytest.mark.parametrize("value,expected", [("Trader.One", "trader.one"), (" abc ", "abc"), ("a-b_c.1", "a-b_c.1"),
                                            ("1abc", "1abc"), ("a" * 32, "a" * 32)])
def test_valid_ids_are_normalised(value, expected):
    assert server.normalize_hts_id(value) == expected


@pytest.mark.parametrize("value", ["ab", "a" * 33, "bad id", "한글아이디", "123456", "123456789012345678", " 1234 ", None, 5])
def test_invalid_ids_are_refused(value):
    with pytest.raises(ValueError, match="hts_id_policy"):
        server.normalize_hts_id(value)
