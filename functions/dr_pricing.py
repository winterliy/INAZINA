"""Indicative depositary-receipt reference pricing helpers."""

import math


DEVIATION_ALERT_THRESHOLD_PERCENT = 10.0


def reference_metrics(dr_price, underlying_price, underlying_per_receipt, fx_rate):
    """Return an indicative DR value and deviation warning for web clients."""
    try:
        values = tuple(float(value) for value in
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
        "referenceValue": reference,
        "referencePremiumPercent": premium,
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
