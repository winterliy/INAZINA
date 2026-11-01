"""Fill the price history the charts would show as empty while the bot was down.

Only blanks users can actually see are filled: gaps of a minute or more inside the
longest bounded chart range (the last 30 days), whether between two records (an
earlier outage) or after the last one (the outage that just ended — prices do not
move while the bot is off, so it ends at the last price). Each gap gets a Brownian
bridge pinned to the real prices on both sides, sized by the asset's own one-second
volatility. Only history rows are written; prices are untouched.

Only time the asset could have traded is filled: its exchange's regular hours
(functions/trading_hours.py), never an unlisted, inactive or delisted asset, and never an
exchange an admin left closed or paused. A filled gap is written to
``market_price_gap_fills`` so a later restart does not fill its coarse steps again.
"""
from functions.exact_money import ExactDecimal
import math
import random
import time

from functions import trading_hours
from managers.market_session_manager import MarketSessionManager

VIEWABLE_SECONDS = 30 * 86400   # the longest bounded chart range ("1m")
MIN_GAP_SECONDS = 60            # shorter pauses are left as they are
MAX_POINTS_PER_GAP = 2000       # long gaps get coarser steps, not millions of rows
VOLATILITY_SAMPLE = 600         # recent one-second returns used to size the walk
VOLATILITY_BOUNDS = (1e-5, 0.01)

# Every listed product with its exchange and lifecycle status.
_LISTINGS = ("SELECT code, exchange_id, status FROM market_assets "
             "UNION ALL SELECT company_code, exchange_id, status FROM company "
             "UNION ALL SELECT product_code, exchange_id, status FROM investment "
             "UNION ALL SELECT product_code, exchange_id, status FROM treasury_bond_products")


def _volatility(prices):
    """Standard deviation of one-step log returns, clamped to a sane band."""
    returns = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    if len(returns) < 2:
        return VOLATILITY_BOUNDS[0]
    mean = sum(returns) / len(returns)
    sigma = math.sqrt(sum((value - mean) ** 2 for value in returns) / (len(returns) - 1))
    return min(VOLATILITY_BOUNDS[1], max(VOLATILITY_BOUNDS[0], sigma))


def bridge(start_price, end_price, intervals, sigma, rng):
    """Points inside the open ``intervals`` [(a, b), …]: a random walk from ``start_price`` to ``end_price``.

    The walk runs on trading time only, so a closed night is a jump, not a flat line.
    """
    start_price, end_price = ExactDecimal(start_price), ExactDecimal(end_price)
    if sigma == 0 and start_price == end_price:
        span = sum(b - a for a, b in intervals)
        step = max(1, math.ceil(span / MAX_POINTS_PER_GAP))
        passed = 0
        points = []
        for a, b in intervals:
            points.extend((a + offset - passed, start_price) for offset in range(step, span, step) if passed <= offset < passed + b - a)
            passed += b - a
        return points
    span = sum(b - a for a, b in intervals)
    step = max(1, math.ceil(span / MAX_POINTS_PER_GAP))
    offsets = list(range(step, span, step))
    if not offsets:
        return []
    walk, level = [], 0.0
    for _ in range(len(offsets) + 1):
        level += rng.gauss(0.0, sigma * math.sqrt(step))
        walk.append(level)
    # Pin the walk's end to the log distance between the two real prices.
    total = walk[-1] - math.log(end_price / start_price)
    count = len(walk)  # len(offsets) + 1
    points, passed, index = [], 0, 0
    for a, b in intervals:
        while index < len(offsets) and offsets[index] < passed + (b - a):
            epoch = a + offsets[index] - passed
            price = start_price * math.exp(walk[index] - total * (index + 1) / count)
            points.append((epoch, max(1, round(price))))
            index += 1
        passed += b - a
    return points


def _markets(conn):
    """{asset: trading-hours schedule} for assets that may be filled; others are absent."""
    exchanges = {str(row["exchange_id"]): dict(row) for row in conn.execute("SELECT * FROM market_exchanges")}
    markets = {}
    for code, exchange_id, status in conn.execute(_LISTINGS):
        exchange = exchanges.get(str(exchange_id))
        if status != "active" or exchange is None:
            continue
        # A schedule close reopens by itself; an admin's 휴장 or 정지 means the market really stopped.
        if exchange["status"] != "open" and not exchange.get("scheduled_close"):
            continue
        markets[str(code)] = trading_hours.from_row(exchange)
    return markets


