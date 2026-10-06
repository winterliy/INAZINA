"""Recurring, nearby price targets for ordinary automatic market movement.

Targets provide a soft direction, not a promised settlement price. GARCH
innovations can move a quote above and below the target until its cycle ends,
when the market manager samples a new target from the then-current quote.
Administrator events have their own deadline and take priority over cycles.
"""

from dataclasses import dataclass
import math
import random

from .stochastic_price_engine import MAX_PRICE
from .price_backend import kernels


def _finite_number(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        raise ValueError(f"{name}은 유한한 숫자여야 합니다.")
    return value


def _price(value):
    _finite_number(value, "가격")
    if not 1 <= value <= MAX_PRICE:
        raise ValueError(f"가격은 1~{MAX_PRICE:,} 사이여야 합니다.")
    return float(value)


@dataclass(frozen=True)
class AutoTargetConfig:
    enabled: bool = True
    range_percent: float = 10.0
    min_seconds: int = 60
    max_seconds: int = 180
    pull: float = 0.08
    noise_multiplier: float = 6.0
    band_percent: float = 25.0

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ValueError("auto_target_enabled는 0 또는 1이어야 합니다.")
        for field in ("range_percent", "min_seconds", "max_seconds", "pull",
                      "noise_multiplier", "band_percent"):
            _finite_number(getattr(self, field), f"auto_target_{field}")
        if not 0 <= self.range_percent <= 50:
            raise ValueError("auto_target_range_percent는 0~50 사이여야 합니다.")
        if (not 1 <= self.min_seconds <= self.max_seconds <= 604800
                or int(self.min_seconds) != self.min_seconds
                or int(self.max_seconds) != self.max_seconds):
            raise ValueError("자동 목표 기간은 1~604800 사이의 정수 초이고 최소가 최대 이하여야 합니다.")
        if not 0 <= self.pull <= 1:
            raise ValueError("auto_target_pull은 0~1 사이여야 합니다.")
        if not 1 <= self.noise_multiplier <= 30:
            raise ValueError("auto_target_noise_multiplier는 1~30 사이여야 합니다.")
        if not 0 <= self.band_percent <= 100:
            raise ValueError("auto_target_band_percent는 0~100 사이여야 합니다.")

    @classmethod
    def from_settings(cls, settings):
        defaults = cls()
        enabled = settings.get("auto_target_enabled", int(defaults.enabled))
        if isinstance(enabled, bool):
            enabled = int(enabled)
        try:
            numeric_enabled = float(enabled)
            if numeric_enabled not in (0.0, 1.0):
                raise ValueError("auto_target_enabled는 0 또는 1이어야 합니다.")
            values = {"enabled": bool(numeric_enabled)}
            for field in ("range_percent", "min_seconds", "max_seconds", "pull",
                          "noise_multiplier", "band_percent"):
                raw = settings.get(f"auto_target_{field}", getattr(defaults, field))
                if isinstance(raw, bool):
                    raise ValueError(f"auto_target_{field}는 숫자여야 합니다.")
                values[field] = float(raw)
            config = cls(**values)
        except (TypeError, OverflowError) as error:
            raise ValueError("system.db의 자동 목표 설정은 유한한 숫자여야 합니다.") from error
        return cls(**{**values, "min_seconds": int(config.min_seconds),
                      "max_seconds": int(config.max_seconds)})


@dataclass(frozen=True)
class TargetCycle:
    anchor_price: float
    target_price: float
    start_epoch: int
    end_epoch: int

    def __post_init__(self):
        _price(self.anchor_price)
        _price(self.target_price)
        for name in ("start_epoch", "end_epoch"):
            value = _finite_number(getattr(self, name), name)
            if int(value) != value:
                raise ValueError("자동 목표 시각은 정수 초여야 합니다.")
        if self.end_epoch <= self.start_epoch:
            raise ValueError("자동 목표 종료 시각은 시작 시각보다 뒤여야 합니다.")


def sample_target(current_price, epoch, config, *, rng=random):
    """Choose a nearby fractional target and a cycle duration, inclusively."""
    price = _price(current_price)
    _finite_number(epoch, "자동 목표 시작 시각")
    if int(epoch) != epoch:
        raise ValueError("자동 목표 시작 시각은 정수 초여야 합니다.")
    offset = rng.uniform(-config.range_percent, config.range_percent) / 100
    target = min(float(MAX_PRICE), max(1.0, price * (1 + offset)))
    duration = rng.randint(int(config.min_seconds), int(config.max_seconds))
    return TargetCycle(price, target, int(epoch), int(epoch) + duration)


def cycle_bounds(cycle, config):
    """Return a bounded fluctuation band containing both anchor and target."""
    return kernels.bounds_value(cycle.anchor_price, cycle.target_price, config.band_percent)


def target_pull(price, cycle, config, *, elapsed_steps=1.0):
    """Return a log drift toward a soft target; this does not force arrival."""
    current = _price(price)
    _finite_number(elapsed_steps, "자동 목표 경과 단계")
    if elapsed_steps < 0:
        raise ValueError("자동 목표 경과 단계는 0 이상이어야 합니다.")
    return kernels.pull_value(current, cycle.target_price, config.pull, elapsed_steps)
