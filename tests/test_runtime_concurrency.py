import asyncio
import threading
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from main import InazinaBot
from managers.user_manager import UserManager
from web_api.server import BoundedThreadingHTTPServer, SingleFlightTTL


class RuntimeConcurrencyTests(unittest.TestCase):
    def test_background_mutations_are_serialized(self):
        async def run():
            state = {"active": 0, "maximum": 0}
            state_lock = threading.Lock()

            def mutation():
                with state_lock:
                    state["active"] += 1
                    state["maximum"] = max(state["maximum"], state["active"])
                time.sleep(0.03)
                with state_lock:
                    state["active"] -= 1

            bot = SimpleNamespace(_background_write_lock=asyncio.Lock())
            await asyncio.gather(*(
                InazinaBot._run_background_write(bot, mutation) for _ in range(4)
            ))
            self.assertEqual(state["maximum"], 1)

        asyncio.run(run())

    def test_http_server_has_bounded_request_slots(self):
        with patch.object(BoundedThreadingHTTPServer, "server_bind"), \
             patch.object(BoundedThreadingHTTPServer, "server_activate"):
            server = BoundedThreadingHTTPServer(
                ("127.0.0.1", 0), lambda *args: None, max_threads=3
            )
        try:
            self.assertEqual(server._request_slots._value, 3)
        finally:
            server.server_close()

    def test_singleflight_runs_one_loader_for_concurrent_reads(self):
        cache = SingleFlightTTL(ttl_seconds=1)
        calls = 0
        calls_lock = threading.Lock()
        barrier = threading.Barrier(5)

        def loader():
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.03)
            return {"value": 7}

        def read():
            barrier.wait()
            return cache.get(("portfolio", "user"), loader)

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: read(), range(5)))
        self.assertEqual(results, [{"value": 7}] * 5)
        self.assertEqual(calls, 1)

        cache.invalidate(("portfolio", "user"))
        self.assertEqual(cache.get(("portfolio", "user"), loader), {"value": 7})
        self.assertEqual(calls, 2)

    def test_portfolio_catalog_is_one_query_and_preserves_collision_detection(self):
        rows = [
            {"code": "A", "name": "주식", "price": 101.6, "type": "stock",
             "quote_currency": "NWX", "source": "asset"},
            {"code": "DUP", "name": "중복 주식", "price": 10, "type": "stock",
             "quote_currency": "NWX", "source": "asset"},
            {"code": "DUP", "name": None, "price": None, "type": None,
             "quote_currency": None, "source": "saving"},
        ]
        executed = []
        def execute(sql, parameters):
            executed.append((sql, parameters))
            return SimpleNamespace(fetchall=lambda: rows)
        connection = SimpleNamespace(execute=execute)
        calls = 0

        @contextmanager
        def readonly():
            nonlocal calls
            calls += 1
            yield connection

        manager = object.__new__(UserManager)
        manager.db = SimpleNamespace(readonly=readonly)
        catalog = manager._portfolio_asset_catalog("user")
        self.assertEqual(calls, 1)
        self.assertEqual(executed[0][1], ("user",) * 4)
        self.assertIn("WITH needed", executed[0][0])
        self.assertEqual(catalog["A"]["price"], 102)
        self.assertIsNone(catalog["DUP"])


if __name__ == "__main__":
    unittest.main()
