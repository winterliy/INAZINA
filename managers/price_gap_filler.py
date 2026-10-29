"""Fill the price history the charts would show as empty while the bot was down.

Only blanks users can actually see are filled: gaps of a minute or more inside the
longest bounded chart range (the last 30 days), whether between two records (an
earlier outage) or after the last one (the outage that just ended — prices do not
move while the bot is off, so it ends at the last price). Each gap gets a Brownian
bridge pinned to the real prices on both sides, sized by the asset's own one-second
volatility. Only history rows are written; prices are untouched.
"""
import math
import random
import time

VIEWABLE_SECONDS = 30 * 86400   # the longest bounded chart range ("1m")
MIN_GAP_SECONDS = 60            # shorter pauses are left as they are
MAX_POINTS_PER_GAP = 2000       # long gaps get coarser steps, not millions of rows
VOLATILITY_SAMPLE = 600         # recent one-second returns used to size the walk
VOLATILITY_BOUNDS = (1e-5, 0.01)


def _volatility(prices):
    """Standard deviation of one-step log returns, clamped to a sane band."""
    returns = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    if len(returns) < 2:
        return VOLATILITY_BOUNDS[0]
    mean = sum(returns) / len(returns)
    sigma = math.sqrt(sum((value - mean) ** 2 for value in returns) / (len(returns) - 1))
    return min(VOLATILITY_BOUNDS[1], max(VOLATILITY_BOUNDS[0], sigma))


def bridge(start_price, end_price, start, end, sigma, rng):
    """Points strictly between ``start`` and ``end``: a random walk from ``start_price`` to ``end_price``."""
    span = end - start
    step = max(1, math.ceil(span / MAX_POINTS_PER_GAP))
    epochs = list(range(start + step, end, step))
    if not epochs:
        return []
    walk, level = [], 0.0
    for _ in range(len(epochs) + 1):
        level += rng.gauss(0.0, sigma * math.sqrt(step))
        walk.append(level)
    # Pin the walk's end to the log distance between the two real prices.
    total = walk[-1] - math.log(end_price / start_price)
    count = len(walk)
    return [(epoch, start_price * math.exp(walk[index] - total * (index + 1) / count))
            for index, epoch in enumerate(epochs)]


def fill_price_gaps(conn, now=None, rng=None):
    """Fill the visible gaps of every asset. Returns {asset: rows added}."""
    now = int(now if now is not None else time.time())
    rng = rng or random.Random()
    floor = now - VIEWABLE_SECONDS
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
    added = {}
    for code, epoch, price, next_epoch, next_price in gaps:
        price = float(price)
        end, end_price = (int(next_epoch), float(next_price)) if next_epoch is not None else (now, price)
        start = max(int(epoch), floor)
        if not (price > 0 and end_price > 0):
            continue
        recent = [float(r[0]) for r in conn.execute(
            "SELECT price FROM market_price_history WHERE asset_code=? AND recorded_epoch<=? "
            "ORDER BY recorded_epoch DESC LIMIT ?", (code, epoch, VOLATILITY_SAMPLE + 1))][::-1]
        points = bridge(price, end_price, start, end, _volatility(recent), rng)
        conn.executemany(
            "INSERT INTO market_price_history(asset_code, price, recorded_epoch) VALUES (?, ?, ?) "
            "ON CONFLICT(asset_code, recorded_epoch) DO NOTHING",
            [(code, value, point_epoch) for point_epoch, value in points])
        if points:
            added[code] = added.get(code, 0) + len(points)
    return added
