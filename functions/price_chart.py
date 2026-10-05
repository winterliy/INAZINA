"""Small dependency-free PNG renderer for market movement charts."""

import math
from functools import lru_cache
import struct
import time
import zlib
from datetime import datetime, timedelta, timezone


_FONT = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "111", "100", "111"),
    "3": ("110", "001", "111", "001", "110"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "110", "001", "110"),
    "6": ("011", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "110"),
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("011", "100", "100", "100", "011"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("011", "100", "101", "101", "011"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "010"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    "O": ("010", "101", "101", "101", "010"),
    "P": ("110", "101", "110", "100", "100"),
    "Q": ("010", "101", "101", "111", "011"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("011", "100", "010", "001", "110"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
    ".": ("000", "000", "000", "000", "010"),
    ",": ("000", "000", "000", "010", "100"),
    ":": ("000", "010", "000", "010", "000"),
    "/": ("001", "001", "010", "100", "100"),
    "-": ("000", "000", "111", "000", "000"),
    "+": ("000", "010", "111", "010", "000"),
    "%": ("101", "001", "010", "100", "101"),
    " ": ("000", "000", "000", "000", "000"),
}


class _Canvas:
    def __init__(self, width, height, color):
        self.width = width
        self.height = height
        self.pixels = bytearray(color * (width * height))

    def pixel(self, x, y, color):
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = (y * self.width + x) * 3
            self.pixels[offset:offset + 3] = bytes(color)

    def line(self, x0, y0, x1, y1, color, thickness=1):
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        error = dx - dy
        radius = max(0, thickness - 1) // 2
        while True:
            for oy in range(-radius, radius + 1):
                for ox in range(-radius, radius + 1):
                    self.pixel(x0 + ox, y0 + oy, color)
            if x0 == x1 and y0 == y1:
                break
            twice = 2 * error
            if twice > -dy:
                error -= dy
                x0 += sx
            if twice < dx:
                error += dx
                y0 += sy

    def fill_rect(self, x0, y0, x1, y1, color):
        left, right = sorted((x0, x1))
        top, bottom = sorted((y0, y1))
        left, right = max(0,left), min(self.width-1,right)
        top, bottom = max(0,top), min(self.height-1,bottom)
        if left > right or top > bottom:
            return
        row = bytes(color) * (right-left+1)
        for y in range(top, bottom + 1):
            offset = (y*self.width+left)*3
            self.pixels[offset:offset+len(row)] = row

    def text(self, x, y, value, color, scale=2):
        cursor = x
        for char in str(value).upper():
            glyph = _FONT.get(char)
            if glyph is None:
                cursor += 4 * scale
                continue
            for row, pattern in enumerate(glyph):
                for column, bit in enumerate(pattern):
                    if bit == "1":
                        for oy in range(scale):
                            for ox in range(scale):
                                self.pixel(cursor + column * scale + ox, y + row * scale + oy, color)
            cursor += 4 * scale

    def png(self):
        raw = b"".join(
            b"\x00" + self.pixels[y * self.width * 3:(y + 1) * self.width * 3]
            for y in range(self.height)
        )

        def chunk(kind, data):
            return (
                struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
            )

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">2I5B", self.width, self.height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b"")
        )


def render_price_chart(
    points, period_seconds, current_price=None, width=960, height=500,
    integer_prices=False, volumes=None, upper_indicators=None, lower_indicators=None,
    anchor_epoch=None, end_epoch=None, trades=None, candle_seconds=None, horizontal_levels=None,
):
    """Reuse identical immutable chart inputs, including the exact visible end time."""
    if end_epoch is None:
        from functions.indicator_chart import render_chart
        return render_chart(points,period_seconds,current_price,width,height,integer_prices,volumes,
            upper_indicators,lower_indicators,anchor_epoch,end_epoch,trades,candle_seconds,horizontal_levels)
    return _cached_price_chart(tuple(map(tuple,points)),period_seconds,current_price,width,height,integer_prices,
        tuple(map(tuple,volumes)) if volumes is not None else None,
        tuple(upper_indicators) if upper_indicators is not None else None,
        tuple(lower_indicators) if lower_indicators is not None else None,anchor_epoch,end_epoch,
        tuple(map(tuple,trades)) if trades is not None else None,candle_seconds,
        tuple(tuple(level.items()) for level in horizontal_levels) if horizontal_levels is not None else None)


@lru_cache(maxsize=16)
def _cached_price_chart(points,period_seconds,current_price,width,height,integer_prices,volumes,
    upper_indicators,lower_indicators,anchor_epoch,end_epoch,trades,candle_seconds,horizontal_levels):
    from functions.indicator_chart import render_chart
    return render_chart(points,period_seconds,current_price,width,height,integer_prices,volumes,
        upper_indicators,lower_indicators,anchor_epoch,end_epoch,trades,candle_seconds,
        [dict(level) for level in horizontal_levels] if horizontal_levels is not None else None)


def render_rate_chart(points, period_seconds, end_epoch=None, width=960, height=500,
                      sample_seconds=None):
    """Render dated annual percentages as a step chart, without market candles."""
    end = float(time.time() if end_epoch is None else end_epoch)
    period = float(period_seconds)
    if not math.isfinite(end) or not math.isfinite(period) or period <= 0:
        raise ValueError("금리 그래프 조회 기간이 올바르지 않습니다.")
    if not isinstance(width, int) or not isinstance(height, int) or width < 400 or height < 220:
        raise ValueError("금리 그래프 크기는 최소 400×220이어야 합니다.")
    start = end - period
    series = []
    for epoch, rate in points:
        epoch, rate = float(epoch), float(rate)
        if not math.isfinite(epoch) or not math.isfinite(rate) or not 0 <= rate <= 100:
            raise ValueError("금리 기록은 유한한 시각과 0~100% 연이율이어야 합니다.")
        if epoch <= end:
            series.append((epoch, rate))
    series.sort(key=lambda point: point[0])
    if sample_seconds is not None:
        sample_seconds=float(sample_seconds)
        if not math.isfinite(sample_seconds) or sample_seconds<=0:
            raise ValueError("금리 그래프 주기가 올바르지 않습니다.")
        series=[(math.floor(epoch/sample_seconds)*sample_seconds,rate) for epoch,rate in series]
    # Input order breaks same-time ties, matching effective_epoch/history_id.
    unique = []
    for point in series:
        if unique and unique[-1][0] == point[0]:
            unique[-1] = point
        else:
            unique.append(point)
    baseline = 0.0
    for epoch, rate in unique:
        if epoch <= start:
            baseline = rate
        else:
            break
    visible = [(start, baseline)] + [(epoch, rate) for epoch, rate in unique if start < epoch <= end]
    if visible[-1][0] != end:
        visible.append((end, visible[-1][1]))

    background, grid, text = (11, 18, 32), (38, 52, 73), (166, 181, 201)
    color = (245, 190, 65)
    canvas = _Canvas(width, height, background)
    left, right, top, bottom = 100, width - 28, 54, height - 66
    values = [rate for _, rate in visible]
    minimum, maximum = min(values), max(values)
    margin = max((maximum - minimum) * .1, abs(maximum) * .025, .2)
    low, high = max(0, minimum - margin), min(100, maximum + margin)
    if low == high:
        low, high = (0, .2) if maximum == 0 else (99.8, 100)

    def x_for(epoch):
        return left + round((epoch - start) / period * (right - left))

    def y_for(rate):
        return bottom - round((rate - low) / (high - low) * (bottom - top))

    for i in range(6):
        y = top + round(i * (bottom - top) / 5)
        canvas.line(left, y, right, y, grid)
        value = high - (high - low) * i / 5
        canvas.text(4, y - 5, f"{value:.3f}%", text, scale=2)
    kst = timezone(timedelta(hours=9))
    for i in range(6):
        x = left + round(i * (right - left) / 5)
        canvas.line(x, top, x, bottom, grid)
        dt = datetime.fromtimestamp(start + period * i / 5, kst)
        label = dt.strftime("%H:%M:%S" if period <= 60 else "%H:%M" if period <= 3600 else "%m/%d %H:%M")
        canvas.text(max(0, min(width - len(label) * 8, x - len(label) * 4)), height - 42, label, text)
    # Each value stays constant until the next effective change, then jumps.
    for (epoch, rate), (next_epoch, next_rate) in zip(visible, visible[1:]):
        x0, x1 = x_for(epoch), x_for(next_epoch)
        y0, y1 = y_for(rate), y_for(next_rate)
        canvas.line(x0, y0, x1, y0, color, thickness=3)
        canvas.line(x1, y0, x1, y1, color, thickness=3)
    canvas.line(left, 22, left + 20, 22, color, thickness=3)
    canvas.text(left + 30, 17, "ANNUAL RATE %", text)
    canvas.text(max(left, right - 180), 17, f"{visible[-1][1]:.4f}%", color)
    canvas.text(left, height - 16, "TIME KST", text, scale=1)
    if not unique:
        canvas.text(left + 24, top + 24, "NO RATE HISTORY", text)
    return canvas.png()
