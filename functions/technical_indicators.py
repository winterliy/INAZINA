"""OHLCV indicators. None denotes unavailable/warm-up data, never a fake zero."""

import math
from collections import deque
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
UPPER_INDICATORS = {
    "high_low": "최고최저가", "sma": "이동평균선", "ichimoku": "일목균형표",
    "bollinger": "볼린저 밴드", "supertrend": "슈퍼트렌드", "volume_profile": "매물대분석",
    "envelope": "엔벨로프", "fractals": "윌리엄스 프랙탈", "ema": "지수이동평균선",
    "sar": "파라볼릭 SAR", "price_channel": "프라이스 채널", "vwap": "VWAP",
    "anchored_vwap": "고정 VWAP",
}
LOWER_INDICATORS = {
    "volume": "거래량", "mass": "매스 인덱스", "momentum": "모멘텀",
    "volume_osc": "볼륨 오실레이터", "bb_percent": "볼린저 밴드 %B", "bb_width": "볼린저 밴드폭",
    "stochastic": "스토캐스틱", "stoch_rsi": "스토캐스틱 RSI", "williams_r": "윌리엄스 R",
    "disparity": "이격도", "intraday_intensity": "일중 강도지수", "chaikin": "체이킨 오실레이터",
    "trix": "트릭스", "price_osc": "프라이스 오실레이터", "ad": "AD 라인", "adx": "ADX",
    "cci": "CCI", "dmi": "DMI", "macd": "MACD", "mfi": "MFI", "obv": "OBV",
    "roc": "ROC", "rmi": "RMI", "rsi": "RSI",
}
COLORS = [(250, 204, 21), (52, 211, 153), (192, 132, 252), (251, 146, 60), (34, 211, 238)]


def candle_interval(period):
    for maximum, interval in ((60, 1), (600, 10), (1800, 30), (3600, 60),
                              (86400, 900), (604800, 3600)):
        if period <= maximum:
            return interval
    return 21600


def build_candles(points, volumes, interval):
    buckets = {}
    for epoch, price in sorted(points):
        epoch, price = int(epoch), float(price)
        if not math.isfinite(price) or price < 0:
            continue
        bucket = epoch // interval * interval
        candle = buckets.get(bucket)
        if candle is None:
            buckets[bucket] = dict(time=bucket, open=price, high=price, low=price, close=price, volume=0)
        else:
            candle["high"] = max(candle["high"], price)
            candle["low"] = min(candle["low"], price)
            candle["close"] = price
    for epoch, quantity in volumes or ():
        bucket = int(epoch) // interval * interval
        if bucket in buckets:
            buckets[bucket]["volume"] += max(0, int(quantity))
    return [buckets[key] for key in sorted(buckets)]


def rolling(values, period, fn):
    window = deque(maxlen=period)
    result = []
    for value in values:
        window.append(value)
        result.append(fn(list(window)) if len(window) == period and all(v is not None for v in window) else None)
    return result


def sma(values, period):
    window, total, missing, result = deque(), 0, 0, []
    for value in values:
        window.append(value)
        if value is None: missing += 1
        else: total += value
        if len(window) > period:
            removed = window.popleft()
            if removed is None: missing -= 1
            else: total -= removed
        result.append(total / period if len(window) == period and not missing else None)
    return result


def _std(values):
    mean = sum(values) / len(values)
    return math.sqrt(sum((value-mean)**2 for value in values) / len(values))


def _mean_deviation(values):
    mean = sum(values) / len(values)
    return sum(abs(value-mean) for value in values) / len(values)


def ema(values, period, wilder=False):
    result, seed, previous = [], [], None
    alpha = 1 / period if wilder else 2 / (period + 1)
    for value in values:
        if value is None:
            result.append(None)
            seed, previous = [], None
            continue
        if previous is None:
            seed.append(value)
            if len(seed) < period:
                result.append(None)
                continue
            previous = sum(seed) / period
        else:
            previous += alpha * (value - previous)
        result.append(previous)
    return result


def ratio(numerator, denominator, scale=1):
    return [a / b * scale if a is not None and b is not None and b != 0 else None
            for a, b in zip(numerator, denominator)]


def difference(a, b):
    return [x - y if x is not None and y is not None else None for x, y in zip(a, b)]


