"""The word ribbon — Shortify's signature element.  See DESIGN.md.

One chip per word, chip width proportional to that word's duration, gaps drawn
as empty ground.  Timing errors are visible as shape: a word that hangs too long
is a fat chip, a stretch with no caption is a hole.
"""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GObject
import cairo

GROUND_SUNK = (0.059, 0.071, 0.086)
RULE        = (0.165, 0.192, 0.227)
INK_FAINT   = (0.388, 0.439, 0.498)
INK_DARK    = (0.078, 0.063, 0.020)
SIGNAL      = (1.000, 0.831, 0.000)
EDITED      = (0.353, 0.820, 0.604)

CHIP_H   = 30
RULER_H  = 14
PAD_Y    = 5


class Ribbon(Gtk.DrawingArea):
    __gsignals__ = {
        "word-picked": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self):
        super().__init__()
        self.words = []
        self.selected = -1
        self.pps = 64.0          # pixels per second
        self.duration = 0.0
        self.offset = 0.0
        self.set_size_request(-1, CHIP_H + RULER_H + PAD_Y * 2)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._click)

    def load(self, words, duration, offset=0.0):
        self.words = words
        self.duration = max(duration, 0.1)
        self.offset = offset
        self.selected = -1
        self._resize()
        self.queue_draw()

    def set_offset(self, offset):
        self.offset = offset
        self.queue_draw()

    def select(self, idx):
        self.selected = idx
        self.queue_draw()

    def chip_x(self, idx):
        """Left edge of a chip in widget coords, for scroll-into-view."""
        if not (0 <= idx < len(self.words)):
            return 0
        return 20 + (self.words[idx]["start"] + self.offset) * self.pps

    def _resize(self):
        self.set_size_request(int(self.duration * self.pps) + 40,
                              CHIP_H + RULER_H + PAD_Y * 2)

    def _click(self, _w, ev):
        t = (ev.x - 20) / self.pps
        for i, wd in enumerate(self.words):
            if wd["start"] + self.offset <= t <= wd["end"] + self.offset:
                self.selected = i
                self.queue_draw()
                self.emit("word-picked", i)
                return
        # nearest chip when the click lands in a gap
        if self.words:
            i = min(range(len(self.words)),
                    key=lambda k: abs((self.words[k]["start"] + self.offset) - t))
            self.selected = i
            self.queue_draw()
            self.emit("word-picked", i)

    def _draw(self, _w, cr):
        alloc = self.get_allocation()
        cr.set_source_rgb(*GROUND_SUNK)
        cr.paint()

        top = PAD_Y + RULER_H

        # second ruler — monospace-ish ticks, load-bearing for timing
        cr.set_line_width(1)
        cr.select_font_face("Ubuntu Mono", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10)
        sec = 0
        while sec <= int(self.duration) + 1:
            x = 20 + sec * self.pps
            if x > alloc.width:
                break
            major = (sec % 5 == 0)
            cr.set_source_rgb(*(RULE if not major else INK_FAINT))
            cr.move_to(x + 0.5, PAD_Y + (8 if major else 11))
            cr.line_to(x + 0.5, PAD_Y + RULER_H)
            cr.stroke()
            if major:
                cr.set_source_rgb(*INK_FAINT)
                cr.move_to(x + 3, PAD_Y + 8)
                cr.show_text(f"{sec}s")
            sec += 1

        # chips
        cr.select_font_face("Ubuntu", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(12)
        for i, wd in enumerate(self.words):
            x = 20 + (wd["start"] + self.offset) * self.pps
            w = max(3.0, (wd["end"] - wd["start"]) * self.pps)
            if x + w < 0 or x > alloc.width:
                continue
            x += 1.0
            w = max(2.0, w - 2.0)      # hairline gap: chips must read as separate
            col = EDITED if wd.get("edited") else SIGNAL
            if i == self.selected:
                cr.set_source_rgb(*col)
                cr.rectangle(x - 2, top - 3, w + 4, CHIP_H + 6)
                cr.fill()
                cr.set_source_rgb(*GROUND_SUNK)
                cr.rectangle(x, top, w, CHIP_H)
                cr.fill()
                cr.set_source_rgb(*col)
                cr.rectangle(x + 2, top + 2, w - 4, CHIP_H - 4)
                cr.fill()
            else:
                cr.set_source_rgb(*col)
                cr.rectangle(x, top, w, CHIP_H)
                cr.fill()

            label = wd["text"]
            ext = cr.text_extents(label)
            if ext.width < w - 8:
                cr.set_source_rgb(*INK_DARK)
                cr.move_to(x + (w - ext.width) / 2, top + CHIP_H / 2 + 4)
                cr.show_text(label)
        return False
