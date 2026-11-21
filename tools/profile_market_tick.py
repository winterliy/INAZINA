"""Time MarketManager.update_prices on a SCRATCH PostgreSQL database and print per-tick round trips and table digests (JSON).

    createdb -T inazina_profile_<date> inazina_profile_base      # one clone per code version, fresh for every run
    INAZINA_PG_DSN=postgresql:///inazina_profile_base PYTHONHASHSEED=0 TICKS=6 python tools/profile_market_tick.py <code root> <label>

Refuses any DSN that is not a scratch `inazina_profile_*` database (the production DB must never be written by a benchmark). Every run uses
the same seeds and the same `now`, and `now` lies in the past: the engine clamps to the real clock, so a future `now` makes two runs
differ. Two code versions are equal when the `digests` are equal; compare `median_ms` only for runs made one after the other, because
the machine load changes. See docs/bottleneck-resolution.md.
"""
import hashlib, json, os, random, statistics, sys, time
root, label = sys.argv[1], sys.argv[2]
dsn = os.environ.get("INAZINA_PG_DSN", "")
assert dsn.startswith("postgresql:///inazina_profile_"), f"refusing DSN {dsn!r}"
os.environ["INAZINA_DB_BACKEND"] = "postgres"
sys.path.insert(0, root); os.chdir(root)
import psycopg
from database import DatabaseManager, pg_backend
from managers.market_manager import MarketManager

trips = {"statement": 0, "savepoint": 0, "batch": 0, "batch_rows": 0}
orig_execute, orig_many = psycopg.Cursor.execute, psycopg.Cursor.executemany
def execute(self, query, params=None, **kw):
    text = query if isinstance(query, str) else str(query)
    trips["savepoint" if text.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK TO")) else "statement"] += 1
    return orig_execute(self, query, params, **kw)
def executemany(self, query, params_seq, **kw):
    params_seq = list(params_seq)
    trips["batch"] += 1; trips["batch_rows"] += len(params_seq)
    return orig_many(self, query, params_seq, **kw)
psycopg.Cursor.execute, psycopg.Cursor.executemany = execute, executemany

NOW = 1_795_200_000
TICKS = int(os.environ.get("TICKS", "8"))
db = DatabaseManager(); market = MarketManager(db)
random.seed(42); t0 = time.perf_counter(); market.update_prices(now=NOW); first = (time.perf_counter() - t0) * 1000
for key in trips: trips[key] = 0
times = []
for i in range(1, TICKS + 1):
    random.seed(42 + i); t0 = time.perf_counter(); market.update_prices(now=NOW + i * 5); times.append((time.perf_counter() - t0) * 1000)
db.close()
digests = {}
with pg_backend.connect(readonly=True) as conn:
    for table in ("market_assets", "company", "investment", "market_simulation_state", "market_auto_targets", "market_price_history", "market_exchanges", "market_exchange_index_history", "market_exchange_index_components"):
        try:
            rows = sorted(repr(tuple(row)) for row in conn.execute(f"SELECT * FROM {table}").fetchall())
        except Exception as exc:
            digests[table] = f"unreadable: {type(exc).__name__}"; continue
        digests[table] = f"{len(rows)}:{hashlib.sha256(chr(10).join(rows).encode()).hexdigest()[:16]}"
print(json.dumps({"label": label, "first_ms": round(first), "median_ms": round(statistics.median(times)), "min_ms": round(min(times)), "max_ms": round(max(times)),
                  "per_tick_trips": {k: round(v / TICKS) for k, v in trips.items()}, "digests": digests}, ensure_ascii=False))