def strength(values, period=14, momentum=1):
    changes = [None] * min(momentum, len(values)) + [values[i] - values[i - momentum] for i in range(momentum, len(values))]
    gains = ema([max(0, v) if v is not None else None for v in changes], period, True)
    losses = ema([max(0, -v) if v is not None else None for v in changes], period, True)
    return [None if g is None or loss is None else
            50.0 if g == 0 and loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + g / loss)
            for g, loss in zip(gains, losses)]


def _series(label, values, index=0, kind="line", levels=()):
    return {"label": label, "values": values, "color": COLORS[index % len(COLORS)], "kind": kind, "levels": levels}


def calculate_indicators(candles, upper=(), lower=(), anchor_epoch=None):
    n = len(candles)
    if not n or (not upper and not lower):
        return {}, {}
    c = [v["close"] for v in candles]
    h = [v["high"] for v in candles]
    l = [v["low"] for v in candles]
    v = [v["volume"] for v in candles]
    typical = [(a + b + close) / 3 for a, b, close in zip(h, l, c)]
    upper_keys, lower_keys = set(upper), set(lower)
    need_bands = bool(upper_keys & {'bollinger'} or lower_keys & {'bb_percent','bb_width'})
    mid20 = sma(c, 20) if need_bands or upper_keys & {'sma','envelope'} or lower_keys & {'disparity'} else []
    deviation = rolling(c, 20, _std) if need_bands else []
    bb_hi = [m + 2 * s if m is not None else None for m, s in zip(mid20, deviation)]
    bb_lo = [m - 2 * s if m is not None else None for m, s in zip(mid20, deviation)]
    hi14, lo14 = (rolling(h, 14, max), rolling(l, 14, min)) if lower_keys & {'stochastic','williams_r'} else ([],[])
    rsi = strength(c) if lower_keys & {'rsi','stoch_rsi'} else []
    tr = ([h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
          if upper_keys & {'supertrend'} or lower_keys & {'adx','dmi'} else [])
    ad, accumulation = [], 0.0
    intensity = [(2 * close - hi - lo) / (hi - lo) * volume if hi != lo else 0.0
                 for close, hi, lo, volume in zip(c, h, l, v)] if lower_keys & {'intraday_intensity','chaikin','ad'} else []
    for amount in intensity:
        accumulation += amount
        ad.append(accumulation)
    upper_result, lower_result = {}, {}
    for key in upper:
        series = []
        if key in ("high_low", "price_channel"):
            period = n if key == "high_low" else 20
            series = [_series(f"HIGH{period}", rolling(h, period, max)), _series(f"LOW{period}", rolling(l, period, min), 1)]
            if key == "high_low":
                series = [_series("HIGH", [max(h)] * n), _series("LOW", [min(l)] * n, 1)]
        elif key in ("sma", "ema"):
            fn = sma if key == "sma" else ema
            series = [_series(f"{key.upper()}{p}", (mid20 if key == 'sma' and p == 20 else fn(c, p)), i) for i, p in enumerate((5, 20, 60))]
        elif key == "bollinger":
            series = [_series("BB20", mid20), _series("BB+2", bb_hi, 1), _series("BB-2", bb_lo, 1)]
        elif key == "envelope":
            series = [_series("ENV+2.5%", [x * 1.025 if x is not None else None for x in mid20]),
                      _series("ENV-2.5%", [x * .975 if x is not None else None for x in mid20], 1)]
        elif key == "ichimoku":
            def midpoint(period):
                return [None if a is None else (a + b) / 2 for a, b in zip(rolling(h, period, max), rolling(l, period, min))]
            conversion, base, span_b = midpoint(9), midpoint(26), midpoint(52)
            span_a = [None if a is None or b is None else (a + b) / 2 for a, b in zip(conversion, base)]
            series = [_series("TENKAN9", conversion), _series("KIJUN26", base, 1),
                      _series("SPAN A", [None] * 26 + span_a, 2), _series("SPAN B", [None] * 26 + span_b, 3),
                      _series("CHIKOU", c[26:] + [None] * min(26, n), 4)]
        elif key in ("vwap", "anchored_vwap"):
            numerator = denominator = 0.0
            day, values = None, []
            for candle, price, volume in zip(candles, typical, v):
                this_day = datetime.fromtimestamp(candle["time"], KST).date()
                if key == "vwap" and day != this_day:
                    numerator = denominator = 0.0
                    day = this_day
                if key == "anchored_vwap" and anchor_epoch is not None and candle["time"] < anchor_epoch:
                    values.append(None)
                    continue
                numerator += price * volume
                denominator += volume
                values.append(numerator / denominator if denominator else None)
            series = [_series("VWAP" if key == "vwap" else "AVWAP", values)]
        elif key == "fractals":
            highs, lows = [None] * n, [None] * n
            for i in range(2, n - 2):
                if all(h[i] > h[j] for j in (i - 2, i - 1, i + 1, i + 2)):
                    highs[i] = h[i]
                if all(l[i] < l[j] for j in (i - 2, i - 1, i + 1, i + 2)):
                    lows[i] = l[i]
            series = [_series("FRACTAL H", highs, 0, "marker"), _series("FRACTAL L", lows, 1, "marker")]
        elif key == "sar":
            values = [l[0]]
            rising, extreme, acceleration = True, h[0], .02
            for i in range(1, n):
                candidate = values[-1] + acceleration * (extreme - values[-1])
                if rising:
                    candidate = min(candidate, l[i - 1], l[max(0, i - 2)])
                    if l[i] < candidate:
                        candidate, rising, extreme, acceleration = extreme, False, l[i], .02
                    elif h[i] > extreme:
                        extreme, acceleration = h[i], min(.2, acceleration + .02)
                else:
                    candidate = max(candidate, h[i - 1], h[max(0, i - 2)])
                    if h[i] > candidate:
                        candidate, rising, extreme, acceleration = extreme, True, h[i], .02
                    elif l[i] < extreme:
                        extreme, acceleration = l[i], min(.2, acceleration + .02)
                values.append(candidate)
            series = [_series("SAR .02/.2", values, 0, "marker")]
        elif key == "supertrend":
            atr = ema(tr, 10, True)
            values, final_upper, final_lower, trend = [], None, None, -1
            for i in range(n):
                if atr[i] is None:
                    values.append(None)
                    continue
                upper_band, lower_band = (h[i] + l[i]) / 2 + 3 * atr[i], (h[i] + l[i]) / 2 - 3 * atr[i]
                previous_upper, previous_lower = final_upper, final_lower
                final_upper = upper_band if final_upper is None or upper_band < final_upper or c[i - 1] > final_upper else final_upper
                final_lower = lower_band if final_lower is None or lower_band > final_lower or c[i - 1] < final_lower else final_lower
                if previous_upper is not None and trend == -1 and c[i] > final_upper:
                    trend = 1
                elif previous_lower is not None and trend == 1 and c[i] < final_lower:
                    trend = -1
                values.append(final_lower if trend == 1 else final_upper)
            series = [_series("SUPERTREND10/3", values)]
        elif key == "volume_profile":
            # Rendering uses actual trade prices when provided, otherwise OHLC typical price.
            series = [_series("VOLUME PROFILE", typical, 0, "profile")]
        upper_result[key] = series

    for key in lower:
        series = []
        if key == "volume":
            series = [_series("VOLUME", v, 0, "bar")]
        elif key == "mass":
            first = ema(difference(h, l), 9)
            series = [_series("MASS9/25", rolling(ratio(first, ema(first, 9)), 25, sum), levels=(26.5, 27))]
        elif key in ("momentum", "roc"):
            values = [None] * min(14, n) + [c[i] - c[i - 14] if key == "momentum" else
                       (c[i] / c[i - 14] - 1) * 100 if c[i - 14] else None for i in range(14, n)]
            series = [_series(key.upper() + "14", values, levels=(0,))]
        elif key in ("volume_osc", "price_osc"):
            source, fast, slow = (v, 5, 10) if key == "volume_osc" else (c, 12, 26)
            series = [_series(key.upper(), ratio(difference(ema(source, fast), ema(source, slow)), ema(source, slow), 100), levels=(0,))]
        elif key == "bb_percent":
            series = [_series("BB %B", ratio(difference(c, bb_lo), difference(bb_hi, bb_lo)), levels=(0, 1))]
        elif key == "bb_width":
            series = [_series("BB WIDTH %", ratio(difference(bb_hi, bb_lo), mid20, 100))]
        elif key in ("stochastic", "williams_r"):
            raw_k = ratio(difference(c, lo14), difference(hi14, lo14), 100)
            if key == "williams_r":
                series = [_series("WILLIAMS R14", [x - 100 if x is not None else None for x in raw_k], levels=(-80, -20))]
            else:
                k = sma(raw_k, 3)
                series = [_series("STOCH K14/3", k, levels=(20, 80)), _series("D3", sma(k, 3), 1)]
        elif key == "stoch_rsi":
            k = sma(ratio(difference(rsi, rolling(rsi, 14, min)),
                          difference(rolling(rsi, 14, max), rolling(rsi, 14, min)), 100), 3)
            series = [_series("STOCH RSI K", k, levels=(20, 80)), _series("D3", sma(k, 3), 1)]
        elif key == "disparity":
            series = [_series("DISPARITY20", ratio(c, mid20, 100), levels=(100,))]
        elif key == "intraday_intensity":
            series = [_series("INTRADAY INTENSITY", intensity, 0, "bar", levels=(0,))]
        elif key == "chaikin":
            series = [_series("CHAIKIN3/10", difference(ema(ad, 3), ema(ad, 10)), levels=(0,))]
        elif key == "trix":
            triple = ema(ema(ema(c, 18), 18), 18)
            values = [None] + [None if triple[i] is None or triple[i - 1] in (None, 0) else
                               (triple[i] / triple[i - 1] - 1) * 100 for i in range(1, n)]
            series = [_series("TRIX18", values, levels=(0,))]
        elif key == "ad":
            series = [_series("AD LINE", ad)]
        elif key in ("adx", "dmi"):
            up, down = [None], [None]
            for i in range(1, n):
                plus, minus = h[i] - h[i - 1], l[i - 1] - l[i]
                up.append(plus if plus > 0 and plus > minus else 0)
                down.append(minus if minus > 0 and minus > plus else 0)
            atr = ema([None] + tr[1:], 14, True)
            positive, negative = ratio(ema(up, 14, True), atr, 100), ratio(ema(down, 14, True), atr, 100)
            if key == "dmi":
                series = [_series("+DI14", positive), _series("-DI14", negative, 1)]
            else:
                dx = [None if a is None or b is None else 0 if a + b == 0 else abs(a - b) / (a + b) * 100
                      for a, b in zip(positive, negative)]
                series = [_series("ADX14", ema(dx, 14, True), levels=(25,))]
        elif key == "cci":
            mean = sma(typical, 20)
            deviation = rolling(typical, 20, _mean_deviation)
            series = [_series("CCI20", ratio(difference(typical, mean), deviation, 1 / .015), levels=(-100, 100))]
        elif key == "macd":
            macd = difference(ema(c, 12), ema(c, 26))
            signal = ema(macd, 9)
            series = [_series("MACD12/26", macd), _series("SIGNAL9", signal, 1),
                      _series("HIST", difference(macd, signal), 2, "bar", levels=(0,))]
        elif key == "mfi":
            positive, negative = [None], [None]
            for i in range(1, n):
                positive.append(typical[i] * v[i] if typical[i] > typical[i - 1] else 0)
                negative.append(typical[i] * v[i] if typical[i] < typical[i - 1] else 0)
            pos, neg = rolling(positive, 14, sum), rolling(negative, 14, sum)
            values = [None if a is None or b is None or a + b == 0 else 100 if b == 0 else 100 - 100 / (1 + a / b)
                      for a, b in zip(pos, neg)]
            series = [_series("MFI14", values, levels=(20, 80))]
        elif key == "obv":
            values, total = [0], 0
            for i in range(1, n):
                total += v[i] * (1 if c[i] > c[i - 1] else -1 if c[i] < c[i - 1] else 0)
                values.append(total)
            series = [_series("OBV", values)]
        elif key in ("rmi", "rsi"):
            series = [_series("RMI20/5" if key == "rmi" else "RSI14", strength(c, 20, 5) if key == "rmi" else rsi, levels=(30, 70))]
        lower_result[key] = series
    return upper_result, lower_result
