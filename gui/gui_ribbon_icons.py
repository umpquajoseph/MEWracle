#!/usr/bin/env python3
"""gui_ribbon_icons.py -- MODERN-UI ONLY: the G-code page's ribbon icons.

One line-icon set, drawn in code so there is no asset pipeline: every icon
sits on the same 24-unit grid, is stroked at the same weight with round caps
and joins, and takes two colours only -- `ink` for the drawing and `accent`
for the one part that says what the tool does (the plus of a new thing, the
needle of a gauge, the arrow of a move). nTop's flat monochrome line icons
with Fusion's quieter accent.

Drawn at `SS` times the size and scaled down with LANCZOS, so curves and
diagonals are smooth. The background is transparent, so an icon sits on
whatever the button's colour is (hover, pressed) without a redraw.

    icon_image("find", 24, ink="#1c1e21", accent="#2f6fd6")  -> PIL RGBA
    ICONS                                                     -> the names

The ribbon's buttons (`gui_gcode_cad.ToolButton`) take an icon by name."""

if __name__ == "__main__":  # run as a script: put the program's folders on sys.path
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
    import app_paths  # noqa: E402,F401

import math

from PIL import Image, ImageDraw

#: supersampling factor: drawn at SS x the size, scaled down
SS = 6
#: stroke weight, in grid units (1 unit = 1 px at the 24 px size)
STROKE = 1.5

_CACHE = {}


def _rgba(hex_color, alpha=255):
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


