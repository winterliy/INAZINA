"""Low-volume timing for command preparation; never log arguments or user data."""
from functools import wraps
import logging
from time import perf_counter


def log_slow(stage, threshold_ms=100):
    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            started = perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                elapsed = (perf_counter() - started) * 1000
                if elapsed >= threshold_ms:
                    logging.getLogger('inazina.performance').warning('%s %.1f ms', stage, elapsed)
        return measured
    return decorate
