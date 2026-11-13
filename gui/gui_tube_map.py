#!/usr/bin/env python3
"""gui_tube_map.py -- "Where to measure": the mandrel lag tube, unrolled.

WHAT IT IS FOR. Paper section 2.4.6 asks the user to measure ONE wide and ONE
narrow gap between neighbouring lines, in the middle of each band, across the
lines. On the 3-D rod a 1 mm alternation on a 3 mm mandrel is a few pixels
wrapped round a cylinder, and a first-time user could not find it. This strip
sits under the Calibration page's 3-D preview whenever the lag tube is shown
and draws the same simulated fibre FLAT:

  * a row of tiles, one per band in print order, each band unrolled --
    along the mandrel left to right, once around it bottom to top -- with a
    dashed line where to measure (the band's middle), and its angle above it;
  * a close-up of one band's middle (click a tile to choose; the first fitted
    band by default), with the two neighbouring gaps drawn as brackets across
    the lines and labelled "wide" and "narrow" with their widths.

The gaps are READ OFF the drawn fibre (`mew_sweeps.tube_mid_gaps`), never
predicted separately, so the numbers on the brackets are what the picture
shows. With the preview's Physics toggle off the fibre is the commanded path
and every gap is equal; the close-up says so rather than inventing an
alternation.

Tk only, no VTK: it is a Canvas beside the render, not part of it. The data
side (`band_view`, `pick_pair`) is plain Python so a headless test can check
what would be drawn.
"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk

import mew_sweeps

#: bracket colours: blue for the wide gap, orange for the narrow one -- the two
#: are also told apart in words on the bracket, so colour is never the only cue
WIDE_HEX = "#2f6fd6"
NARROW_HEX = "#d9822b"

_TILE_GAP = 8          # px between tiles
_TILE_H_MAX = 64       # px: the tallest a tile may be drawn
_PAD = 6


# --------------------------------------------------------------------------- #
# data: what one band looks like, unrolled
# --------------------------------------------------------------------------- #
def _wrap_pieces(a, b, circ):
    """Segment a->b in (u, v), cut at every multiple of `circ` in v, each piece
    shifted into [0, circ). Yields ((u0, v0), (u1, v1))."""
    (ua, va), (ub, vb) = (a[0], a[1]), (b[0], b[1])
    if va == vb:
        k = math.floor(va / circ)
        yield (ua, va - k * circ), (ub, vb - k * circ)
        return
    lo, hi = min(va, vb), max(va, vb)
    cuts = [lo]
    k = math.floor(lo / circ) + 1
    while k * circ < hi:
        cuts.append(k * circ)
        k += 1
    cuts.append(hi)
    if vb < va:
        cuts.reverse()
    for c0, c1 in zip(cuts[:-1], cuts[1:]):
        t0 = (c0 - va) / (vb - va)
        t1 = (c1 - va) / (vb - va)
        vm = 0.5 * (c0 + c1)
        k = math.floor(vm / circ)
        yield ((ua + t0 * (ub - ua), c0 - k * circ),
               (ua + t1 * (ub - ua), c1 - k * circ))


def band_view(paths_uv, row, diam=0.015):
    """One band, ready to draw: its unrolled fibre as wrapped pieces, where it
    is cut for measuring and the gaps there.

    Returns dict(angle, check, u0, u1, circ, pieces, gaps, touch) where
    `pieces` is a list of ((u, v), (u, v)) with v in [0, circ), `gaps` is
    `mew_sweeps.tube_mid_gaps`' reading, and `touch` is True when neighbours
    come closer than a fibre is wide (`diam`, mm) -- read off the fibre, or
    predicted by the model: a band whose lines overlap has no narrow gap to
    measure, and the close-up says so instead of labelling one."""
    u0, u1 = float(row["x_start_mm"]), float(row["x_end_mm"])
    circ = float(row["circ_mm"])
    slack = 0.5 * float(row["spacing_mm"])
    pieces = []
    for q in paths_uv:
        for a, b in zip(q[:-1], q[1:]):
            if (min(a[0], b[0]) < u0 - slack
                    or max(a[0], b[0]) > u1 + slack):
                continue
            pieces.extend(_wrap_pieces(a, b, circ))
    gaps = mew_sweeps.tube_mid_gaps(paths_uv, row)
    pred_narrow = row.get("pred_narrow_mm")
    touch = (any(w < diam for _i, _j, w in gaps["gaps"])
             or (pred_narrow is not None and float(pred_narrow) < diam))
    return dict(angle=float(row["angle_deg"]),
                check=row.get("check_band") == "yes",
                u0=u0, u1=u1, circ=circ, pieces=pieces, gaps=gaps,
                pred_wide=row.get("pred_wide_mm"), pred_narrow=pred_narrow,
                touch=touch)


def pick_pair(gaps):
    """The two neighbouring gaps to point at: `(k_first, k_second)` indices
    into `gaps["gaps"]` where the first gap's far line is the second's near
    one, or None when the band has fewer than two gaps.

    A pair that stays clear of v = 0 is preferred, nearest the middle of the
    list. A band that closes around the mandrel with too few lines for one
    (two lines, as a 70 deg band has at the default spacing) takes a pair
    across it; `_draw_detail` unwraps the positions, so it draws the same."""
    g = gaps["gaps"]
    pairs = [(a, b) for a in range(len(g)) for b in range(len(g))
             if a != b and g[a][1] == g[b][0]]
    if not pairs:
        return None
    lin = [k for k, (i, j, _w) in enumerate(g) if j == i + 1]
    mid = 0.5 * (len(lin) - 1)

    def score(p):
        wraps = any(g[k][1] != g[k][0] + 1 for k in p)
        return (wraps, abs(p[0] + 0.5 - mid))
    return min(pairs, key=score)


def _clip(p, q, box):
    """Liang-Barsky: segment p->q clipped to box (x0, y0, x1, y1), or None."""
    x0, y0, x1, y1 = box
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pk, qk in ((-dx, p[0] - x0), (dx, x1 - p[0]),
                   (-dy, p[1] - y0), (dy, y1 - p[1])):
        if pk == 0:
            if qk < 0:
                return None
            continue
        t = qk / pk
        if pk < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    return ((p[0] + t0 * dx, p[1] + t0 * dy), (p[0] + t1 * dx, p[1] + t1 * dy))


# --------------------------------------------------------------------------- #
# the widget
# --------------------------------------------------------------------------- #
class TubeMeasureMap(ttk.Frame):
    """The strip under the 3-D preview. `show(bands, fibre_hex, physics_on,
    palette)` draws it; `hide()` takes it off the page."""

    HEIGHT = 340

    def __init__(self, parent):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self._canvas = tk.Canvas(self, height=self.HEIGHT, highlightthickness=0,
                                 borderwidth=0)
        self._canvas.grid(row=0, column=0, sticky="ew")
        self._canvas.bind("<Configure>", lambda _e: self._redraw())
        self._canvas.bind("<Button-1>", self._on_click)
        self._bands = []
        self._sel = None
        self._fibre = "#cc4033"
        self._physics = True
        self._pal = dict(bg="#f6f7f9", card="#ffffff", fg="#1c1e21",
                         hint="#7a8087", border="#d9dce1")
        self._tiles = []            # (x0, y0, x1, y1) per band, for clicks

    # -- public ------------------------------------------------------------ #
    def show(self, bands, fibre_hex, physics_on, palette=None):
        self._bands = list(bands)
        self._fibre = fibre_hex or self._fibre
        self._physics = bool(physics_on)
        if palette:
            self._pal.update(palette)
        if self._sel is None or self._sel >= len(self._bands):
            self._sel = self.default_selection(self._bands)
        self._redraw()

    @staticmethod
    def default_selection(bands):
        """The first FITTED band, or the first band when all are checks."""
        for k, b in enumerate(bands):
            if not b["check"]:
                return k
        return 0 if bands else None

    def select(self, k):
        if 0 <= k < len(self._bands):
            self._sel = k
            self._redraw()

    def caption(self):
        """What the close-up's title says, for the band on show -- also the
        text a headless test reads."""
        if self._sel is None or not self._bands:
            return ""
        return self._detail_title(self._bands[self._sel])

    # -- drawing ----------------------------------------------------------- #
    def _on_click(self, e):
        for k, (x0, y0, x1, y1) in enumerate(self._tiles):
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                self.select(k)
                return

    def _text(self, c, x, y, W, text, font="MewHint", fill=None):
        """A wrapped line of text at (x, y); returns the y below it."""
        item = c.create_text(x, y, anchor="nw", fill=fill or self._pal["fg"],
                             font=font, width=max(80, W - x - _PAD),
                             text=text)
        box = c.bbox(item)
        return (box[3] if box else y + 16) + 4

    def _redraw(self):
        c = self._canvas
        c.delete("all")
        pal = self._pal
        c.configure(background=pal["bg"])
        W = max(200, c.winfo_width())
        if not self._bands:
            return
        y = self._text(c, _PAD, 2, W, "Where to measure", font="MewSection")
        y = self._text(c, _PAD, y, W,
                       "The tube unrolled: along the mandrel left to right, "
                       "once around it bottom to top. Measure at the dashed "
                       "line. Click a band to zoom in below.",
                       fill=pal["hint"])
        y = self._draw_tiles(c, W, y + 2)
        self._draw_detail(c, W, y + 10)

    def _draw_tiles(self, c, W, top):
        """The row of bands; returns the y below it."""
        pal = self._pal
        n = len(self._bands)
        tw = (W - 2 * _PAD - (n - 1) * _TILE_GAP) / float(n)
        span = max(b["u1"] - b["u0"] for b in self._bands) or 1.0
        circ = max(b["circ"] for b in self._bands) or 1.0
        k = min(tw / span, _TILE_H_MAX / circ)
        self._tiles = []
        bottom = top
        for i, b in enumerate(self._bands):
            x0 = _PAD + i * (tw + _TILE_GAP)
            w = (b["u1"] - b["u0"]) * k
            h = b["circ"] * k
            bx0 = x0 + 0.5 * (tw - w)
            by0 = top + 16
            box = (bx0, by0, bx0 + w, by0 + h)
            sel = i == self._sel
            c.create_rectangle(*box, fill=pal["card"],
                               outline=WIDE_HEX if sel else pal["border"],
                               width=2 if sel else 1)
            label = ("%g° check" if b["check"] else "%g°") % b["angle"]
            c.create_text(x0 + 0.5 * tw, top, anchor="n", text=label,
                          fill=WIDE_HEX if sel else pal["fg"], font="MewHint")

            def to_px(u, v, b=b, bx0=bx0, by0=by0, h=h):
                return (bx0 + (u - b["u0"]) * k, by0 + h - v * k)
            for (p, q) in b["pieces"]:
                seg = _clip(to_px(*p), to_px(*q), box)
                if seg:
                    c.create_line(*seg[0], *seg[1], fill=self._fibre)
            g = b["gaps"]
            if g["ring"]:
                yy = to_px(0.0, g["at"] % b["circ"])[1]
                c.create_line(box[0] - 3, yy, box[2] + 3, yy, dash=(3, 2),
                              fill=pal["fg"])
            else:
                xx = to_px(g["at"], 0.0)[0]
                c.create_line(xx, box[1] - 3, xx, box[3] + 3, dash=(3, 2),
                              fill=pal["fg"])
            self._tiles.append((x0, top, x0 + tw, by0 + h))
            bottom = max(bottom, by0 + h)
        return bottom

    def _detail_title(self, b):
        g = b["gaps"]
        pair = pick_pair(g)
        name = ("%g° check band" if b["check"] else "%g° band") % b["angle"]
        if pair is None:
            return "%s: too few lines here to show two gaps." % name
        if b.get("touch") and self._physics and not b["check"]:
            pn = b.get("pred_narrow")
            said = ("the model predicts a narrow gap of %.2f mm" % float(pn)
                    if pn is not None else
                    "the drawn narrow gap is thinner than a fiber")
            return ("Close-up of the %s's middle. Lines overlap here: %s, so "
                    "every other pair of lines touches and there is no narrow "
                    "gap to measure. Widen the line spacing or slow down."
                    % (name, said))
        if b["check"]:
            return ("Close-up of the %s's middle. It is a check: measure the "
                    "two gaps the same way, and expect them to be equal."
                    % name)
        if not self._physics:
            return ("Close-up of the %s's middle. Physics is off, so the "
                    "lines are drawn where they are programmed and every gap "
                    "is equal. Tick Physics above to see the wide and narrow "
                    "gaps." % name)
        return ("Close-up of the %s's middle. Measure across the lines: one "
                "wide gap and one narrow gap, like these two." % name)

    def _draw_detail(self, c, W, top):
        pal = self._pal
        b = self._bands[self._sel]
        y = self._text(c, _PAD, top, W, self._detail_title(b))
        H = self.HEIGHT - y - 4
        g = b["gaps"]
        pair = pick_pair(g)
        if pair is None or H < 30:
            return
        (i, j, w1), (_j, m, w2) = g["gaps"][pair[0]], g["gaps"][pair[1]]
        cr = g["cross"]
        ring = g["ring"]
        a = math.radians(b["angle"])
        # the crossing points in (u, v), and the across-the-lines direction
        circ = b["circ"]
        if ring:
            # the cut is half a turn past where the rings START, an unwrapped
            # v many turns up the tube; the pieces are drawn within one turn
            pts = [(cr[x], g["at"] % circ) for x in (i, j, m)]
            nrm = (1.0, 0.0)
        else:
            # unwrapped along v: a pair across v = 0 continues past the turn,
            # then the whole triple is shifted back into the first turn
            vs = [cr[i]]
            for x in (j, m):
                vv = cr[x]
                while vv < vs[-1]:
                    vv += circ
                vs.append(vv)
            back = math.floor(vs[1] / circ) * circ
            pts = [(g["at"], vv - back) for vv in vs]
            nrm = (-math.sin(a), math.cos(a))
        # a close-up about twice as wide as tall, so it shows the band's
        # middle and not the turns at its ends; the legend sits to its right
        legend_w = 170
        bh = H
        bw = min(W - 2 * _PAD - legend_w, 2.2 * bh)
        box = (_PAD, y, _PAD + bw, y + bh)
        c.create_rectangle(*box, fill=pal["card"], outline=pal["border"])
        # the two brackets in ONE straight line across the lines, as the
        # operator measures them: from line i to its neighbour, then on from
        # that same point to the next line. Started each at its own crossing
        # of the cut, they sat 1/cos(angle) apart along the lines, at opposite
        # corners of a 70 deg close-up.
        mid_pt = (pts[0][0] + w1 * nrm[0], pts[0][1] + w1 * nrm[1])
        ends = [pts[0], mid_pt,
                (mid_pt[0] + w2 * nrm[0], mid_pt[1] + w2 * nrm[1])]
        # framed on that line with a margin, never on the crossings (a 70 deg
        # band zoomed out to three turns) nor on the gap widths alone
        us, vs_ = [e[0] for e in ends], [e[1] for e in ends]
        floor = 0.8 * max(w1 + w2, 1e-3)
        du = max(max(us) - min(us), floor)
        dv = max(max(vs_) - min(vs_), floor)
        k = min(bw / (1.5 * du), bh / (1.5 * dv))
        cu, cv = 0.5 * (max(us) + min(us)), 0.5 * (max(vs_) + min(vs_))
        cx, cy = 0.5 * (box[0] + box[2]), 0.5 * (box[1] + box[3])

        def to_px(u, v):
            return (cx + (u - cu) * k, cy - (v - cv) * k)
        for (p, q) in b["pieces"]:
            for shift in (-circ, 0.0, circ, 2.0 * circ):
                seg = _clip(to_px(p[0], p[1] + shift),
                            to_px(q[0], q[1] + shift), box)
                if seg:
                    c.create_line(*seg[0], *seg[1], fill=self._fibre, width=3,
                                  tags=("detail_fibre",))
        # the two brackets, each from a line straight across to its neighbour,
        # named in the legend beside the close-up rather than on the fibre
        plain = b["check"] or not self._physics
        wide_first = w1 >= w2
        ly = box[1] + 6
        for idx, (start, end, width) in enumerate(
                ((ends[0], ends[1], w1), (ends[1], ends[2], w2))):
            is_wide = (idx == 0) == wide_first
            col = WIDE_HEX if is_wide else NARROW_HEX
            c.create_line(*to_px(*start), *to_px(*end), fill=col, width=2,
                          arrow="both")
            if plain:
                word = "gap %d" % (idx + 1)
            else:
                word = "wide gap" if is_wide else "narrow gap"
            lx = box[2] + 10
            c.create_line(lx, ly + 7, lx + 22, ly + 7, fill=col, width=2,
                          arrow="both")
            touching = not plain and not is_wide and b.get("touch")
            c.create_text(lx + 28, ly, anchor="nw", fill=col, font="MewHint",
                          text=("%s: lines touch" % word if touching
                                else "%s: %.2f mm" % (word, width)))
            ly += 20
        if not plain and b.get("pred_wide") is not None:
            self._text(c, box[2] + 10, ly + 2, W,
                       "Simulated at this print's settings. Your print will "
                       "differ; the pattern will not.", fill=pal["hint"])