class _Pen:
    """Drawing in grid units on a supersampled canvas."""

    def __init__(self, size, ink, accent):
        self.k = size * SS / 24.0
        self.im = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.im)
        self.ink, self.acc = _rgba(ink), _rgba(accent)

    def _c(self, col):
        return self.acc if col == "a" else self.ink

    def _p(self, pts):
        return [(x * self.k, y * self.k) for x, y in pts]

    def _cap(self, x, y, col, w):
        r = w * self.k / 2.0
        X, Y = x * self.k, y * self.k
        self.d.ellipse((X - r, Y - r, X + r, Y + r), fill=col)

    def line(self, pts, col="i", w=STROKE, closed=False):
        """A polyline with round joins and caps."""
        c = self._c(col)
        if closed:
            pts = list(pts) + [pts[0]]
        self.d.line(self._p(pts), fill=c, width=max(1, round(w * self.k)),
                    joint="curve")
        for x, y in pts:
            self._cap(x, y, c, w)

    def rect(self, x0, y0, x1, y1, r=1.5, col="i", fill=None, w=STROKE):
        k = self.k
        self.d.rounded_rectangle(
            (x0 * k, y0 * k, x1 * k, y1 * k), radius=r * k,
            outline=self._c(col) if col else None,
            fill=None if fill is None else (
                self._c(fill) if fill in ("a", "i") else _rgba(fill)),
            width=max(1, round(w * k)) if col else 0)

    def circle(self, cx, cy, r, col="i", fill=None, w=STROKE):
        k = self.k
        self.d.ellipse(((cx - r) * k, (cy - r) * k, (cx + r) * k,
                        (cy + r) * k),
                       outline=self._c(col) if col else None,
                       fill=None if fill is None else self._c(fill),
                       width=max(1, round(w * k)) if col else 0)

    def arc(self, cx, cy, r, a0, a1, col="i", w=STROKE):
        """An arc from a0 to a1 degrees (0 = east, clockwise, as PIL)."""
        k = self.k
        c = self._c(col)
        self.d.arc(((cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k),
                   a0, a1, fill=c, width=max(1, round(w * k)))
        for a in (a0, a1):
            t = math.radians(a)
            self._cap(cx + r * math.cos(t), cy + r * math.sin(t), c, w)

    def arrow(self, x0, y0, x1, y1, col="i", head=3.6, w=STROKE):
        """A line from (x0, y0) with an open arrowhead at (x1, y1)."""
        self.line([(x0, y0), (x1, y1)], col, w)
        t = math.atan2(y1 - y0, x1 - x0)
        for s in (-1, 1):
            a = t + math.pi + s * math.radians(40)
            self.line([(x1 + head * math.cos(a), y1 + head * math.sin(a)),
                       (x1, y1)], col, w)

    def poly(self, pts, col="i", fill=None, w=STROKE):
        """A closed polygon, optionally filled, outline with round joins."""
        if fill is not None:
            self.d.polygon(self._p(pts), fill=self._c(fill))
        if col:
            self.line(pts, col, w, closed=True)

    def plus(self, cx, cy, r, col="a", w=STROKE + 0.3):
        self.line([(cx - r, cy), (cx + r, cy)], col, w)
        self.line([(cx, cy - r), (cx, cy + r)], col, w)

    def knock_rect(self, x0, y0, x1, y1):
        """Clear a rectangle, so a shape drawn over it hides what is behind."""
        k = self.k
        self.d.rectangle((x0 * k, y0 * k, x1 * k, y1 * k), fill=(0, 0, 0, 0))

    def knock(self, cx, cy, r):
        """Clear a disc (a badge's halo), so a badge reads over the drawing."""
        k = self.k
        self.d.ellipse(((cx - r) * k, (cy - r) * k, (cx + r) * k,
                        (cy + r) * k), fill=(0, 0, 0, 0))


# -- the icons: each draws on a 24 x 24 grid, content inside 2.5..21.5 ------- #

def _scaffold(p):
    # a lattice (the scaffold) with a plus badge: a new one
    p.rect(3, 3, 16.5, 16.5, r=1.2)
    p.line([(9.75, 3), (9.75, 16.5)])
    p.line([(3, 9.75), (16.5, 9.75)])
    p.knock(18, 18, 5.4)
    p.plus(18, 18, 3.4)


def _base(p):
    # the dense pad printed first, a scaffold standing on it
    p.rect(3, 15.5, 21, 20.5, r=1.2, col="a")
    for x in (6.6, 10.2, 13.8, 17.4):
        p.line([(x, 16), (x, 20)], "a", w=1.1)
    p.rect(6, 3, 18, 12.5, r=1.2)
    p.line([(12, 3), (12, 12.5)])
    p.line([(6, 7.75), (18, 7.75)])


def _footprint(p):
    # a sketched outline: a pentagon, its corners marked
    pts = [(12, 3), (21, 9.6), (17.6, 20.5), (6.4, 20.5), (3, 9.6)]
    p.poly(pts)
    for x, y in pts:
        p.knock(x, y, 2.3)
        p.circle(x, y, 1.6, col="a", fill="a", w=1)


def _subsection(p):
    # a whole lattice, one piece of it lifted out in the accent: the
    # representative section kept for the preview and the export
    p.rect(3, 3, 21, 21, r=1.2, w=1.1)
    for v in (9, 15):
        p.line([(v, 3), (v, 21)], w=1.1)
        p.line([(3, v), (21, v)], w=1.1)
    p.knock_rect(9.8, 9.8, 22.4, 22.4)
    p.rect(10.5, 10.5, 21.5, 21.5, r=1.4, col="a", w=STROKE + 0.2)
    p.line([(16, 10.5), (16, 21.5)], "a", w=1.1)
    p.line([(10.5, 16), (21.5, 16)], "a", w=1.1)


def _array(p):
    # the original and its copies
    p.rect(3, 3, 10.5, 10.5, r=1.4, col="a", fill="a")
    for x, y in ((13.5, 3), (3, 13.5), (13.5, 13.5)):
        p.rect(x, y, x + 7.5, y + 7.5, r=1.4)


def _wave(p):
    # a straight fiber, waved
    pts = [(3 + 18 * i / 40.0,
            12 - 5.5 * math.sin(2 * math.pi * 1.25 * i / 40.0))
           for i in range(41)]
    p.line(pts, "a")
    p.line([(3, 20.5), (21, 20.5)], w=1.2)


def _angles(p):
    # two rays and the angle between them
    p.line([(20.5, 20), (3.5, 20), (15, 4)])
    p.arc(3.5, 20, 9.5, -54, -4, col="a")
    p.circle(3.5, 20, 1.6, col=None, fill="a")


def _params(p):
    # every scaffold's settings, in one table
    p.rect(3, 4, 21, 20, r=1.6)
    p.line([(3, 9), (21, 9)])
    p.line([(9.5, 9), (9.5, 20)])
    for y in (12.7, 16.3):
        p.line([(12, y), (18.5, y)], "a", w=1.3)
        p.line([(5.3, y), (7.2, y)], w=1.3)


def _insert(p):
    # a new line between two
    p.line([(3, 5), (21, 5)])
    p.line([(3, 19), (21, 19)])
    p.plus(6, 12, 3)
    p.line([(11, 12), (21, 12)], "a")


def _stitch(p):
    # another file, brought into this one
    p.line([(4, 3), (12, 3), (16, 7), (16, 21), (4, 21)], closed=True)
    p.line([(12, 3), (12, 7), (16, 7)], w=1.2)
    p.knock(17, 14, 4.2)
    p.arrow(21.5, 14, 9, 14, col="a")


def _library(p):
    # saved edits on a shelf
    p.rect(3.5, 4, 8, 20, r=1)
    p.rect(9.5, 6.5, 14, 20, r=1)
    p.poly([(15.2, 7.6), (19.4, 6.4), (22, 19.2), (17.8, 20.4)], col="a")
    p.line([(2.5, 21), (21.5, 21)])


def _speed(p):
    # a gauge and its needle
    p.arc(12, 14, 9, 160, 380)
    for a in (180, 225, 270, 315, 360):
        t = math.radians(a)
        p.line([(12 + 6.6 * math.cos(t), 14 + 6.6 * math.sin(t)),
                (12 + 7.6 * math.cos(t), 14 + 7.6 * math.sin(t))], w=1.1)
    p.line([(12, 14), (17, 8)], "a", w=STROKE + 0.3)
    p.circle(12, 14, 1.8, col=None, fill="a")


def _move(p):
    # a part, moved and scaled
    p.rect(3, 9, 15, 21, r=1.2)
    p.arrow(9, 15, 20.5, 3.5, col="a")


def _translate(p):
    # Fusion's Move: four arrows out of a held point
    for x1, y1 in ((12, 2.8), (21.2, 12), (12, 21.2), (2.8, 12)):
        p.arrow(12, 12, x1, y1, col="a", head=3.2)
    p.circle(12, 12, 2.2, col=None, fill="i")


def _delete(p):
    p.line([(3.5, 6), (20.5, 6)])
    p.line([(9, 6), (9.6, 3), (14.4, 3), (15, 6)])
    p.line([(5.6, 6), (6.8, 21), (17.2, 21), (18.4, 6)])
    for x in (10, 14):
        p.line([(x, 10), (x, 17)], w=1.2)


def _code(p):
    # the g-code text: brackets and a slash
    p.line([(8, 7), (3, 12), (8, 17)])
    p.line([(16, 7), (21, 12), (16, 17)])
    p.line([(13.6, 4.5), (10.4, 19.5)], "a")


def _find(p):
    p.circle(10.5, 10.5, 6.5)
    p.line([(15.3, 15.3), (20.5, 20.5)], "a", w=STROKE + 0.9)


def _compare(p):
    # two texts side by side, one difference marked
    p.rect(3, 3.5, 11, 20.5, r=1.2)
    p.rect(13, 3.5, 21, 20.5, r=1.2)
    for y in (7.5, 15.5):
        p.line([(5.5, y), (8.5, y)], w=1.2)
        p.line([(15.5, y), (18.5, y)], w=1.2)
    p.line([(5.5, 11.5), (8.5, 11.5)], w=1.2)
    p.line([(15.5, 11.5), (18.5, 11.5)], "a", w=2.0)


def _jet(p):
    # play, in this picture
    p.circle(12, 12, 9)
    p.poly([(9.8, 7.8), (16.6, 12), (9.8, 16.2)], col="a", fill="a", w=1.2)


def _viewer(p):
    # play, in another window
    p.rect(2.5, 4, 21.5, 20, r=1.6)
    p.line([(2.5, 8), (21.5, 8)], w=1.2)
    p.poly([(10, 10.6), (15.6, 14), (10, 17.4)], col="a", fill="a", w=1.2)


def _open(p):
    # a folder, opening
    p.line([(3, 19), (3, 5), (9, 5), (11, 7.5), (18, 7.5), (18, 10.5)])
    p.line([(3, 19), (6.5, 10.5), (21.5, 10.5), (18, 19), (3, 19)], "a")


def _flavor(p):
    # the printer's firmware: a chip
    p.rect(7, 7, 17, 17, r=1.4)
    for v in (9.5, 12, 14.5):
        p.line([(v, 3.5), (v, 7)], w=1.2)
        p.line([(v, 17), (v, 20.5)], w=1.2)
        p.line([(3.5, v), (7, v)], w=1.2)
        p.line([(17, v), (20.5, v)], w=1.2)
    p.rect(10, 10, 14, 14, r=0.8, col=None, fill="a")


def _output(p):
    # options: sliders
    for y, x in ((5.5, 15), (12, 8), (18.5, 13)):
        p.line([(3, y), (21, y)], w=1.2)
        p.knock(x, y, 3.2)
        p.circle(x, y, 2.2, col="i" if x != 8 else "a")


def _save(p):
    p.line([(3.5, 14.5), (3.5, 20.5), (20.5, 20.5), (20.5, 14.5)])
    p.arrow(12, 3, 12, 15, col="a")


def _copy(p):
    p.rect(8.5, 3, 20.5, 15.5, r=1.6)
    p.knock_rect(2, 7, 17, 22.5)
    p.rect(3.5, 8.5, 15.5, 21, r=1.6, col="a")


def _paste(p):
    # a board with a clip, and a page on it
    p.rect(4, 5, 20, 21, r=1.8)
    p.rect(8.5, 2.5, 15.5, 7, r=1.2, col="a", fill="a")
    p.line([(8, 12), (16, 12)], w=1.2)
    p.line([(8, 16), (14, 16)], w=1.2)


def _duplicate(p):
    # a part and its twin, offset, with a plus
    p.rect(3, 8.5, 14, 20.5, r=1.6)
    p.knock_rect(8.5, 3, 21.5, 15)
    p.rect(9, 3.5, 20.5, 14.5, r=1.6, col="a")
    p.plus(15, 9, 2.4, col="a")


def _send(p):
    # a file going out through a tray's open side
    p.line([(3.5, 14.5), (3.5, 20.5), (20.5, 20.5), (20.5, 14.5)])
    p.arrow(5, 12, 19, 5, col="a")


def _design(p):
    # a preset (bookmark) with a down arrow: save the design
    p.poly([(5.5, 3), (18.5, 3), (18.5, 21), (12, 16.5), (5.5, 21)])
    p.arrow(12, 6, 12, 13, col="a")


def _more(p):
    # the tools that did not fit: three dots in a tray
    p.rect(3, 6.5, 21, 17.5, r=2.5)
    for x in (7.5, 12, 16.5):
        p.circle(x, 12, 1.7, col=None, fill="a")


def _quality(p):
    # a level: rising bars, the top one marked
    for i, x in enumerate((3, 8, 13)):
        p.rect(x, 17 - 4.5 * i, x + 3.5, 20.5, r=1)
    p.rect(18, 3.5, 21.5, 20.5, r=1, col="a", fill="a")


def _export(p):
    # a part (a cube), sent out
    p.poly([(3, 8.5), (9.5, 5), (16, 8.5), (16, 16.5), (9.5, 20),
            (3, 16.5)])
    p.line([(3, 8.5), (9.5, 12), (16, 8.5)])
    p.line([(9.5, 12), (9.5, 20)])
    p.knock(18, 7, 4.5)
    p.arrow(14, 11, 21.5, 3.5, col="a")


def _presets(p):
    # a saved set: a bookmark with a star
    p.poly([(5.5, 3), (18.5, 3), (18.5, 21), (12, 16.5), (5.5, 21)])
    pts = []
    for i in range(10):
        r = 3.6 if i % 2 == 0 else 1.6
        t = math.radians(-90 + 36 * i)
        pts.append((12 + r * math.cos(t), 9.6 + r * math.sin(t)))
    p.poly(pts, col="a", fill="a", w=0.8)


def _collector(p):
    # what the print lands on: a plate seen at an angle, a rod over it
    p.poly([(3, 15.5), (9, 11.5), (21, 11.5), (15, 15.5)])
    p.line([(3, 15.5), (3, 18), (15, 18), (15, 15.5)], w=1.2)
    p.line([(15, 18), (21, 14), (21, 11.5)], w=1.2)
    p.rect(5, 3.5, 19, 8.5, r=2.5, col="a")
    p.line([(9.5, 3.5), (11.5, 8.5)], "a", w=1.1)
    p.line([(13.5, 3.5), (15.5, 8.5)], "a", w=1.1)


def _tube(p):
    # a tube on its mandrel, a helix wound round it
    p.line([(4, 7), (20, 7)])
    p.line([(4, 17), (20, 17)])
    p.arc(4, 12, 5, 90, 270)
    p.arc(20, 12, 5, 270, 450, w=1.2)
    for x0 in (5.5, 11.5):
        p.line([(x0, 17), (x0 + 6, 7)], "a", w=1.3)
    p.line([(17.5, 17), (20.5, 12)], "a", w=1.3)


def _family(p):
    # a pattern family: a diamond lattice, one cell lit
    for x in (3, 9, 15):
        p.line([(x, 21), (x + 6, 3)], w=1.2)
        p.line([(x + 6, 21), (x, 3)], w=1.2)
    p.poly([(12, 7.5), (14.5, 12), (12, 16.5), (9.5, 12)], col="a",
           fill="a", w=0.8)


def _cell(p):
    # one unit cell drawn on its grid: nodes and the edges that join them
    p.rect(3, 3, 21, 21, r=1.2, w=1.1)
    pts = [(3, 12), (8, 6), (16, 6), (21, 12), (16, 18), (8, 18)]
    p.poly(pts, col="a")
    for x, y in pts:
        p.knock(x, y, 2.1)
        p.circle(x, y, 1.5, col="a", fill="a", w=1)


def _advanced(p):
    # the solver's knobs: three sliders, one set
    for y, x in ((6, 15), (12, 8), (18, 13)):
        p.line([(3, y), (21, y)], w=1.2)
        p.knock(x, y, 2.6)
        p.circle(x, y, 1.9, col="a", fill="a", w=1)


def _undo(p, m=1):
    # a half turn back, its arrow at the start; m=-1 mirrors it (redo)
    def X(x):
        return x if m > 0 else 24 - x
    p.arc(12, 13.5, 7, 180, 360)
    p.line([(X(19), 13.5), (X(19), 19)])
    p.line([(X(1.6), 10.4), (X(5), 13.8), (X(8.4), 10.4)])


def _redo(p):
    _undo(p, -1)


def _apply(p):
    # a page with a tick: a computed value written into the Physics page
    p.poly([(4, 2.5), (15, 2.5), (20, 7.5), (20, 21.5), (4, 21.5)])
    p.line([(8, 13), (11, 16.5), (17, 9.5)], col="a", w=2.2)


def _measure(p):
    # a ruler laid between two points, the point marks in the accent
    p.poly([(2.5, 15.5), (15.5, 2.5), (21.5, 8.5), (8.5, 21.5)])
    for k in (0.28, 0.5, 0.72):
        x, y = 2.5 + (8.5 - 2.5) * 0 + 13 * k, 15.5 - 13 * k
        p.line([(x, y), (x + 2.2, y + 2.2)], w=1.2)
    p.circle(4.4, 19.6, 2.1, col="a", fill="a", w=1)
    p.circle(19.6, 4.4, 2.1, col="a", fill="a", w=1)


def _colour(p):
    # three bars shaded from one end to the other
    for i, y in enumerate((4.5, 10.5, 16.5)):
        p.rect(3, y, 21, y + 4, r=1.2, col="i")
        p.line([(3 + 4 + i * 2, y + 2), (21 - 3 - (2 - i) * 2, y + 2)],
               "a", w=2.2)


def _target(p):
    # a target ring, and the fiber laid through its centre
    p.circle(12, 12, 8.5)
    p.circle(12, 12, 4.2)
    p.line([(2.5, 21.5), (21.5, 2.5)], "a", w=2.0)
    p.circle(12, 12, 1.6, col=None, fill="a")


ICONS = {
    "scaffold": _scaffold, "base": _base, "footprint": _footprint,
    "subsection": _subsection,
    "array": _array, "wave": _wave, "angles": _angles, "params": _params,
    "insert": _insert, "stitch": _stitch, "library": _library,
    "speed": _speed, "move": _move, "translate": _translate,
    "delete": _delete,
    "code": _code, "find": _find, "compare": _compare, "jet": _jet,
    "viewer": _viewer, "open": _open, "flavor": _flavor,
    "output": _output, "save": _save, "copy": _copy, "paste": _paste,
    "duplicate": _duplicate, "send": _send, "design": _design,
    "undo": _undo, "redo": _redo,
    "quality": _quality, "export": _export, "presets": _presets,
    "collector": _collector, "tube": _tube, "family": _family,
    "cell": _cell, "advanced": _advanced, "more": _more,
    "measure": _measure, "target": _target, "colour": _colour, "apply": _apply,
}


def icon_image(name, size, ink, accent):
    """Icon `name` at `size` px, `ink` and `accent` as #rrggbb: a PIL RGBA
    image, cached."""
    key = (name, size, ink, accent)
    im = _CACHE.get(key)
    if im is None:
        p = _Pen(size, ink, accent)
        ICONS[name](p)
        im = p.im.resize((size, size), Image.LANCZOS)
        _CACHE[key] = im
    return im


def sheet(path, size=24, themes=(("#ffffff", "#1c1e21", "#2f6fd6"),
                                 ("#333639", "#e6e8ea", "#7aa9f5"))):
    """Every icon on each theme's ground, one row per theme, at 1x and 2x:
    the look-at for the set (`python gui/gui_ribbon_icons.py out.png`)."""
    names = list(ICONS)
    pad = size // 2
    rows = []
    for scale in (1, 2):
        s = size * scale
        for bg, ink, acc in themes:
            row = Image.new("RGBA", (len(names) * (s + pad) + pad, s + 2 * pad),
                            _rgba(bg))
            for i, n in enumerate(names):
                ic = icon_image(n, s, ink, acc)
                row.alpha_composite(ic, (pad + i * (s + pad), pad))
            rows.append(row)
    out = Image.new("RGBA", (max(r.width for r in rows),
                             sum(r.height for r in rows)), (0, 0, 0, 255))
    y = 0
    for r in rows:
        out.paste(r, (0, y))
        y += r.height
    out.convert("RGB").save(path)
    return out.size


if __name__ == "__main__":
    import sys
    print(sheet(sys.argv[1] if len(sys.argv) > 1 else "icons.png"))
