"""Scalar kernels shared by the Python and optional Cython backends.

Public wrappers validate inputs and draw random numbers. Float annotations
become C doubles only when this file is compiled; money and epochs stay Python.
"""
import math

MAX_PRICE = float(2**53 - 1)
MIN_RETURN = math.log(.8)
MAX_RETURN = math.log(1.2)


def variance_value(variance: float, innovation: float, alpha: float,
                   beta: float, baseline: float) -> float:
    try:
        squared: float = math.pow(innovation, 2.0)
        updated: float = (1 - alpha - beta) * baseline + alpha * squared + beta * variance
    except OverflowError:
        return baseline
    return updated if math.isfinite(updated) else baseline


def price_value(price: float, variance: float, rho: float, market_shock: float,
                stock_shock: float, bias: float, jump: float):
    shock: float = rho * market_shock + math.sqrt(1 - rho**2) * stock_shock
    raw_return: float = bias - variance / 2 + math.sqrt(variance) * shock + jump
    log_return: float = min(MAX_RETURN, max(MIN_RETURN, raw_return))
    new_price: float = min(MAX_PRICE, max(1.0, price * math.exp(log_return)))
    realized: float = math.log(new_price / price)
    innovation: float = realized - bias + variance / 2
    return new_price, variance, innovation, realized, shock


def pull_value(current: float, target: float, pull: float, elapsed: float) -> float:
    return pull * math.log(target / current) * elapsed


def bounds_value(anchor: float, target: float, band: float):
    fraction: float = band / 100
    return (max(1.0, min(anchor, target) * (1 - fraction)),
            min(MAX_PRICE, max(anchor, target) * (1 + fraction)))

SOURCE_HASH = '7d1341842fb4934090b1b89ce04a1ed16dd3a4bf70e70f9f793cb0bafa82698b'
