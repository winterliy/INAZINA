"""Tiny terminal renderer: 3D points -> truecolor ASCII grid, plus star field and key input."""
import math
import os
import random
import select
import shutil
import sys
import termios
import time
import tty

RAMP = " .:-=+*#%@"
DIST = 3.2


class Canvas:
    def __init__(self, max_cols=110):
        cols, rows = shutil.get_terminal_size((88, 40))
        self.cols, self.rows = min(cols, max_cols), rows - 1
        # world unit -> cells; terminal cells are ~2x taller than wide
        self.sy = self.rows * 0.36
        self.sx = self.sy * 2.0
        self.cx, self.cy = self.cols / 2, self.rows * 0.45
        n = self.cols * self.rows
        self.z, self.ch, self.rgb = [0.0] * n, [" "] * n, [None] * n
        rng = random.Random(3)
        self.stars = [(rng.randrange(self.cols), rng.randrange(self.rows), rng.uniform(0, 6.3), rng.random())
                      for _ in range(self.cols * self.rows // 45)]

    def clear(self, t):
        n = self.cols * self.rows
        self.z[:] = [1e9] * n
        self.ch[:] = [" "] * n
        self.rgb[:] = [None] * n
        for x, y, ph, kind in self.stars:
            tw = 0.5 + 0.5 * math.sin(t * (1.5 + kind * 2) + ph)
            if tw < 0.25:
                continue
            g = int(70 + 150 * tw)
            self.put(x, y, "+" if kind > 0.85 and tw > 0.8 else ("·" if tw < 0.6 else "."), (g, g, int(g * 1.05) % 256), 1e8)

    def put(self, x, y, c, rgb, z=0.0):
        if 0 <= x < self.cols and 0 <= y < self.rows:
            i = y * self.cols + x
            if z < self.z[i]:
                self.z[i], self.ch[i], self.rgb[i] = z, c, rgb

    def point(self, x, y, z, rgb, b=1.0):
        """Project a world point (y up, z toward viewer is negative) and shade by brightness b."""
        f = DIST / (z + DIST)
        sx, sy = int(self.cx + x * f * self.sx), int(self.cy - y * f * self.sy)
        b = max(0.0, min(1.0, b))
        c = RAMP[1 + int(b * (len(RAMP) - 2) + 0.5)]
        k = 0.35 + 0.65 * b
        self.put(sx, sy, c, (int(rgb[0] * k), int(rgb[1] * k), int(rgb[2] * k)), z)

    def text(self, row, col, s, color_fn):
        for i, c in enumerate(s):
            if c != " ":
                self.put(col + i, row, c, color_fn(i), -1e9)

    def render(self):
        out, last = ["\033[H"], None
        for y in range(self.rows):
            base = y * self.cols
            for x in range(self.cols):
                rgb = self.rgb[base + x]
                if rgb != last:
                    out.append("\033[38;2;%d;%d;%dm" % rgb if rgb else "\033[0m")
                    last = rgb
                out.append(self.ch[base + x])
            out.append("\033[0m\n")
            last = None
        sys.stdout.write("".join(out))
        sys.stdout.flush()


def rot_y(x, z, a):
    c, s = math.cos(a), math.sin(a)
    return x * c + z * s, -x * s + z * c


def smooth(v):
    v = max(0.0, min(1.0, v))
    return v * v * (3 - 2 * v)


def run(draw, fps=24):
    """Main loop: draw(canvas, t). Keys: space pause, r restart, q/Esc quit."""
    canvas = Canvas()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None
    if old:
        tty.setcbreak(fd)
    sys.stdout.write("\033[?25l\033[2J")
    t, paused, last = 0.0, False, time.time()
    try:
        while True:
            now = time.time()
            if not paused:
                t += now - last
            last = now
            if old and select.select([sys.stdin], [], [], 0)[0]:
                k = sys.stdin.read(1)
                if k in ("q", "\x1b"):
                    break
                if k == " ":
                    paused = not paused
                if k == "r":
                    t = 0.0
            canvas.clear(t)
            draw(canvas, t)
            canvas.render()
            time.sleep(max(0.0, 1 / fps - (time.time() - now)))
    except KeyboardInterrupt:
        pass
    finally:
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\033[0m\033[?25h\033[2J\033[H")
