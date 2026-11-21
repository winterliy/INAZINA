"""Count logical statements, raw PostgreSQL round trips and time per SQL shape for non-tick scenarios on a SCRATCH database (JSON).

    createdb -T inazina_profile_20261121 inazina_profile_e1
    INAZINA_PG_DSN=postgresql:///inazina_profile_e1 PYTHONHASHSEED=0 python tools/profile_paths.py <code root> <label> [scenario ...]

Scenarios: order_rest (limit order that rests, then cancel), order_fill (a buy and a sell that match), reads (portfolio, orders, order book,
admin overview, exchange indexes), interest (process_all_interests), bank_reads, login. With no scenario name, all run. Each scenario runs
REPEAT times after a warm-up and reports per-run wall ms, logical statements, raw round trips (statement / savepoint), the shapes that cost the
most, and a digest of the result and of the tables it touches so two code versions can be compared for identical behavior.

Refuses any DSN that is not a scratch `inazina_profile_*` database. Mutating scenarios change the clone, so use a fresh clone for every run.
Compare `ms` only between runs made one after the other (the machine load changes); judge by ratios. See docs/bottleneck-resolution.md.
"""
import hashlib, json, os, re, statistics, sys, time
from collections import Counter, defaultdict

root, label = sys.argv[1], sys.argv[2]
wanted = sys.argv[3:]
dsn = os.environ.get("INAZINA_PG_DSN", "")
assert dsn.startswith("postgresql:///inazina_profile_"), f"refusing DSN {dsn!r}"
os.environ["INAZINA_DB_BACKEND"] = "postgres"
sys.path.insert(0, root); os.chdir(root)
import psycopg
from database import pg_backend

REPEAT = int(os.environ.get("REPEAT", "5"))
logical, logical_t, raw = Counter(), defaultdict(float), Counter()
orig_exec, orig_many = pg_backend.PgCursor.execute, pg_backend.PgCursor.executemany
orig_raw, orig_raw_many = psycopg.Cursor.execute, psycopg.Cursor.executemany


def shape(sql):
    text = re.sub(r"\s+", " ", str(sql).strip())
    return re.sub(r"'[^']*'", "'?'", text)[:110]


def timed(self, sql, parameters=()):
    key, t0 = shape(sql), time.perf_counter()
    try:
        return orig_exec(self, sql, parameters)
    finally:
        logical[key] += 1; logical_t[key] += time.perf_counter() - t0


def timed_many(self, sql, seq):
    key, t0 = "(batch) " + shape(sql), time.perf_counter()
    try:
        return orig_many(self, sql, seq)
    finally:
        logical[key] += 1; logical_t[key] += time.perf_counter() - t0


def raw_exec(self, query, params=None, **kw):
    text = query if isinstance(query, str) else str(query)
    # "joined": the savepoint, the statement and the release sent as one message (one round trip)
    raw["joined" if text.startswith("SAVEPOINT inazina_statement;") else
        "savepoint" if text.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK TO")) else "statement"] += 1
    return orig_raw(self, query, params, **kw)


def raw_many(self, query, params_seq, **kw):
    raw["batch"] += 1
    return orig_raw_many(self, query, params_seq, **kw)


pg_backend.PgCursor.execute, pg_backend.PgCursor.executemany = timed, timed_many
psycopg.Cursor.execute, psycopg.Cursor.executemany = raw_exec, raw_many

from web_api import server  # noqa: E402

BUYER, SELLER = os.environ.get("BUYER", "20230216"), os.environ.get("SELLER", "1076861147560542278")
ASSET = os.environ.get("ASSET", "ASELOA")
TABLES = ("market_orders", "market_execution_events", "users", "accounts", "user_assets", "market_assets", "company")


EPOCH = re.compile(r"\b1[78]\d{8}(?:\.\d+)?\b")  # wall-clock epochs differ between runs: mask them


def table_digest():
    out = {}
    with pg_backend.connect(readonly=True) as conn:
        for table in TABLES:
            try:
                rows = sorted(EPOCH.sub("E", repr(tuple(r))) for r in conn.execute(f"SELECT * FROM {table}").fetchall())
            except Exception as exc:
                out[table] = f"unreadable:{type(exc).__name__}"; continue
            out[table] = f"{len(rows)}:{hashlib.sha256(chr(10).join(rows).encode()).hexdigest()[:12]}"
    return out


def last_price():
    with pg_backend.connect(readonly=True) as conn:
        row = conn.execute("SELECT share_price FROM company WHERE company_code=?", (ASSET.split(":", 1)[-1],)).fetchone() \
            or conn.execute("SELECT price FROM market_assets WHERE code=?", (ASSET,)).fetchone()
    return int(row[0]) if row else 1000


def scenario_order_rest(step):
    book, results = server.domain().order_book, []
    price = max(1, last_price() // 2)
    ok, msg = book.place_limit_order(BUYER, ASSET, "buy", 1, price); results.append((ok, msg[:40]))
    with pg_backend.connect(readonly=True) as conn:
        order_id = conn.execute("SELECT MAX(order_id) FROM market_orders WHERE user_id=?", (BUYER,)).fetchone()[0]
    results.append(book.cancel_order(BUYER, order_id))
    return results


def scenario_order_fill(step):
    book, results = server.domain().order_book, []
    price = last_price()
    results.append(book.place_limit_order(SELLER, ASSET, "sell", 1, price))
    results.append(book.place_limit_order(BUYER, ASSET, "buy", 1, price))
    return results


def scenario_reads(step):
    book, results = server.domain().order_book, []
    results.append(len(str(server.portfolio(BUYER))))
    results.append(len(str(book.get_user_orders(BUYER))))
    results.append(len(str(book.get_user_open_orders(BUYER))))
    results.append(str(book.get_order_book(ASSET)))
    results.append(str(server.admin_overview())[:2000])
    results.append(len(str(server.exchange_indexes())))
    return results


def scenario_interest(step):
    return str(server.domain().bot.interest_mgr.process_all_interests(now=1_795_200_000 + step * 86400))[:200]


SCENARIOS = {"order_rest": scenario_order_rest, "order_fill": scenario_order_fill, "reads": scenario_reads,
             "interest": scenario_interest}

server.domain()  # build the managers before measuring
report = {"label": label, "repeat": REPEAT, "scenarios": {}}
for name, run in SCENARIOS.items():
    if wanted and name not in wanted:
        continue
    try:
        run(0)  # warm-up (translation cache, imports)
        walls, digest, top = [], hashlib.sha256(), None
        logical.clear(); logical_t.clear(); raw.clear()
        for step in range(1, REPEAT + 1):
            t0 = time.perf_counter(); result = run(step); walls.append((time.perf_counter() - t0) * 1000)
            digest.update(repr(result).encode())
        shapes = sorted(logical_t.items(), key=lambda kv: -kv[1])[:6]
        report["scenarios"][name] = {
            "median_ms": round(statistics.median(walls), 1), "min_ms": round(min(walls), 1),
            "logical_per_run": round(sum(logical.values()) / REPEAT, 1),
            "raw_per_run": {k: round(v / REPEAT, 1) for k, v in raw.items()},
            "top_shapes": [[shape_, round(t * 1000 / REPEAT, 2), round(logical[shape_] / REPEAT, 1)] for shape_, t in shapes],
            "result_digest": digest.hexdigest()[:12]}
    except Exception as exc:
        report["scenarios"][name] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
report["table_digests"] = table_digest()
print(json.dumps(report, ensure_ascii=False, indent=1))
