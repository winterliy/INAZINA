"""Dependency-free multi-panel market charts with independent indicator scales."""

import math
import time
from datetime import datetime

from functions.price_chart import _Canvas
from functions.technical_indicators import build_candles, candle_interval, calculate_indicators, KST


def render_chart(points, period_seconds, current_price=None, width=960, height=500,
                 integer_prices=False, volumes=None, upper_indicators=None, lower_indicators=None,
                 anchor_epoch=None, end_epoch=None, trades=None, candle_seconds=None, horizontal_levels=None):
    background, grid, text = (11, 18, 32), (38, 52, 73), (166, 181, 201)
    rising, falling = (239, 68, 68), (59, 130, 246)
    end = int(time.time() if end_epoch is None else end_epoch)
    start = end - int(period_seconds)
    upper = tuple(("sma",) if upper_indicators is None else upper_indicators)
    lower = tuple(("volume",) if lower_indicators is None else lower_indicators)
    points = [(int(t), int(float(p) + .5) if integer_prices else float(p)) for t, p in points]
    if not points and current_price is not None:
        points = [(end, float(current_price))]
    interval = int(candle_seconds) if candle_seconds is not None else candle_interval(period_seconds)
    if interval <= 0:
        raise ValueError("캔들 주기는 0보다 커야 합니다.")
    candles = build_candles(points, volumes, interval)
    upper_data, lower_data = calculate_indicators(candles, upper, lower, anchor_epoch)
    visible = [(i, candle) for i, candle in enumerate(candles) if start < candle["time"] + interval and candle["time"] <= end]
    # Visible-range high/low; preceding candles are only indicator warm-up.
    if "high_low" in upper_data and visible:
        for series, value in zip(upper_data["high_low"], (max(c["high"] for _, c in visible), min(c["low"] for _, c in visible))):
            series["values"] = [value] * len(candles)
    levels = []
    for item in horizontal_levels or ():
        value = float(item['value'])
        if not math.isfinite(value) or value <= 0:
            raise ValueError('가격 기준선은 0보다 큰 유한한 값이어야 합니다.')
        levels.append({'label': str(item['label'])[:25], 'value': value,
                       'color': tuple(item.get('color', (251, 191, 36)))})
    legend = [s for group in upper_data.values() for s in group]
    legend_rows = max(1, math.ceil((len(legend) + len(levels)) / 5))
    left, right = 155, width - 28
    top, bottom = 34 + legend_rows * 17, 320 + legend_rows * 17
    panel_height = 128
    total_height = max(height, bottom + len(lower_data) * panel_height + 70)
    canvas = _Canvas(width, total_height, background)
    # The selected duration is the horizontal viewport, including Ichimoku.
    # Future spans remain calculated but are clipped outside this viewport.
    plot_end = end

    def x_for(epoch):
        return left + round((epoch - start) / max(1, plot_end - start) * (right - left))

    def series_epoch(i):
        if i < len(candles):
            return candles[i]["time"] + interval / 2
        return (candles[-1]["time"] if candles else end) + (i - len(candles) + 1.5) * interval

    def valid(value):
        return value is not None and math.isfinite(value)

    def in_range(i):
        return start <= series_epoch(i) <= plot_end

    extremes = [c[k] for _, c in visible for k in ("high", "low")]
    extremes.extend(item['value'] for item in levels)
    for group in upper_data.values():
        for series in group:
            if series["kind"] != "profile":
                extremes.extend(value for i, value in enumerate(series["values"]) if valid(value) and in_range(i))
    minimum, maximum = min(extremes, default=0), max(extremes, default=1)
    margin = max((maximum - minimum) * .08, abs(maximum) * .005, .01)
    price_min, price_max = minimum - margin, maximum + margin

    def mapper(minimum, maximum, upper_y, lower_y):
        spread = maximum - minimum or 1
        return lambda value: max(upper_y, min(lower_y, lower_y - round((value - minimum) / spread * (lower_y - upper_y))))

    price_y = mapper(price_min, price_max, top, bottom)
    for i in range(6):
        y = top + round(i * (bottom - top) / 5)
        canvas.line(left, y, right, y, grid)
        value = price_max - (price_max - price_min) * i / 5
        canvas.text(2, y - 5, f"{value:,.0f}" if integer_prices else f"{value:,.2f}", text, scale=2)
    for i in range(6):
        x = left + round(i * (right - left) / 5)
        canvas.line(x, top, x, total_height - 48, grid)
        epoch = start + (plot_end - start) * i / 5
        dt = datetime.fromtimestamp(epoch, KST)
        label = dt.strftime("%H:%M:%S" if period_seconds <= 60 else "%H:%M" if period_seconds <= 3600 else "%m/%d %H:%M")
        canvas.text(max(0, min(width - len(label) * 8, x - len(label) * 4)), total_height - 35, label, text)
    candle_width = max(2, min(12, round((right - left) * interval / max(1, plot_end - start) * .62)))
    if "ichimoku" in upper_data:
        span_a = upper_data["ichimoku"][2]["values"]
        span_b = upper_data["ichimoku"][3]["values"]
        for i, (a, b) in enumerate(zip(span_a, span_b)):
            if valid(a) and valid(b) and in_range(i):
                x = max(left, min(right, x_for(series_epoch(i))))
                color = (47, 31, 40) if a >= b else (24, 39, 62)
                canvas.fill_rect(x - candle_width // 2, price_y(a), x + candle_width // 2, price_y(b), color)
    for _, candle in visible:
        x = max(left, min(right, x_for(candle["time"] + interval / 2)))
        color = rising if candle["close"] >= candle["open"] else falling
        canvas.line(x, price_y(candle["high"]), x, price_y(candle["low"]), color)
        y0, y1 = price_y(candle["open"]), price_y(candle["close"])
        canvas.fill_rect(x - candle_width // 2, min(y0, y1), x + candle_width // 2, max(y0, y1), color)

    def draw_series(series, y_for, panel_top, panel_bottom):
        previous = None
        for i, value in enumerate(series["values"]):
            if not valid(value) or not in_range(i):
                previous = None
                continue
            x, y = min(right, max(left, x_for(series_epoch(i)))), y_for(value)
            if series["kind"] == "marker":
                canvas.fill_rect(x - 2, y - 2, x + 2, y + 2, series["color"])
            elif series["kind"] == "bar":
                color = rising if value >= 0 else falling
                if series["label"] == "VOLUME" and i < len(candles):
                    color = rising if candles[i]["close"] >= candles[i]["open"] else falling
                baseline = y_for(0)
                canvas.fill_rect(x - candle_width // 2, min(y, baseline), x + candle_width // 2, max(y, baseline), color)
            elif previous:
                canvas.line(*previous, x, y, series["color"])
            previous = (x, y)

    for i, series in enumerate(legend):
        x, y = left + i % 5 * 150, 10 + i // 5 * 17
        canvas.line(x, y + 4, x + 10, y + 4, series["color"])
        canvas.text(x + 15, y, series["label"], text, scale=1)
        if series["kind"] != "profile":
            draw_series(series, price_y, top, bottom)
    for index, level in enumerate(levels, len(legend)):
        x, y = left + index % 5 * 150, 10 + index // 5 * 17
        canvas.line(x, y + 4, x + 10, y + 4, level['color'])
        canvas.text(x + 15, y, level['label'], text, scale=1)
        level_y = price_y(level['value'])
        for segment_x in range(left, right, 12):
            canvas.line(segment_x, level_y, min(segment_x + 7, right), level_y,
                        level['color'], thickness=2)
    if "volume_profile" in upper:
        bins = [0] * 24
        profile_trades = [(epoch, quantity, price) for epoch, quantity, price in (trades or ()) if start <= epoch <= end]
        if not profile_trades:
            profile_trades = [(c["time"], c["volume"], (c["high"] + c["low"] + c["close"]) / 3) for _, c in visible]
        for _, quantity, price in profile_trades:
            i = max(0, min(23, int((price - price_min) / (price_max - price_min) * 24)))
            bins[i] += max(0, quantity)
        peak = max(bins, default=0)
        if peak:
            for i, volume in enumerate(bins):
                if volume:
                    y = price_y(price_min + (i + .5) / 24 * (price_max - price_min))
                    canvas.line(right - round(volume / peak * 115), y, right, y, (94, 80, 35), thickness=3)

    for panel, (key, group) in enumerate(lower_data.items()):
        panel_top = bottom + 22 + panel * panel_height
        panel_bottom = panel_top + panel_height - 30
        values = [value for series in group for i, value in enumerate(series["values"]) if valid(value) and in_range(i)]
        levels = sorted(set(level for series in group for level in series["levels"]))
        values.extend(levels)
        if any(s["kind"] == "bar" for s in group):
            values.append(0)
        low, high = min(values, default=0), max(values, default=1)
        if low == high:
            low, high = low - max(abs(low) * .05, 1), high + max(abs(high) * .05, 1)
        y_for = mapper(low, high, panel_top + 14, panel_bottom)
        canvas.line(left, panel_top, right, panel_top, grid)
        canvas.line(left, panel_bottom, right, panel_bottom, grid)
        for level in levels:
            canvas.line(left, y_for(level), right, y_for(level), grid)
        canvas.text(2, panel_top + 15, f"{high:,.2f}", text, scale=1)
        canvas.text(2, panel_bottom - 6, f"{low:,.2f}", text, scale=1)
        for i, series in enumerate(group):
            canvas.text(left + i * 190, panel_top + 3, series["label"], series["color"], scale=1)
            draw_series(series, y_for, panel_top, panel_bottom)
        if not any(valid(value) and in_range(i) for series in group for i, value in enumerate(series["values"])):
            canvas.text(left + 260, panel_top + 40, "WAITING FOR DATA", text)
    canvas.text(left, total_height - 14, "TIME KST", text, scale=1)
    return canvas.png()
