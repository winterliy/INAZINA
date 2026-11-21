"""Time HTTP requests against the Python data service started on a SCRATCH PostgreSQL clone, with per-request round trips (JSON).

    createdb -T inazina_profile_20261121 inazina_profile_e3
    INAZINA_PG_DSN=postgresql:///inazina_profile_e3 PYTHONHASHSEED=0 python tools/profile_http.py <code root> <label> [port 5460-5469]

Starts `web_api.server` as a child process of this script (only this PID is ever stopped) on a port in 5460-5469 with a throw-away token, sets a
known web password for the two users in the clone, then calls each endpoint COLD (first call after start) and WARM (median of REPEAT) and
reports ms, the number of statements and round trips the request made (the child writes counters to a file at exit through the
`INAZINA_PROFILE_COUNTERS` hook below), and a digest of each response body (volatile fields masked) so two code versions can be compared.
Refuses any DSN that is not a scratch `inazina_profile_*` database.
"""
import hashlib, json, os, re, statistics, subprocess, sys, tempfile, time, urllib.request, urllib.error

root, label = sys.argv[1], sys.argv[2]
port = int(sys.argv[3]) if len(sys.argv) > 3 else 5460
dsn = os.environ.get("INAZINA_PG_DSN", "")
assert dsn.startswith("postgresql:///inazina_profile_"), f"refusing DSN {dsn!r}"
assert 5460 <= port <= 5469, "use a port in 5460-5469"
REPEAT = int(os.environ.get("REPEAT", "7"))
TOKEN = "profile-http-token"
USER, ADMIN = os.environ.get("BUYER", "20230216"), os.environ.get("ADMIN", "1076861147560542278")
PASSWORD = "Profile-Pass-1234!"
VOLATILE = re.compile(r"\b1[78]\d{8}(?:\.\d+)?\b|\"(?:serverTime|now|elapsed\w*|token|sessionId)\":\s*\"?[^,}\"]*\"?")

HOOK = r'''
import atexit, json, os, signal, sys, time, threading
import psycopg
from collections import Counter
sys.path.insert(0, os.environ["PROFILE_ROOT"])
log, lock = [], threading.Lock()
cur_exec, cur_many = psycopg.Cursor.execute, psycopg.Cursor.executemany
state = threading.local()
def ex(self, query, params=None, **kw):
    if getattr(state, "n", None) is not None:
        state.n += 1
    return cur_exec(self, query, params, **kw)
def em(self, query, seq, **kw):
    if getattr(state, "n", None) is not None:
        state.n += 1
    return cur_many(self, query, seq, **kw)
psycopg.Cursor.execute, psycopg.Cursor.executemany = ex, em
from http.server import BaseHTTPRequestHandler
orig = BaseHTTPRequestHandler.handle_one_request
def handle(self):
    state.n = 0; t0 = time.perf_counter()
    try:
        return orig(self)
    finally:
        with lock:
            log.append((getattr(self, "path", ""), state.n, round((time.perf_counter() - t0) * 1000, 2)))
            with open(os.environ["PROFILE_COUNTERS"], "w") as f: json.dump(log, f)
        state.n = None
BaseHTTPRequestHandler.handle_one_request = handle
import runpy
runpy.run_module("web_api.server", run_name="__main__")
'''


def call(method, path, user=None, body=None):
    headers = {"x-inazina-internal-token": TOKEN, "content-type": "application/json"}
    if user:
        headers["x-inazina-user-id"] = user
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload, status = response.read(), response.status
    except urllib.error.HTTPError as error:
        payload, status = error.read(), error.code
    return status, payload, (time.perf_counter() - t0) * 1000


