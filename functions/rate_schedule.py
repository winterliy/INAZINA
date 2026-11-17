"""Mixed and stepped contract interest rates.

A schedule is an ordered list of segments, each starting on a contract day:
``fixed`` segments carry their own annual percent, ``floating`` ones add a
spread to a reference rate supplied at reset time. Optional lifetime floor and
cap, a per-reset movement limit and a rounding step apply to every result.
Simulator contract terms: not a statement of any jurisdiction's lending rules.
"""
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _percent(value, name):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{name} 값이 숫자가 아닙니다.") from None
    if not number.is_finite():
        raise ValueError(f"{name} 값이 유한한 숫자가 아닙니다.")
    return number


class RateSchedule:
    def __init__(self, segments, floor, cap, max_change_per_reset, rounding_step):
        self._segments = segments  # [(from_day, kind, value)], strictly increasing, starting at day 0
        self._floor, self._cap = floor, cap
        self._max_change, self._step = max_change_per_reset, rounding_step

    @classmethod
    def parse(cls, segments, *, floor=None, cap=None, max_change_per_reset=None, rounding_step=None):
        parsed, previous = [], None
        for segment in segments or ():
            day, kind = segment.get("from_day"), segment.get("kind")
            if isinstance(day, bool) or not isinstance(day, int) or day < 0:
                raise ValueError("구간 시작일은 0 이상의 정수여야 합니다.")
            if previous is None and day != 0:
                raise ValueError("첫 금리 구간은 계약 0일에 시작해야 합니다.")
            if previous is not None and day <= previous:
                raise ValueError("금리 구간 시작일은 순서대로 증가해야 합니다.")
            if kind not in ("fixed", "floating"):
                raise ValueError("금리 구간 종류는 fixed 또는 floating이어야 합니다.")
            field = "rate" if kind == "fixed" else "spread"
            if segment.get(field) is None:
                raise ValueError(f"{kind} 구간에는 {field} 값이 필요합니다.")
            parsed.append((day, kind, _percent(segment[field], field)))
            previous = day
        if not parsed:
            raise ValueError("금리 구간이 하나 이상 필요합니다.")
        floor = None if floor is None else _percent(floor, "floor")
        cap = None if cap is None else _percent(cap, "cap")
        change = None if max_change_per_reset is None else _percent(max_change_per_reset, "max_change_per_reset")
        step = None if rounding_step is None else _percent(rounding_step, "rounding_step")
        if floor is not None and cap is not None and floor > cap:
            raise ValueError("금리 하한이 상한보다 클 수 없습니다.")
        if change is not None and change < 0:
            raise ValueError("1회 변동폭 제한은 0 이상이어야 합니다.")
        if step is not None and step <= 0:
            raise ValueError("금리 반올림 단위는 0보다 커야 합니다.")
        return cls(parsed, floor, cap, change, step)

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        return cls.parse(data["segments"], floor=data.get("floor"), cap=data.get("cap"),
                         max_change_per_reset=data.get("max_change_per_reset"),
                         rounding_step=data.get("rounding_step"))

    def to_json(self):
        def text(value):
            return None if value is None else str(value)
        return json.dumps({
            "segments": [{"from_day": day, "kind": kind, ("rate" if kind == "fixed" else "spread"): str(value)}
                         for day, kind, value in self._segments],
            "floor": text(self._floor), "cap": text(self._cap),
            "max_change_per_reset": text(self._max_change), "rounding_step": text(self._step),
        }, sort_keys=True)

    def segment_boundaries(self):
        """Contract days on which the applicable segment changes (a reset is due on each)."""
        return [day for day, _, _ in self._segments]

    def _segment(self, contract_day):
        if isinstance(contract_day, bool) or not isinstance(contract_day, int) or contract_day < 0:
            raise ValueError("계약 경과일은 0 이상의 정수여야 합니다.")
        return next(segment for segment in reversed(self._segments) if segment[0] <= contract_day)

    def _bounded(self, rate):
        if self._step is not None:
            rate = (rate / self._step).to_integral_value(rounding=ROUND_HALF_UP) * self._step
        if self._floor is not None:
            rate = max(rate, self._floor)
        if self._cap is not None:
            rate = min(rate, self._cap)
        return rate

    def rate_at(self, contract_day, *, reference_rate=None):
        """The contract rate on ``contract_day``, ignoring the per-reset limit."""
        _, kind, value = self._segment(contract_day)
        if kind == "fixed":
            return self._bounded(value)
        if reference_rate is None:
            raise ValueError("변동 구간에는 지표금리가 필요합니다.")
        return self._bounded(_percent(reference_rate, "reference_rate") + value)

    def reset(self, previous_rate, contract_day, *, reference_rate=None):
        """The rate after a reset on ``contract_day``; a floating reset moves at most the per-reset limit."""
        target = self.rate_at(contract_day, reference_rate=reference_rate)
        if self._segment(contract_day)[1] == "fixed" or self._max_change is None:
            return target
        previous = _percent(previous_rate, "previous_rate")
        return self._bounded(min(max(target, previous - self._max_change), previous + self._max_change))
