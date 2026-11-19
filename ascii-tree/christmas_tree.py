#!/usr/bin/env python3
"""ASCII Christmas Tree — a rotating 3D point-cloud tree drawn in the terminal.
Keys: space pause · r restart · q quit"""
import math
import random

from ascii3d import Canvas, rot_y, run, smooth

TOP, BOTTOM, R = 1.15, -0.75, 0.85
TIERS = 6
T_GROW, T_LIGHTS, T_GIFTS, T_MSG = 6.0, 6.5, 7.5, 8.5
MESSAGE = "MERRY CHRISTMAS 2026"
RED, GOLD, GREEN, BLUE, WHITE = (230, 40, 40), (255, 196, 60), (60, 200, 90), (60, 110, 255), (240, 240, 255)

rng = random.Random(42)

# Tree needles: points on a tiered cone. h = 0 at the tip, 1 at the base.
NEEDLES = []
for _ in range(4200):
    h = rng.random() ** 0.8
    local = (h * TIERS) % 1.0                       # each tier flares out then steps back in
    r = R * (0.08 + 0.92 * h) * (0.55 + 0.45 * local) * (0.75 + 0.25 * rng.random())
    a = rng.uniform(0, math.tau)
    y = TOP - h * (TOP - BOTTOM) - local * 0.06
    g = rng.choice([(30, 120, 50), (40, 150, 60), (50, 175, 70), (70, 200, 90)])
    NEEDLES.append((h, r * math.cos(a), y, r * math.sin(a), g, rng.random()))

# Ornaments sit on the outer surface.
ORNAMENTS = []
for _ in range(110):
    h = rng.uniform(0.12, 0.97)
    local = (h * TIERS) % 1.0
    r = R * (0.08 + 0.92 * h) * (0.55 + 0.45 * local) * 1.02
    a = rng.uniform(0, math.tau)
    ORNAMENTS.append((h, r * math.cos(a), TOP - h * (TOP - BOTTOM), r * math.sin(a),
                      rng.choice([RED, GOLD, BLUE, WHITE, RED]), rng.uniform(0, 6.3)))

# Presents: (x, z, w, h, d, box color, ribbon color)
GIFTS = [(-0.75, 0.25, 0.32, 0.26, 0.3, BLUE, GOLD), (0.72, 0.1, 0.36, 0.3, 0.32, RED, GOLD),
         (0.35, 0.55, 0.26, 0.2, 0.26, RED, WHITE), (-0.3, 0.6, 0.24, 0.18, 0.24, GOLD, RED)]
GIFT_PTS = []
for gx, gz, w, hh, d, col, rib in GIFTS:
    for _ in range(int(900 * w * hh)):
        u, v = rng.random(), rng.random()
        face = rng.random()
        if face < 0.5:                                   # front/back faces
            x, y, z = gx + (u - 0.5) * w, BOTTOM - 0.22 + v * hh, gz + (0.5 if face < 0.25 else -0.5) * d
        else:                                            # top face
            x, y, z = gx + (u - 0.5) * w, BOTTOM - 0.22 + hh, gz + (v - 0.5) * d
        ribbon = abs(x - gx) < w * 0.09 or abs(z - gz) < d * 0.09
        GIFT_PTS.append((x, y, z, rib if ribbon else col))

SNOW = [[rng.uniform(-2.5, 2.5), rng.uniform(-1.2, 1.6), rng.uniform(-1, 1), rng.uniform(0.08, 0.2)] for _ in range(140)]


def draw(cv: Canvas, t):
    spin = t * 0.45
    grow = smooth(t / T_GROW) * 1.08                    # how far down the tree is revealed
    light = smooth((t - T_LIGHTS) / 1.5)

    for h, x, y, z, col, j in NEEDLES:
        if h > grow:
            continue
        x, z = rot_y(x, z, spin)
        fresh = smooth((grow - h) / 0.08)               # new branches fade in
        b = (0.35 + 0.45 * (0.5 - z / 2) + 0.2 * j) * fresh
        cv.point(x, y, z, col, b)

    # trunk
    for i in range(60):
        a = i * 0.7 + spin
        y = BOTTOM - 0.22 * (i / 60)
        if grow >= 1:
            cv.point(0.07 * math.cos(a), y, 0.07 * math.sin(a), (120, 75, 35), 0.6)

    for h, x, y, z, col, ph in ORNAMENTS:
        if h > grow or light <= 0:
            continue
        x, z = rot_y(x, z, spin)
        blink = 0.55 + 0.45 * math.sin(t * 3 + ph)
        cv.point(x, y, z - 0.02, col, light * blink)

    # star on top
    if grow > 0.02:
        pulse = 0.75 + 0.25 * math.sin(t * 4)
        for k in range(10):
            a = k * math.tau / 10 + spin * 0.5
            rr = 0.13 if k % 2 == 0 else 0.055
            for s in (0.4, 0.7, 1.0):
                cv.point(rr * s * math.cos(a), TOP + 0.12 + rr * s * math.sin(a), -0.05, GOLD, pulse)
        cv.point(0, TOP + 0.12, -0.06, (255, 240, 180), 1)

    gifts = smooth((t - T_GIFTS) / 1.0)
    if gifts > 0:
        for x, y, z, col in GIFT_PTS:
            cv.point(x, y - (1 - gifts) * 0.4, z, col, 0.45 + 0.4 * gifts)

    for f in SNOW:
        f[1] -= f[3] / 24
        f[0] += math.sin(t + f[2] * 5) * 0.004
        if f[1] < -1.3:
            f[1] = 1.6
        cv.point(f[0], f[1], f[2], WHITE, 0.25)

    if t > T_MSG:
        shown = min(len(MESSAGE), int((t - T_MSG) * 10))
        row = min(cv.rows - 2, int(cv.cy + 1.15 * cv.sy))
        col0 = (cv.cols - len(MESSAGE) * 2) // 2
        palette = [RED, GOLD, GREEN]
        spaced = " ".join(MESSAGE[:shown])
        cv.text(row, col0, spaced, lambda i: palette[(i // 2 + int(t * 1.5)) % 3])


if __name__ == "__main__":
    run(draw)