def fill_price_gaps(conn, now=None, rng=None):
    """Fill the visible gaps of every tradable asset. Returns {asset: rows added}."""
    now = int(now if now is not None else time.time())
    rng = rng or random.Random()
    floor = now - VIEWABLE_SECONDS
    markets = _markets(conn)
    # Each record in the window next to the one after it (``now`` for an asset's last record).
    gaps = conn.execute(
        "SELECT asset_code, recorded_epoch, price, next_epoch, next_price FROM ("
        " SELECT asset_code, recorded_epoch, price,"
        "  LEAD(recorded_epoch) OVER w AS next_epoch, LEAD(price) OVER w AS next_price"
        " FROM market_price_history WHERE recorded_epoch >= ?"
        " WINDOW w AS (PARTITION BY asset_code ORDER BY recorded_epoch))"
        " WHERE COALESCE(next_epoch, ?) - recorded_epoch >= ?",
        (floor, now, MIN_GAP_SECONDS)).fetchall()
    # A gap that started before the window: from its edge to the asset's first record inside it.
    for code, first_epoch, first_price in conn.execute(
            "SELECT asset_code, MIN(recorded_epoch), price FROM market_price_history "
            "WHERE recorded_epoch >= ? GROUP BY asset_code", (floor,)).fetchall():
        if first_epoch - floor < MIN_GAP_SECONDS:
            continue
        before = conn.execute(
            "SELECT price FROM market_price_history WHERE asset_code=? AND recorded_epoch<? "
            "ORDER BY recorded_epoch DESC LIMIT 1", (code, floor)).fetchone()
        if before:
            gaps.append((code, floor, before[0], first_epoch, first_price))
    fills = {}
    for code, start, end in conn.execute(
            "SELECT asset_code, start_epoch, end_epoch FROM market_price_gap_fills WHERE end_epoch >= ?", (floor,)):
        fills.setdefault(code, []).append((int(start), int(end)))
    added, sigmas = {}, {}
    for code, epoch, price, next_epoch, next_price in gaps:
        schedule = markets.get(code)
        if schedule is None:
            continue
        price = ExactDecimal(price)
        end, end_price = (int(next_epoch), ExactDecimal(next_price)) if next_epoch is not None else (now, price)
        start = max(int(epoch), floor)
        if not (price > 0 and end_price > 0):
            continue
        # The steps of an earlier fill are gaps themselves; they are already as filled as they get.
        # A gap that runs past an earlier fill only gets the part after it.
        start = max([start] + [b for a, b in fills.get(code, ()) if a <= start < b])
        if end - start < MIN_GAP_SECONDS:
            continue
        if code not in sigmas:
            # One volatility per asset: its real ticks before its first gap in this run.
            recent = [ExactDecimal(r[0]) for r in conn.execute(
                "SELECT price FROM market_price_history WHERE asset_code=? AND recorded_epoch<=? "
                "ORDER BY recorded_epoch DESC LIMIT ?", (code, epoch, VOLATILITY_SAMPLE + 1))][::-1]
            sigmas[code] = _volatility(recent)
        intervals = trading_hours.open_intervals(schedule, start, end)
        points = bridge(price, end_price, intervals, sigmas[code], rng)
        conn.executemany(
            "INSERT INTO market_price_history(asset_code, price, recorded_epoch) VALUES (?, ?, ?) "
            "ON CONFLICT(asset_code, recorded_epoch) DO NOTHING",
            [(code, value, point_epoch) for point_epoch, value in points])
        conn.execute("INSERT INTO market_price_gap_fills(asset_code, start_epoch, end_epoch, points, filled_epoch) "
                     "VALUES (?, ?, ?, ?, ?)", (code, start, end, len(points), now))
        if points:
            added[code] = added.get(code, 0) + len(points)
    return added
