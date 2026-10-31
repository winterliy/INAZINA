"""Indicative depositary-receipt reference pricing helpers."""

import math
from functions.exact_money import ExactDecimal, native_value


DEVIATION_ALERT_THRESHOLD_PERCENT = 10.0


def reference_metrics(dr_price, underlying_price, underlying_per_receipt, fx_rate):
    """Return an indicative DR value and deviation warning for web clients."""
    try:
        values = tuple(ExactDecimal(value) for value in
                       (dr_price, underlying_price, underlying_per_receipt, fx_rate))
    except (TypeError, ValueError, OverflowError):
        return {}
    if not all(math.isfinite(value) and value > 0 for value in values):
        return {}
    price, underlying, ratio, fx = values
    reference = underlying * ratio * fx
    premium = (price / reference - 1) * 100
    if not math.isfinite(reference) or not math.isfinite(premium):
        return {}
    return {
        "referenceValue": native_value(reference),
        "referencePremiumPercent": float(premium),
        "referenceDeviationAlert": (
            abs(premium) + 1e-9 >= DEVIATION_ALERT_THRESHOLD_PERCENT
        ),
    }


def alert_state(metrics):
    """Classify informational premium/discount state from reference metrics."""
    if not metrics:
        return "unavailable"
    if not metrics.get("referenceDeviationAlert"):
        return "normal"
    return "premium" if metrics["referencePremiumPercent"] > 0 else "discount"


def convergence_drift(current_price, reference_price, step_fraction, annual_speed=0.25):
    """Log-price pull toward indicative parity, scaled to an annual step fraction."""
    try:
        current, reference, fraction, speed = map(
            float, (current_price, reference_price, step_fraction, annual_speed)
        )
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if (not all(math.isfinite(value) for value in (current, reference, fraction, speed))
            or current <= 0 or reference <= 0 or fraction < 0 or speed < 0):
        return 0.0
    return speed * math.log(reference/current) * fraction
