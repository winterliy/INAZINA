#!/usr/bin/env python3
"""ASCII Rose Bouquet — stems grow, 19 roses bloom one by one, petals fall, "FOR YOU <3".
Keys: space pause · r restart · q quit"""
import math
import random

from ascii3d import Canvas, rot_y, run, smooth

N_ROSES = 19
WAIST, BOTTOM_Y = (0.0, -0.45, 0.0), -1.05
T_STEMS, T_BLOOM, BLOOM_GAP, T_MSG, T_FALL = 2.5, 2.5, 0.35, 10.0, 9.0
MESSAGE = "FOR YOU <3"
REDS = [(200, 30, 45), (225, 50, 60), (170, 20, 40), (235, 110, 130), (215, 80, 100)]
GREEN, DARK_GREEN, GOLD, WHITE = (60, 190, 80), (35, 120, 55), (240, 190, 60), (240, 240, 250)

rng = random.Random(7)

# Rose heads spread over a dome above the waist.
ROSES = []
for k in range(N_ROSES):
    # golden-angle spiral over a spherical cap
    u = (k + 0.5) / N_ROSES
    theta = math.acos(1 - u * 0.75)                      # 0 = straight up
    phi = k * 2.39996
    d = 0.95
    pos = (d * math.sin(theta) * math.cos(phi) * 1.15, 0.15 + d * math.cos(theta) * 0.55,
           d * math.sin(theta) * math.sin(phi) * 0.7)
    ROSES.append((pos, rng.choice(REDS), rng.uniform(0, math.tau), T_BLOOM + k * BLOOM_GAP))


def rose_points(open_, color, turn, seed):
    """Spiral petals of one rose; open_ goes 0 (bud) -> 1 (full bloom)."""
    r = random.Random(seed)
    pts = []
    for layer in range(5):
        n_pet = 3 + layer
        rad = (0.04 + 0.035 * layer) * (0.35 + 0.65 * open_)
        lift = 0.06 * (1 - layer / 5) * (1 - 0.5 * open_)
        shade = 0.55 + 0.1 * layer
        for p in range(n_pet):
            base = turn + p * math.tau / n_pet + layer * 0.6
            for _ in range(10):
                a = base + r.uniform(-0.45, 0.45)
                rr = rad * r.uniform(0.55, 1.0)
                pts.append((rr * math.cos(a), lift + rr * 0.6 * r.random(), rr * math.sin(a), color, shade))
    return pts


ROSE_CACHE = {}


def rose(k, open_):
    key = (k, round(open_ * 20))
    if key not in ROSE_CACHE:
        _, col, turn, _ = ROSES[k]
        ROSE_CACHE[key] = rose_points(key[1] / 20, col, turn, k)
    return ROSE_CACHE[key]


def bezier(a, b, c, u):
    return tuple((1 - u) ** 2 * a[i] + 2 * (1 - u) * u * b[i] + u * u * c[i] for i in range(3))


LEAVES = []
for _ in range(26):
    phi, h = rng.uniform(0, math.tau), rng.uniform(-0.15, 0.6)
    LEAVES.append(((math.cos(phi) * 0.95, h, math.sin(phi) * 0.6), phi, rng.uniform(0.12, 0.2)))

FALL = [(rng.randrange(N_ROSES), rng.uniform(-1, 1), rng.uniform(0, 6.3)) for _ in range(14)]


def draw(cv: Canvas, t):
    sway = math.sin(t * 0.5) * 0.35
    stems = smooth(t / T_STEMS)

    def P(x, y, z, col, b):
        x, z = rot_y(x, z, sway)
        cv.point(x, y, z, col, b)

    # stem bundle below the ribbon
    for i in range(7):
        off = (i - 3) * 0.025
        for s in range(40):
            u = s / 40
            if u > stems:
                break
            P(off * (1 + u), WAIST[1] - u * (WAIST[1] - BOTTOM_Y), 0, GREEN, 0.55)

    # stems from waist up to each rose
    for k, (pos, _, _, _) in enumerate(ROSES):
        mid = (pos[0] * 0.3, (WAIST[1] + pos[1]) / 2, pos[2] * 0.3)
        reach = smooth(stems * 1.4 - k * 0.02)
        for s in range(30):
            u = s / 30
            if u > reach:
                break
            P(*bezier(WAIST, mid, pos, u), DARK_GREEN if s % 3 else GREEN, 0.5)

    # leaves
    for (lx, ly, lz), phi, size in LEAVES:
        if stems < 1:
            continue
        for i in range(24):
            u, v = i / 24, (i * 7 % 5) / 5 - 0.4
            w = math.sin(u * math.pi) * size * 0.35
            P(lx + math.cos(phi) * u * size - math.sin(phi) * v * w, ly + u * size * 0.4,
              lz + math.sin(phi) * u * size + math.cos(phi) * v * w, GREEN, 0.45)

    # roses
    opened = 0
    for k, (pos, _, _, t0) in enumerate(ROSES):
        o = smooth((t - t0) / 1.2)
        if t < t0 - 0.6:
            continue
        opened += o >= 1
        for x, y, z, col, sh in rose(k, o):
            P(pos[0] + x, pos[1] + y, pos[2] + z, col, sh * (0.6 + 0.4 * max(o, 0.3)))

    # gold ribbon bow at the waist
    if stems >= 1:
        for i in range(70):
            a = i / 70 * math.tau
            P(0.13 * math.cos(a), WAIST[1] + 0.03 * math.sin(a * 3), 0.08 * math.sin(a), GOLD, 0.8)
            loop = math.sin(a * 2) * 0.12
            P(loop * math.cos(a), WAIST[1] + 0.06 + abs(loop) * 0.4, -0.12, GOLD, 0.7)
        for s in range(18):
            P(0.04 + s * 0.008, WAIST[1] - s * 0.016, -0.1, GOLD, 0.6)
            P(-0.04 - s * 0.008, WAIST[1] - s * 0.016, -0.1, GOLD, 0.6)

    # falling petals
    if t > T_FALL:
        for i, (k, drift, ph) in enumerate(FALL):
            if t - T_FALL < i * 0.7:
                continue
            age = (t - T_FALL - i * 0.7) % 6
            (x, y, z), col, _, _ = ROSES[k]
            P(x + drift * 0.3 * age + 0.1 * math.sin(age * 2 + ph), y - age * 0.4, z - 0.1, col, 0.7)

    if t > T_MSG:
        shown = min(len(MESSAGE), int((t - T_MSG) * 8))
        row = min(cv.rows - 2, int(cv.cy + 1.25 * cv.sy))
        col0 = (cv.cols - len(MESSAGE) * 2) // 2
        cv.text(row, col0, " ".join(MESSAGE[:shown]), lambda i: (230, 40, 60) if i >= 16 else WHITE)

    cv.text(cv.rows - 1, 1, "roses open %d/%d" % (opened, N_ROSES), lambda i: (110, 110, 120))


if __name__ == "__main__":
    run(draw)
