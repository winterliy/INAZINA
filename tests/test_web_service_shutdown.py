import os
import selectors
import signal
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Runs the real main() with domain() and the listener stubbed. The listener has
# the same blocking serve_forever/shutdown contract without requiring loopback
# access, so these process-signal tests work in network-restricted sandboxes.
LAUNCHER = textwrap.dedent("""
    import threading
    import sys
    sys.path.insert(0, sys.argv[1])
    from web_api import server
    server.domain = lambda: None
    server.INTERNAL_TOKEN = "test"

    class LocalServerStub:
        def __init__(self, address, handler):
            self.stopped = threading.Event()
        def serve_forever(self):
            self.stopped.wait()
        def shutdown(self):
            self.stopped.set()
        def server_close(self):
            self.stopped.set()

    server.BoundedThreadingHTTPServer = LocalServerStub
    server.main()
""")


class ServiceShutdownTests(unittest.TestCase):
    def run_and_stop(self, signum):
        process = subprocess.Popen([sys.executable, "-c", LAUNCHER, str(ROOT)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={**os.environ, "PYTHONUNBUFFERED": "1"})
        deadline = time.time() + 15
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            ready = False
            while time.time() < deadline:
                if selector.select(timeout=min(0.1, max(0, deadline-time.time()))):
                    line = process.stdout.readline()
                    if "listening" in line:
                        ready = True
                        break
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(f"service exited before signal: {stdout}\n{stderr}")
        if not ready:
            process.kill()
            self.fail("service did not start")
        process.send_signal(signum)
        stdout, stderr = process.communicate(timeout=10)
        return process.returncode, stdout, stderr

    def test_ctrl_c_stops_quietly(self):
        code, stdout, stderr = self.run_and_stop(signal.SIGINT)
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("KeyboardInterrupt", stderr)
        self.assertIn("stopped", stdout)

    def test_sigterm_from_the_api_stops_quietly(self):
        code, stdout, stderr = self.run_and_stop(signal.SIGTERM)
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertIn("stopped", stdout)


if __name__ == "__main__":
    unittest.main()
