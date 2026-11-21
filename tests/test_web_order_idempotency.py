"""The order endpoint's clientOrderId idempotency key (a retried request must not place a second order)."""
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from database import DatabaseManager
from database.db_manager import DBManager
from web_api import server

ORDER = {"assetCode": "BIG", "side": "buy", "quantity": 1, "limitPrice": "100"}
KEY = "order-key-0001"


@pytest.fixture
def book():
    previous = DBManager.database_dir
    with tempfile.TemporaryDirectory() as root:
        DBManager.database_dir = Path(root)
        db = DatabaseManager()
        order_book = Mock()
        order_book.place_limit_order.return_value = (True, "접수됨")
        try:
            with patch.object(server, "domain", return_value=SimpleNamespace(order_book=order_book)):
                yield order_book
        finally:
            db.close()
            DBManager.database_dir = previous


def place(**extra):
    return server.execute_order_once("user", {**ORDER, **extra})


def seed(key, *, state="pending", fingerprint="x", message="", age=0):
    """Write a request row directly, as if an earlier call had left it behind."""
    with server.connect_db(timeout=5) as connection:
        server.ensure_order_requests_table(connection)
        connection.execute(
            "INSERT INTO web_order_requests(user_id,client_order_id,fingerprint,state,message,created_epoch) "
            "VALUES ('user',?,?,?,?,?)", (key, fingerprint, state, message, int(time.time()) - age))


def test_without_a_key_every_call_places_an_order(book):
    assert place() == (True, "접수됨", False)
    assert place() == (True, "접수됨", False)
    assert book.place_limit_order.call_count == 2


def test_same_key_replays_the_first_answer_instead_of_ordering_again(book):
    assert place(clientOrderId=KEY) == (True, "접수됨", False)
    book.place_limit_order.return_value = (True, "다른 답")
    assert place(clientOrderId=KEY) == (True, "접수됨", True)
    assert book.place_limit_order.call_count == 1


def test_a_rejected_order_forgets_the_key_so_the_user_can_fix_and_resend(book):
    book.place_limit_order.return_value = (False, "잔액이 부족합니다.")
    assert place(clientOrderId=KEY) == (False, "잔액이 부족합니다.", False)
    book.place_limit_order.return_value = (True, "접수됨")
    assert place(clientOrderId=KEY) == (True, "접수됨", False)
    assert book.place_limit_order.call_count == 2


def test_an_error_while_ordering_forgets_the_key(book):
    book.place_limit_order.side_effect = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        place(clientOrderId=KEY)
    book.place_limit_order.side_effect = None
    assert place(clientOrderId=KEY) == (True, "접수됨", False)


def test_a_key_still_in_progress_is_refused(book):
    seed(KEY, fingerprint='["BIG","buy",1,"100",null]')
    with pytest.raises(server.OrderInProgress):
        place(clientOrderId=KEY)
    book.place_limit_order.assert_not_called()


def test_a_stale_pending_claim_from_a_crash_is_reclaimed(book):
    seed(KEY, age=server.ORDER_REQUEST_PENDING_TTL + 5)
    assert place(clientOrderId=KEY) == (True, "접수됨", False)


def test_the_same_key_for_a_different_order_is_refused(book):
    place(clientOrderId=KEY)
    with pytest.raises(ValueError, match="다른 주문"):
        place(clientOrderId=KEY, quantity=2)
    assert book.place_limit_order.call_count == 1


def test_keys_are_scoped_to_the_account(book):
    server.execute_order_once("alice", {**ORDER, "clientOrderId": KEY})
    assert server.execute_order_once("bob", {**ORDER, "clientOrderId": KEY})[2] is False
    assert book.place_limit_order.call_count == 2


@pytest.mark.parametrize("key", ["", "short", "has space in it", "x" * 65, 12345678, True])
def test_a_malformed_key_is_rejected_before_anything_runs(book, key):
    with pytest.raises(ValueError, match="주문 요청 번호"):
        place(clientOrderId=key)
    book.place_limit_order.assert_not_called()


def test_old_finished_keys_are_cleaned_up(book):
    seed("old-key-0001", state="done", message="옛 주문", age=server.ORDER_REQUEST_TTL + 5)
    place(clientOrderId=KEY)
    with server.connect_db(timeout=5) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM web_order_requests WHERE client_order_id='old-key-0001'").fetchone()[0] == 0
