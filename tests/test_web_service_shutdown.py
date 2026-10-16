import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Runs the real main() with domain() stubbed so no database is touched.
LAUNCHER = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[1])
    from web_api import server
    server.domain = lambda: None
    server.INTERNAL_TOKEN = "test"
    server.PORT = int(sys.argv[2])
    server.main()
""")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServiceShutdownTests(unittest.TestCase):
    def run_and_stop(self, signum):
        port = free_port()
        process = subprocess.Popen([sys.executable, "-c", LAUNCHER, str(ROOT), str(port)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={**os.environ, "PYTHONUNBUFFERED": "1"})
        deadline = time.time() + 15
        while time.time() < deadline:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        else:
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
