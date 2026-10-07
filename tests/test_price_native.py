"""Optional numeric backend parity; all databases remain outside this module."""
import math
import os
import random
import subprocess
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from managers import _price_math as python_math
from managers import stochastic_price_engine as garch
from managers import auto_target_engine as target
try:
    from managers import _price_math_native as native_math
except ImportError:
    native_math = None


@contextmanager
def backend(module):
    with patch.object(garch, 'kernels', module), patch.object(target, 'kernels', module):
        yield


class PriceNativeTests(unittest.TestCase):
    def test_python_fallback_without_compiler_or_extension(self):
        code = '''
import importlib.abc, sys
class MissingNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'managers._price_math_native':
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, MissingNative())
from managers.price_backend import BACKEND
from managers.stochastic_price_engine import simulate_step, GarchConfig
assert BACKEND == 'python'
assert simulate_step(100, None, 0, GarchConfig(), market_shock=0).price > 0
'''
        for selection in ('python', 'auto'):
            subprocess.run([sys.executable, '-c', code], check=True,
                           env={**os.environ, 'INAZINA_PRICE_BACKEND': selection})
        stale = '''
import sys, types
sys.modules['managers._price_math_native'] = types.SimpleNamespace(SOURCE_HASH='old')
from managers.price_backend import BACKEND
assert BACKEND == 'python'
'''
        subprocess.run([sys.executable, '-c', stale], check=True,
                       env={**os.environ, 'INAZINA_PRICE_BACKEND': 'auto'})

    @unittest.skipIf(native_math is None, 'Build optional Cython extension to check parity')
    def test_trajectories_rounding_and_rng_match(self):
        for rho in (0, .55, 1):
            for initial in (.5, 1, 1000, garch.MAX_PRICE):
                outputs = []
                for module in (python_math, native_math):
                    rng = random.Random(123)
                    price, variance, innovation = initial, None, 0
                    trace = []
                    with backend(module):
                        config = garch.GarchConfig(market_correlation=rho)
                        for tick in range(100):
                            result = garch.simulate_step(price, variance, innovation, config,
                                market_shock=.5, rng=rng, directional_bias=.001,
                                jump=10 if tick == 10 else -10 if tick == 20 else 0)
                            trace.append(tuple(vars(result).values()))
                            price, variance, innovation = result.price, result.variance, result.innovation
                    outputs.append((trace, rng.getstate()))
                self.assertEqual(outputs[0][1], outputs[1][1])
                for expected, actual in zip(outputs[0][0], outputs[1][0]):
                    self.assertEqual(math.floor(expected[0] + .5), math.floor(actual[0] + .5))
                    for a, b in zip(expected, actual):
                        self.assertTrue(math.isclose(a, b, rel_tol=1e-13, abs_tol=1e-15), (a, b))

    @unittest.skipIf(native_math is None, 'Build optional Cython extension to check parity')
    def test_variance_extremes_freeze_and_validation(self):
        for alpha in (0, .05):
            config = garch.GarchConfig(alpha=alpha)
            for variance, innovation in ((None, 0), ('0.001', '0.02'), (-1, 0),
                    (math.inf, 0), (1, math.nan), (1, 1e308), (1, -1e308)):
                with backend(python_math): expected = garch.next_variance(variance, innovation, config)
                with backend(native_math): actual = garch.next_variance(variance, innovation, config)
                self.assertEqual(actual, expected)
        for module in (python_math, native_math):
            with backend(module):
                rng = random.Random(42)
                state = rng.getstate()
                step = garch.simulate_step(100, None, 0, garch.GarchConfig(volatility=0),
                                          market_shock=1, jump=1, rng=rng)
                self.assertEqual(step.price, 100)
                self.assertEqual(rng.getstate(), state)
                for price in (True, '100', 0, math.inf, garch.MAX_PRICE + 1):
                    with self.assertRaises(ValueError):
                        garch.simulate_step(price, None, 0, garch.GarchConfig(), market_shock=0)

    @unittest.skipIf(native_math is None, 'Build optional Cython extension to check parity')
    def test_target_bounds_pull_and_unbounded_epochs(self):
        for module in (python_math, native_math):
            with backend(module):
                for band in (0, 25, 100):
                    config = target.AutoTargetConfig(band_percent=band)
                    cycle = target.sample_target(1000, 10**30, config, rng=random.Random(7))
                    self.assertGreater(cycle.end_epoch, 10**30)
                    self.assertEqual(target.cycle_bounds(cycle, config),
                        python_math.bounds_value(cycle.anchor_price, cycle.target_price, band))
                    for price in (1, 1000, garch.MAX_PRICE):
                        for elapsed in (0, .5, 2):
                            self.assertAlmostEqual(target.target_pull(price, cycle, config, elapsed_steps=elapsed),
                                config.pull * math.log(cycle.target_price / price) * elapsed)
