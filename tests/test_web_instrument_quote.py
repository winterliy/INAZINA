"""GET /internal/market/quote/<code>: the latest price of one instrument, checked right before an order."""
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from database import DatabaseManager
from database.db_manager import DBManager
from web_api import server


class InstrumentQuoteTests(unittest.TestCase):
    def setUp(self):
        self.previous = DBManager.database_dir
        self.root = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.root.name)
        self.db = DatabaseManager()
        self.db.system.execute("INSERT INTO market_assets(code,name,exchange_id,price,type) VALUES ('QT','견적','QT',150,'stock')")

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.previous
        self.root.cleanup()

    def test_reads_the_latest_price_of_a_plain_instrument(self):
        quote = server.instrument_quote("QT")
        self.assertEqual(quote["code"], "QT")
        self.assertEqual(float(quote["price"]), 150.0)
        self.assertIn("asOf", quote)

    def test_follows_a_price_change_without_the_full_list(self):
        self.db.system.execute("UPDATE market_assets SET price=175 WHERE code='QT'")
        with patch.object(server, "instruments", side_effect=AssertionError("the heavy list must not be read")):
            self.assertEqual(float(server.instrument_quote("QT")["price"]), 175.0)

    def test_an_unknown_code_is_none(self):
        self.assertIsNone(server.instrument_quote("NOPE"))

    def test_an_inactive_instrument_is_none(self):
        self.db.system.execute("UPDATE market_assets SET status='delisted' WHERE code='QT'")
        self.assertIsNone(server.instrument_quote("QT"))

    def test_the_route_answers_200_and_404(self):
        for code, status in (("QT", 200), ("NOPE", 404)):
            response = {}
            handler = SimpleNamespace(headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "u"},
                                      path=f"/internal/market/quote/{code}", rfile=io.BytesIO(b""),
                                      send_json=lambda s, v, r=response: r.update(status=s, value=v))
            with patch.object(server, "INTERNAL_TOKEN", "secret"), \
                 patch.object(server, "identity", return_value={"userId": "u", "role": "user"}):
                server.Handler.do_GET(handler)
            self.assertEqual(response["status"], status, code)

    def place_current(self, limit_price):
        book = Mock()
        book.place_limit_order.return_value = (True, "접수됨")
        with patch.object(server, "domain", return_value=SimpleNamespace(order_book=book)):
            result = server.execute_order("u", {"assetCode": "QT", "side": "buy", "quantity": 1,
                                                "orderType": "current", "limitPrice": limit_price})
        return result, book

    def test_a_current_price_order_is_placed_at_the_latest_price(self):
        result, book = self.place_current("150")
        self.assertEqual(result, (True, "접수됨"))
        self.assertEqual(book.place_limit_order.call_args.args, ("u", "QT", "buy", 1, 150))

    def test_a_current_price_order_is_refused_when_the_price_moved(self):
        self.db.system.execute("UPDATE market_assets SET price=175 WHERE code='QT'")
        with self.assertRaises(server.PriceChanged) as caught:
            self.place_current("150")
        self.assertEqual(float(caught.exception.price), 175.0)

    def test_a_limit_order_keeps_its_own_price_even_if_the_market_moved(self):
        self.db.system.execute("UPDATE market_assets SET price=175 WHERE code='QT'")
        book = Mock()
        book.place_limit_order.return_value = (True, "접수됨")
        with patch.object(server, "domain", return_value=SimpleNamespace(order_book=book)):
            server.execute_order("u", {"assetCode": "QT", "side": "buy", "quantity": 1, "orderType": "limit", "limitPrice": "150"})
        self.assertEqual(book.place_limit_order.call_args.args[-1], 150)

    def test_the_order_route_answers_409_price_changed_with_the_new_price(self):
        import json
        body = json.dumps({"assetCode": "QT", "side": "buy", "quantity": 1, "orderType": "current", "limitPrice": "150"}).encode()
        response = {}
        handler = SimpleNamespace(headers={"x-inazina-internal-token": "secret", "x-inazina-user-id": "u", "content-length": str(len(body))},
                                  path="/internal/market/orders", rfile=io.BytesIO(body),
                                  send_json=lambda s, v: response.update(status=s, value=v))
        with patch.object(server, "INTERNAL_TOKEN", "secret"), \
             patch.object(server, "identity", return_value={"userId": "u", "role": "user"}), \
             patch.object(server, "trading_user_id", return_value="u"), \
             patch.object(server, "execute_order_once", side_effect=server.PriceChanged(175)):
            server.Handler.do_POST(handler)
        self.assertEqual(response["status"], 409)
        self.assertEqual((response["value"]["error"], response["value"]["price"]), ("price_changed", "175"))


if __name__ == "__main__":
    unittest.main()
