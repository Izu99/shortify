#!/usr/bin/env python3
"""Generate the app icon: the word ribbon, which is what Shortify actually is."""
import cairo, sys, math
from pathlib import Path

GROUND = (0.078, 0.094, 0.114)
SIGNAL = (1.000, 0.831, 0.000)
INK    = (0.902, 0.922, 0.945)

def rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()

def draw(size, path):
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(s)
    u = size / 256.0

    cr.set_source_rgb(*GROUND)
    rounded(cr, 0, 0, size, size, 56 * u)
    cr.fill()

    # chips of differing width: the ribbon's whole idea is that width == duration
    widths = [30, 62, 18, 44, 24]
    x = 26 * u
    for i, w in enumerate(widths):
        w *= u
        h = 54 * u
        y = (size - h) / 2
        cr.set_source_rgb(*SIGNAL)
        rounded(cr, x, y, w, h, 4 * u)
        cr.fill()
        x += w + 8 * u

    # baseline rule, echoing the ruler under the real ribbon
    cr.set_source_rgba(*INK, 0.30)
    cr.rectangle(26 * u, size * 0.70, size - 52 * u, 3 * u)
    cr.fill()

    s.write_to_png(path)

if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    for n in (16, 24, 32, 48, 64, 128, 256):
        draw(n, str(out / f"shortify-{n}.png"))
    print(f"icons written to {out}")
