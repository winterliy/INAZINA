"""GARCH price steps with a shared market factor and heavy-tailed innovations.

One physical scheduler step represents a configurable number of trading
minutes. Prices remain fractional here so that small, realistic returns are
not discarded when the displayed NWX quote is rounded by the caller.
"""

from dataclasses import dataclass
import math
import random


TRADING_MINUTES_PER_YEAR = 252 * 390
MAX_PRICE = 2**53 - 1
MIN_LOG_RETURN = math.log(0.8)
MAX_LOG_RETURN = math.log(1.2)


@dataclass(frozen=True)
class GarchConfig:
    alpha: float = 0.05
    beta: float = 0.90
    annual_volatility: float = 0.30
    market_correlation: float = 0.55
    step_seconds: int = 1
    trading_minutes_per_step: float = 1.0
    student_df: float = 6.0
    volatility: float = 0.15

    def __post_init__(self):
        values = (self.alpha, self.beta, self.annual_volatility,
                  self.market_correlation, self.step_seconds,
                  self.trading_minutes_per_step, self.student_df,
                  self.volatility)
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) for value in values):
            raise ValueError("GARCH 설정은 유한한 숫자여야 합니다.")
        if self.alpha < 0 or self.beta < 0 or self.alpha + self.beta >= 1:
            raise ValueError("garch_alpha와 garch_beta는 0 이상이고 합이 1 미만이어야 합니다.")
        if self.annual_volatility <= 0:
            raise ValueError("garch_annual_volatility는 0보다 큰 연간 변동성 비율이어야 합니다.")
        if not 0 <= self.market_correlation <= 1:
            raise ValueError("garch_market_correlation은 0~1 사이여야 합니다.")
        if not 1 <= self.step_seconds <= 86400 or int(self.step_seconds) != self.step_seconds:
            raise ValueError("garch_step_seconds는 1~86400 사이의 정수 초여야 합니다.")
        if self.trading_minutes_per_step <= 0:
            raise ValueError("garch_trading_minutes_per_step는 0보다 커야 합니다.")
        if self.student_df <= 2:
            raise ValueError("garch_student_df는 분산이 유한하도록 2보다 커야 합니다.")
        if not 0 <= self.volatility <= 1:
            raise ValueError("market_volatility는 0~1 사이여야 합니다.")
        if not math.isfinite(self.step_variance):
            raise ValueError("GARCH 연간 변동성과 시간 설정이 너무 큽니다.")

    @classmethod
    def from_settings(cls, settings, volatility=None):
        """Parse settings rows without overwriting operators' saved values."""
        defaults = cls()
        fields = (
            ("alpha", "garch_alpha"), ("beta", "garch_beta"),
            ("annual_volatility", "garch_annual_volatility"),
            ("market_correlation", "garch_market_correlation"),
            ("step_seconds", "garch_step_seconds"),
            ("trading_minutes_per_step", "garch_trading_minutes_per_step"),
            ("student_df", "garch_student_df"),
        )
        values = {}
        try:
            for field, key in fields:
                value = settings.get(key, getattr(defaults, field))
                if isinstance(value, bool):
                    raise ValueError(f"{key}는 숫자여야 합니다.")
                values[field] = float(value)
            raw_volatility = (settings.get("market_volatility", defaults.volatility)
                              if volatility is None else volatility)
            if isinstance(raw_volatility, bool):
                raise ValueError("market_volatility는 숫자여야 합니다.")
            values["volatility"] = float(raw_volatility)
            config = cls(**values)
        except (TypeError, OverflowError) as error:
            raise ValueError("system.db의 GARCH 설정은 유한한 숫자여야 합니다.") from error
        # Keep scheduler bucket arithmetic integral after string parsing.
        return cls(**{**values, "step_seconds": int(config.step_seconds)})

    @property
    def step_variance(self):
        annual_sigma = self.annual_volatility * (self.volatility / 0.15)
        try:
            return annual_sigma**2 * (self.trading_minutes_per_step / TRADING_MINUTES_PER_YEAR)
        except OverflowError:
            return math.inf

    @property
    def omega(self):
        return (1 - self.alpha - self.beta) * self.step_variance


@dataclass(frozen=True)
class PriceStep:
    price: float
    variance: float
    innovation: float
    log_return: float
    shock: float


def standardized_shock(rng=random, df=6.0):
    """Return a Student-t innovation scaled to unit variance (df > 2)."""
    if isinstance(df, bool) or not math.isfinite(df) or df <= 2:
        raise ValueError("Student-t 자유도는 유한하고 2보다 커야 합니다.")
    # Chi-square(df) is Gamma(df/2, 2). Multiplying a t sample by
    # sqrt((df-2)/df) yields variance one, unlike an unscaled t sample.
    for _ in range(8):
        denominator = rng.gammavariate(df / 2, 2)
        if denominator > 0 and math.isfinite(denominator):
            value = rng.gauss(0, 1) * math.sqrt((df - 2) / denominator)
            if math.isfinite(value):
                return value
    # Underflow at exceptional configuration/sample extremes cannot store an
    # infinite variance in the persistence table.
    return rng.gauss(0, 1)


def next_variance(previous_variance, previous_innovation, config):
    """Update h_t = omega + alpha * epsilon_(t-1)^2 + beta * h_(t-1)."""
    if not config.step_variance:
        return 0.0
    if previous_variance is None:
        return config.step_variance
    try:
        variance = float(previous_variance)
        innovation = float(previous_innovation or 0.0)
    except (ValueError, TypeError, OverflowError):
        return config.step_variance
    if not math.isfinite(variance) or variance < 0 or not math.isfinite(innovation):
        return config.step_variance
    try:
        updated = config.omega + config.alpha * innovation**2 + config.beta * variance
    except OverflowError:
        return config.step_variance
    return updated if math.isfinite(updated) else config.step_variance


def simulate_step(price, previous_variance, previous_innovation, config, *,
                  market_shock, rng=random, directional_bias=0.0, jump=0.0):
    """Advance one price using a common factor, stock shock and event jump.

    directional_bias and jump are log-return amounts. A price circuit breaker
    limits a single simulated quote step to -20%/+20%; scheduled event targets
    are handled separately by the market manager. Realized innovations include
    jumps, so a news shock also raises the following GARCH variance.
    """
    inputs = (price, market_shock, directional_bias, jump)
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) for value in inputs):
        raise ValueError("가격과 가격 충격은 유한한 숫자여야 합니다.")
    if not 0 < price <= MAX_PRICE:
        raise ValueError(f"가격은 0보다 크고 {MAX_PRICE:,} 이하여야 합니다.")
    variance = next_variance(previous_variance, previous_innovation, config)
    if not variance:
        # A zero legacy volatility remains a freeze switch for ordinary
        # price updates. Explicit events are applied by the caller afterwards.
        return PriceStep(float(price), 0.0, 0.0, 0.0, 0.0)
    rho = config.market_correlation
    shock = rho * market_shock + math.sqrt(1 - rho**2) * standardized_shock(rng, config.student_df)
    raw_return = directional_bias - variance / 2 + math.sqrt(variance) * shock + jump
    log_return = min(MAX_LOG_RETURN, max(MIN_LOG_RETURN, raw_return))
    next_price = min(float(MAX_PRICE), max(1.0, price * math.exp(log_return)))
    realized_return = math.log(next_price / price)
    innovation = realized_return - directional_bias + variance / 2
    return PriceStep(next_price, variance, innovation, realized_return, shock)
