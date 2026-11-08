"""Compare real pricing calls and optional market ticks on disposable DB copies."""
import argparse
import hashlib
from contextlib import ExitStack
from pathlib import Path
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from managers import _price_math, stochastic_price_engine as garch, auto_target_engine as target


def validate_native_source(module):
    source_hash = hashlib.sha256(Path(_price_math.__file__).read_bytes()).hexdigest()
    if getattr(module, 'SOURCE_HASH', None) != source_hash:
        raise RuntimeError('Native price module is stale; rebuild it with build_price_native.py build_ext --inplace')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, help='Optional DB, read-only backup; never tick the source')
    args = parser.parse_args()
    from managers import _price_math_native
    validate_native_source(_price_math_native)
    backends = {'python': _price_math, 'cython': _price_math_native}
    config = garch.GarchConfig()
    cycle = target.TargetCycle(1000, 1100, 0, 60)
    auto = target.AutoTargetConfig()
    for label, module in backends.items():
        samples = []
        with patch.object(garch, 'kernels', module), patch.object(target, 'kernels', module):
            for _ in range(7):
                rng = random.Random(42)
                start = time.perf_counter()
                for _ in range(10000):
                    garch.simulate_step(1000, .001, .002, config, market_shock=.5, rng=rng)
                    target.target_pull(1000, cycle, auto)
                    target.cycle_bounds(cycle, auto)
                samples.append((time.perf_counter() - start) * 1000)
        print(f'{label}: 10,000 pricing calls median {statistics.median(samples[1:]):.2f} ms')
    if not args.database:
        return
    from database import DatabaseManager
    from database.db_manager import DBManager
    from managers.market_manager import MarketManager
    old_dir = DBManager.database_dir
    old_random = random.getstate()
    try:
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            snapshot = Path(temp) / 'source.db'
            with sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(snapshot) as dest:
                src.backup(dest)
            markets = {}
            initial_epochs = {}
            samples = {label: [] for label in backends}
            for label in backends:
                directory = Path(temp) / label
                directory.mkdir()
                with sqlite3.connect(snapshot) as src, sqlite3.connect(directory / 'inazina.db') as dest:
                    src.backup(dest)
                DBManager.database_dir = directory
                db = DatabaseManager()
                stack.callback(db.close)
                markets[label] = MarketManager(db)
                initial_epochs[label] = {r['asset_code']: r['last_epoch'] for r in
                    db.assets.execute('SELECT asset_code,last_epoch FROM market_simulation_state')}
            now = int(time.time())
            for tick in range(32):
                labels = list(backends) if tick % 2 else list(reversed(backends))
                for label in labels:
                    DBManager.database_dir = Path(temp) / label
                    random.seed(42 + tick)
                    module = backends[label]
                    with patch.object(garch, 'kernels', module), patch.object(target, 'kernels', module):
                        start = time.perf_counter()
                        markets[label].update_prices(now=now + tick)
                        samples[label].append((time.perf_counter() - start) * 1000)
            for label, timings in samples.items():
                advanced = sum(r['last_epoch'] != initial_epochs[label].get(r['asset_code']) for r in
                    markets[label].db.assets.execute('SELECT asset_code,last_epoch FROM market_simulation_state'))
                if not advanced:
                    raise RuntimeError(f'{label}: no simulation advanced; market state cannot benchmark active pricing')
                values = sorted(timings[2:])
                print(f'{label}: tick median {statistics.median(values):.2f} ms, p95 {values[28]:.2f} ms; {advanced} states advanced')
            for table in ('company', 'investment', 'market_assets', 'market_simulation_state', 'market_auto_targets',
                          'market_event_targets', 'market_quote_overrides',
                          'market_price_history', 'market_exchanges', 'market_exchange_index_history',
                          'leveraged_etf_state'):
                rows = [[tuple(r) for r in market.db.assets.execute(f'SELECT * FROM {table} ORDER BY 1')]
                        for market in markets.values()]
                if rows[0] != rows[1]:
                    raise AssertionError(f'Backend result differs: {table}')
            print('Tick result parity: OK')
    finally:
        DBManager.database_dir = old_dir
        random.setstate(old_random)


if __name__ == '__main__':
    main()
