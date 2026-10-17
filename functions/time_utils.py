"""Shared validation for externally supplied UNIX epoch timestamps."""

import math
import time


MAX_SUPPORTED_EPOCH = 253402300799.0  # 9999-12-31 23:59:59 UTC


def validate_epoch(value):
    """Return a finite, supported UTC epoch; use wall time when omitted."""
    if value is None:
        value = time.time()
    if isinstance(value, bool):
        raise ValueError("시각은 유효한 UNIX epoch이어야 합니다.")
    try:
        epoch = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("시각은 유효한 UNIX epoch이어야 합니다.") from exc
    if not math.isfinite(epoch) or not 0 <= epoch <= MAX_SUPPORTED_EPOCH:
        raise ValueError("시각은 유효한 UNIX epoch이어야 합니다.")
    return epoch
