"""A long-run anchor for automatic market movement, so a quote cannot wander all the way down to 1 won (or run away upward).

The ordinary movement (GARCH noise plus short target cycles that are re-anchored to the *current* quote every 1-3 minutes) has no memory of
where a price came from, so it can drift one way for hours: on 2026-11-26 some stocks lost 75-87% in two hours and others sat on the
floor. The guard keeps a slow moving average of each quote (the anchor, half-life 12 h by default) and
  - pulls the price back, gently, once it is more than 20% away from the anchor (log distance, only the part beyond that), and
  - never lets one step leave the band [anchor x (1 - band), anchor / (1 - band)] (default -50% / +100%).
Administrator events and edited prices are not limited: the manager restarts the anchor from the quote after them.
Settings (system settings, all optional): price_guard_enabled, price_guard_halflife_hours, price_guard_band_percent, price_guard_pull.
A 100 won floor for stocks sits beside it: price_floor_won (0 = off), price_floor_zone_percent, price_floor_pull. A stock below the floor is lifted to it
(once, and the console is told); near it the quote is gently pushed up so it does not keep hitting the floor.
"""
import math
from dataclasses import dataclass

SOFT_ZONE = 0.20          # log distance from the anchor inside which nothing pulls


@dataclass(frozen=True)
class PriceGuardConfig:
    enabled: bool = True
    halflife_hours: float = 12.0
    band_percent: float = 50.0
    pull: float = 0.02
    floor: float = 100.0              # a stock is never left below this many won (0 = off)
    floor_zone_percent: float = 15.0  # within this share above the floor the price is gently pushed up
    floor_pull: float = 0.05

    @classmethod
    def from_settings(cls, settings):
        """Read the settings; anything unusable gives the defaults, because a bad value must not stop the market."""
        try:
            enabled = float(settings.get("price_guard_enabled", 1))
            halflife = float(settings.get("price_guard_halflife_hours", cls.halflife_hours))
            band = float(settings.get("price_guard_band_percent", cls.band_percent))
            pull = float(settings.get("price_guard_pull", cls.pull))
            floor = float(settings.get("price_floor_won", cls.floor))
            zone = float(settings.get("price_floor_zone_percent", cls.floor_zone_percent))
            floor_pull = float(settings.get("price_floor_pull", cls.floor_pull))
        except (TypeError, ValueError, OverflowError):
            return cls()
        if (enabled not in (0.0, 1.0) or not all(map(math.isfinite, (halflife, band, pull)))
                or halflife <= 0 or not 0 < band < 100 or not 0 <= pull <= 1
                or not all(map(math.isfinite, (floor, zone, floor_pull))) or floor < 0 or not 0 < zone <= 100 or not 0 <= floor_pull <= 1):
            return cls()
        return cls(bool(enabled), halflife, band, pull, floor, zone, floor_pull)


def next_anchor(anchor, price, step_seconds, config):
    """Move the anchor toward the quote by the share of a half-life that one step covers (a missing anchor starts at the quote)."""
    if anchor is None or not anchor > 0:
        return float(price)
    share = 1 - 0.5 ** (max(0.0, float(step_seconds)) / (config.halflife_hours * 3600))
    return float(anchor) + (float(price) - float(anchor)) * share


def guard_bias(latent, anchor, config):
    """A log drift back toward the anchor: zero inside the soft zone, growing with the distance beyond it."""
    if not config.enabled or anchor is None or not anchor > 0 or not latent > 0:
        return 0.0
    distance = math.log(float(anchor) / float(latent))      # positive when the price is below the anchor
    excess = abs(distance) - SOFT_ZONE
    return math.copysign(config.pull * excess, distance) if excess > 0 else 0.0


def clamp_price(price, anchor, config, prior=None):
    """Keep the price inside the band around the anchor (never below the market's own floor of 1). A price that is already outside the band
    (an old anchor, an administrator's edit) is not snapped back: it is only stopped from going further out than the prior price."""
    if not config.enabled or anchor is None or not anchor > 0:
        return price
    lower = max(1.0, float(anchor) * (1 - config.band_percent / 100))
    upper = float(anchor) / (1 - config.band_percent / 100)
    if prior is not None and prior > 0:
        lower, upper = min(lower, float(prior)), max(upper, float(prior))
    return min(upper, max(lower, price))


def floor_bias(latent, config):
    """A log drift upward for a stock near or below its floor: zero outside the support zone above the floor, stronger the lower it is."""
    if not config.enabled or config.floor <= 0 or not latent > 0:
        return 0.0
    top = config.floor * (1 + config.floor_zone_percent / 100)
    return config.floor_pull * math.log(top / float(latent)) if float(latent) < top else 0.0