def main():
    tmp = tempfile.mkdtemp(prefix="profile-http-")
    env = {**os.environ, "INAZINA_DB_BACKEND": "postgres", "INAZINA_PYTHON_PORT": str(port), "INAZINA_INTERNAL_TOKEN": TOKEN,
           "INAZINA_DB_PATH": os.path.join(tmp, "inazina.db"), "PROFILE_ROOT": root, "PROFILE_COUNTERS": os.path.join(tmp, "counters.json"),
           "PYTHONHASHSEED": "0", "INAZINA_READ_CACHE_SECONDS": os.environ.get("INAZINA_READ_CACHE_SECONDS", "0")}
    # known passwords for the two clone users (clone only)
    sys.path.insert(0, root); os.chdir(root)
    os.environ["INAZINA_DB_BACKEND"] = "postgres"
    from database import pg_backend
    from web_api import server
    with pg_backend.connect() as conn:
        for uid in (USER, ADMIN):
            salt = "ab12cd34ef56ab78"
            conn.execute("UPDATE web_auth_credentials SET salt=?, password_hash=? WHERE user_id=?",
                         (salt, server.password_hash(PASSWORD, salt), uid))
    child = subprocess.Popen([sys.executable, "-c", HOOK], cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                if call("GET", "/internal/health")[0] == 200:
                    break
            except Exception:
                time.sleep(1)
        # health call is not part of the report: remember where the log starts
        requests = [
            ("health", "GET", "/internal/health", None, None),
            ("login", "POST", "/internal/auth/login", None, {"loginId": USER, "password": PASSWORD}),
            ("login_admin", "POST", "/internal/auth/login", None, {"loginId": ADMIN, "password": PASSWORD}),
            ("login_wrong", "POST", "/internal/auth/login", None, {"loginId": USER, "password": "nope"}),
            ("me", "GET", "/internal/me", USER, None),
            ("portfolio", "GET", "/internal/market/portfolio", USER, None),
            ("instruments", "GET", "/internal/market/instruments", USER, None),
            ("exchange_indexes", "GET", "/internal/market/exchange-indexes", USER, None),
            ("me_history", "GET", "/internal/me/history", USER, None),
            ("me_credit", "GET", "/internal/me/credit", USER, None),
            ("me_stats", "GET", "/internal/me/stats", USER, None),
            ("loan_schedules", "GET", "/internal/me/loan-schedules", USER, None),
            ("hts_account", "GET", "/internal/hts/account", USER, None),
            ("admin_overview", "GET", "/internal/admin/overview", ADMIN, None),
            ("admin_instruments", "GET", "/internal/admin/instruments", ADMIN, None),
            ("admin_users", "GET", "/internal/admin/users", ADMIN, None),
            ("admin_funding", "GET", "/internal/admin/funding", ADMIN, None),
            ("admin_liquidity", "GET", "/internal/admin/liquidity", ADMIN, None),
            ("admin_alm", "GET", "/internal/admin/alm", ADMIN, None),
        ]
        report = {"label": label, "repeat": REPEAT, "requests": {}}
        for name, method, path, user, body in requests:
            samples, digest = [], None
            for index in range(1 + REPEAT):
                status, payload, ms = call(method, path, user, body)
                samples.append(ms)
                if index == 0:
                    digest = f"{status}:{hashlib.sha256(VOLATILE.sub('V', payload.decode('utf8', 'replace')).encode()).hexdigest()[:12]}"
            report["requests"][name] = {"cold_ms": round(samples[0], 1), "warm_ms": round(statistics.median(samples[1:]), 1), "digest": digest}
    finally:
        child.terminate()  # only the PID this script started
        child.wait(timeout=30)
    try:
        log = json.load(open(env["PROFILE_COUNTERS"]))
        by_path = {}
        for path, statements, ms in log:
            by_path.setdefault(path.split("?")[0], []).append(statements)
        for name, method, path, user, body in requests:
            counts = by_path.get(path.split("?")[0], [])
            if counts and name in report["requests"]:
                report["requests"][name]["statements_cold"] = counts[0]
                report["requests"][name]["statements_warm"] = statistics.median(counts[1:]) if len(counts) > 1 else counts[0]
    except Exception as exc:
        report["counter_error"] = repr(exc)
    print(json.dumps(report, ensure_ascii=False, indent=1))


main()
