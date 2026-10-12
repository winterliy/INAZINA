import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from main import InazinaBot
from web_api.server import BoundedThreadingHTTPServer


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


if __name__ == "__main__":
    unittest.main()
