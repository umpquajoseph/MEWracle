#!/usr/bin/env python3
"""
scaffold_gcode.py

Generate MEW (melt-electrowriting) scaffold G-code from simple parameters, then
optionally render it straight into a sagged fiber STL using gcode_to_fibers.py.

    python engine/scaffold_gcode.py -o scaffold.gcode [--stl scaffold.stl] \
        --width 10 --height 10 --stepover 0.5 --rotation 90 \
        --fiber-diameter 0.02 --layers 4 --speed 1016

The emitted file follows the MEWron / continuous-flow convention gcode_to_fibers
reads in "g1" mode: a G21/G90 header, one `G0 X.. Y.. Z..` travel per layer that
sets that layer's Z, then the whole layer as ONE continuous `G1` fiber -- parallel
passes spaced `stepover` apart, clipped to the footprint, laid boustrophedon and
joined by ballooning U-turns at the edges. Layer height == fiber diameter, and
layer i runs at `start_angle + i * rotation` degrees (square 90, triangle 60,
decagon 36) unless an explicit per-layer `angle_pattern` overrides it.

The footprint is the width x height rectangle by default; `--shape
circle/ellipse/polygon/custom` (see `footprint_outline`) makes it a centred
CONVEX polygon the fibers are clipped to instead. Rectangle keeps the faster
axis-aligned box clip, so its output is byte-identical. Concave outlines are not
supported (gated by `_polygon_is_convex`): a continuous-flow print cannot
traverse the disjoint spans one would produce.

Mandrel mode (`generate_mandrel_gcode` / `generate_mandrel_cell_gcode`, X + the
rotary A) is a SIBLING emitter below, sharing none of the planar machinery.

No `;` comments and no M30 footer ever reach the exported file; see the
end-of-print exit in `generate_multi_scaffold_gcode`.

Requires: numpy (only for the optional --stl step, via gcode_to_fibers).
"""

if __name__ == "__main__":  # run as a script: put the program's folders on sys.path
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
    import app_paths  # noqa: E402,F401

import argparse
import contextlib
import json
import math
import os
import re
import sys

import array_travel
import gcode_exit
import mew_tube
import perimeter_travel as ptravel
import mew_unit_cell

_INF = float("inf")

# A turnaround always joins two ADJACENT fibers (consecutive passes are one
# stepover apart). When both pass ends land on a footprint edge nearly PARALLEL
# to the fibers, that one-stepover offset stretches ALONG the edge to
# chord = stepover / sin(a), where `a` is the angle between the fiber and the
# edge. The loop is still legitimate -- just elongated -- and balloons cleanly
# outside the part, so we keep ballooning it however steep the layer angle. Only
# a near-tangent grazing (`a` below ~3 deg, where the loop would span many
# stepovers and bulge absurdly far past the part) falls back to a straight
# connector along the edge. The cutoff is expressed in the real variable -- the
# grazing angle -- so it does not collapse whole families of side loops at a
# fixed layer angle (a chord <= 2*pitch test would cut off at a 30 deg grazing,
# i.e. a 60 deg start angle, dropping every steeper side loop).
_LOOP_GRAZE_SIN = 0.05      # min sin(fiber-to-edge angle) that still loops (~3 deg)


# --------------------------------------------------------------------------- #
# The out-of-band line map (`--line-map`)
# --------------------------------------------------------------------------- #
# Where every emitted line sits in the scaffold's structure, for the G-code
# page's document (`mew_gcode.LineMap`): a hand edit there is recorded as
# "after layer 7", never as a line number, and re-placed when the design
# changes. A mark says "line i starts this": a "group" (the stabilization base
# or a scaffold, in `--scaffold-groups` order), a "layer", a deposition "pass",
# the "link" (turnaround) that ends a pass, or the "exit". A link opens no
# bucket of its own -- its lines stay in the pass they end, so `Address` is
# unchanged -- it only tells the layer shift where a pass's fibre stops
# (`mew_gcode.Document._layer_shift`). Never written INTO the g-code -- no
# comments in exported
# g-code is a hard rule -- and None (the default) records nothing, so every
# emitter's output is byte-identical with or without a recording.
_LINE_MARKS = None


def _mark(lines, kind, **info):
    """Record that the next line appended to `lines` starts a `kind` unit."""
    if _LINE_MARKS is not None:
        _LINE_MARKS.append(dict(info, line=len(lines), kind=kind))


@contextlib.contextmanager
def record_line_map():
    """Record the marks of every emitter called inside the block; yields the
    list they land in. Line numbers index the emitter's own `lines`, which
    `translate_gcode` preserves one for one."""
    global _LINE_MARKS
    prev = _LINE_MARKS
    _LINE_MARKS = []
    try:
        yield _LINE_MARKS
    finally:
        _LINE_MARKS = prev


def format_coord(v, precision=4):
    """A G-code coordinate as a compact string: fixed precision, then trailing
    zeros (and any bare trailing dot) trimmed. Every emitter in this module uses
    it, so they all format numbers the same way."""
    return f"{v:.{precision}f}".rstrip("0").rstrip(".")


def translate_gcode(text, dx, dy, dz, precision=4):
    """Translate every X/Y/Z coordinate in a g-code program by (dx, dy, dz) --
    used to re-base the coordinate origin (e.g. onto the collector corner). I/J
    arc-centre offsets are RELATIVE, so they're left untouched; F and all other
    words pass through. A zero offset returns the text unchanged."""
    if dx == 0.0 and dy == 0.0 and dz == 0.0:
        return text
    shift = {"X": dx, "Y": dy, "Z": dz}
    out = []
    for line in text.splitlines():
        toks = []
        for t in line.split():
            a = t[:1]
            if a in shift and len(t) > 1:
                try:
                    toks.append(a + format_coord(float(t[1:]) + shift[a], precision))
                    continue
                except ValueError:
                    pass
            toks.append(t)
        out.append(" ".join(toks))
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _unit(vx, vy):
    n = math.hypot(vx, vy)
    return (vx / n, vy / n) if n > 1e-12 else (0.0, 0.0)


def parse_angle_pattern(text):
    """Parse a per-layer angle pattern such as ``'0, 60, 90'`` into a list of
    floats (absolute fiber directions in degrees), one entry per layer and cycled
    when there are more layers than entries.

    Accepts commas and/or whitespace as separators and tolerates surrounding
    brackets or parentheses, so ``'(0, 60, 90)'`` and ``'0 60 90'`` both work.
    Returns None for blank/None input -- the caller then falls back to the
    ``start_angle + i * rotation`` progression. Raises ValueError on a
    non-numeric token (the empty-after-strip case is treated as blank -> None)."""
    if text is None:
        return None
    s = text.strip().strip("()[]{}").strip()
    if not s:
        return None
    angles = [float(tok) for tok in re.split(r"[,\s]+", s) if tok]
    return angles or None


def parse_layer_value_list(text):
    """Parse an OPTIONAL per-layer value list (the per-layer stepover / period
    offset that ride alongside an ``angle_pattern``), e.g. ``'0.3, , 0.5'`` ->
    ``[0.3, None, 0.5]``.

    Split on COMMAS ONLY, so an empty slot (a blank cell = "use the scaffold
    default for this layer") is preserved by position, aligned index-for-index
    with the angle list. Surrounding brackets/parens are tolerated. Returns None
    for blank/None input, or when every slot is empty (the list is then fully
    inert). Raises ValueError on a non-numeric, non-empty token."""
    if text is None:
        return None
    s = text.strip().strip("()[]{}").strip()
    if not s:
        return None
    out = []
    for tok in s.split(","):
        tok = tok.strip()
        out.append(float(tok) if tok else None)
    if all(x is None for x in out):
        return None
    return out


def parse_linear_pattern(text):
    """Parse a linear-pattern shape such as ``'0,0; 0,1; 1,1; 1,0'`` into a list of
    ``(x, y)`` float pairs -- ABSOLUTE vertices in the fiber-local frame (first
    number along the fiber/tangent, second perpendicular to it) that the head
    traces in order along each straight fiber.

    Pairs are separated by ``';'`` or newlines; the two numbers within a pair by a
    comma and/or whitespace. Surrounding brackets/parens are tolerated, so
    ``'(0,0); (0,1)'`` works too. Returns None for blank/None input. Raises
    ValueError on a non-numeric token or a pair that is not exactly two numbers."""
    if text is None:
        return None
    s = text.strip().strip("()[]{}").strip()
    if not s:
        return None
    pairs = []
    for chunk in re.split(r"[;\n]+", s):
        chunk = chunk.strip().strip("()[]{}").strip()
        if not chunk:
            continue
        toks = [tok for tok in re.split(r"[,\s]+", chunk) if tok]
        if len(toks) != 2:
            raise ValueError(
                f"linear-pattern point '{chunk}' must be two numbers (x, y)")
        pairs.append((float(toks[0]), float(toks[1])))
    return pairs or None


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def _clip_segment_to_box(px, py, dx, dy, xmin, xmax, ymin, ymax):
    """Liang-Barsky clip of the infinite line through (px, py) with direction
    (dx, dy) against the axis-aligned box. Returns ((x0, y0), (x1, y1)) ordered
    along +(dx, dy), or None if the line misses the box."""
    t0, t1 = -_INF, _INF
    for p, q in ((-dx, px - xmin), (dx, xmax - px),
                 (-dy, py - ymin), (dy, ymax - py)):
        if abs(p) < 1e-12:
            if q < 0:                 # parallel to this edge and outside it
                return None
            continue
        r = q / p
        if p < 0:                     # line entering w.r.t. this edge
            if r > t1:
                return None
            if r > t0:
                t0 = r
        else:                         # line leaving w.r.t. this edge
            if r < t0:
                return None
            if r < t1:
                t1 = r
    if t0 > t1:
        return None
    return (px + t0 * dx, py + t0 * dy), (px + t1 * dx, py + t1 * dy)


# --------------------------------------------------------------------------- #
# Footprint outline (the shape the serpentine fills)
# --------------------------------------------------------------------------- #
# Every non-rectangular footprint is a closed polygon centred on the origin (the
# frame _layer_passes builds the box in) and each fiber is clipped to it; the
# serpentine, turnarounds, travel minimiser and STL converter all sit downstream
# of that clip. Rectangle keeps the faster box clip, so it is byte-identical.
#
# _clip_segment_to_polygon already returns every span of a concave outline, but
# the serpentine would have to traverse the gaps, which a continuous-flow print
# cannot do -- hence the _polygon_is_convex gate.
_SHAPES = ("rectangle", "circle", "ellipse", "polygon", "custom")


def _normalize_polygon(polygon):
    """Recentre a caller-supplied polygon (a list of ``(x, y)``) on its bounding-
    box centre, returning a new vertex list, so a custom outline sits in the same
    origin-centred frame the parametric shapes use. Raises ValueError on fewer
    than three vertices."""
    pts = [(float(x), float(y)) for x, y in (polygon or [])]
    if len(pts) < 3:
        raise ValueError("custom footprint needs at least 3 vertices")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    return [(x - cx, y - cy) for x, y in pts]


def footprint_outline(shape, width, height, sides=64, custom=None):
    """The footprint outline for `shape` as a closed polygon -- a list of
    ``(x, y)`` vertices in CCW order, centred on the origin.

    - ``rectangle`` -> the four box corners (normally short-circuited upstream to
      the faster box clip; provided for completeness / tests).
    - ``circle`` -> a regular ``sides``-gon of DIAMETER ``width`` (``height``
      ignored, so it stays round). ``sides`` is the smoothness.
    - ``ellipse`` -> ``sides`` points on the ellipse with axes ``width`` x
      ``height``.
    - ``polygon`` -> a regular ``sides``-gon inscribed in the circle of diameter
      ``width`` (truly regular; ``height`` ignored).
    - ``custom`` -> the caller-supplied `custom` vertices, recentred on their bbox.

    Width/height stay the footprint's bounding box everywhere else in the
    pipeline; circle/polygon ignore height (the GUI locks it equal to width), so
    their bbox is ``width`` x ``width``."""
    shape = (shape or "rectangle").lower()
    hw, hh = width / 2.0, height / 2.0
    if shape == "rectangle":
        return [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    if shape in ("circle", "ellipse"):
        n = max(3, int(sides))
        b = hw if shape == "circle" else hh        # circle = equal-axis ellipse
        return [(hw * math.cos(2.0 * math.pi * k / n),
                 b * math.sin(2.0 * math.pi * k / n)) for k in range(n)]
    if shape == "polygon":
        n = max(3, int(sides))
        # phase: a flat side at the bottom for even n, a vertex at the top for odd
        # n -- the conventional "pointing up" orientation. Inscribed in radius hw.
        phase = math.pi / 2.0 + (math.pi / n if n % 2 == 0 else 0.0)
        return [(hw * math.cos(2.0 * math.pi * k / n + phase),
                 hw * math.sin(2.0 * math.pi * k / n + phase)) for k in range(n)]
    if shape == "custom":
        return _normalize_polygon(custom)
    raise ValueError(f"unknown shape '{shape}' (expected one of {_SHAPES})")


def _polygon_is_convex(polygon):
    """True if the simple closed `polygon` (>=3 vertices) is convex -- every turn
    has the same sign (collinear vertices, zero cross, are tolerated) and the
    outline encloses non-zero area. Gates a custom footprint to the convex family
    the serpentine handles in one span. A self-intersecting vertex order can fool
    this; the draw UI emits points in click order, so the outline stays simple."""
    n = len(polygon)
    if n < 3:
        return False
    sign = 0
    for i in range(n):
        ax, ay = polygon[i]
        bx, by = polygon[(i + 1) % n]
        cx, cy = polygon[(i + 2) % n]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if abs(cross) < 1e-12:
            continue
        s = 1 if cross > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            return False
    # A wholly collinear / all-identical outline never flips the turn-sign, so it
    # would read as "convex" and then clip to an empty scaffold. Require non-zero
    # shoelace area so the validator (and the draw UI that shares this gate) reject
    # it; a redundant collinear vertex on a real polygon still has full area.
    area2 = abs(sum(polygon[i][0] * polygon[(i + 1) % n][1]
                    - polygon[(i + 1) % n][0] * polygon[i][1]
                    for i in range(n)))
    return area2 > 1e-12


def _point_in_polygon(x, y, polygon):
    """Even-odd ray-cast point-in-polygon test. Used only on span midpoints (never
    on the boundary itself), so exact on-edge ties don't matter."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            xint = xi + (xj - xi) * (y - yi) / (yj - yi)
            if x < xint:
                inside = not inside
        j = i
    return inside


def _clip_segment_to_polygon(px, py, dx, dy, polygon):
    """Every span where the infinite line through ``(px, py)`` with direction
    ``(dx, dy)`` lies INSIDE the closed `polygon`, each as ``((x0, y0), (x1, y1))``
    ordered along ``+(dx, dy)``. The generic counterpart to _clip_segment_to_box:
    one span for a convex polygon, possibly several for a concave one.

    Method: intersect the line with every edge segment, collect the line
    parameters `t`, sort them, and keep each interval whose MIDPOINT is inside the
    polygon (a ray-cast test). The midpoint test makes it robust to lines grazing
    a vertex (duplicate `t`) or running collinear with an edge (skipped), without
    special-casing either."""
    ts = []
    n = len(polygon)
    for i in range(n):
        ax, ay = polygon[i]
        bx, by = polygon[(i + 1) % n]
        ex, ey = bx - ax, by - ay                   # edge vector
        denom = dx * ey - dy * ex                   # cross(D, E)
        if abs(denom) < 1e-12:                       # parallel / collinear -> skip
            continue
        wx, wy = ax - px, ay - py                   # A - P
        s = (wx * dy - wy * dx) / denom             # edge parameter in [0, 1]
        if -1e-9 <= s <= 1.0 + 1e-9:
            ts.append((wx * ey - wy * ex) / denom)  # line parameter t
    if len(ts) < 2:
        return []
    ts.sort()
    spans = []
    for i in range(len(ts) - 1):
        t0, t1 = ts[i], ts[i + 1]
        if t1 - t0 < 1e-9:                           # duplicate (line thru a vertex)
            continue
        tm = 0.5 * (t0 + t1)
        if _point_in_polygon(px + tm * dx, py + tm * dy, polygon):
            spans.append(((px + t0 * dx, py + t0 * dy),
                          (px + t1 * dx, py + t1 * dy)))
    return spans


def _dist_point_to_segment(px, py, ax, ay, bx, by):
    """Euclidean distance from point ``(px, py)`` to the segment ``(ax,ay)-(bx,by)``."""
    dx, dy = bx - ax, by - ay
    d2 = dx * dx + dy * dy
    if d2 < 1e-18:                          # degenerate segment -> point distance
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / d2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _point_near_polygon(x, y, polygon, margin):
    """True if ``(x, y)`` is inside `polygon` OR within `margin` of its boundary --
    i.e. inside the polygon grown OUTWARD by `margin` (with rounded corners). Used
    to test inter-layer hops against the ACTUAL deposited extent: the bare footprint
    plus the band the turnaround balloons and any wave pattern overhang past it.
    Testing distance-to-boundary (not a mitred offset) keeps acute corners from
    spiking, so the grown region hugs the real deposit."""
    if _point_in_polygon(x, y, polygon):
        return True
    if margin <= 0.0:
        return False
    n = len(polygon)
    for i in range(n):
        ax, ay = polygon[i]
        bx, by = polygon[(i + 1) % n]
        if _dist_point_to_segment(x, y, ax, ay, bx, by) <= margin:
            return True
    return False


def _hop_crosses_polygon(p0_abs, p1_abs, polygon, ox, oy, margin=0.0,
                         endpoint_clear=None):
    """True if the straight travel (G0) hop from `p0_abs` to `p1_abs` (absolute mm)
    would drag across the deposited part -- the footprint `polygon` (origin-centred
    local coords, placed at offset (ox, oy)) grown outward by `margin`, the actual
    overhang of the deposit past its footprint (turnaround balloons + wave fringe).

    The two endpoints are fibre ends sitting ON the deposit perimeter, so a stretch
    within `endpoint_clear` of either end is the hop legitimately leaving/joining its
    OWN fibre, not crossing foreign material -- those samples are skipped. Everything
    in between is sampled (densely enough that a thin clip is not stepped over)
    against the grown footprint; a single sample on deposited material means the hop
    must be routed around the part instead of dragged across it.

    `endpoint_clear` is the FIBRE-scale reach of that own-fibre exemption and MUST be
    decoupled from `margin`: `margin` (the turnaround-balloon overhang) can be a large
    fraction of the footprint, so using it as the exemption radius would
    let the two endpoint disks cover the ENTIRE hop whenever `2*margin >= length` --
    every interior sample skipped, the function returning False without testing
    anything, and a genuine cross-the-part hop dragged straight over the deposit. Pass
    a fibre-pitch-scale clearance (~one stepover) instead: a hop only counts as its
    own fibre for about one pitch past each end, and beyond that any sample inside the
    grown footprint is foreign material. `None` falls back to `margin`, with the
    swallow described above.

    `margin <= 0` reduces to the bare-footprint interior test (no overhang to clear),
    which is what a footprint with no balloon/wave bulge wants."""
    x0, y0 = p0_abs[0] - ox, p0_abs[1] - oy
    x1, y1 = p1_abs[0] - ox, p1_abs[1] - oy
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return False
    clear = margin if endpoint_clear is None else endpoint_clear
    # sample ~every 0.5 mm (at least 11 interior samples) so a hop that only nicks
    # the part between coarse steps is still caught
    nsamp = max(12, int(length / 0.5) + 1)
    for k in range(1, nsamp):
        t = k / float(nsamp)
        sx, sy = x0 + dx * t, y0 + dy * t
        if clear > 0.0 and (
                math.hypot(sx - x0, sy - y0) <= clear
                or math.hypot(sx - x1, sy - y1) <= clear):
            continue                        # still in an endpoint's own fibre region
        if _point_near_polygon(sx, sy, polygon, margin):
            return True
    return False


# --------------------------------------------------------------------------- #
# Serpentine geometry
# --------------------------------------------------------------------------- #
def _graded_offsets(lo, hi, stepover, stepover_end, c, u_lo, u_hi):
    """Perpendicular offsets, ascending, for passes whose spacing ramps ACROSS
    the layer: `stepover` where the footprint starts along the grade direction
    (`u_lo`) to `stepover_end` where it ends (`u_hi`), linear between.

    `lo`..`hi` is the footprint's extent along the pass normal n, and `c` is
    n . g, the cosine between n and the grade direction g. A pass at offset t
    sits, for the grade, where its foot t*n projects onto g: u = c*t. Each gap
    takes the spacing at its own midpoint (solved in closed form for the linear
    ramp), so the spacing between two fibers is the one asked for where they
    are. A layer whose fibers run ALONG g (c = 0) is spaced evenly at the
    spacing midway across the part, as no single fiber can change spacing
    along its own length. The band is centred like the even one: the leftover
    under one gap is split between the two edges, so a ramp with
    stepover_end == stepover gives exactly the even offsets."""
    lo_s, hi_s = min(stepover, stepover_end), max(stepover, stepover_end)
    span_u = u_hi - u_lo
    B = (stepover_end - stepover) / span_u if span_u > 1e-12 else 0.0
    A = stepover - B * u_lo
    k = B * c                         # d(spacing)/dt along the pass normal
    offs = [lo]
    t = lo
    while True:
        if abs(k) < 1.0:              # gap = spacing at the gap's midpoint
            gap = (A + k * t) / (1.0 - 0.5 * k)
        else:                         # a ramp steeper than any print needs
            gap = A + k * t
        gap = min(hi_s, max(lo_s, gap))
        if t + gap > hi + 1e-9:
            break
        t += gap
        offs.append(t)
    shift = 0.5 * (hi - offs[-1])
    return [x + shift for x in offs]


def local_stepover(stepover, stepover_end, grade_angle, width, height, dx, dy,
                   across_deg, footprint_angle=0.0, shape="rectangle",
                   shape_sides=64, footprint_polygon=None):
    """The fiber spacing (mm) a scaffold lays at one point, measured along
    `across_deg` (plate frame, degrees) between fibers that run square to it.

    `(dx, dy)` is the point relative to the footprint's centre, in the plate
    frame. An even scaffold (`stepover_end` None, or equal) is `stepover`
    everywhere. A graded one is the spacing `_layer_passes` gives there: the
    ramp read where that pass's foot projects onto the grade direction, so
    fibers that run along the grade are at the midway value. This is how
    "N pores" becomes a length on a graded layer."""
    if stepover_end is None or abs(stepover_end - stepover) <= 1e-12:
        return stepover
    fa = math.radians(footprint_angle or 0.0)
    ca, sa = math.cos(-fa), math.sin(-fa)        # plate -> un-rotated frame
    px, py = dx * ca - dy * sa, dx * sa + dy * ca
    ar = math.radians(across_deg) - fa
    nx, ny = math.cos(ar), math.sin(ar)
    gr = math.radians(grade_angle or 0.0)
    gx, gy = math.cos(gr), math.sin(gr)
    hw, hh = width / 2.0, height / 2.0
    verts = (footprint_outline(shape, width, height, shape_sides,
                               footprint_polygon)
             if shape and shape != "rectangle"
             else ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)))
    us = [vx * gx + vy * gy for vx, vy in verts]
    u_lo, u_hi = min(us), max(us)
    u = (px * nx + py * ny) * (nx * gx + ny * gy)
    f = (u - u_lo) / (u_hi - u_lo) if u_hi - u_lo > 1e-12 else 0.5
    return stepover + (stepover_end - stepover) * min(1.0, max(0.0, f))


def _layer_passes(angle_deg, width, height, stepover, flip=False, polygon=None,
                  phase_offset=0.0, stepover_end=None, grade_angle=0.0):
    """Ordered list of (start, end) points for one layer's serpentine.

    Fibers run along `angle_deg`; parallel passes are spaced `stepover` apart,
    centred on and clipped to the width x height footprint (centred on origin).

    `flip` scans the passes from the OPPOSITE perpendicular side, so the two
    serpentine ends land on the other diagonal. The deposited fiber set is
    identical; only the entry/exit corners (and which edge each U-turn balloons
    on) differ, which is what gives the layer-to-layer travel minimiser more
    entry corners to choose from.

    `phase_offset` (mm) slides the whole band perpendicular to the fibres (along
    +(nx, ny)). Guard lines are added on both ends so the shifted band still
    fills the footprint; 0 (the default) adds neither shift nor guard lines and
    is byte-identical.

    `polygon` (origin-centred ``(x, y)`` vertices) replaces the box with an
    arbitrary CONVEX outline: the scan band is centred on the polygon's
    perpendicular extent and each line clipped to it, one span per scan line.
    `None` keeps the original axis-aligned box clip, byte-identical.

    `stepover_end` grades the spacing WITHIN the layer: `stepover` at the
    footprint's start edge along `grade_angle` (degrees, in the same
    un-rotated frame as `angle_deg`; 0 = along +X) to `stepover_end` at its far
    edge (`_graded_offsets`). None, or equal to `stepover`, is the even layer,
    byte-identical."""
    th = math.radians(angle_deg)
    dx, dy = math.cos(th), math.sin(th)        # fiber direction
    nx, ny = -dy, dx                           # perpendicular (pass spacing)

    hw, hh = width / 2.0, height / 2.0
    if stepover_end is not None and abs(stepover_end - stepover) > 1e-12:
        verts = (polygon if polygon is not None
                 else ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)))
        gr = math.radians(grade_angle)
        gx, gy = math.cos(gr), math.sin(gr)
        us = [vx * gx + vy * gy for vx, vy in verts]
        # The graded band is built by stepping from one edge, so it must step
        # from the same PHYSICAL edge for every layer that runs the same way.
        # A layer at angle+180 has the opposite normal; without this it would
        # step from the far edge, lay a different set of lines and the layers
        # would not stack. `sg` turns every normal into the one of the angle
        # in [0, 180), which leaves those layers byte-identical.
        sg = 1.0 if (nx < -1e-12 or (abs(nx) <= 1e-12 and ny > 0)) else -1.0
        ts = [sg * (vx * nx + vy * ny) for vx, vy in verts]
        offs = _graded_offsets(min(ts), max(ts), stepover, stepover_end,
                               sg * (nx * gx + ny * gy), min(us), max(us))
        offs = sorted(sg * x for x in offs)
        if phase_offset:
            # guard lines past both ends at the end gaps, so the shifted band
            # still fills the footprint (out-of-footprint lines clip away)
            g0 = offs[1] - offs[0] if len(offs) > 1 else stepover
            g1 = offs[-1] - offs[-2] if len(offs) > 1 else stepover
            n0 = int(math.ceil(abs(phase_offset) / g0)) + 1
            n1 = int(math.ceil(abs(phase_offset) / g1)) + 1
            offs = ([offs[0] - j * g0 for j in range(n0, 0, -1)] + offs
                    + [offs[-1] + j * g1 for j in range(1, n1 + 1)])
            offs = [x + phase_offset for x in offs]
        if flip:
            offs = offs[::-1]
        passes = []
        for s in offs:
            if polygon is None:
                seg = _clip_segment_to_box(s * nx, s * ny, dx, dy,
                                           -hw, hw, -hh, hh)
                if seg is not None:
                    passes.append(seg)
            else:
                passes.extend(_clip_segment_to_polygon(s * nx, s * ny, dx, dy,
                                                       polygon))
        return passes
    if polygon is None:
        # extent of the box footprint measured along the perpendicular direction
        corners = ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
        projs = [cx * nx + cy * ny for cx, cy in corners]
        span = max(projs) - min(projs)

        n_lines = max(1, int(math.floor(span / stepover)) + 1)
        base = -((n_lines - 1) * stepover) / 2.0   # centre the band of fibers

        # A period offset phase-shifts the band; add guard lines on both ends so
        # the shifted grid still fills the footprint (out-of-box lines clip away).
        extra = (int(math.ceil(abs(phase_offset) / stepover)) + 1
                 if phase_offset else 0)
        passes = []
        for k in range(-extra, n_lines + extra):
            idx = (n_lines - 1 - k) if flip else k  # flip = scan from the far side
            s = base + idx * stepover               # perpendicular offset
            if phase_offset:
                s += phase_offset
            seg = _clip_segment_to_box(s * nx, s * ny, dx, dy,
                                       -hw, hw, -hh, hh)
            if seg is None:
                continue
            # Passes are returned in raw +(dx,dy) order; the serpentine
            # orientation is decided later by _orient_passes, which joins the
            # NEAREST pair of endpoints. A fixed k%2 reversal here assumes a
            # convex left-to-right sweep and breaks near the corners of an angled
            # footprint.
            passes.append(seg)
        return passes

    # arbitrary convex polygon footprint -- same scan, but the band is centred on
    # the polygon's actual perpendicular extent (it need not be symmetric about 0)
    # and each line is clipped to the polygon. _clip_segment_to_polygon returns a
    # list of spans (one for a convex outline); extend keeps passes a flat list.
    projs = [vx * nx + vy * ny for vx, vy in polygon]
    lo, hi = min(projs), max(projs)
    n_lines = max(1, int(math.floor((hi - lo) / stepover)) + 1)
    base = (lo + hi) / 2.0 - ((n_lines - 1) * stepover) / 2.0
    extra = (int(math.ceil(abs(phase_offset) / stepover)) + 1
             if phase_offset else 0)
    passes = []
    for k in range(-extra, n_lines + extra):
        idx = (n_lines - 1 - k) if flip else k
        s = base + idx * stepover
        if phase_offset:
            s += phase_offset
        passes.extend(_clip_segment_to_polygon(s * nx, s * ny, dx, dy, polygon))
    return passes


def _orient_passes(passes, start_flip=False):
    """Flip each pass so it starts at whichever end is nearest the previous
    pass's exit, so every turnaround joins the CLOSEST pair of pass ends. Pass 0
    has no predecessor and is oriented by the same rule against pass 1 (see the
    inline note) -- otherwise its raw clip order inverts the first loop.

    `start_flip` inverts pass 0, which cascades: every pass reverses its travel
    direction, so the snake's two FREE ENDS move to the opposite footprint edge.
    The deposited grid is identical; only the entry/exit corners change.
    `_emit_one_scaffold` generates both values as extra travel-minimisation
    candidates -- without them an EVEN pass count leaves both free ends on one
    edge and a layer change off the other costs a full-footprint hop.

    A fixed alternating reversal assumes a convex sweep and breaks near the
    corners of an ANGLED footprint, where two adjacent passes' ends on one edge
    sit pitch / sin(fiber-to-edge angle) apart while their other ends are a
    normal pitch apart: the rigid scheme joins the FAR pair and the turnaround
    balloons across the whole corner."""
    if len(passes) < 2:
        return list(passes)
    # Pass 0 must be oriented too. Every later pass is flipped so its EXIT end is
    # the one nearest the next pass; pass 0 has no predecessor, so left alone it
    # stays in raw +(dx,dy) clip order. On a corner pass that puts its "exit" at
    # the FAR end, so the first turnaround's outward (= exit travel direction)
    # points the wrong way and _turn_points balloons that loop INWARD, into the
    # scaffold. Orient pass 0 by the same nearest-join rule: its exit is whichever
    # of its two ends sits closer to pass 1.
    a0, b0 = passes[0]
    near_a = min((a0[0] - p[0]) ** 2 + (a0[1] - p[1]) ** 2 for p in passes[1])
    near_b = min((b0[0] - p[0]) ** 2 + (b0[1] - p[1]) ** 2 for p in passes[1])
    choose_ab = near_b <= near_a
    if start_flip:                              # send the free ends to the far edge
        choose_ab = not choose_ab
    oriented = [(a0, b0) if choose_ab else (b0, a0)]
    for a, b in passes[1:]:
        ex, ey = oriented[-1][1]                # previous pass's exit point
        da = (a[0] - ex) ** 2 + (a[1] - ey) ** 2
        db = (b[0] - ex) ** 2 + (b[1] - ey) ** 2
        oriented.append((a, b) if da <= db else (b, a))
    return oriented


def _wave_pass(a, b, table, period, tangent, pin_tips=True):
    """Trace one straight deposition pass ``a -> b`` as a WAVY polyline by applying
    the linear-pattern shape `table` along it. `table` is a list of ABSOLUTE
    ``(x, y)`` vertices of ONE period -- ``x`` runs along the fiber, ``y`` is the
    perpendicular displacement -- repeating every `period` mm (the authored
    wavelength PLUS any flat period-spacing gap).

    Vertex ``i`` sits at the base slot ``i * period / N``, so the
    ``(along, perp)`` offset form the sampler wants is ``along_i = x_i - i *
    period / N``; the wrap to the next period spans ``(x_{N-1}, y_{N-1}) ->
    (period + x_0, y_0)``. Drawing a square is therefore just its four corners.

    Phase is read from each point's projection onto `tangent` -- a fixed spatial
    corrugation, not arc length from the pass start. `tangent` is the LAYER's
    single deposition direction, shared by every pass so they never cross-hatch,
    and it is the FULL 360 deg direction rather than folded to [0, 180): a layer
    laid at `angle + 180` gets ``-tangent`` and deposits the base wave rigidly
    ROTATED, which is what makes 0/90/180/270 a genuine rotation series for an
    asymmetric table.

    The table is piecewise-linear in phase, so the ``period / N`` breakpoints plus
    the two pass ends reproduce the wave exactly with the fewest points.
    `pin_tips` (the default) drops BOTH wave offsets at the tips so each lands on
    the bare grid corner; without it the layer-rotating corrugation slides the tip
    ALONG the edge in opposite directions on a layer and its 180-degree partner,
    staggering the turnaround loops. The authoring preview passes False."""
    ax, ay = a
    bx, by = b
    L = math.hypot(bx - ax, by - ay)
    if L < 1e-12 or not table or period <= 0:
        return [(ax, ay), (bx, by)]
    ux, uy = (bx - ax) / L, (by - ay) / L       # unit travel direction of THIS pass
    tx, ty = tangent                            # the LAYER's deposition direction
    px, py = -ty, tx                            # layer perpendicular (rot +90)
    sign = ux * tx + uy * ty                    # +1 if this pass runs +tangent, else -1
    n = len(table)
    W = period
    step = W / n
    # absolute (x, y) vertices -> the (along, perp) offsets the sampler uses: vertex
    # i sits at base slot i*step, so along_i = x_i - i*step lands it exactly on x_i.
    offs = [(vx - i * step, vy) for i, (vx, vy) in enumerate(table)]
    # projection of the pass START onto the layer tangent; the projection at
    # arc-length s along the pass is phase_origin + s * sign.
    phase_origin = ax * tx + ay * ty

    # breakpoints: the two pass ends, plus every table-segment boundary the
    # projection crosses inside the pass (epsilons drop zero-length slivers at the
    # endpoints already in the set).
    esses = {0.0, L}
    proj0, proj1 = phase_origin, phase_origin + L * sign
    lo, hi = (proj0, proj1) if proj0 <= proj1 else (proj1, proj0)
    m = math.floor(lo / step) + 1
    while m * step < hi - 1e-9:
        s = (m * step - phase_origin) / sign    # sign is +/-1
        if 1e-9 < s < L - 1e-9:
            esses.add(s)
        m += 1
    pts = []
    for s in sorted(esses):
        proj = phase_origin + s * sign
        phase = (proj % W) / W                   # in [0, 1)
        pos = phase * n
        k = int(math.floor(pos)) % n
        frac = pos - math.floor(pos)
        a0, p0 = offs[k]
        a1, p1 = offs[(k + 1) % n]
        along = a0 + (a1 - a0) * frac
        perp = p0 + (p1 - p0) * frac
        # Pin the two TIPS to the bare pass end (the footprint grid corner) by
        # dropping BOTH wave offsets there; interior points keep the full
        # corrugation. See the docstring for why the `perp` half matters.
        if pin_tips and (s <= 1e-9 or s >= L - 1e-9):
            along = 0.0
            perp = 0.0
        x = ax + s * ux + along * tx + perp * px
        y = ay + s * uy + along * ty + perp * py
        pts.append((x, y))
    return pts


def _flatten_passes(passes, arc_radius, arc_segments, pitch, g2g3=False,
                    pattern=None, vflip=False, layer_dir=None, arc_scale=None):
    """Flatten a serpentine pass list into one continuous point list, joining
    each pass to the next with a rounded U-turn (the layer is one fiber).

    Each point is `(x, y, link_in, arc)`. `link_in` is True when the segment
    ENTERING this point is a fiber link -- the turnaround joining one pass to the
    next, whether a ballooning arc or a straight connector -- so emission can run
    every link at a separate feedrate. Only the straight deposition passes
    (a -> b) stay False -> main speed. Tagging the entering edge (not the point)
    keeps the classification reversal-safe via `_reverse_path`.

    `arc` is None for a straight segment; for a turnaround emitted as a single
    true circular arc (`g2g3=True`) it is `(cx, cy, ccw)` -- the loop's absolute
    centre and orientation -- which `_emit_one_scaffold` turns into one G2/G3
    word instead of a fan of G1 chords. Storing the centre in ABSOLUTE coords
    (not an I/J offset) is what makes arcs survive `_reverse_path`: reversal only
    has to flip `ccw`, and I/J is recomputed from the live previous point at emit.

    `pitch` is the perpendicular pass spacing (stepover). Every turnaround joins
    two adjacent fibers and balloons; the only fallback to a straight connector
    is a near-tangent grazing on an edge nearly parallel to the fibers, where the
    loop would span many pitches and bulge absurdly far past the part. See
    `_LOOP_GRAZE_SIN` for the cutoff (and why it is on the grazing angle, not a
    fixed multiple of pitch).

    `arc_scale` (a graded layer, whose pass spacing changes across it) sizes
    each turnaround from the gap it actually joins: radius `arc_scale` times
    the perpendicular gap between the two passes, which is also the pitch its
    grazing test uses. None keeps one `arc_radius` and `pitch` for the layer."""
    passes = _orient_passes(passes, start_flip=vflip)   # join nearest endpoints

    # `pattern` (table, wavelength, spacing) waves each straight pass via _wave_pass,
    # using the layer's deposition direction `layer_dir` as the shared wave frame so
    # the whole layer corrugates consistently and `angle + 180` layers come out
    # rotated (see _wave_pass). The shape repeats every `wavelength + spacing` mm --
    # the spacing is a flat gap between repeats. The turnarounds then reconnect to the
    # WAVY pass ends so the layer stays one continuous fiber. None (the default) keeps
    # every pass a single straight a->b segment, byte-identical to before. Travel
    # direction (`outward`) is taken from the ORIGINAL straight pass so each loop
    # still balloons the same way.
    table, wavelength, spacing = pattern if pattern else (None, 0.0, 0.0)
    period = wavelength + spacing

    def _pass_pts(a, b):
        if table:
            return _wave_pass(a, b, table, period, layer_dir)
        return [(a[0], a[1]), (b[0], b[1])]

    wpasses = [_pass_pts(a, b) for (a, b) in passes]
    pts = [(wpasses[0][0][0], wpasses[0][0][1], False, None)]
    for k, (a, b) in enumerate(passes):
        wp = wpasses[k]
        for (wx, wy) in wp[1:]:
            pts.append((wx, wy, False, None))   # deposition pass (wavy or straight)
        if k + 1 < len(passes):
            b_eff = wp[-1]                       # wavy end of this pass
            a_next = wpasses[k + 1][0]           # wavy start of the next pass
            chord = math.hypot(a_next[0] - b_eff[0], a_next[1] - b_eff[1])
            if arc_scale is not None:
                # this turn's own gap: the next pass's distance off this
                # pass's line, measured square to the fibers
                L = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
                ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
                nb = passes[k + 1][0]
                gap = abs((nb[0] - b[0]) * uy - (nb[1] - b[1]) * ux) or pitch
                pitch, arc_radius = gap, arc_scale * gap
            # Balloon unless the fiber grazes an edge below ~3 deg: the adjacent
            # gap is pitch / sin(a), so chord <= pitch / _LOOP_GRAZE_SIN keeps
            # every loop up to a near-tangent grazing (steep layer angles loop
            # fine), then falls back to a straight connector. (1e-9 guards the
            # float-equality boundary.)
            if (arc_radius and arc_radius > 1e-9
                    and chord <= pitch / _LOOP_GRAZE_SIN + 1e-9):
                # Which way the balloon bulges. The pass-travel direction is the
                # usual cue and balloons every interior turnaround cleanly past
                # the edge it sits on (the circle solver normalises it once). But
                # at a CORNER join -- this pass ends on one footprint edge and the
                # next starts on the ADJACENT, perpendicular edge, which angled
                # (triangle / decagon) layers routinely produce near the corners --
                # the pass-travel direction runs ALONG the first edge, i.e. back
                # into the part, so ballooning that way inverts the loop and it
                # crosses the scaffold. Detect that (travel points toward the
                # footprint centre, which is the ORIGIN in this pre-offset frame)
                # and balloon radially out through the corner instead. Interior
                # loops, where travel already points outward, are byte-identical.
                outward = (b[0] - a[0], b[1] - a[1])
                mid_x = (b_eff[0] + a_next[0]) / 2.0
                mid_y = (b_eff[1] + a_next[1]) / 2.0
                if outward[0] * mid_x + outward[1] * mid_y < 0.0:
                    outward = (mid_x, mid_y)
                if g2g3:
                    # one true arc: keep just the endpoint, tagged with the loop
                    # circle so emission writes a single G2/G3. arc_segments
                    # (the G1 fidelity knob) is moot here.
                    sol = _turn_circle(b_eff, a_next, outward, arc_radius)
                    if sol is not None:
                        cx, cy, _R, _ab, sweep = sol
                        pts.append((a_next[0], a_next[1], True,
                                    (cx, cy, sweep > 0)))
                else:
                    pts.extend((px, py, True, None) for (px, py)
                               in _turn_points(b_eff, a_next, outward,
                                               arc_radius, arc_segments))
            if (abs(a_next[0] - pts[-1][0]) > 1e-9
                    or abs(a_next[1] - pts[-1][1]) > 1e-9):
                # entering edge of a_next is the link's last segment (or, with no
                # arc points, the whole straight connector) -> loop speed. Skipped
                # in g2g3 mode, where the arc point above already IS a_next.
                pts.append((a_next[0], a_next[1], True, None))
    return pts


def _reverse_path(pts):
    """Reverse a flattened `(x, y, link_in, arc)` path, preserving each edge's
    link-ness and arc. `link_in` and `arc` tag the segment ENTERING a point, so
    on reversal both shift one slot (the same physical edge now enters from the
    other end); the new first point has no entering edge. An arc additionally
    flips orientation -- traversing the same circle backwards swaps CW<->CCW --
    while its absolute centre is unchanged (I/J is recomputed at emit time)."""
    n = len(pts)
    rev = []
    for k in range(n):
        x, y, _f, _arc = pts[n - 1 - k]
        if k >= 1:
            link_in = pts[n - k][2]
            arc = pts[n - k][3]
            if arc is not None:
                cx, cy, ccw = arc
                arc = (cx, cy, not ccw)
        else:
            link_in, arc = False, None
        rev.append((x, y, link_in, arc))
    return rev


def _turn_circle(b, a_next, outward, arc_radius):
    """Solve the ballooning turnaround loop as one circle. Returns
    `(cx, cy, R, ab, sweep)` -- absolute centre, radius, the angle of `b` on the
    circle, and the SIGNED sweep to `a_next` (>0 is CCW / G3, <0 is CW / G2) --
    or None if the two pass ends coincide.

    The loop is a single circle of radius `arc_radius`: both pass ends lie ON the
    circle and the fiber follows the REFLEX (major) arc, so the loop balloons out
    past the scaffold edge and is WIDER than the pass pitch (the circle's sides
    sit outboard of the two fibers, not tangent to them). `outward` is the pass
    travel direction at `b`; it picks which way the balloon bulges. With
    arc_radius == pitch/2 this degenerates to the tangent semicircle; smaller
    values are clamped up to pitch/2 (a circle can't span a wider chord).

    Shared by the G1 discretiser (`_turn_points`) and the G2/G3 arc emitter so
    both describe the EXACT same loop -- the only difference is serialization."""
    bx, by = b
    ax, ay = a_next
    # the ACTUAL join distance between the two pass ends -- equals the pass
    # pitch in the interior, but stretches to pitch / sin(grazing angle) on a
    # corner/angled join, and the circle below must reach both ends. Do NOT
    # substitute `stepover` here: that breaks angled (triangle/decagon) layers.
    chord = math.hypot(ax - bx, ay - by)
    if chord < 1e-9:
        return None
    R = max(float(arc_radius), chord / 2.0)             # circle must reach both
    mx, my = (bx + ax) / 2.0, (by + ay) / 2.0           # chord midpoint
    odx, ody = _unit(*outward)
    # Centre sits on the chord's PERPENDICULAR BISECTOR, displaced to whichever
    # side `outward` (the pass travel direction) points, so the reflex arc
    # balloons away from the scaffold AND both pass ends lie exactly on the
    # circle. NB: the centre must be on the bisector for the ends to be R from
    # it -- displacing along `outward` itself only coincides with the bisector
    # when outward is perpendicular to the chord (axis-aligned passes). For
    # angled layers (triangle/decagon) it is not, which threw the centre off the
    # bisector and broke the loops (ends no longer on the circle).
    ux, uy = (ax - bx) / chord, (ay - by) / chord       # unit chord direction
    px, py = -uy, ux                                     # unit chord-perpendicular
    if px * odx + py * ody < 0.0:                        # face the outward side
        px, py = -px, -py
    dmc = math.sqrt(max(0.0, R * R - (chord / 2.0) ** 2))
    cx, cy = mx + px * dmc, my + py * dmc

    ab = math.atan2(by - cy, bx - cx)
    aa = math.atan2(ay - cy, ax - cx)
    sweep_ccw = (aa - ab) % (2.0 * math.pi)             # in [0, 2pi)
    sweep_cw = sweep_ccw - 2.0 * math.pi
    # take whichever direction's mid-arc point lies further OUTWARD -> the
    # balloon, i.e. the reflex arc through the outward apex
    def outwardness(sweep):
        ang = ab + sweep / 2.0
        return (math.cos(ang) * odx + math.sin(ang) * ody)
    sweep = sweep_ccw if outwardness(sweep_ccw) >= outwardness(sweep_cw) else sweep_cw
    return cx, cy, R, ab, sweep


def _turn_points(b, a_next, outward, arc_radius, n_arc):
    """Interior points of a ballooning turnaround loop from the end of one pass
    (`b`) to the start of the next (`a_next`), excluding the two endpoints --
    the G1-chord approximation of the circle `_turn_circle` solves."""
    sol = _turn_circle(b, a_next, outward, arc_radius)
    if sol is None:
        return []
    cx, cy, R, ab, sweep = sol
    seg = max(2, int(round(n_arc * abs(sweep) / math.pi)))
    pts = []
    for j in range(1, seg):
        ang = ab + sweep * (j / seg)
        pts.append((cx + R * math.cos(ang), cy + R * math.sin(ang)))
    return pts


# --------------------------------------------------------------------------- #
# Layer-to-layer linking loop (lead-in / lead-out)
# --------------------------------------------------------------------------- #
# A layer change turns the head in place, and in continuous-flow MEW the lagging
# jet rounds (and can whip) the first stretch of the new layer. The loop replaces
# that with an outboard LASSO (see `_link_loop_circle`), so the whole direction
# change happens where the head is already moving. It is kink-free at any turn
# but unbounded in size: its far point is R * (1 + sec(turn/2)), which runs away
# toward 180 deg -- the 144 deg change of the 36 deg decagon preset reached
# 4.24 R, a 12.7 mm teardrop off a 26 mm scaffold. The budget below is the reach
# a 90 deg turn already gives, so a preset turning 90 or less is byte-identical.
_LINK_LOOP_MAX_REACH = 1.0 + math.sqrt(2.0)


def _link_loop_circle(p, d_out, d_in, radius, part_centre):
    """Solve the layer-to-layer linking loop as one outboard circle.

    `p` is the shared corner (absolute mm) where the previous layer exits moving
    along unit heading `d_out` and the next layer must leave moving along unit
    heading `d_in`. `radius` is the loop radius (reuses the turnaround
    `arc_radius`); `part_centre` is the footprint centre, used to keep the loop
    bulging AWAY from the scaffold.

    The circle is tangent to both rays from the corner (+`d_out` and -`d_in`),
    which fixes its centre on their bisector at R / cos(turn/2) and the feet at
    R * tan(turn/2); the arc's orientation falls out of matching the head's
    heading where it joins the circle.

    Returns `(cx, cy, R, a1, sweep, t1, t2, ccw)` -- centre, radius, the angle of
    the first foot `t1`, the SIGNED sweep to the second foot `t2` (>0 CCW / G3,
    <0 CW / G2), both feet (absolute) and the orientation -- or None when no
    clean outboard loop exists: a degenerate heading or radius, a turn too
    straight (<~1 deg) or too near a U-turn, or one whose only loop would bulge
    INTO the part.

    `R` is always the commanded `radius`. The reach cap pulls the CENTRE in
    instead, turning the feet into secant feet, so a sharp layer change gets a
    sharp JOIN rather than a shrivelled loop; shrinking R to hold the reach was
    tried and rejected, because a 165 deg turn took the loop to 0.19 R. Inert for
    turns up to 90 deg."""
    dox, doy = d_out
    dix, diy = d_in
    if (dox * dox + doy * doy) < 0.5 or (dix * dix + diy * diy) < 0.5:
        return None                          # a zero/degenerate heading -> no loop
    R = float(radius)
    if R < 1e-9:
        return None                          # no radius (straight connectors) -> none
    c = max(-1.0, min(1.0, dox * dix + doy * diy))      # cos(turn angle)
    cos_half = math.sqrt((1.0 + c) / 2.0)               # cos(turn/2) >= 0
    sin_half = math.sqrt((1.0 - c) / 2.0)               # sin(turn/2) >= 0
    if sin_half < 1e-3 or cos_half < 1e-3:
        return None                          # ~straight (no corner) or ~U-turn
    D = R / cos_half                                    # corner -> circle centre
    L = R * sin_half / cos_half                         # corner -> tangent foot
    # Hold the far point (D + R) to _LINK_LOOP_MAX_REACH * R by pulling the CENTRE
    # DISTANCE in, never the radius. The legs then meet the circle as SECANTS, so
    # the join angle appears out on the loop where the head is already moving and
    # never at `p`. The cap is CONTINUOUS in the turn (see the discriminant note
    # below); a threshold BETWEEN SHAPES is what flipped one loop of nine on
    # `presets/standard_scaffold.json` when a square's exactly-90 corner rounded a
    # single ULP high (`aceb79f`).
    d_cap = (_LINK_LOOP_MAX_REACH - 1.0) * R
    if D > d_cap * (1.0 + 1e-12):
        D = d_cap
        # Leg length to the NEAR intersection of the ray with the circle. The
        # discriminant is R^2 - (centre->ray distance)^2 and is >= 0 because
        # d_cap * cos_half < R here; it goes to 0 exactly as D approaches the
        # tangent distance, so this reduces to `L = R * tan(turn/2)` at the seam
        # (agreement measured to <1e-6 mm).
        L = D * sin_half - math.sqrt(max(0.0, R * R - (D * cos_half) ** 2))
    # bisector of the two rays (+d_out and -d_in); the tangent circle sits on this
    # side of the corner and the loop bulges along it. Require it to point OUTBOARD
    # (away from the part centre), else these headings only admit a loop that
    # crosses the scaffold -> no loop.
    mx, my = dox - dix, doy - diy
    mn = math.hypot(mx, my)
    if mn < 1e-9:
        return None
    mx, my = mx / mn, my / mn
    if mx * (part_centre[0] - p[0]) + my * (part_centre[1] - p[1]) > 0.0:
        return None
    cx, cy = p[0] + mx * D, p[1] + my * D
    t1 = (p[0] + dox * L, p[1] + doy * L)
    t2 = (p[0] - dix * L, p[1] - diy * L)
    a1 = math.atan2(t1[1] - cy, t1[0] - cx)
    a2 = math.atan2(t2[1] - cy, t2[0] - cx)
    # orientation: the head reaches t1 moving along d_out; match the circle's CCW
    # tangent there (perpendicular to the radius t1 - centre).
    ccw = (dox * -(t1[1] - cy) + doy * (t1[0] - cx)) > 0.0
    sweep = (a2 - a1) % (2.0 * math.pi)                 # in [0, 2pi)
    if not ccw:
        sweep -= 2.0 * math.pi                          # CW -> negative
    return cx, cy, R, a1, sweep, t1, t2, ccw


def _line_crossing(p, d, q, e):
    """Where the line through `p` along `d` meets the line through `q` along
    `e` (absolute mm), or None when the two are within ~0.06 deg of parallel."""
    den = d[0] * e[1] - d[1] * e[0]
    if abs(den) < 1e-3:
        return None
    s = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / den
    return (p[0] + d[0] * s, p[1] + d[1] * s)


def _link_loop_points(p, d_out, d_in, radius, part_centre, n_arc, g2g3):
    """The linking loop from corner `p` back to `p` as an ordered list of
    `(x, y, arc)` moves (absolute mm) the head traces AFTER arriving at `p`: out
    along `d_out` to the first tangent foot, around the outboard circle, and back
    along `d_in` to `p`. `arc` is None for a straight chord, or `(cx, cy, ccw)` for
    one true G2/G3 arc to that point when `g2g3`. `n_arc` is the G1-chord fidelity
    (segments per half-turn), ignored when `g2g3`. Empty list when no clean
    outboard loop exists (`_link_loop_circle` returns None). One construction at
    every turn -- the reach cap lives in `_link_loop_circle`, as a centre distance,
    so there is nothing here that can pick a different shape."""
    sol = _link_loop_circle(p, d_out, d_in, radius, part_centre)
    if sol is None:
        return []
    cx, cy, R, a1, sweep, t1, t2, ccw = sol
    pts = [(t1[0], t1[1], None)]                         # straight out to t1
    if g2g3:
        pts.append((t2[0], t2[1], (cx, cy, ccw)))        # one true arc t1 -> t2
    else:
        seg = max(2, int(round(n_arc * abs(sweep) / math.pi)))
        for j in range(1, seg):                          # interior arc chords
            ang = a1 + sweep * (j / seg)
            pts.append((cx + R * math.cos(ang), cy + R * math.sin(ang), None))
        pts.append((t2[0], t2[1], None))                 # final chord to t2
    pts.append((p[0], p[1], None))                       # straight back in to p
    return pts


# How straight the last `link_loop_dist` of path must be before its chord is
# trusted as a lead-in heading: net displacement over path length. Below it the
# head has retraced its own line and the chord's DIRECTION is the quotient of two
# nearly cancelling vectors, ill-conditioned as 1/|chord|. 0.2 sets that
# sensitivity: at the 0.5 mm default the chord is held to >= 0.1 mm, so the
# g-code's own 1e-4 mm of rounding moves the lead-in by at most 1e-3 rad. See
# `_lead_in_chord` for why a band and not a point.
_LEAD_IN_MIN_STRAIGHT = 0.2


def _pass_run_dirs(pts):
    """`(first, last)` -- the unit net displacement of the layer's FIRST and
    LAST deposition pass, or `(0.0, 0.0)` for a pass with no extent.

    A serpentine layer is one path whose straight passes are joined by
    turnarounds, and `_flatten_passes` marks a turnaround-entering segment
    `link_in`. So the first pass ends at the last point before the first mark and
    the last pass starts at the last marked point; a single-pass layer has no
    mark and both reduce to the whole path.

    THE ENTRY'S SIGN CANNOT COME FROM THE PART CENTRE. Which end a layer is
    entered from is decided by the four-candidate travel minimisation, which
    never consults the centre, so "point the axis inboard" agrees only when the
    entry sits on an edge the axis crosses steeply: over test 6d's sweep the two
    disagreed on 38 of the 366 arrival loops a wave emits, each handing the head
    into the layer start 180 deg against the fibre it was about to lay. This test
    is also the better-conditioned one -- `pin_tips` puts the pass ends on the
    bare grid corners, so the dot is +/- the full pass length, while the centre
    dot passes through zero on any entry lying on a perpendicular edge.

    `last` IS NOT THE MIRROR OF `first` and must not sign an exit heading. At the
    exit the head ARRIVES, and its last commanded segment runs from the final
    interior wave vertex back to the pinned tip -- opposite the pass's net run in
    425 of 908 layers measured. It is returned so the symmetry is refuted once
    here; the exit heading is `prev_exit_dir`.
    """
    def _net(a, b):
        return _unit(pts[b][0] - pts[a][0], pts[b][1] - pts[a][1])

    n = len(pts)
    if n < 2:
        return (0.0, 0.0), (0.0, 0.0)
    k = 1
    while k < n and not pts[k][2]:
        k += 1
    j = n - 1
    while j > 0 and not pts[j][2]:
        j -= 1
    return _net(0, k - 1), _net(j, n - 1)


def _lead_in_chord(entry, prev_exit, exit_dir, dist):
    """The loop's lead-in heading: the CHORD of the last `dist` mm of the path
    the head actually travelled before it arrived at `entry`.

        travel = entry - prev_exit        gap = |travel|
        chord  = travel + max(0, dist - gap) * exit_dir

    i.e. walk `dist` back from `entry`: along the layer-change travel first, then
    on down the previous layer's last fibre. Returns the unit chord, or `(0, 0)`
    when that stretch doubles back on itself, which `_link_loop_circle` refuses.

    WHY A CHORD AND NOT A CHOICE. The arrival cue is the TRAVEL when the hop has
    length and the previous layer's EXIT heading when it does not, but picking
    between them on a length threshold is answered by floating-point noise: the
    rotation-90 gap is 0.0 on some changes and up to 1.6e-14 mm on others. The
    chord has no branch -- at `gap == 0` it IS `exit_dir`, bit-identical to the
    pre-travel code, and at `gap >= dist` it is the travel heading.

    WHERE IT IS NOT CONTINUOUS, AND WHY THAT CANNOT BE FIXED. A hop running back
    along the fibre just laid cancels against it, passing through zero at
    `gap = dist / 2`. No formula avoids that: the map must be a CONSTANT at
    `gap == 0` and the IDENTITY at `gap >= dist`, and a family deforming one into
    the other on the circle of headings cannot stay non-zero throughout. Only
    WHERE the singularity sits is a choice, so it is put where the loop is refused
    anyway and given WIDTH (`_LEAD_IN_MIN_STRAIGHT`) -- a finite band is what
    keeps the two opposite lead-ins from ever being ADJACENT (`aceb79f`)."""
    tx = entry[0] - prev_exit[0]
    ty = entry[1] - prev_exit[1]
    if tx == 0.0 and ty == 0.0:
        return exit_dir                  # no travel at all -> the exit heading
    d = float(dist)
    back = d - math.hypot(tx, ty)
    if back > 0.0:                       # travel shorter than the look-back
        tx += back * exit_dir[0]
        ty += back * exit_dir[1]
    n = math.hypot(tx, ty)
    if n <= _LEAD_IN_MIN_STRAIGHT * max(0.0, d) or n < 1e-12:
        return (0.0, 0.0)                # the path doubled back -> no lead-in
    return (tx / n, ty / n)


# --------------------------------------------------------------------------- #
# G-code emission
# --------------------------------------------------------------------------- #
def _emit_one_scaffold(lines, current_f, fmt, *, width, height, stepover, rotation,
                       fiber_diameter, layers, speed, start_angle, arc_radius,
                       arc_segments, start_x, start_y, prev_exit=None,
                       loop_speed=None, start_z=0.0, g2g3=False,
                       angle_pattern=None, angle_stepovers=None,
                       angle_offsets=None, linear_pattern=None,
                       linear_wavelength=0.0, linear_spacing=0.0,
                       shape="rectangle", shape_sides=64, footprint_polygon=None,
                       link_loops="off", link_loop_angle=25.0, link_loop_dist=0.5,
                       link_loop_travel=False,
                       footprint_angle=0.0, stack_layers=True,
                       layer_overrides=None, perimeter_travel=False,
                       perimeter_travel_offset=1.0, perimeter_obstacles=None,
                       lead_in_from=(0.0, 0.0), extent_out=None,
                       stepover_end=None, grade_angle=0.0):
    """Append one scaffold's G0/G1 moves to `lines` and return
    `(current_f, prev_exit)`.

    `perimeter_travel` (off by default, so the output is byte-identical) routes
    the travel INTO this scaffold along its perimeter, `perimeter_travel_offset`
    mm clear of the outermost fibre (`perimeter_travel.py`): every layer change
    whose free ends are not one shared corner, and the first entry -- from
    `lead_in_from` when this is the program's first scaffold, or from
    `prev_exit` round the `perimeter_obstacles` lanes when it follows another.
    The obstacles are every scaffold already laid, as lane polygons; None leaves
    the first entry to the caller. `extent_out`, if a list, receives every point
    this scaffold deposits (absolute, arcs sampled) so the caller can build its
    lane for the scaffolds that follow.

    `layer_overrides` ({layer index (0-based): {key: value}}, see
    `LAYER_OVERRIDE_KEYS`) replaces a scaffold value on single layers. Absent
    or empty, every layer takes the scaffold values and the output is
    byte-identical to a call without it.

    Every keyword's user-facing meaning is documented once, at
    `generate_scaffold_gcode`. What is only true down here:

    `current_f` is the feedrate (mm/min) in effect from a previous G1, or None if
    none has been emitted; F is (re)emitted only on a change, so a whole
    multi-scaffold print at one speed still emits it once. No G21 header here --
    the caller wraps the file so every scaffold shares one program. Every LINK
    switches to `loop_speed`; only deposition passes stay at `speed`.

    `prev_exit` is the ABSOLUTE (x, y) the previous layer or scaffold finished
    at; each layer is entered from whichever of its two ends is nearer, and the
    returned value continues that minimisation across the stabilization -> main
    boundary. The four candidates come from `flip` x `start_flip` x reversal. A
    linking loop is sited only on the direct straight hop and never on the first
    entry: the around-the-part router arrives pointing inward, where no outboard
    circle exists (see the inline note).

    `footprint_angle` is applied as a rigid turn of the GENERATED passes, not by
    re-clipping against a rotated polygon -- that dropped the edge-parallel
    extreme pass and desynced consecutive layers' corners, so the loops stopped
    firing. The inter-scaffold and exit travel still route around each
    footprint's un-rotated bbox, a slightly loose exclusion."""
    arc_radius_is_default = arc_radius is None
    if arc_radius is None:
        arc_radius = 1.5 * stepover
    loop_feed = speed if loop_speed is None else loop_speed
    # the scaffold values a layer override falls back to, and the frame they
    # build (re-derived below only for a layer that moves, resizes or turns it)
    base = dict(width=width, height=height, footprint_angle=footprint_angle,
                start_x=start_x, start_y=start_y, speed=speed,
                loop_feed=loop_feed, arc_segments=arc_segments,
                link_loops=link_loops)
    layer_overrides = layer_overrides or {}
    # linear pattern: wave each straight fiber via the absolute (x, y) vertex table,
    # repeating every linear_wavelength + linear_spacing mm (spacing = flat gap).
    # Active only when both a non-empty table and a positive wavelength are given;
    # otherwise off (every pass stays a straight a->b segment).
    pattern = ((linear_pattern, linear_wavelength, max(0.0, linear_spacing))
               if linear_pattern and linear_wavelength > 0 else None)
    # Peak PERPENDICULAR reach of the wave (max |y| in the vertex table), 0 when no
    # pattern. A linear pattern rides its corrugation right to the pass tips, so at a
    # square corner the previous layer's exit tip and the next layer's entry tip no
    # longer coincide -- they sit up to this far off the shared corner (in each
    # layer's own perpendicular). It widens the linking-loop coincidence gate below
    # so a true corner still reads as coincident, and flags the pattern case where
    # the loop headings must come from the fibre axes, not the wavy end legs.
    wave_perp_max = (max((abs(v[1]) for v in linear_pattern), default=0.0)
                     if pattern is not None else 0.0)

    def _layer_frame(width, height, footprint_angle, start_x, start_y):
        """The footprint frame one set of size / turn / start values builds:
        (poly_gen, fa_ca, fa_sa, poly, poly bbox or None, ox, oy)."""
        # Footprint outline the passes are GENERATED + clipped against, in the
        # UN-rotated frame. A rectangle stays on the fast axis-aligned box clip
        # (poly_gen=None) so its output is byte-identical; any other shape becomes a
        # centred convex polygon the fibers are clipped to. Resolved once per scaffold.
        poly_gen = (footprint_outline(shape, width, height, shape_sides,
                                      footprint_polygon)
                    if shape and shape != "rectangle" else None)
        # Footprint rotation is a RIGID turn about the centre. The passes are generated
        # in the un-rotated frame (poly_gen + the fill angle WITHOUT footprint_angle) and
        # every generated point is then rotated by footprint_angle (see the layer loop),
        # so the rotated scaffold is geometrically identical to the un-rotated one --
        # square corners still coincide layer-to-layer, and the linking loops fire. The
        # old path re-clipped the fibres against a ROTATED polygon instead, which
        # silently dropped the edge-parallel extreme pass (the scan-line count lost one
        # to an FP floor, and a scan line lying on a rotated edge was skipped by the
        # boundary-grazing case in _clip_segment_to_polygon). That desynced consecutive
        # layers' free ends by ~half a pitch, so the loop's coincidence gate rejected the
        # corner and left a bare travel hop where a loop belongs. 0 deg -> identity.
        if footprint_angle:
            _fa = math.radians(footprint_angle)
            fa_ca, fa_sa = math.cos(_fa), math.sin(_fa)
        else:
            fa_ca, fa_sa = 1.0, 0.0
        # Rotated outline, used ONLY for the inter-layer travel routing and the
        # deposited-extent bbox (the deposit really is turned). None for an un-rotated
        # rectangle, so that case keeps the fast straight-hop path byte-identical.
        if poly_gen is not None:
            poly = [(x * fa_ca - y * fa_sa, x * fa_sa + y * fa_ca)
                    for (x, y) in poly_gen]
        elif footprint_angle:
            poly = [(x * fa_ca - y * fa_sa, x * fa_sa + y * fa_ca)
                    for (x, y) in footprint_outline("rectangle", width, height)]
        else:
            poly = None
        # local-frame bounding box of the footprint polygon, so the inter-layer travel
        # gate can measure how far the deposit (balloons + waves) overhangs the bare
        # footprint and grow the crossing test by exactly that.
        pbox = None
        if poly is not None:
            pbox = (min(p[0] for p in poly), min(p[1] for p in poly),
                    max(p[0] for p in poly), max(p[1] for p in poly))
        # geometry is built centred on the origin; offset so the lower-left corner
        # of the footprint (its bounding box) lands on (start_x, start_y)
        ox = start_x + width / 2.0
        oy = start_y + height / 2.0
        return poly_gen, fa_ca, fa_sa, poly, pbox, ox, oy

    base_key = (width, height, footprint_angle, start_x, start_y)
    frame = _layer_frame(*base_key)
    base_frame = frame
    poly_gen, fa_ca, fa_sa, poly, pbox, ox, oy = frame

    # running [xmin, ymin, xmax, ymax] of everything deposited so far (absolute mm,
    # turnaround balloons included), used to route inter-layer travel around the
    # part on a non-rectangular footprint. Only tracked when a polygon footprint is
    # in play, so rectangles are completely unaffected.
    deposited_bbox = None
    # absolute points of everything this scaffold has deposited (arcs sampled),
    # for its perimeter lane. None unless the perimeter option or the caller's
    # `extent_out` needs them, so every other print pays nothing.
    perim_on = bool(perimeter_travel)
    perim_pts = [] if (perim_on or extent_out is not None) else None
    # heading (unit vector) at the previous layer's exit, so a linking loop can
    # match its lead-in to where the head was actually travelling. None until the
    # first layer of THIS scaffold is laid (loops only fire for i > 0).
    prev_exit_dir = None
    # the previous layer's fibre AXIS (its deposition direction, unsigned by
    # travel). A linear pattern makes the wavy exit/entry LEGS a poor cue for the
    # linking loop's headings, so the pattern path below rebuilds them from this
    # axis instead. None until the first layer is laid.
    prev_layer_dir = None
    # the previous layer's stepover, for the linking loop's corner test below
    prev_layer_stepover = None
    # The previous exit in SCAFFOLD-LOCAL coordinates, which is what the entry
    # minimiser keys on. The absolute key is the same distance algebraically but
    # forms (small + ox) - (small + ox), each half rounding FIRST, so a geometric
    # TIE -- two entries equidistant, as on a diagonal layer straddling the
    # corner -- breaks on the low bits of the plate POSITION, and the next change
    # reads 45 deg at one placement and 135 at another (the preview zeroes start,
    # the export places it). An INHERITED cross-scaffold `prev_exit` has no local
    # form, so it is derived by subtracting (ox, oy) and reintroduces that
    # rounding: for that ONE selection the near-equal candidates are clustered and
    # the tie broken on (local y, local x) then enumeration order. A
    # single-scaffold generation enters with prev_exit None and never reaches it.
    if prev_exit is None:
        prev_exit_local = None
        seam_selection = False
    else:
        prev_exit_local = (prev_exit[0] - ox, prev_exit[1] - oy)
        seam_selection = True

    def _emit_link_loop(corner, d_out, d_in, radius, start=None, land=None):
        """Append ONE linking loop at `corner`: the head arrives along `d_out`,
        leaves along `d_in`, and in between rides an outboard circle of `radius`
        that starts and ends at `corner` (so the caller's position is unchanged
        either way). Nothing is appended when `_link_loop_circle` refuses the
        corner. The whole loop runs at the link feedrate. Returns whether a
        loop was appended.

        `start` / `land` BRIDGE a corner whose two fibre ends do not meet: the
        head starts at `start` on the `d_out` line and lands on `land` on the
        `d_in` line, with `corner` where the two lines cross. The circle is the
        one built at `corner`; only its last point becomes `land`. Refused --
        nothing appended -- unless both ends sit within a leg's length L of
        `corner` along their own line: `start` then lies before the first foot
        and `land` after the second, so no leg runs backwards, and a near-
        parallel pair (whose crossing is far off) cannot stretch a leg.

        Both loops a layer change can carry go through here -- the DEPARTURE at
        the previous layer's exit and the ARRIVAL at the new layer's entry -- so
        the two cannot drift into different emission, feed or bbox handling."""
        nonlocal current_f
        loop = _link_loop_points(corner, d_out, d_in, radius, (ox, oy),
                                 arc_segments, g2g3)
        if not loop:
            return False
        hx, hy = corner
        if land is not None:
            leg = math.hypot(loop[0][0] - corner[0], loop[0][1] - corner[1])
            if (abs((start[0] - corner[0]) * d_out[0]
                    + (start[1] - corner[1]) * d_out[1]) > leg
                    or abs((land[0] - corner[0]) * d_in[0]
                           + (land[1] - corner[1]) * d_in[1]) > leg):
                return False
            hx, hy = start
            loop = loop[:-1] + [(land[0], land[1], None)]
        for (lx, ly, larc) in loop:
            if larc is not None:
                lcx, lcy, lccw = larc
                word = (f"{'G3' if lccw else 'G2'} "
                        f"X{fmt(lx)} Y{fmt(ly)} "
                        f"I{fmt(lcx - hx)} J{fmt(lcy - hy)}")
            else:
                word = f"G1 X{fmt(lx)} Y{fmt(ly)}"
            if loop_feed != current_f:
                word += f" F{fmt(loop_feed)}"
                current_f = loop_feed
            lines.append(word)
            if perim_pts is not None:
                if larc is not None:
                    perim_pts.extend(ptravel.arc_samples(
                        (hx, hy), (lx, ly), (larc[0], larc[1]), larc[2]))
                else:
                    perim_pts.append((lx, ly))
            hx, hy = lx, ly
            # fold the outboard loop into the deposited extent so a later
            # layer's travel routes around it too (polygon footprints only;
            # rectangles never consult the bbox).
            if poly is not None and deposited_bbox is not None:
                deposited_bbox[0] = min(deposited_bbox[0], lx)
                deposited_bbox[1] = min(deposited_bbox[1], ly)
                deposited_bbox[2] = max(deposited_bbox[2], lx)
                deposited_bbox[3] = max(deposited_bbox[3], ly)
        return True

    def _layer_extent(pts):
        """A layer's points in absolute mm, its G2/G3 arcs sampled so a lane
        clears the bulge of a turnaround and not only its end points."""
        out = [(pts[0][0] + ox, pts[0][1] + oy)]
        px, py = pts[0][0], pts[0][1]
        for (x, y, _li, arc) in pts[1:]:
            if arc is not None:
                out.extend((sx + ox, sy + oy) for sx, sy in ptravel.arc_samples(
                    (px, py), (x, y), (arc[0], arc[1]), arc[2]))
            else:
                out.append((x + ox, y + oy))
            px, py = x, y
        return out

    def _perimeter_hop(i, entry, pts, layer_stepover):
        """The waypoints (ending at `entry`) that carry the head into layer `i`
        along the perimeter lane, or None to leave the hop as it was: no lane
        route applies here, or none exists. Applies to the program's first
        entry (from `lead_in_from`), to the first entry after another scaffold
        (round `perimeter_obstacles`), and to a layer change whose free ends
        are not one shared corner -- a shared corner is a turn in place, which
        a detour would only turn into a stray tail."""
        if i > 0 and prev_exit is not None:
            gap = math.hypot(entry[0] - prev_exit[0], entry[1] - prev_exit[1])
            corner = max(link_loop_dist, math.hypot(
                prev_layer_stepover or layer_stepover, layer_stepover))
            if gap <= corner:
                return None
            start, others = prev_exit, []
        elif i == 0 and prev_exit is None:
            start, others = lead_in_from, []
        elif i == 0 and perimeter_obstacles is not None:
            start, others = prev_exit, list(perimeter_obstacles)
        else:
            return None
        lane = ptravel.lane_polygon(
            perim_pts + _layer_extent(pts),
            perimeter_travel_offset + 0.5 * fiber_diameter)
        if lane is None:
            return None
        if i == 0 and prev_exit is None and ptravel.inside(lane, start):
            return None               # the head already starts at the part
        lanes = others + [lane]
        way = ptravel.plan(start, entry, lanes, own=len(lanes) - 1)
        if way is None:
            return None
        if not way or way[-1] != entry:
            way = list(way) + [entry]
        return way

    for i in range(layers):
        # This layer's values: the scaffold's, with any override for layer i on
        # top. Re-bound every layer from `base`, so an override never leaks
        # into the next layer; with no overrides each is re-bound to itself.
        lo = layer_overrides.get(i) or {}
        width = lo.get("width", base["width"])
        height = lo.get("height", base["height"])
        footprint_angle = lo.get("footprint_angle", base["footprint_angle"])
        key = (width, height, footprint_angle,
               lo.get("start_x", base["start_x"]),
               lo.get("start_y", base["start_y"]))
        want = base_frame if key == base_key else _layer_frame(*key)
        if want is not frame:
            # the layer moved, resized or turned: re-key the previous exit to
            # this layer's centre, which the entry minimiser measures from
            frame = want
            poly_gen, fa_ca, fa_sa, poly, pbox, ox, oy = frame
            if prev_exit is not None:
                prev_exit_local = (prev_exit[0] - ox, prev_exit[1] - oy)
        speed = lo.get("speed", base["speed"])
        loop_feed = lo.get("loop_speed",
                           base["loop_feed"] if loop_speed is not None
                           else speed)
        arc_segments = lo.get("arc_segments", base["arc_segments"])
        link_loops = base["link_loops"]
        if "link_loops" in lo:
            link_loops = ((link_loops if link_loops in ("auto", "manual")
                           else "auto") if lo["link_loops"] else "off")
        # A per-layer `angle_pattern` (list of absolute fiber directions) takes
        # precedence and cycles when there are more layers than entries; without
        # it, layers step by the fixed `rotation` from `start_angle` as before.
        # `gen_angle` is the fill direction in the UN-rotated frame (used to generate
        # the passes against poly_gen); `angle` adds the rigid footprint rotation and
        # is the true deposited direction (drives layer_dir and the wave frame).
        if angle_pattern:
            gen_angle = angle_pattern[i % len(angle_pattern)]
        else:
            gen_angle = start_angle + i * rotation
        if "angle" in lo:
            gen_angle = lo["angle"]
        # One direction, one print: fold to [0, 360) before any trig. 495 and
        # 135 are the same direction, but cos/sin of the unwrapped radians
        # differ in the last bits, and the entry selection below breaks an
        # exact geometric TIE (two candidates equidistant from the previous
        # exit) on those bits -- so an angle list that keeps counting up, or
        # the default `start_angle + i * rotation` past layer 4, laid a
        # different edge fibre than the wrapped angle. Exact for integer and
        # finite-float angles; [0, 360) angles are untouched.
        gen_angle = float(gen_angle) % 360.0
        angle = (gen_angle + footprint_angle) % 360.0
        # Per-layer stepover / period offset ride alongside the angle pattern
        # (aligned by the same cycling index). A None/short/absent entry -> the
        # scaffold stepover / no offset, so an omitted table is byte-identical. The
        # turnaround-loop radius tracks the layer's own stepover only when
        # arc_radius was the 1.5 x stepover default; an explicit arc_radius holds.
        layer_stepover = stepover
        layer_offset = 0.0
        if angle_pattern:
            k = i % len(angle_pattern)
            if (angle_stepovers and k < len(angle_stepovers)
                    and angle_stepovers[k] is not None):
                layer_stepover = angle_stepovers[k]
            if (angle_offsets and k < len(angle_offsets)
                    and angle_offsets[k] is not None):
                layer_offset = angle_offsets[k]
        if "stepover" in lo:
            layer_stepover = lo["stepover"]
        layer_arc = (1.5 * layer_stepover if arc_radius_is_default
                     else arc_radius)
        if "arc_radius_mult" in lo:
            layer_arc = lo["arc_radius_mult"] * layer_stepover
        # An in-layer grade: the spacing ramps from layer_stepover at the
        # start edge to layer_end at the far edge (`_layer_passes`). Each
        # turnaround is then sized from the gap it joins (`arc_scale`), and
        # every clearance below that is "about one stepover" takes the wider
        # end, so it holds across the whole layer.
        layer_end = lo.get("stepover_end", stepover_end)
        layer_grade = lo.get("grade_angle", grade_angle)
        pass_step = layer_stepover
        arc_scale = None
        if layer_end is not None and abs(layer_end - layer_stepover) > 1e-12:
            if "arc_radius_mult" in lo:
                arc_scale = lo["arc_radius_mult"]
            elif arc_radius_is_default:
                arc_scale = 1.5
            layer_stepover = max(layer_stepover, layer_end)
        else:
            layer_end = None
        # start_z lifts the whole scaffold off the plate datum; layer i still
        # sits one fiber-height above the last, just shifted up by the offset.
        # stack_layers=False drops that per-layer step, holding every layer at
        # start_z (the fibre diameter still sets the overhang clearances below).
        layer_z = lo.get("start_z", start_z)
        z = (layer_z + i * fiber_diameter) if stack_layers else layer_z

        # The linear-pattern shape is authored as absolute (x, y) vertices in a
        # fiber-local frame; the layer's FULL deposition direction rotates that frame
        # rigidly, so layer i deposits the base pattern rotated by `angle`. 0/90/180/270 are thus true
        # 0/90/180/270 rotations -- an asymmetric / looping pattern rotates with the
        # layer instead of just phase-shifting. All passes of the layer share this
        # one direction (so the serpentine never cross-hatches within a layer), and
        # same-angle layers share it too (so 0/90/0/90 stacks). See _wave_pass.
        th = math.radians(angle)
        layer_dir = (math.cos(th), math.sin(th))

        # Travel minimisation: `flip` (which perpendicular side the scan starts
        # from), `vflip` (which edge the two free ends sit on) and a path reversal
        # give four ways to enter the SAME fiber pattern. Enter by whichever is
        # nearest the previous layer's exit, shrinking the G0 hop -- a real
        # dragged fiber in continuous MEW -- from a full footprint edge to roughly
        # the corner offset. All four matter for EVEN pass counts, where both free
        # ends otherwise share one edge. Only the entry/exit and the side each
        # loop balloons change; the deposited grid is identical.
        candidates = []
        for flip in (False, True):
            passes = _layer_passes(gen_angle, width, height, pass_step,
                                   flip=flip, polygon=poly_gen,
                                   phase_offset=layer_offset,
                                   stepover_end=layer_end,
                                   grade_angle=layer_grade)
            if not passes:
                continue
            # Rigid footprint rotation: turn the un-rotated passes about the centre
            # (the origin in this pre-offset frame) so the whole layer -- and thus the
            # whole scaffold -- rotates as one, exactly matching the rotated `poly`
            # used for routing. footprint_angle == 0 leaves the passes untouched.
            if footprint_angle:
                passes = [((ax * fa_ca - ay * fa_sa, ax * fa_sa + ay * fa_ca),
                           (bx * fa_ca - by * fa_sa, bx * fa_sa + by * fa_ca))
                          for ((ax, ay), (bx, by)) in passes]
            for vflip in (False, True):
                pts = _flatten_passes(passes, layer_arc, arc_segments,
                                      layer_stepover, g2g3=g2g3, pattern=pattern,
                                      vflip=vflip, layer_dir=layer_dir,
                                      arc_scale=arc_scale)
                candidates.append(pts)
                candidates.append(_reverse_path(pts))
        if not candidates:
            continue
        _mark(lines, "layer", layer=i)

        if prev_exit_local is not None:
            plx, ply = prev_exit_local
            if seam_selection:
                # cross-scaffold first entry: prev_exit_local was derived by
                # subtracting this scaffold's (ox, oy) from an inherited absolute
                # exit and carries that FP jitter, so cluster the near-minimum
                # candidates and break the tie deterministically.
                keys = [(p[0][0] - plx) ** 2 + (p[0][1] - ply) ** 2
                        for p in candidates]
                kmin = min(keys)
                tol = max(1e-12, 1e-9 * kmin)
                tied = [j for j, k in enumerate(keys) if k - kmin <= tol]
                sel = min(tied, key=lambda j: (candidates[j][0][1],
                                               candidates[j][0][0]))
                pts = candidates[sel]
            else:
                # within-scaffold layer: exact local key, plain first-wins min.
                pts = min(candidates,
                          key=lambda p: (p[0][0] - plx) ** 2
                                        + (p[0][1] - ply) ** 2)
        else:
            pts = candidates[0]
        seam_selection = False

        x0, y0 = pts[0][0], pts[0][1]
        entry = (x0 + ox, y0 + oy)
        # which way this layer's FIRST pass actually runs -- the linking loop's
        # only honest source for the SIGN of the entry heading (`_pass_run_dirs`).
        # Read once, here, from the candidate that was actually chosen.
        first_run, _last_run = _pass_run_dirs(pts)
        # Inter-layer travel: the straight G0 to this layer's entry is itself a
        # dragged fiber, and on a non-rectangular footprint consecutive layers'
        # free ends sit across the part, so it cuts a stray fiber over the
        # scaffold. When it would cross, route AROUND the exclusion zone instead,
        # exactly as the array/exit travel does. Both the zone and the CROSSING
        # TEST use the ACTUAL deposited extent (`deposited_bbox`, balloons and
        # wave overhang included) grown by `overhang`, never the bare footprint --
        # a hop skirting just outside the footprint edge still drags across that
        # fringe. Rectangles keep the single straight hop, byte-identical.
        if poly is not None and deposited_bbox is not None:
            xmin, ymin, xmax, ymax = deposited_bbox
            overhang = max(0.0,
                           (ox + pbox[0]) - xmin, xmax - (ox + pbox[2]),
                           (oy + pbox[1]) - ymin, ymax - (oy + pbox[3]))
            overhang += 0.5 * fiber_diameter          # clear the fibre's own radius
        else:
            overhang = 0.0
        # The own-fibre exemption is a FIBRE-scale reach (~one stepover past each
        # end), NOT the full balloon `overhang` -- otherwise a large balloon swallows
        # the whole hop and a genuine cross-the-part drag goes undetected (capped at
        # `overhang` so it never exceeds the grown-region margin).
        endpoint_clear = min(overhang, max(0.5 * fiber_diameter, layer_stepover))
        perim_way = (_perimeter_hop(i, entry, pts, layer_stepover)
                     if perim_on else None)
        if perim_way is not None:
            # PERIMETER TRAVEL: out to the lane, along it the shorter way round,
            # and in to the entry, where the new layer's Z is set. Like the
            # around-the-part route below it carries no linking loop: it arrives
            # pointing inward, where no outboard circle exists.
            for k, (wx, wy) in enumerate(perim_way):
                word = f"G0 X{fmt(wx)} Y{fmt(wy)}"
                if k == len(perim_way) - 1:
                    word += f" Z{fmt(z)}"
                if k == 0 and current_f is None:
                    current_f = speed       # the program's first motion seeds F
                    word += f" F{fmt(speed)}"
                lines.append(word)
        elif (poly is not None and i > 0 and prev_exit is not None
                and deposited_bbox is not None
                and _hop_crosses_polygon(prev_exit, entry, poly, ox, oy,
                                         margin=overhang,
                                         endpoint_clear=endpoint_clear)):
            margin = layer_stepover                  # clearance past the part
            p_out = gcode_exit.outward_edge_exit(
                prev_exit[0], prev_exit[1], ox, oy, xmin, xmax, ymin, ymax,
                margin=margin)
            e_out = gcode_exit.outward_edge_exit(
                entry[0], entry[1], ox, oy, xmin, xmax, ymin, ymax, margin=margin)
            lines.append(f"G0 X{fmt(p_out[0])} Y{fmt(p_out[1])}")   # Z stays modal
            for wx, wy in array_travel.plan_exit_travel(
                    (p_out[0], p_out[1]), (e_out[0], e_out[1]),
                    [(xmin, ymin, xmax, ymax)], margin):
                lines.append(f"G0 X{fmt(wx)} Y{fmt(wy)}")
            # final inward hop to the entry, where the new layer's Z is set
            lines.append(f"G0 X{fmt(entry[0])} Y{fmt(entry[1])} Z{fmt(z)}")
        else:
            # A LAYER CHANGE HAS TWO CORNERS, and in continuous-flow MEW both are
            # deposited fibre: the DEPARTURE out of the old layer's last fibre
            # into the hop, and the ARRIVAL out of the hop into the new one. Where
            # the free ends coincide the hop has no length and the arrival loop
            # alone rounds the single corner. The headings and the gap the two
            # share are computed once, below, before either is emitted.
            #
            # The around-the-part router above is given NEITHER loop: it arrives
            # pointing INWARD, so the only circle tangent to both rays sits inside
            # the part (over 5 footprints x 5 rotations, 85 of 86 routed arrivals
            # are refused `inboard-bisector` and the 86th is a u-turn), and its
            # departure's first leg is a multi-waypoint route, not one hop.
            link_geom = None
            if (link_loops in ("auto", "manual") and i > 0
                    and prev_exit is not None and prev_exit_dir is not None
                    and len(pts) >= 2):
                gap = math.hypot(entry[0] - prev_exit[0],
                                 entry[1] - prev_exit[1])
                # WHAT COUNTS AS THE SAME CORNER. A grid whose footprint is not
                # a whole number of stepovers leaves a margin of up to one
                # stepover between its last pass and the edge, so a layer ends
                # (15, 14.3) and the next starts (14.3, 15): one corner, 0.99 mm
                # apart on the default 15 mm / 1.7 mm stabilization base, which
                # the 0.5 mm link distance alone refused -- no loops at all on
                # such a footprint. Each end sits within its own layer's
                # stepover of the corner, so the gap is under the hypot of the
                # two; the link distance still governs when it is the larger.
                # A change to the FAR side of the part is a footprint away and
                # stays refused.
                corner = max(link_loop_dist, math.hypot(
                    prev_layer_stepover or layer_stepover, layer_stepover))
                if pattern is not None and prev_layer_dir is not None:
                    # Linear pattern: the wave rides to the tips, so the end LEGS
                    # wobble off-axis and the two tips sit up to wave_perp_max off
                    # the shared corner. Both headings are rebuilt from the fibre
                    # AXES, which have no wobble, and the coincidence gate widened
                    # by that reach. `d_in` is signed by the first pass's RUN --
                    # exactly the direction the head departs into deposition,
                    # because `pin_tips` puts the pass ends on the bare grid
                    # corners -- where the part centre reversed 38 arrival loops
                    # over test 6d's sweep (see `_pass_run_dirs`). Only a
                    # degenerate pass falls back to the centre test.
                    if first_run != (0.0, 0.0):
                        si = 1.0 if (layer_dir[0] * first_run[0]
                                     + layer_dir[1] * first_run[1]) >= 0 else -1.0
                    else:
                        si = 1.0 if (layer_dir[0] * (ox - entry[0])
                                     + layer_dir[1] * (oy - entry[1])) >= 0 \
                            else -1.0
                    d_in = (layer_dir[0] * si, layer_dir[1] * si)
                    # `d_exit` keeps its centre sign, which is known wrong on some
                    # changes. Resist the symmetry with `d_in`: the head ARRIVES
                    # at the exit, and the last pass's net run points the other
                    # way in 425 of 908 layers measured, so signing `d_exit` by
                    # the run put 29 backwards-opening departure loops into the
                    # sweep. See `_pass_run_dirs`.
                    so = 1.0 if (prev_layer_dir[0] * (prev_exit[0] - ox)
                                 + prev_layer_dir[1] * (prev_exit[1] - oy)) >= 0 \
                        else -1.0
                    d_exit = (prev_layer_dir[0] * so, prev_layer_dir[1] * so)
                    coincidence = corner + 2.0 * wave_perp_max
                else:
                    # No pattern: the end legs ARE the fibre directions, so
                    # keep the original cues.
                    d_in = _unit(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
                    d_exit = prev_exit_dir
                    coincidence = corner
                link_geom = (gap, d_in, d_exit, coincidence)

            # --- the DEPARTURE corner, rounded BEFORE the hop is made ----- #
            # Emitted before the G0 that climbs, so the loop lies in the plane of
            # the fibre it continues, and it starts and ends at `prev_exit`, so
            # the hop that follows is unchanged.
            #
            # Drawn exactly when the hop is at least the look-back long -- the
            # same condition that makes `_lead_in_chord` return the hop's own
            # heading, so the two corners really are separate turns. A shorter
            # hop is ONE corner: a second loop within a look-back of the first
            # would sit on top of it, which is also what keeps a coincident
            # corner from growing a duplicate. `layer_arc` is the NEW layer's
            # radius, so one layer change draws one size of loop.
            if (link_geom is not None and link_loop_travel and pattern is None
                    and gap >= link_loop_dist):
                d_travel = _unit(entry[0] - prev_exit[0], entry[1] - prev_exit[1])
                turn_out = math.degrees(math.acos(max(-1.0, min(1.0,
                    d_exit[0] * d_travel[0] + d_exit[1] * d_travel[1]))))
                if link_loops == "manual" or turn_out >= link_loop_angle:
                    _emit_link_loop(prev_exit, d_exit, d_travel, layer_arc)

            hop_at = len(lines)                      # the climb, if a bridge moves it
            hop_xy = f"G0 X{fmt(entry[0])} Y{fmt(entry[1])}"
            word = f"{hop_xy} Z{fmt(z)}"
            if current_f is None:
                # No F emitted yet, so this travel IS the program's first
                # motion (no plate datum -> no Z-only lead-in above it). Seed
                # the modal feed here rather than on the first G1; see the
                # lead-in in generate_multi_scaffold_gcode for why. `speed`,
                # not `loop_feed`: layer 0 never carries a linking loop, so
                # the move this hop runs into is a deposition pass.
                current_f = speed
                word += f" F{fmt(speed)}"
            lines.append(word)

            # --- the ARRIVAL corner, rounded after the hop ---------------- #
            if link_geom is not None:
                gap, d_in, d_exit, coincidence = link_geom
                # `d_exit` is the last DEPOSITED fibre's heading. The chord folds
                # in whatever of the layer-change travel falls inside the
                # look-back and reduces to `d_exit` untouched when there is none,
                # so with the opt-out off every gate below is the pre-travel one.
                # With it on the coincidence gate goes, and the turn angle, the
                # outboard-bisector test and the reach cap are what remain.
                #
                # Not under a linear pattern: this branch feeds `d_exit` into
                # `_lead_in_chord`, and there `d_exit` is still the part-centre
                # guess, so opening the gate builds more loops on a cue already
                # known to put 29 backwards-opening loops into the 6d sweep.
                if link_loop_travel and pattern is None:
                    d_out = _lead_in_chord(entry, prev_exit, d_exit,
                                           link_loop_dist)
                    at_corner = True
                else:
                    d_out = d_exit
                    at_corner = gap <= coincidence
                turn = math.degrees(math.acos(max(-1.0, min(1.0,
                    d_out[0] * d_in[0] + d_out[1] * d_in[1]))))
                if (at_corner
                        and (link_loops == "manual" or turn >= link_loop_angle)):
                    # ENDS THAT DO NOT MEET. On a footprint that is not a whole
                    # number of stepovers the old layer ends up to a stepover
                    # short of the corner the new one starts from (0.7 mm a
                    # side on the 15 mm / 1.7 mm base). A loop drawn at the
                    # entry then sets off along the OLD fibre's heading from
                    # the wrong point: a diagonal hop, then a run parallel to
                    # the pass just laid and 0.7 mm off it. Instead the loop is
                    # built where the two fibre LINES cross, the climb becomes a
                    # pure Z hop at the exit, and the head runs on along the old
                    # fibre, around, and down the new one through the entry.
                    # Ends that meet keep the entry loop, byte-identical; so do
                    # the travel opt-in and a linear pattern, which have their
                    # own lead-in cues.
                    cross = (None if (link_loop_travel or pattern is not None
                                      or gap < 1e-6)
                             else _line_crossing(prev_exit, d_out, entry, d_in))
                    if cross is not None and _emit_link_loop(
                            cross, d_out, d_in, layer_arc,
                            start=prev_exit, land=entry):
                        lines[hop_at] = (f"G0 X{fmt(prev_exit[0])} "
                                         f"Y{fmt(prev_exit[1])}"
                                         + lines[hop_at][len(hop_xy):])
                    else:
                        _emit_link_loop(entry, d_out, d_in, layer_arc)
        px, py = x0, y0                         # previous point, for arc I/J
        n_pass, in_pass = 0, False
        for (x, y, link_in, arc) in pts[1:]:
            # link_in marks the entering segment as a fiber link (turnaround) ->
            # loop speed; a straight deposition pass stays at the main speed
            feed = loop_feed if link_in else speed
            if not link_in and not in_pass:     # a deposition pass begins
                _mark(lines, "pass", **{"pass": n_pass})
                n_pass += 1
            elif link_in and in_pass:           # ...and ends: its turnaround
                _mark(lines, "link")
            in_pass = not link_in
            if arc is not None:
                # one true circular arc: G3 (CCW) / G2 (CW), centre given as the
                # I/J offset from the CURRENT head position (px, py). The offset
                # is frame-independent, so it ignores the ox/oy part offset.
                cx, cy, ccw = arc
                word = (f"{'G3' if ccw else 'G2'} X{fmt(x + ox)} Y{fmt(y + oy)} "
                        f"I{fmt(cx - px)} J{fmt(cy - py)}")
            else:
                word = f"G1 X{fmt(x + ox)} Y{fmt(y + oy)}"
            if feed != current_f:
                word += f" F{fmt(feed)}"
                current_f = feed
            lines.append(word)
            px, py = x, y
        # grow the deposited extent with this layer's points (balloons included),
        # so the next layer's travel routes around everything laid down. Only for a
        # polygon footprint -- rectangles never consult it.
        if poly is not None:
            for (xx, yy, _li, _ar) in pts:
                ax, ay = xx + ox, yy + oy
                if deposited_bbox is None:
                    deposited_bbox = [ax, ay, ax, ay]
                else:
                    deposited_bbox[0] = min(deposited_bbox[0], ax)
                    deposited_bbox[1] = min(deposited_bbox[1], ay)
                    deposited_bbox[2] = max(deposited_bbox[2], ax)
                    deposited_bbox[3] = max(deposited_bbox[3], ay)
        if perim_pts is not None:
            perim_pts.extend(_layer_extent(pts))
        prev_exit = (pts[-1][0] + ox, pts[-1][1] + oy)
        # exact local exit for the NEXT layer's selection key (no ox round-trip),
        # so within-scaffold ties stay position-independent.
        prev_exit_local = (pts[-1][0], pts[-1][1])
        # heading at this layer's exit (its last emitted segment), for a linking
        # loop on the next layer change. Directions are offset-invariant, so the
        # local pts give the absolute heading.
        if len(pts) >= 2:
            prev_exit_dir = _unit(pts[-1][0] - pts[-2][0],
                                  pts[-1][1] - pts[-2][1])
        prev_layer_dir = layer_dir       # fibre axis, for the next linking loop
        prev_layer_stepover = layer_stepover
    if extent_out is not None:
        extent_out.extend(perim_pts)
    return current_f, prev_exit


def emit_scaffold_layers(lines, first_g1, prev_exit, fmt, *, width, height,
                         stepover, fiber_diameter, layers, speed,
                         arc_radius=None, start_x=0.0, start_y=0.0,
                         arc_segments=8, rotation=90.0, start_angle=0.0,
                         start_z=0.0, g2g3=False):
    """Append one scaffold's G0/G1 moves to `lines`; return
    `(first_g1, prev_exit, top_z)`.

    A thin public wrapper over `_emit_one_scaffold` for OTHER generators (e.g.
    the string-art tool) that want to prepend a stabilization scaffold to their
    output as one continuous program, without re-implementing the serpentine
    geometry. The caller owns the G21/G90 header and the one-time F emission (gated by `first_g1`), so the scaffold and whatever
    follows share one continuous fiber. This wrapper runs at a single `speed`
    (no separate loop speed), preserving its `first_g1` in/out contract.

    `top_z` is start_z + layers * fiber_diameter -- the Z one fiber-height above
    the scaffold's top layer, i.e. where the caller should lay whatever prints
    next (the string art). `start_z` lifts the whole scaffold off the plate
    datum (default 0.0). It defaults to a square grid (90 deg rotation, 0 deg
    start angle) so a stabilization base stays a predictable square.

    `g2g3` writes the turnaround loops as true G2/G3 arcs, as the scaffold
    generator's own `g2g3` does. False (the default) is byte-identical."""
    current_f = None if first_g1 else speed
    current_f, prev_exit = _emit_one_scaffold(
        lines, current_f, fmt, width=width, height=height, stepover=stepover,
        rotation=rotation, fiber_diameter=fiber_diameter, layers=layers,
        speed=speed, start_angle=start_angle, arc_radius=arc_radius,
        arc_segments=arc_segments, start_x=start_x, start_y=start_y,
        prev_exit=prev_exit, start_z=start_z, g2g3=g2g3)
    return (current_f is None), prev_exit, start_z + layers * fiber_diameter


def _sag_resolution_argv(args):
    """`--sag-resolution` for a converter launch, or [] at the default 1 so the
    argv a default run sends is unchanged. The converter validates the value."""
    res = float(getattr(args, "sag_resolution", 1.0))
    return [] if res == 1.0 else ["--sag-resolution", repr(res)]


def _nonfinite_numbers(value):
    """Every non-finite float in `value`, a number or a (nested) list/tuple of
    numbers; strings, bools, None and dicts are not numbers here."""
    if isinstance(value, float):
        return [] if math.isfinite(value) else [value]
    if isinstance(value, (list, tuple)):
        return [x for v in value for x in _nonfinite_numbers(v)]
    return []


#: The most moves one program may hold. A real MEW scaffold is well under a
#: million (a 30 mm square at 0.5 mm, 100 layers, is 90 000; a 100 mm one at
#: 0.1 mm, 100 layers, about 1.7 million); this ceiling sits several times
#: above that and still writes in a minute or two (the generator does about
#: 100 000 moves a second). What lands above it is a slip of the finger -- a stepover of 0.0001
#: for 0.1, 100000 layers for 100 -- and generating it anyway runs for an hour,
#: fills the disk with gigabytes of text and reads to the user as a hang.
MAX_PROGRAM_MOVES = 10_000_000


def _estimated_moves(p):
    """``(total, from_pattern)``: an UPPER estimate of the path steps one
    scaffold dict costs to generate, from its numbers alone, before any of it
    is generated, and how many of them its linear pattern adds.

    Passes per layer are the footprint diagonal over the finest stepover any
    layer uses; each pass is one straight move plus its turnaround's chords
    (`arc_segments` per HALF-turn, so twice that a loop) plus, with a linear
    pattern, its vertices once per repeat. A G2/G3 turnaround is ONE line but
    is costed as its chords all the same: the generator still works out the
    loop, and a 0.0001 mm stepover with arcs ran as long as one without."""
    w, h = float(p["width"]), float(p["height"])
    diag = math.hypot(w, h)
    steps = [float(p["stepover"])]
    steps += [float(x) for x in (p.get("angle_stepovers") or ())
              if x is not None]
    for lo in (p.get("layer_overrides") or {}).values():
        if "stepover" in lo:
            steps.append(float(lo["stepover"]))
    steps = [s for s in steps if s > 0]
    if not steps:                       # refused by its own validation
        return 0, 0
    passes = int(p["layers"]) * (diag / min(steps) + 1)
    per_pass = 1 + 2 * max(1, int(p.get("arc_segments", 8)))
    pattern = 0.0
    lp = p.get("linear_pattern")
    if lp:
        period = float(p.get("linear_wavelength", 0.0)) + float(
            p.get("linear_spacing", 0.0))
        if period > 0:
            pattern = len(lp) * (diag / period + 1)
    return passes * (per_pass + pattern), passes * pattern


def _check_program_size(scaffolds, stabilization=None):
    """Refuse a program whose estimated size is past `MAX_PROGRAM_MOVES`,
    naming the numbers that made it so, before a line of it is written."""
    parts = [("scaffold %d" % (i + 1), sc) for i, sc in enumerate(scaffolds)]
    if stabilization is not None:
        parts.append(("the stabilization base", dict(
            stabilization, arc_segments=scaffolds[0].get("arc_segments", 8))))
    sizes = [(_estimated_moves(sc), who, sc) for who, sc in parts]
    total = sum(n for (n, _pat), _who, _sc in sizes)
    if total <= MAX_PROGRAM_MOVES:
        return
    (n, pat), who, sc = max(sizes, key=lambda t: t[0][0])
    if pat > 0.5 * n:
        cause = ("a linear pattern repeating every %g mm"
                 % (float(sc.get("linear_wavelength", 0.0))
                    + float(sc.get("linear_spacing", 0.0))),
                 "check the linear pattern's wavelength")
    else:
        cause = ("%g x %g mm at a %g mm stepover, %d layers"
                 % (sc["width"], sc["height"], sc["stepover"],
                    int(sc["layers"])),
                 "check the stepover and the layer count")
    raise ValueError(
        "this design would be about %s million path steps (%s, %s), far past "
        "the %d million a scaffold program can sensibly hold -- %s"
        % (format(round(total / 1e6), ","), who, cause[0],
           MAX_PROGRAM_MOVES // 1_000_000, cause[1]))


def _validate_scaffold_params(p, label):
    """Raise ValueError if a scaffold parameter dict is geometrically invalid.
    Shared by the single- and multi-scaffold entry points so every scaffold --
    main, stabilization, or one of several stacked -- is checked the same way."""
    # any field: a NaN/inf start or angle is otherwise written as `Xnan`
    for key, val in p.items():
        if _nonfinite_numbers(val):
            raise ValueError(f"{label} {key} must be a finite number, "
                             f"got {val!r}")
    if (not math.isfinite(p["width"]) or not math.isfinite(p["height"])):
        raise ValueError(f"{label} width and height must be finite numbers")
    if min(p["width"], p["height"]) <= 0:
        raise ValueError(f"{label} width and height must be positive")
    if not math.isfinite(p["stepover"]):
        raise ValueError(f"{label} stepover must be a finite number")
    if p["stepover"] <= 0:
        raise ValueError(f"{label} stepover must be positive")
    if p.get("stepover_end") is not None and p["stepover_end"] <= 0:
        raise ValueError(f"{label} stepover at the far edge must be positive")
    if not math.isfinite(p["fiber_diameter"]):
        raise ValueError(f"{label} fiber-diameter must be a finite number")
    if p["fiber_diameter"] <= 0:
        raise ValueError(f"{label} fiber-diameter must be positive")
    if p["layers"] < 1:
        raise ValueError(f"{label} layers must be at least 1")
    if not math.isfinite(p["speed"]):
        raise ValueError(f"{label} speed must be a finite number")
    if p["speed"] <= 0:
        raise ValueError(f"{label} speed must be positive")
    if p.get("loop_speed") is not None and not math.isfinite(p["loop_speed"]):
        raise ValueError(f"{label} loop_speed must be a finite number")
    if p.get("loop_speed") is not None and p["loop_speed"] <= 0:
        raise ValueError(f"{label} loop_speed must be positive")
    # A negative arc radius flips every turnaround inside out and a chord
    # count under 2 makes it a spike; both were written without complaint.
    if p.get("arc_radius") is not None and p["arc_radius"] < 0:
        raise ValueError(f"{label} turnaround radius must not be negative")
    if not p.get("g2g3") and int(p.get("arc_segments", 8)) < 2:
        raise ValueError(f"{label} arc segments must be at least 2")
    ap = p.get("angle_pattern")
    if ap is not None:
        if not isinstance(ap, (list, tuple)) or not ap:
            raise ValueError(
                f"{label} angle_pattern must be a non-empty list of numbers")
        if any(not isinstance(a, (int, float)) for a in ap):
            raise ValueError(f"{label} angle_pattern entries must be numbers")
    # per-layer stepover / period offset lists (aligned to angle_pattern; each
    # entry a number or None = "use the scaffold default for that layer").
    for key, what in (("angle_stepovers", "per-layer stepover"),
                      ("angle_offsets", "per-layer period offset")):
        vals = p.get(key)
        if vals is None:
            continue
        if not isinstance(vals, (list, tuple)):
            raise ValueError(f"{label} {key} must be a list of numbers/None")
        for x in vals:
            if x is None:
                continue
            if not isinstance(x, (int, float)) or not math.isfinite(x):
                raise ValueError(f"{label} {what} entries must be numbers")
            if key == "angle_stepovers" and x <= 0:
                raise ValueError(f"{label} {what} must be positive")
    lp = p.get("linear_pattern")
    if lp is not None:
        if not isinstance(lp, (list, tuple)) or not lp:
            raise ValueError(
                f"{label} linear_pattern must be a non-empty list of "
                f"[x, y] vertices")
        for pt in lp:
            if (not isinstance(pt, (list, tuple)) or len(pt) != 2
                    or any(not isinstance(c, (int, float))
                           or not math.isfinite(c) for c in pt)):
                raise ValueError(
                    f"{label} linear_pattern entries must be [x, y] "
                    f"number pairs")
        wl = p.get("linear_wavelength")
        if not (isinstance(wl, (int, float)) and wl > 0):
            raise ValueError(
                f"{label} linear_wavelength must be a positive number when "
                f"linear_pattern is set")
        sp = p.get("linear_spacing", 0.0)
        if not (isinstance(sp, (int, float)) and sp >= 0):
            raise ValueError(
                f"{label} linear_spacing must be a non-negative number")
    shape = p.get("shape", "rectangle")
    if shape not in _SHAPES:
        raise ValueError(
            f"{label} shape must be one of {', '.join(_SHAPES)}")
    if shape in ("circle", "ellipse", "polygon"):
        sides = p.get("shape_sides", 64)
        if not (isinstance(sides, (int, float)) and int(sides) >= 3):
            raise ValueError(
                f"{label} shape_sides must be an integer >= 3 for a "
                f"{shape} footprint")
    if shape == "custom":
        poly = p.get("footprint_polygon")
        if not isinstance(poly, (list, tuple)) or len(poly) < 3:
            raise ValueError(
                f"{label} custom shape needs at least 3 [x, y] vertices")
        for pt in poly:
            if (not isinstance(pt, (list, tuple)) or len(pt) != 2
                    or any(not isinstance(c, (int, float))
                           or not math.isfinite(c) for c in pt)):
                raise ValueError(
                    f"{label} custom shape vertices must be [x, y] number pairs")
        # gate the deferred concave work: only convex outlines clip to one span
        if not _polygon_is_convex(_normalize_polygon(poly)):
            raise ValueError(
                f"{label} custom shape must be convex for now (concave outlines "
                f"are not supported yet)")
    ll = p.get("link_loops", "off")
    if ll not in ("off", "auto", "manual"):
        raise ValueError(
            f"{label} link_loops must be 'off', 'auto', or 'manual'")
    lt = p.get("link_loop_travel", False)
    if not isinstance(lt, bool):
        raise ValueError(f"{label} link_loop_travel must be true or false")
    pt = p.get("perimeter_travel", False)
    if not isinstance(pt, bool):
        raise ValueError(f"{label} perimeter_travel must be true or false")
    po = p.get("perimeter_travel_offset", 1.0)
    if not (isinstance(po, (int, float)) and not isinstance(po, bool)
            and math.isfinite(po) and po >= 0):
        raise ValueError(
            f"{label} perimeter_travel_offset must be a non-negative number")
    if ll != "off":
        for key in ("link_loop_angle", "link_loop_dist"):
            v = p.get(key)
            if v is not None and not (isinstance(v, (int, float))
                                      and math.isfinite(v) and v >= 0):
                raise ValueError(
                    f"{label} {key} must be a non-negative number")


def _laid_lane(sc, extent, travel_offset, box):
    """The lane polygon a scaffold that is laid becomes for the travel that
    follows: its perimeter lane when it asked for one (`extent` is every point
    it deposited), else its box (x0, x1, y0, y1) grown by the array travel
    offset, which is how the older router sees it."""
    if sc.get("perimeter_travel", False) and extent:
        lane = ptravel.lane_polygon(
            extent, float(sc.get("perimeter_travel_offset", 1.0))
            + 0.5 * sc["fiber_diameter"])
        if lane is not None:
            return lane
    x0, x1, y0, y1 = box
    return ptravel.box_polygon((x0, y0, x1, y1), max(0.0, travel_offset))


def generate_multi_scaffold_gcode(scaffolds, *, precision=4, stabilization=None,
                                  plate_extent=None,
                                  exit_margin=gcode_exit.DEFAULT_MARGIN,
                                  travel_offset=0.0, lead_in_from=(0.0, 0.0)):
    """Return G-code for one OR MORE scaffolds laid down as a single continuous
    program (one G21/G90 header, one end-of-print exit move, no M30 footer).

    A scaffold with `perimeter_travel` set (and `perimeter_travel_offset` mm,
    default 1) has every automatic travel into and out of it routed along its
    perimeter lane instead of round its bounding box: the lead-in (from
    `lead_in_from`, the head's start in this program's frame), the layer
    changes, the hop from the scaffold before, and -- when it is the last
    scaffold -- the end-of-print exit. See `perimeter_travel.py`.

    `scaffolds` is a non-empty list of parameter dicts, each carrying the keyword
    arguments `_emit_one_scaffold` takes. They are emitted in list order,
    threading `prev_exit` across the whole print so each scaffold's first layer
    enters nearest where the previous one finished.

    Vertical stacking is the caller's job: give scaffold k a start_z equal to the
    previous scaffold's top (start_z + layers * fiber_diameter). Nothing here
    forces it -- a scaffold with its own start_x/start_y and start_z=0 prints
    side by side.

    `travel_offset` (mm, default 0 = off) runs the nozzle that far clear of the
    finished scaffold's footprint, out the nearest edge it faces, before heading
    to the next scaffold's start: in continuous-flow MEW that hop is a dragged
    fiber and a straight one tracks back ACROSS the scaffold just finished.
    Applied only BETWEEN scaffolds; the stabilization -> first-scaffold join
    stays one continuous fiber.

    `stabilization` is printed first and inherits the FIRST scaffold's speed, arc
    segments and start_z. `plate_extent` is the build-plate footprint
    (x0, x1, y0, y1) the end-of-print exit clears, a circular plate using its
    bounding square; None skips the exit move."""
    if not scaffolds:
        raise ValueError("need at least one scaffold")
    # every scaffold is checked BEFORE any is written, so a bad scaffold 3
    # costs no time on scaffolds 1 and 2, and the size check reads valid numbers
    for i, sc in enumerate(scaffolds):
        _validate_scaffold_params(sc, f"scaffold {i + 1}")
    _check_program_size(scaffolds, stabilization)

    fmt = lambda v: format_coord(v, precision)  # noqa: E731
    lines = ["G21", "G90"]
    current_f = None        # feedrate in effect; None until the first F is emitted
    prev_exit = None        # last (x,y) laid down, threaded for travel minimising

    # Lead-in over the build plate: the first scaffold move is a G0 from machine
    # home up to the first point at print height, and rising diagonally from z=0
    # would cut THROUGH the collector slab. Lift to print height first, with a
    # Z-only G0 at the origin. first_z == 0 emits nothing, so a no-plate file is
    # byte-identical.
    first_z = scaffolds[0].get("start_z", 0.0)
    if first_z > 0.0:
        # The lead-in carries the print FEED because F is modal: a G0 before any
        # F word runs at the consumer's assumed DEFAULT_FEED (600 mm/min in
        # gcode_to_fibers, gcode_player.parse_path and estimate_print_time
        # alike), and a continuous-flow MEW head never rapids -- it holds ONE
        # feed for the whole print. Leaving it off stepped the jet's lag and
        # fibre diameter in one frame at the travel -> print transition.
        current_f = scaffolds[0]["speed"]
        lines.append(f"G0 Z{fmt(first_z)} F{fmt(current_f)}")

    # Optional stabilization scaffold, printed FIRST and joined to the rest as
    # one continuous program (same file, single G21, F emitted just once). It
    # inherits the FIRST scaffold's print speed, arc segments, start_z and
    # interlayer linking loops (mode, angle, distance, travel), but is ALWAYS
    # laid down as a square grid -- 90 deg layer rotation, 0 deg start angle --
    # regardless of the main pattern, so the base stays a predictable square.
    # Its footprint, position, stepover, fiber diameter, layers and arc radius
    # are its own.
    if stabilization is not None:
        if min(stabilization["width"], stabilization["height"]) <= 0:
            raise ValueError("stabilization width and height must be positive")
        if stabilization["stepover"] <= 0:
            raise ValueError("stabilization stepover must be positive")
        if stabilization["fiber_diameter"] <= 0:
            raise ValueError("stabilization fiber-diameter must be positive")
        if stabilization["layers"] < 1:
            raise ValueError("stabilization layers must be at least 1")
        _mark(lines, "group", group=0)
        current_f, prev_exit = _emit_one_scaffold(
            lines, current_f, fmt, speed=scaffolds[0]["speed"], rotation=90.0,
            start_angle=0.0, arc_segments=scaffolds[0].get("arc_segments", 8),
            prev_exit=prev_exit, start_z=scaffolds[0].get("start_z", 0.0),
            g2g3=scaffolds[0].get("g2g3", False),
            stack_layers=scaffolds[0].get("stack_layers", True),
            link_loops=scaffolds[0].get("link_loops", "off"),
            link_loop_angle=scaffolds[0].get("link_loop_angle", 25.0),
            link_loop_dist=scaffolds[0].get("link_loop_dist", 0.5),
            link_loop_travel=scaffolds[0].get("link_loop_travel", False),
            perimeter_travel=scaffolds[0].get("perimeter_travel", False),
            perimeter_travel_offset=scaffolds[0].get(
                "perimeter_travel_offset", 1.0),
            lead_in_from=lead_in_from,
            **stabilization)

    # Footprints (x0, y0, x1, y1) of every scaffold, so transitions AND the
    # end-of-print exit can route AROUND the exclusion zones instead of cutting
    # back across them. Built once up front so the router sees the whole layout.
    boxes = [(sc["start_x"], sc["start_y"],
              sc["start_x"] + sc["width"], sc["start_y"] + sc["height"])
             for sc in scaffolds]
    # inter-scaffold routing only kicks in with a travel offset (without one the
    # hops stay direct, as before); the exit always routes around `boxes`.
    footprints = boxes if travel_offset > 0 else None

    # footprint of the scaffold just laid down, so the next transition can run the
    # nozzle clear of IT before traveling on. None until the first scaffold is
    # emitted -- the stabilization -> scaffold 1 join is left continuous.
    prev_box = None
    # the lane of every scaffold laid so far (perimeter travel's obstacles): its
    # own perimeter lane when it asked for one, else its box grown by the array
    # travel offset, which is what the older router treats it as
    laid_lanes = []
    for i, sc in enumerate(scaffolds):
        _validate_scaffold_params(sc, f"scaffold {i + 1}")
        # the travel into a scaffold belongs to it (its entry), so the mark
        # goes before the offset run and route below
        _mark(lines, "group", group=i + (stabilization is not None))
        sc_perim = bool(sc.get("perimeter_travel", False))
        sc_extent = [] if sc_perim else None
        # PERIMETER TRAVEL into a scaffold that follows another: the whole route
        # (out to the lane of the one it leaves, round every lane laid so far,
        # in to its entry) is run by `_emit_one_scaffold`, because the lane of
        # THIS scaffold is known only once its first layer is. The older
        # box-round-the-array hop below is skipped for it.
        routed = sc_perim and prev_box is not None and prev_exit is not None
        if (not routed and travel_offset > 0 and prev_exit is not None
                and prev_box is not None):
            # 1) Run the nozzle the travel offset clear of the scaffold just
            #    finished, straight out the nearest edge it faces (a clean
            #    perpendicular exit that never tracks back across it).
            xmin, xmax, ymin, ymax = prev_box
            ex, ey, _edge = gcode_exit.outward_edge_exit(
                prev_exit[0], prev_exit[1],
                (xmin + xmax) / 2.0, (ymin + ymax) / 2.0,
                xmin, xmax, ymin, ymax, margin=travel_offset)
            lines.append(f"G0 X{fmt(ex)} Y{fmt(ey)}")    # Z stays modal
            prev_exit = (ex, ey)
            # 2) Route from that boundary point to the next scaffold through the
            #    open lanes between exclusion zones, re-entering only at the end.
            for wx, wy in array_travel.plan_array_travel(
                    prev_exit, footprints[i], footprints, travel_offset):
                lines.append(f"G0 X{fmt(wx)} Y{fmt(wy)}")
                prev_exit = (wx, wy)
        current_f, prev_exit = _emit_one_scaffold(
            lines, current_f, fmt, prev_exit=prev_exit, extent_out=sc_extent,
            perimeter_obstacles=list(laid_lanes) if routed else None,
            lead_in_from=lead_in_from, **sc)
        prev_box = (sc["start_x"], sc["start_x"] + sc["width"],
                    sc["start_y"], sc["start_y"] + sc["height"])
        laid_lanes.append(_laid_lane(sc, sc_extent, travel_offset, prev_box))

    # --- end-of-print exit -------------------------------------------------- #
    # Park the nozzle off the part so the polymer that keeps flowing after the
    # last move trails off the print instead of collecting on top of it.
    # plate_exit_point heads outward off the part (away from the true FOOTPRINT
    # centre -- not the move bbox, which the origin lead-in would skew) toward the
    # nearest PLATE edge that still has room, so the exit can never reverse back
    # across the print. The route to it goes AROUND every scaffold footprint, so the
    # dragged exit fibre never crosses a finished scaffold. Z stays modal (top print
    # height).
    if plate_extent is not None and prev_exit is not None and boxes:
        _mark(lines, "exit")
        pxmin = min(b[0] for b in boxes)
        pymin = min(b[1] for b in boxes)
        pxmax = max(b[2] for b in boxes)
        pymax = max(b[3] for b in boxes)
        goal = gcode_exit.plate_exit_point(
            prev_exit[0], prev_exit[1], (pxmin, pxmax, pymin, pymax),
            plate_extent, bounds=None, margin=exit_margin)
        start = prev_exit
        last = scaffolds[-1]
        if last.get("perimeter_travel", False) and laid_lanes:
            # PERIMETER TRAVEL out: from the last scaffold's lane, round every
            # lane, to the exit point. No route -> the older exit below.
            way = ptravel.plan(start, goal, laid_lanes)
            if way is not None:
                for wx, wy in way:
                    lines.append(f"G0 X{fmt(wx)} Y{fmt(wy)}")
                return "\n".join(lines) + "\n"
        # With a travel offset the exclusion zones are grown, so step the nozzle
        # clear of the last scaffold first -- otherwise the router's start would
        # sit inside an expanded zone and it couldn't plan a route.
        if travel_offset > 0 and prev_box is not None:
            xmin, xmax, ymin, ymax = prev_box
            bx, by, _edge = gcode_exit.outward_edge_exit(
                prev_exit[0], prev_exit[1], (xmin + xmax) / 2.0,
                (ymin + ymax) / 2.0, xmin, xmax, ymin, ymax, margin=travel_offset)
            lines.append(f"G0 X{fmt(bx)} Y{fmt(by)}")
            start = (bx, by)
        # route around the footprints (grown by travel_offset; 0 -> a direct hop
        # to the goal, which plate_exit_point already chose to clear the part)
        for wx, wy in array_travel.plan_exit_travel(
                start, goal, boxes, max(0.0, travel_offset)):
            lines.append(f"G0 X{fmt(wx)} Y{fmt(wy)}")

    # No M30 footer. Klipper wires M28/M29/M30 to one error handler, so M30
    # RAISES at the end of a MEWron print: motion has finished, but the error
    # trips on_error_gcode and the job reports `cancelled` instead of `complete`,
    # which the Machine-link page and the live reveal both read. The PARSER still
    # skips M30, so files that end in one load.
    return "\n".join(lines) + "\n"


def generate_scaffold_gcode(width, height, stepover, rotation, fiber_diameter,
                            layers, speed, *, start_angle=0.0, arc_radius=None,
                            arc_segments=8, start_x=0.0, start_y=0.0,
                            precision=4, stabilization=None, loop_speed=None,
                            start_z=0.0, plate_extent=None,
                            exit_margin=gcode_exit.DEFAULT_MARGIN, g2g3=False,
                            angle_pattern=None, angle_stepovers=None,
                            angle_offsets=None, linear_pattern=None,
                            linear_wavelength=0.0, linear_spacing=0.0,
                            shape="rectangle", shape_sides=64,
                            footprint_polygon=None, footprint_angle=0.0,
                            link_loops="off",
                            link_loop_angle=25.0, link_loop_dist=0.5,
                            link_loop_travel=False,
                            stack_layers=True, perimeter_travel=False,
                            perimeter_travel_offset=1.0,
                            lead_in_from=(0.0, 0.0),
                            stepover_end=None, grade_angle=0.0):
    """Return MEW scaffold G-code (str) for a SINGLE scaffold -- a thin wrapper
    over `generate_multi_scaffold_gcode` with a one-scaffold list.

    Layer height == fiber_diameter and layer i runs at start_angle + i * rotation
    degrees, unless `angle_pattern` (absolute per-layer angles) overrides both and
    cycles. stack_layers=False instead holds every layer, and the stabilization
    base that inherits the setting, at its start Z.

    arc_radius: radius of the ballooning turnaround loops (default 1.5 * stepover,
      wider than the pass pitch); 0 gives straight connectors.
    loop_speed: feedrate (mm/min) for those links only; None inherits `speed`,
      which the stabilization scaffold always runs at anyway.
    g2g3: emit each turnaround as one true G2/G3 arc rather than a fan of G1
      chords (arc_segments then irrelevant) -- same circle, other serialization.
    start_x, start_y: the footprint's lower-left corner. start_z: first-layer Z,
      to which the caller adds the plate top. plate_extent: the build-plate
      footprint (x0, x1, y0, y1) enabling the end-of-print exit, None skipping it.
    shape: 'rectangle' (default, byte-identical), 'circle'/'polygon' (diameter =
      width), 'ellipse' (axes width x height) or 'custom' (footprint_polygon),
      CONVEX only; shape_sides is the smoothness / side count, and width x height
      stays the bounding box the travel routing uses.
    link_loops: 'off' (default), 'auto' (gated on link_loop_angle deg) or
      'manual'; link_loop_dist (mm) is the lead-in look-back, link_loop_travel
      also loops a change reached along a travel fibre, and the radius is
      arc_radius. See `_emit_one_scaffold`.
    perimeter_travel: off by default (byte-identical). On, every automatic
      travel runs along the scaffold's perimeter `perimeter_travel_offset` mm
      clear of its outermost fibre, not round its bounding box. See
      `perimeter_travel.py`.
    stepover_end: grades the pore size WITHIN each layer. The fiber spacing
      runs from `stepover` at the footprint's start edge to `stepover_end` at
      its far edge, along `grade_angle` degrees (0 = left to right along X,
      90 = front to back along Y, turning with footprint_angle). Each fiber is
      spaced for the pore where it lies, so a layer whose fibers run along the
      grade is spaced evenly at the midway value. None (default) is even
      spacing, byte-identical. See `_layer_passes`."""
    scaffold = dict(
        width=width, height=height, stepover=stepover, rotation=rotation,
        fiber_diameter=fiber_diameter, layers=layers, speed=speed,
        start_angle=start_angle, arc_radius=arc_radius, arc_segments=arc_segments,
        start_x=start_x, start_y=start_y, loop_speed=loop_speed, start_z=start_z,
        stack_layers=stack_layers,
        g2g3=g2g3, angle_pattern=angle_pattern, angle_stepovers=angle_stepovers,
        angle_offsets=angle_offsets, linear_pattern=linear_pattern,
        linear_wavelength=linear_wavelength, linear_spacing=linear_spacing,
        shape=shape, shape_sides=shape_sides, footprint_polygon=footprint_polygon,
        footprint_angle=footprint_angle,
        link_loops=link_loops, link_loop_angle=link_loop_angle,
        link_loop_dist=link_loop_dist, link_loop_travel=link_loop_travel,
        perimeter_travel=perimeter_travel,
        perimeter_travel_offset=perimeter_travel_offset,
        stepover_end=stepover_end, grade_angle=grade_angle)
    return generate_multi_scaffold_gcode(
        [scaffold], precision=precision, stabilization=stabilization,
        plate_extent=plate_extent, exit_margin=exit_margin,
        lead_in_from=lead_in_from)


#: The keys `_scaffold_from_spec` reads with `spec[...]`; the rest are optional.
_SPEC_REQUIRED = ("width", "height", "stepover", "rotation", "fiber_diameter",
                  "layers", "speed")


#: what a layer override may replace, and how its value is read. "angle" is
#: the layer's fill direction (the angle `angle_pattern` lists, before the
#: footprint turn); "link_loops" switches the corner loops on or off for the
#: change INTO that layer. Fiber diameter and the layer count stay per scaffold.
LAYER_OVERRIDE_KEYS = {
    "width": float, "height": float, "stepover": float, "speed": float,
    "loop_speed": float, "start_x": float, "start_y": float,
    "start_z": float, "angle": float, "footprint_angle": float,
    "arc_radius_mult": float, "arc_segments": int, "link_loops": bool,
    "stepover_end": float, "grade_angle": float,
}
_LAYER_OVERRIDE_POSITIVE = ("width", "height", "stepover", "speed",
                            "loop_speed", "stepover_end")


def parse_layer_overrides(raw, layers=None):
    """A spec's ``layer_overrides`` -- ``{"<layer, 1-based>": {key: value}}``,
    as JSON text or a dict -- as ``{layer index (0-based): {key: value}}``.
    Blank values and unknown keys are dropped; a bad value raises ValueError
    naming the layer and key. Empty or None -> {}."""
    if not raw:
        return {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    out = {}
    for lk, vals in raw.items():
        li = int(lk) - 1
        if li < 0 or (layers is not None and li >= layers):
            continue
        row = {}
        for k, v in (vals or {}).items():
            cast = LAYER_OVERRIDE_KEYS.get(k)
            if cast is None or v is None or (isinstance(v, str)
                                             and not v.strip()):
                continue
            try:
                if cast is bool:
                    val = (v if isinstance(v, bool)
                           else str(v).strip().lower() in ("1", "true", "on"))
                elif cast is int:
                    val = int(float(v))
                else:
                    val = float(str(v).split()[0])
            except (TypeError, ValueError):
                raise ValueError(f"layer {li + 1} {k}: {v!r} is not a number")
            if cast is float and not math.isfinite(val):
                raise ValueError(f"layer {li + 1} {k}: must be finite")
            if k in _LAYER_OVERRIDE_POSITIVE and val <= 0:
                raise ValueError(f"layer {li + 1} {k}: must be positive")
            if k == "arc_radius_mult" and val < 0:
                raise ValueError(f"layer {li + 1} {k}: must not be negative")
            if k == "arc_segments" and val < 2:
                raise ValueError(f"layer {li + 1} {k}: must be at least 2")
            row[k] = val
        if row:
            out[li] = row
    return out


def _scaffold_from_spec(spec):
    """Turn one JSON scaffold spec (GUI / external schema) into the keyword dict
    `_emit_one_scaffold` expects. `arc_radius_mult` is resolved against this
    scaffold's own stepover (absent -> None -> the 1.5 * stepover default); every
    other field passes through, with sensible fallbacks for the optional ones."""
    stepover = spec["stepover"]
    arc_radius = (spec["arc_radius_mult"] * stepover
                  if spec.get("arc_radius_mult") is not None else None)
    return dict(
        width=spec["width"], height=spec["height"], stepover=stepover,
        rotation=spec["rotation"], fiber_diameter=spec["fiber_diameter"],
        layers=spec["layers"], speed=spec["speed"],
        start_angle=spec.get("start_angle", 0.0), arc_radius=arc_radius,
        arc_segments=spec.get("arc_segments", 8),
        start_x=spec.get("start_x", 0.0), start_y=spec.get("start_y", 0.0),
        start_z=spec.get("start_z", 0.0), loop_speed=spec.get("loop_speed"),
        stack_layers=spec.get("stack_layers", True),
        g2g3=spec.get("g2g3", False), angle_pattern=spec.get("angle_pattern"),
        angle_stepovers=spec.get("angle_stepovers"),
        angle_offsets=spec.get("angle_offsets"),
        linear_pattern=spec.get("linear_pattern"),
        linear_wavelength=spec.get("linear_wavelength", 0.0),
        linear_spacing=spec.get("linear_spacing", 0.0),
        shape=spec.get("shape", "rectangle"),
        shape_sides=spec.get("shape_sides", 64),
        footprint_polygon=spec.get("footprint_polygon"),
        footprint_angle=spec.get("footprint_angle", 0.0),
        link_loops=spec.get("link_loops", "off"),
        link_loop_angle=spec.get("link_loop_angle", 25.0),
        link_loop_dist=spec.get("link_loop_dist", 0.5),
        link_loop_travel=bool(spec.get("link_loop_travel", False)),
        perimeter_travel=bool(spec.get("perimeter_travel", False)),
        perimeter_travel_offset=spec.get("perimeter_travel_offset", 1.0),
        stepover_end=spec.get("stepover_end"),
        grade_angle=spec.get("grade_angle", 0.0),
        layer_overrides=parse_layer_overrides(spec.get("layer_overrides"),
                                              spec["layers"]))


# --------------------------------------------------------------------------- #
# Mandrel mode -- tubes on a rotating collector
# --------------------------------------------------------------------------- #
# A SIBLING of the planar emitter, not a mode inside it: nothing above this line
# is touched by mandrel mode, which is what makes "mandrel off is byte-identical"
# a property of the code rather than of a test. The planar machinery (footprint
# clipping, ballooning turnarounds, linking loops, travel routing) has no
# counterpart on a tube. The path itself is McColl Eq 6.1, ported -- see
# `_zigzag_layer`. Two hard rules this honours: no `;` comments in exported
# g-code, and the F word is the RESULTANT surface speed (McColl Eq 1.3), which is
# what `gcode_to_fibers.parse_gcode` under a rotary frame assumes we emit.

#: Pattern families. Only the zigzag (McColl's Strategy 3.1) is built, and its rotary
#: coordinate GROWS WITHOUT BOUND across a tall print --
#: McColl's own caveat that it may exceed the CNC interpolator's fixed-point
#: range. "infinity" (Strategy 3.2, which keeps A in range) is what fixes that
#: and is NOT built; it is named so an unknown strategy is rejected rather than
#: silently printed as a zigzag.
MANDREL_STRATEGIES = ("zigzag",)

#: How the feed is held across layers as the wall grows. "surface" holds the
#: deposition-surface speed constant, so the fibre is drawn at the same rate on
#: every layer; "omega" holds the
#: MANDREL's angular rate constant instead, so the surface speed drifts upward
#: by (R0 + h)/R0 as the wall builds. "omega" is the opt-in that makes that
#: drift predictable and showable rather than accidental.
MANDREL_SPEED_MODES = ("surface", "omega")

#: How the controller is told the rate of a move that turns the mandrel.
#: "combined" is the Klipper convention every mandrel file has used: ONE F word,
#: the resultant surface speed. "split" is Aerotech's: F is the rate of the
#: DOMINANT (linear) axes and E the rate of the DEPENDENT (rotary) ones, in
#: degrees. Aerotech's Automation1 reference ("G-Code Commands"): a move mixing
#: the two runs at F alone, and E governs only a move with no linear axis in it.
#: Klipper would read an E word as an EXTRUDER move, so "split" is opt-in.
MANDREL_ROTARY_FEEDS = ("combined", "split")

#: The time unit of the split F and E words, "min" or "s". Emitted as the
#: Aerotech header word G75 (units per minute) or G76 (units per second), so the
#: file says which and does not depend on the controller's DefaultTaskTimeUnits.
#: A combined file is always per minute and carries neither word.
FEED_TIME_BASES = ("min", "s")
_TIME_BASE_WORD = {"min": "G75", "s": "G76"}


class _SplitFeed:
    """The F and E words of one move under `rotary_feed="split"`.

    Holds the SURFACE speed, which is what sets fibre diameter and CTS, and
    derives both rates from the move's own shape. A move of axial travel du and
    rotation dA at deposition radius r has surface length L = hypot(du, r*dA);
    it takes L / S. So F = S*|du|/L and E = S*|dA|/L, and both axes finish in
    the same time. That holds under Aerotech's documented rule (F times any move
    with a linear axis) AND under a controller that waits for the slower axis.

    Each word is emitted only on a CHANGE, and only when its axis moves: a ring
    strut gets no F (an F0 is not a rate), an axial strut no E. WHETHER an axis
    moves is read off the FORMATTED coordinates, which are what the controller
    sees, so float noise never makes a ring move "linear". The rates themselves
    come from the EXACT deltas, so a zigzag layer whose passes are identical gets
    one F and one E, not a new pair per pass from rounding jitter.
    """

    def __init__(self, fmt, radius, surface_z, time_base, precision,
                 x0, a0, f0, fixed=None):
        self.fmt = fmt
        # the user's OWN F and E, in the controller's units (`fixed_feed`):
        # written as typed, in place of the rates worked out from the speed
        self.fixed = None if fixed is None else (float(fixed[0]),
                                                 float(fixed[1]))
        self.radius = float(radius)
        self.surface_z = float(surface_z)
        self.scale = 1.0 if time_base == "min" else 1.0 / 60.0
        self.floor = 10.0 ** -int(precision)
        # where the approach G0 left the head, exact and as written
        self.x, self.a = float(x0), float(a0)
        self.xs, self.as_ = fmt(x0), fmt(a0)
        self.last = {"F": f0, "E": None}

    def _word(self, word, value):
        text = self.fmt(max(value, self.floor))
        if text == self.last[word]:
            return ""
        self.last[word] = text
        return " %s%s" % (word, text)

    def move(self, x, a_deg, z, surface_speed):
        """The 'G1 X.. A..[ F..][ E..]' line for a move to (x, a_deg) at
        commanded Z `z`, drawn at `surface_speed` mm/min."""
        xs, as_ = self.fmt(x), self.fmt(a_deg)
        du, da = x - self.x, a_deg - self.a
        line = "G1 X%s A%s" % (xs, as_)
        r = self.radius + (z - self.surface_z)
        length = math.hypot(du, r * math.radians(da))
        if self.fixed is not None:
            if xs != self.xs:
                line += self._word("F", self.fixed[0])
            if as_ != self.as_:
                line += self._word("E", self.fixed[1])
        elif length > 0.0:
            k = surface_speed * self.scale / length
            if xs != self.xs:
                line += self._word("F", k * abs(du))
            if as_ != self.as_:
                line += self._word("E", k * abs(da))
        self.x, self.a, self.xs, self.as_ = x, a_deg, xs, as_
        return line


def split_rotary_feeds(text, radius, surface_z=0.0, time_base="min",
                       precision=4):
    """A COMBINED-feed rod program (`F` = the surface speed, mm/min, as the
    calibration rod prints write it) re-expressed in `rotary_feed="split"`,
    the way the mandrel designer writes it for a flavor that splits the feed:
    the time-base word after `G90`, the approach's F as the linear rate, and
    each printing move's F and E from `_SplitFeed`, so the surface speed is
    the one the combined file meant. Travels keep their place and lose no
    position. A text with an E word or a time-base word already is returned
    as it is, so the pass runs once."""
    fmt = (lambda v: format_coord(v, precision))
    lines = text.splitlines()
    for ln in lines:
        code = ln.split(";", 1)[0].upper().split()
        if any(t in ("G75", "G76") for t in code) or any(
                t[:1] == "E" and len(t) > 1 for t in code[1:]):
            return text
    scale = 1.0 if time_base == "min" else 1.0 / 60.0
    out, sf, speed = [], None, None
    x = a = z = 0.0
    timed = False
    for ln in lines:
        body, _sep, comment = ln.partition(";")
        toks = body.split()
        cmd = toks[0].upper() if toks else ""
        words = {}
        for t in toks[1:]:
            try:
                words[t[0].upper()] = float(t[1:])
            except (ValueError, IndexError):
                pass
        if not timed and cmd == "G90":
            out.append(ln)
            out.append(_TIME_BASE_WORD[time_base])
            timed = True
            continue
        if cmd not in ("G0", "G00", "G1", "G01"):
            out.append(ln)
            continue
        if "F" in words:
            speed = words["F"]
        nx, na, nz = words.get("X", x), words.get("A", a), words.get("Z", z)
        drawn = cmd in ("G1", "G01") and ("X" in words or "A" in words)
        if sf is None and speed is not None:
            sf = _SplitFeed(fmt, radius, surface_z, time_base, precision,
                            x, a, fmt(speed * scale))
        if drawn and sf is not None and speed is not None:
            sf.x, sf.xs, sf.a, sf.as_ = x, fmt(x), a, fmt(a)
            line = sf.move(nx, na, nz, speed)
            if "Z" in words:
                line = line.replace(" A%s" % fmt(na),
                                    " A%s Z%s" % (fmt(na), fmt(nz)), 1)
            out.append(line + ((" ;" + comment) if comment else ""))
        elif "F" in words:
            # a travel or a Z move carries the linear rate, not the surface
            # speed: F times any move with a linear axis
            rate = fmt(speed * scale)
            kept = [t for t in toks if t[:1].upper() != "F"]
            out.append(" ".join(kept + ["F" + rate])
                       + ((" ;" + comment) if comment else ""))
            if sf is not None:
                sf.last["F"] = rate
        else:
            out.append(ln)
        x, a, z = nx, na, nz
    if not timed:
        out.insert(0, _TIME_BASE_WORD[time_base])
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _check_rotary_feed(rotary_feed, time_base, fixed_feed=None):
    if fixed_feed is not None:
        if rotary_feed != "split":
            raise ValueError("fixed_feed (your own F and E) needs "
                             "rotary_feed='split'")
        if len(fixed_feed) != 2 or not all(
                math.isfinite(float(v)) and float(v) > 0 for v in fixed_feed):
            raise ValueError("fixed_feed must be two positive numbers, F and "
                             "E; got %r" % (fixed_feed,))
    if rotary_feed not in MANDREL_ROTARY_FEEDS:
        raise ValueError("unknown rotary_feed %r; expected one of %s"
                         % (rotary_feed, ", ".join(MANDREL_ROTARY_FEEDS)))
    if time_base not in FEED_TIME_BASES:
        raise ValueError("unknown time_base %r; expected one of %s"
                         % (time_base, ", ".join(FEED_TIME_BASES)))


def _zigzag_layer(design):
    """One layer of Strategy 3.1 -- McColl Eq 6.1 as (u_end, t_end) per pass.

    PORTED, NOT DERIVED: [S58] McColl et al., Mater. Des. 155 (2018) 46-58, p.10.
    Decoded in `mew_frame.CylinderFrame`'s (u, v), where u is axial and v = r*t
    circumferential arc length, Eq 6.1 is exactly this -- t rises MONOTONICALLY
    for the whole layer while u ping-pongs 0 <-> L, the (-1)^Li giving the
    direction flip and the (Li + Li mod 2) the offset that starts iteration Li
    where Li-1 ended. `mew_tube` holds Eqs 1.1-5.3; only 6.1 lives here.

    ONE G1 PER LENGTH ITERATION IS EXACT, not a chord approximation: a
    constant-winding-angle helix is a STRAIGHT LINE in (u, v), and a machine
    interpolating X and A linearly traces precisely that line. That is also why
    the sag solver may work in (u, v, h) unchanged.

    LAYERS REPEAT IDENTICALLY (McColl p.4) -- no per-layer rotation and no phase
    offset to invent, because the closure Eq 3.1 buys IS the registration: the
    second layer lands on the first's fibres only if the helix met its own start.

    `t` is the UNWRAPPED mandrel angle in radians. Returns `design.n_pivots`
    entries; after the last one `u` is back at 0 and `t` is a WHOLE number of
    revolutions, the closure Eqs 3.1/4.2 exist to guarantee. Both are asserted by
    `test_mandrel_gcode.py` as invariants, not against a fixture.

    A `mew_tube.TurnedTubeDesign` closes by an END TURN instead: each pass is
    two entries, the helical run to the far end and then a pure rotation at
    that end (same u) onto the next pivot, so the list is twice as long and
    still ends at u = 0 on a whole number of revolutions. A design whose turn
    is zero emits exactly the one-entry-per-pass list above.
    """
    dt = design.pass_rev * 2.0 * math.pi
    turn = math.radians(getattr(design, "end_turn_deg", 0.0))
    out = []
    # pass 0 runs 0 -> L, pass 1 runs L -> 0, ... -- the (-1)^Li of Eq 6.1
    for i in range(design.n_pivots):
        u_end = 0.0 if i % 2 else design.length
        if turn > 1e-12:
            out.append((u_end, (i + 1) * dt - turn))
        out.append((u_end, (i + 1) * dt))
    return out


def mandrel_design(mandrel_diameter, winding_angle, target_length,
                   eta=None, rm=0, max_relative_error=0.05, pivots=None):
    """Resolve the tube parameters a mandrel print needs -> `mew_tube.TubeDesign`.

    Split out from the emitter because the parameter page has to SHOW the
    resolved length, pivot count and pore area before anyone presses Generate --
    an arbitrary (radius, winding angle, length) triple usually does not close.
    Pass `eta` to pin the pivot ladder yourself;
    otherwise the nearest closing length to `target_length` is chosen and a
    triple further away than `max_relative_error` is REJECTED, not rounded to
    silently.

    Pass `pivots` (an even N) instead to close by an END TURN: the tube is
    then exactly `target_length` at exactly `winding_angle`, and the mandrel
    turns at each end onto the next pivot (`mew_tube.TurnedTubeDesign`).
    """
    radius = 0.5 * float(mandrel_diameter)
    if not radius > 0.0:
        raise ValueError("mandrel diameter must be positive, got %r"
                         % (mandrel_diameter,))
    if pivots is not None:
        if eta is not None:
            raise ValueError("pivots (end-turn closure) and eta (length "
                             "closure) are two different tubes; pass one")
        return mew_tube.TurnedTubeDesign(radius, winding_angle, pivots,
                                         target_length)
    if eta is not None:
        return mew_tube.TubeDesign(radius, winding_angle, eta, rm)
    return mew_tube.solve_tube(radius, winding_angle, target_length,
                               max_relative_error=max_relative_error)


def _append_mandrel_exit(lines, x0, x1, margin, fmt):
    """Append the end-of-print exit travel for a rotating collector, in place.

    The plate's exit runs the nozzle off the build plate; a tube has no plate
    edge, so the equivalent is to run it off the END of the printed span along
    the mandrel axis. `gcode_exit.mandrel_exit_x` picks which end -- whichever
    the print finished nearer -- and how far past it.

    A whole-sheet layer always ends back at the near end, but the rule still
    reads the finish rather than assuming it.

    `margin` of None or 0 emits NOTHING: no exit move at all.

    A is deliberately absent from the move: leaving it out holds the rotary
    where the print left it, so the exit is a pure axial translation and the
    trailing polymer is drawn off the end of the wall instead of being wrapped
    around it once more. Z is absent for the same reason -- the standoff the
    wall ended at is the one the nozzle should keep while it runs off.

    The finishing X is read back off the emitted lines rather than passed in,
    because the two emitters track their own position differently and a second
    copy of it is a second thing to get wrong."""
    if not margin:
        return
    x_finish = None
    for ln in reversed(lines):
        if ln.startswith(("G1 X", "G0 X")):
            x_finish = float(ln.split()[1][1:])
            break
    if x_finish is None:                 # nothing was laid; nothing to run off
        return
    _mark(lines, "exit")
    lines.append("G0 X%s" % fmt(
        gcode_exit.mandrel_exit_x(x_finish, x0, x1, float(margin))))


#: The bare rod between two side-by-side copies of a tube, mm, when nobody
#: says: the default exit distance, the shortest gap the exit may park in.
TUBE_COPY_GAP = 5.0


class TubeBlock(object):
    """One run of identical layers in a tube program: a resolved pattern and a
    layer count, plus an optional step and speed of its own.

    design: a `mew_tube.TubeDesign` / `TurnedTubeDesign` (the McColl helix) or
      a `mew_unit_cell.TubeCellDesign`. Every block of one tube shares its
      length -- see `generate_mandrel_blocks_gcode`.
    layers: whole sheets of the pattern, at least 1.
    step: how far the nozzle steps OUT before each of this block's layers, mm;
      None takes the program's `layer_step`.
    speed: this block's surface speed, mm/min; None takes the program's.
    turn_out: PERIMETER TRAVEL on a tube, mm (0 = off, byte-identical). A
      helix closed by an END TURN turns the mandrel in place at the tube's end;
      with `turn_out` the head instead runs that far PAST the end along the
      rod, turns there, and comes back to the end before the next pass, so the
      turn happens clear of the tube's own end. A pattern with no end turn has
      nothing to move out, and a cell family's end ring is structure, so both
      ignore it.
    shift: mm along +X this block's layers sit from the tube's own start
      (0 = the tube's place, byte-identical). The G-code page's per-layer
      Position on mandrel / Offset: a layer placed apart from the tube is the
      tube's length laid that far along the rod. The block is reached by a
      `G0 X` at the held A, as a block that starts at another u always was.
    """

    __slots__ = ("design", "layers", "step", "speed", "turn_out", "shift")

    def __init__(self, design, layers, step=None, speed=None, turn_out=0.0,
                 shift=0.0):
        self.design = design
        self.layers = layers
        self.step = step
        self.speed = speed
        self.turn_out = float(turn_out or 0.0)
        self.shift = float(shift or 0.0)

    def __repr__(self):
        return ("TubeBlock(%r, layers=%r, step=%r, speed=%r, shift=%r)"
                % (self.design, self.layers, self.step, self.speed,
                   self.shift))


class _TubeSheet(object):
    """One block's sheet of the pattern, as the A words the emitter writes.

    `first(a_off)` is where the sheet starts, (u, A); `moves(k, a_off)` the
    rest of layer k of the block, in order. `a_off` is the A the block's own
    angle origin sits at: `start_angle` for the first block, so a one-block
    program writes exactly the expressions the two emitters wrote before
    they were one, and whatever lines the block up with the current A after
    that (`a_off_at`).

    A cell route is restarted at its first vertex on the NEAR end (lowest u)
    when it does not already start there (a wavy family's start sits one
    overhang inside its lowest vertex, and counts). Every built family does,
    so this reaches only a custom drawing whose first node sits inside the
    tube, which would otherwise join its neighbours across the wall.

    `far=True` starts and ends the sheet at the FAR end instead: the cell
    route restarted at its first far-end vertex (the same closed circuit, so
    the same fibres), the winding run mirrored in u (the same two helix
    families, so the same diamonds). The first of several side-by-side
    copies is laid that way, so it finishes facing the gap to the next.
    """

    def __init__(self, design, far=False, turn_out=0.0):
        self.r0 = design.radius
        self.turn_out = (float(turn_out or 0.0) if float(
            getattr(design, "end_turn_deg", 0.0) or 0.0) > 1e-12 else 0.0)
        self.half = 0.5 * float(design.length)
        if isinstance(design, mew_unit_cell.TubeCellDesign):
            route = mew_unit_cell.tube_cell_route(design)
            if len(route) < 2:
                raise ValueError("the routed layer has %d point(s)"
                                 % (len(route),))
            # the layer's own closure: same u, a whole number of circumferences
            dv = route[-1][1] - route[0][1]
            # a wavy ring bulges `axial_overhang` past the end ring it starts
            # on, which is still that end
            over = design.params.get("axial_overhang", 0.0)
            if far:
                u_max = max(u for u, _ in route)
                off_end = route[0][0] < u_max - over - 1e-9
                at_end = lambda u: u >= u_max - 1e-9           # noqa: E731
            else:
                u_min = min(u for u, _ in route)
                off_end = route[0][0] > u_min + over + 1e-9
                at_end = lambda u: u <= u_min + 1e-9           # noqa: E731
            if off_end:
                j = next(i for i, (u, _) in enumerate(route) if at_end(u))
                route = route[j:] + [(u, v + dv) for u, v in route[1:j + 1]]
                # the pattern repeats every circumference: start inside the
                # first turn, not the whole turns into the circuit it was
                circ = 2.0 * math.pi * self.r0
                back = math.floor(route[0][1] / circ + 1e-9) * circ
                route = [(u, v - back) for u, v in route]
            self.cell = True
            self.start = route[0]
            self.body = route[1:]
            self.dv = dv
        else:
            self.cell = False
            self.passes = _zigzag_layer(design)
            self.u0 = 0.0
            if far:
                self.u0 = design.length
                self.passes = [(design.length - u, t) for u, t in self.passes]
            # one sheet, in degrees of A; after it u is back at u0
            self.turn_deg = math.degrees(self.passes[-1][1])

    def first(self, a_off):
        if self.cell:
            return self.start[0], a_off + math.degrees(self.start[1] / self.r0)
        return self.u0, a_off

    def a_off_at(self, a_now):
        """The `a_off` that starts this sheet at the rotary angle `a_now`."""
        if self.cell:
            return a_now - math.degrees(self.start[1] / self.r0)
        return a_now

    def moves(self, k, a_off):
        if self.cell:
            for u, v in self.body:
                yield u, a_off + math.degrees((v + k * self.dv) / self.r0)
        else:
            prev_u = None
            for u_end, t_end in self.passes:
                a = a_off + k * self.turn_deg + math.degrees(t_end)
                if (self.turn_out > 0.0 and prev_u is not None
                        and abs(u_end - prev_u) < 1e-9):
                    # an END TURN (a pure rotation at the tube's end): out
                    # past the end at the held A, turn there, back to the end
                    a_before = a_prev
                    out = u_end + (self.turn_out if u_end > self.half
                                   else -self.turn_out)
                    yield out, a_before
                    yield out, a
                prev_u, a_prev = u_end, a
                yield u_end, a


def generate_mandrel_blocks_gcode(mandrel_diameter, blocks, fiber_diameter,
                                  speed, *, layer_step=None,
                                  speed_mode="surface", start_x=0.0,
                                  start_angle=0.0, start_z=0.0, precision=4,
                                  exit_margin=None, rotary_feed="combined",
                                  time_base="min", surface_z=0.0,
                                  fixed_feed=None, copies=1,
                                  copy_gap=TUBE_COPY_GAP):
    """Return MEW tube g-code (str) for a rotating mandrel: X + the rotary A.

    ONE TUBE (or `copies` of it): a call of `generate_mandrel_scaffolds_gcode`,
    the one emitter. `generate_mandrel_gcode` (the McColl helix) and
    `generate_mandrel_cell_gcode` (a unit-cell family) are one-block calls of
    this. `blocks` is a list of `TubeBlock`s laid in order, outward, so a tube
    can be 4 layers of winding, then 4 of re-entrant, then 4 of winding.

    A is DEGREES of mandrel rotation, right-hand rule about machine X, UNWRAPPED
    -- what `mew_frame.CylinderFrame` and `parse_gcode(frame=...)` read back, and
    the ISO 6983 rotary axis. Z, and so `start_z`, is measured OUTWARD FROM THE
    BARE MANDREL SURFACE.

    THE CONTROLLER MUST BE TOLD ABOUT `A`, AND IT FAILS SILENTLY IF IT IS NOT: an
    unregistered rotary axis makes Klipper DROP the word and answer `ok`, printing
    a flat zigzag from a file identical to the one that works. Register it
    (`MANUAL_STEPPER ... GCODE_AXIS=A`).

    A LAYER IS ONE WHOLE SHEET OF THE PATTERN, on every family:
    a whole winding circuit, out and back over every pivot, or a
    whole traversal of the routed cell. The nozzle steps OUT by the block's
    `step` (default `layer_step`, default the fibre diameter) before every
    layer but the first. A sheet ends where it started, at the near end, a
    whole number of turns on, so the next layer simply continues and the A
    word keeps unwrapping. The step is adjustable because a sheet is not one
    fibre thick everywhere: a winding's two families cross, and stack about
    two diameters at the crossings.

    Each layer is stepped to on a travel, `G0 Z<h>`, never folded into its
    first G1. Two contracts ride on that: (a) every deposition move of a
    layer sits at ONE h, so "layer" keeps meaning a constant radius; (b) the
    converter's per-scaffold grouping asserts one path per layer and a path
    breaks on a travel. The jet does not stop at the hop --
    `parse_gcode(with_travels=True)` reports it as deposited material.

    BETWEEN BLOCKS there is no turn: the next block's
    angle origin is wherever the last one stopped, so the join is the same
    `G0 Z` step as between two layers. Registration matters within a block,
    where a layer must land on the one below; across a change of pattern
    there is nothing to land on. A block whose sheet starts at another u
    (a wavy family's overhang) is reached by a `G0 X` at the held A first.
    Every block shares ONE tube length, the user's; a block resolved to
    another length is REFUSED. Two winding angles rarely close at one length
    by Eq 3.1, so a winding in a multi-block tube wants the end-turn closure
    (`mandrel_design(pivots=...)`), and a cell `fit="fit"`.

    fiber_diameter: mm, the default step.
    speed: mm/min, emitted as F -- the RESULTANT surface speed, not the X-axis
      speed, which is the scalar the diameter and CTS laws consume. speed_mode
      "omega" holds the angular rate instead, so F rises by (R0 + h)/R0.
    rotary_feed: "combined" (the default, and byte-identical to every file
      before it existed) or "split" -- Aerotech's per-move F (linear) and E
      (rotary, degrees), both derived from the same surface speed; see
      `_SplitFeed`. time_base "min"/"s" applies to split only. Split matters
      most on a cell: every strut has its own heading, so F and E change on
      nearly every move, where a zigzag holds one pair for a whole layer.
    surface_z: the Z that means the bare mandrel surface, already folded into
      `start_z` by the caller. Split needs it apart, because its rates depend
      on the true deposition radius R0 + (Z - surface_z).
    fixed_feed: (F, E) in the controller's units, split only -- the user's own
      rates, written on every move in place of the ones worked out from
      `speed`, which then no longer sets the speed of any split move.

    COPIES SIDE BY SIDE (the plate's Array, on a rod): `copies` whole tubes,
    every block of each, laid one after another along +X with `copy_gap` mm
    of bare rod between one's extreme fibre and the next's. Each copy is a
    scaffold group of its own. A copy ends at the end it started from, so the
    first is laid from its FAR end (`_TubeSheet(far=True)`): it finishes
    facing the gap, and the join to the second is a `G0 X` across bare rod at
    the held A, then the `G0 Z` down to the first layer. From the second
    copy on each one finishes facing the copy before it, so the join to a
    THIRD crosses the second's wall and lays one axial fibre over it -- any
    route to it does (`tube_copy_crossings` says which). The exit leaves the
    last copy into the gap behind it, so a gap shorter than the exit distance
    is REFUSED: the nozzle would park on the copy before.
    """
    copies = int(copies)
    if copies < 1:
        raise ValueError("copies must be at least 1, got %r" % (copies,))
    blocks = list(blocks)
    if not blocks:
        raise ValueError("a tube needs at least one layer block")
    pitch = tube_copy_pitch(blocks, copy_gap) if copies > 1 else 0.0
    if copies > 1 and exit_margin and float(exit_margin) > (
            pitch - blocks[0].design.length - tube_overhang(blocks)):
        raise ValueError(
            "the exit leaves the last copy into the %.4f mm gap behind it, so "
            "a %.4f mm exit would park on the copy before. Make the gap at "
            "least the exit distance, or shorten or turn off the exit"
            % (float(copy_gap), float(exit_margin)))
    return generate_mandrel_scaffolds_gcode(
        mandrel_diameter,
        [TubeScaffold(blocks, fiber_diameter, speed, layer_step=layer_step,
                      start_x=start_x + ci * pitch, start_z=start_z)
         for ci in range(copies)],
        speed_mode=speed_mode, start_angle=start_angle, precision=precision,
        exit_margin=exit_margin, rotary_feed=rotary_feed,
        time_base=time_base, surface_z=surface_z, fixed_feed=fixed_feed)


class TubeScaffold(object):
    """One tube of a program that lays several on one rod: its layer blocks,
    its own fibre, speed and layer step, and where it sits.

    blocks: `TubeBlock`s, laid in order, outward (one tube's whole wall).
    fiber_diameter: mm; the default `layer_step`.
    speed: mm/min, the surface speed of any block that names none.
    layer_step: mm the nozzle steps out between layers; None = the fibre.
    start_x: the tube's near end (u = 0) in machine X, collector frame.
    start_z: the Z of its first layer -- the bare-surface Z already in it,
      exactly as `generate_mandrel_blocks_gcode` takes `start_z`. A tube
      stacked on another starts where that one's wall ends.
    """

    __slots__ = ("blocks", "fiber_diameter", "speed", "layer_step",
                 "start_x", "start_z")

    def __init__(self, blocks, fiber_diameter, speed, layer_step=None,
                 start_x=0.0, start_z=0.0):
        self.blocks = list(blocks)
        self.fiber_diameter = fiber_diameter
        self.speed = speed
        self.layer_step = layer_step
        self.start_x = float(start_x)
        self.start_z = float(start_z)

    @property
    def length(self):
        return float(self.blocks[0].design.length)

    def span(self):
        """(x0, x1) of what it prints on the rod, wave overhang and every
        block's shift included."""
        over = tube_overhang(self.blocks)
        lo, hi = tube_shift_range(self.blocks)
        return (self.start_x + lo - over,
                self.start_x + hi + self.length + over)

    def __repr__(self):
        return ("TubeScaffold(%d block(s), start_x=%r, start_z=%r)"
                % (len(self.blocks), self.start_x, self.start_z))


def generate_mandrel_scaffolds_gcode(mandrel_diameter, scaffolds, *,
                                     speed_mode="surface", start_angle=0.0,
                                     precision=4, exit_margin=None,
                                     rotary_feed="combined", time_base="min",
                                     surface_z=0.0, fixed_feed=None,
                                     joins=None):
    """Return MEW g-code (str) laying several tubes on one rotating mandrel.

    THE ONE TUBE EMITTER: `generate_mandrel_blocks_gcode` (one tube, or
    `copies` of it side by side) is a call of it, and so are the two
    single-family emitters. Everything that docstring says about A, Z, a
    layer, a block and the feeds holds here per tube.

    Each `TubeScaffold` is laid whole, in order, as a scaffold group of its
    own (the G-code page's Scaffold 1..N). Where it sits is the caller's:
    its `start_x` along the rod and its `start_z` out from it, so two tubes
    sit side by side, stacked one on the other, or anything between, exactly
    as two 3-axis scaffolds do by their Start X / Z.

    THE JOIN to the next tube is a travel at the held A -- no turn, as
    between blocks: out to its first layer's Z first if that is farther out
    (so the nozzle does not drag through the wall it just laid), then along
    the rod, then in. A sheet ends where it started, so WHICH END a tube is
    laid from decides where the nozzle is after it: the first tube starts at
    the end facing the second (it finishes facing it), every later one at
    the end nearer the nozzle. For copies side by side that is the far end
    of the first and the near end of the rest -- the copies' own rule, so
    their program is byte-identical -- and a tube stacked on another starts
    on the end the one below finished at.

    The jet never stops, so every join is a fibre, and the rule above is
    what keeps it on bare rod: with two tubes side by side the join runs
    only over the gap between them. A sheet cannot finish at the other end
    (a layer is a closed circuit, `_TubeSheet`, and the next layer starts
    where it closed), so from the third tube in a row on, the join crosses
    the tube before it. That is the fewest crossings any order of ends
    allows. `joins`, when a list, receives one `(k, x_from, x_to, crossed)`
    per join into tube k (0-based), `crossed` the 0-based tubes whose span
    the hop runs over.

    The exit leaves the LAST tube along the rod (`_append_mandrel_exit`),
    from the end it finished at. The ends are never swapped to make room
    for it: that would send the join to the last tube along its whole
    length before the tube is laid, a fibre left inside its wall. If the
    exit would reach another tube at or above its own start radius, it
    parks halfway across the bare rod between the two instead.
    """
    _check_rotary_feed(rotary_feed, time_base, fixed_feed)
    if speed_mode not in MANDREL_SPEED_MODES:
        raise ValueError("unknown mandrel speed_mode %r; expected one of %s"
                         % (speed_mode, ", ".join(MANDREL_SPEED_MODES)))
    scaffolds = list(scaffolds)
    if not scaffolds:
        raise ValueError("a mandrel program needs at least one tube")
    many = len(scaffolds) > 1
    plans = []
    for si, sc in enumerate(scaffolds):
        who = "scaffold %d: " % (si + 1) if many else ""
        if sc.fiber_diameter <= 0:
            raise ValueError("%sfiber diameter must be positive, got %r"
                             % (who, sc.fiber_diameter))
        layer_step = (sc.fiber_diameter if sc.layer_step is None
                      else sc.layer_step)
        blocks = sc.blocks
        if not blocks:
            raise ValueError("%sa tube needs at least one layer block" % who)
        length = blocks[0].design.length
        plan = []
        for bi, b in enumerate(blocks):
            name = who + ("block %d" % (bi + 1) if len(blocks) > 1
                          else "the tube")
            n = int(b.layers)
            if n < 1:
                raise ValueError(
                    "layers must be at least 1 on a mandrel, got %r%s."
                    % (b.layers, "" if len(blocks) == 1 and not many
                       else " in " + name))
            step = layer_step if b.step is None else b.step
            if not step > 0:
                raise ValueError("the layer step must be positive, got %r in "
                                 "%s" % (step, name))
            sp = sc.speed if b.speed is None else b.speed
            if not sp > 0:
                raise ValueError("speed must be positive, got %r" % (sp,))
            r0 = b.design.radius
            if abs(0.5 * float(mandrel_diameter) - r0) > 1e-9:
                raise ValueError(
                    "%s was resolved for a %.6f mm mandrel but %.6f mm was "
                    "asked for. The route's v coordinate is ARC LENGTH, so "
                    "replaying it on a different radius changes every "
                    "rotation; re-resolve the design."
                    % (name, 2.0 * r0, float(mandrel_diameter)))
            if abs(b.design.length - length) > 1e-6:
                raise ValueError(
                    "%s is %.4f mm long but block 1 is %.4f mm: every block "
                    "of a tube shares one length. Close a winding by an end "
                    "turn (pivots) and fit a cell with fit='fit', so each "
                    "comes out at exactly the length asked"
                    % (name, b.design.length, length))
            plan.append((b.design, n, step, sp, _block_turn_out(b),
                         float(getattr(b, "shift", 0.0) or 0.0)))
        plans.append(plan)
    radius = scaffolds[0].blocks[0].design.radius

    def fmt(v):
        return format_coord(v, precision)

    def feed_at(sp, h):
        # constant angular rate -> the surface speed at the deposition radius
        # rises with the wall. The GEOMETRY is untouched; only the time
        # parameterisation of the same path changes.
        return sp * (radius + h) / radius if speed_mode == "omega" else sp

    def sheets_of(si, far):
        return [(_TubeSheet(d, far=far, turn_out=out), n, step, sp, dx)
                for d, n, step, sp, out, dx in plans[si]]

    def mid(sc):
        return sc.start_x + 0.5 * sc.length

    # the first tube starts at the end facing the second, so it finishes
    # facing it (the copies' far end)
    first_far = many and mid(scaffolds[1]) > mid(scaffolds[0])
    sheets = sheets_of(0, first_far)
    split = rotary_feed == "split"
    lines = ["G21", "G90"]
    if split:
        lines.append(_TIME_BASE_WORD[time_base])
    # approach the first sheet's start at the first layer's height. A G0, so
    # the parser scores it as travel rather than as the first fibre -- but it
    # carries F, because F is modal and a consumer with no F word in hand runs
    # at its own DEFAULT_FEED (600 mm/min). Layer 0's feed and not `speed`:
    # under speed_mode="omega" the two differ by (r0 + start_z)/r0, and the
    # loop below re-emits F only on a CHANGE.
    sc0 = scaffolds[0]
    a_off = start_angle
    u, a = sheets[0][0].first(a_off)
    current_f = feed_at(plans[0][0][3], sc0.start_z)
    if split:
        f0 = fmt(fixed_feed[0] if fixed_feed is not None else
                 current_f * (1.0 if time_base == "min" else 1.0 / 60.0))
        sf = _SplitFeed(fmt, radius, surface_z, time_base, precision,
                        sc0.start_x + sheets[0][4] + u, a, f0,
                        fixed=fixed_feed)
    else:
        f0 = fmt(current_f)
    lines.append("G0 X%s A%s Z%s F%s"
                 % (fmt(sc0.start_x + sheets[0][4] + u), fmt(a),
                    fmt(sc0.start_z), f0))

    h = sc0.start_z
    x0 = sc0.start_x
    for si, sc in enumerate(scaffolds):
        _mark(lines, "group", group=si)
        if si:
            # the end nearer the nozzle (a tie: the near end); x0 is still
            # the u = 0 of the last block of the tube before
            x_now = x0 + u
            x0 = sc.start_x
            near = sheets_of(si, False)
            far = sheets_of(si, True)
            un = x0 + near[0][4] + near[0][0].first(0.0)[0]
            uf = x0 + far[0][4] + far[0][0].first(0.0)[0]
            sheets = far if abs(x_now - uf) < abs(x_now - un) else near
            x0 = sc.start_x + sheets[0][4]  # its first block's u = 0
            # the join: out first if the next tube starts farther out, then
            # along the rod at the held A, then in. No turn, as between
            # blocks.
            a_off = sheets[0][0].a_off_at(a)
            u, _ = sheets[0][0].first(a_off)
            if joins is not None:
                lo_x, hi_x = sorted((x_now, x0 + u))
                joins.append((si, x_now, x0 + u, [
                    j for j, o in enumerate(scaffolds)
                    if o.span()[0] < hi_x - 1e-9
                    and o.span()[1] > lo_x + 1e-9]))
            if fmt(sc.start_z) != fmt(h) and sc.start_z > h:
                lines.append("G0 Z%s" % fmt(sc.start_z))
                lines.append("G0 X%s" % fmt(x0 + u))
            else:
                lines.append("G0 X%s" % fmt(x0 + u))
                lines.append("G0 Z%s" % fmt(sc.start_z))
            if split:
                sf.x, sf.xs = x0 + u, fmt(x0 + u)
        else:
            x0 = sc.start_x + sheets[0][4]
        li = 0
        z0 = sc.start_z           # h of the block's first layer
        for bi, (sheet, n, step, sp, dx) in enumerate(sheets):
            if bi:
                # no turn between blocks: this block's angle origin is
                # wherever the last one stopped, and only a different start
                # u (or a block placed apart along the rod) needs a move
                a_off = sheet.a_off_at(a)
                u1, _ = sheet.first(a_off)
                x1 = sc.start_x + dx
                if fmt(x1 + u1) != fmt(x0 + u):
                    lines.append("G0 X%s" % fmt(x1 + u1))
                    if split:
                        sf.x, sf.xs = x1 + u1, fmt(x1 + u1)
                x0 = x1
                z0 = h + step
            if len(sheets) > 1:
                _mark(lines, "block", block=bi)
            for k in range(n):
                # z0 + k*step, not a running sum, so a one-block program
                # commands start_z + k*d exactly, as the two emitters always
                # did
                h = z0 + k * step
                _mark(lines, "layer", layer=li)
                if li:
                    lines.append("G0 Z%s" % fmt(h))
                feed = feed_at(sp, h)
                for u, a in sheet.moves(k, a_off):
                    if split:
                        lines.append(sf.move(x0 + u, a, h, feed))
                        continue
                    word = "G1 X%s A%s" % (fmt(x0 + u), fmt(a))
                    if feed != current_f:
                        word += " F%s" % fmt(feed)
                        current_f = feed
                    lines.append(word)
                li += 1
    last = scaffolds[-1]
    _append_mandrel_exit(lines, x0, x0 + last.length, exit_margin, fmt)
    if exit_margin and many and lines[-1].startswith("G0 X"):
        park = float(lines[-1].split()[1][1:])
        lo, hi = last.span()
        # the bare rod the exit runs into, up to the first tube in its way
        if park > hi:
            stops = [sc.span()[0] for sc in scaffolds[:-1]
                     if sc.start_z >= last.start_z - 1e-9
                     and sc.span()[1] > hi + 1e-9 and sc.span()[0] < park]
            edge, stop = hi, max(min(stops), hi) if stops else None
        else:
            stops = [sc.span()[1] for sc in scaffolds[:-1]
                     if sc.start_z >= last.start_z - 1e-9
                     and sc.span()[0] < lo - 1e-9 and sc.span()[1] > park]
            edge, stop = lo, min(max(stops), lo) if stops else None
        if stop is not None:
            lines[-1] = "G0 X%s" % fmt(0.5 * (edge + stop))
    return "\n".join(lines) + "\n"


def tube_overhang(blocks):
    """How far the print reaches past each end of the tiling, mm: the widest
    wave overhang of any block (`gui_mandrel_preview.collector_bounds`), or
    the run-out of an end turn (`TubeBlock.turn_out`)."""
    return max(max(float((getattr(b.design, "params", None) or {})
                         .get("axial_overhang", 0.0) or 0.0),
                   _block_turn_out(b)) for b in blocks)


def tube_shift_range(blocks):
    """(lowest, highest) `TubeBlock.shift` of a tube's blocks, mm, with the
    tube's own place (0) always inside: how far its layers reach either way
    along the rod past the tube's own span."""
    shifts = [float(getattr(b, "shift", 0.0) or 0.0) for b in blocks]
    return min([0.0] + shifts), max([0.0] + shifts)


def _block_turn_out(block):
    """How far this block's end turns run past the tube's end, mm: its
    `turn_out` when its design turns at the ends, else 0."""
    out = float(getattr(block, "turn_out", 0.0) or 0.0)
    if out > 0.0 and float(getattr(block.design, "end_turn_deg", 0.0)
                           or 0.0) > 1e-12:
        return out
    return 0.0


def tube_copy_pitch(blocks, copy_gap=TUBE_COPY_GAP):
    """Near end to near end of two side-by-side copies, mm: one copy's
    printed span, extreme fibre to extreme fibre, plus the bare rod
    between."""
    gap = float(copy_gap)
    if not (gap >= 0.0 and math.isfinite(gap)):
        raise ValueError("the gap between copies must be a finite number "
                         "of at least 0 mm, got %r" % (copy_gap,))
    blocks = list(blocks)
    lo, hi = tube_shift_range(blocks)
    return (float(blocks[0].design.length) + (hi - lo)
            + 2.0 * tube_overhang(blocks) + gap)


def tube_copy_crossings(copies):
    """The copies (1-based) whose join crosses a finished wall: every one
    from the third. A copy ends where it started, so after the second there
    is no way on that does not pass over the copy before."""
    return list(range(3, int(copies) + 1))


def generate_mandrel_gcode(mandrel_diameter, target_length, winding_angle,
                           fiber_diameter, layers, speed, *,
                           eta=None, rm=0, max_relative_error=0.05,
                           design=None, strategy="zigzag",
                           speed_mode="surface", start_x=0.0,
                           start_angle=0.0, start_z=0.0, precision=4,
                           exit_margin=None, rotary_feed="combined",
                           time_base="min", surface_z=0.0, fixed_feed=None,
                           layer_step=None, copies=1, copy_gap=TUBE_COPY_GAP,
                           turn_out=0.0):
    """Return MEW tube g-code (str) for the McColl diamond helix -- a one-block
    `generate_mandrel_blocks_gcode`, which documents every convention.

    turn_out: mm; an end-turn helix runs this far past the tube's end, turns
      there and comes back (`TubeBlock.turn_out`). 0 = turn in place.

    mandrel_diameter, target_length, winding_angle: mm and degrees FROM THE
      MANDREL AXIS (0 = axial, 90 = a ring); `target_length` is a REQUEST, the
      emitted tube being the nearest that CLOSES (`design`/`eta`/`rm` pin it).
    layers: whole winding circuits, each out and back over every pivot.
    layer_step: the Z step between them, default the fibre diameter.
    """
    if strategy not in MANDREL_STRATEGIES:
        raise ValueError("unknown mandrel strategy %r; built: %s (the infinity "
                         "loop is not built yet)"
                         % (strategy, ", ".join(MANDREL_STRATEGIES)))
    _check_rotary_feed(rotary_feed, time_base, fixed_feed)
    if design is None:
        design = mandrel_design(mandrel_diameter, winding_angle, target_length,
                                eta=eta, rm=rm,
                                max_relative_error=max_relative_error)
    return generate_mandrel_blocks_gcode(
        2.0 * design.radius, [TubeBlock(design, layers, turn_out=turn_out)],
        fiber_diameter,
        speed, layer_step=layer_step, speed_mode=speed_mode, start_x=start_x,
        start_angle=start_angle, start_z=start_z, precision=precision,
        exit_margin=exit_margin, rotary_feed=rotary_feed, time_base=time_base,
        surface_z=surface_z, fixed_feed=fixed_feed, copies=copies,
        copy_gap=copy_gap)


def generate_mandrel_cell_gcode(mandrel_diameter, design, fiber_diameter,
                                layers, speed, *, speed_mode="surface",
                                start_x=0.0, start_angle=0.0, start_z=0.0,
                                precision=4, exit_margin=None,
                                rotary_feed="combined", time_base="min",
                                surface_z=0.0, fixed_feed=None,
                                layer_step=None, copies=1,
                                copy_gap=TUBE_COPY_GAP):
    """Return MEW tube g-code (str) for a `mew_unit_cell.TubeCellDesign` -- a
    one-block `generate_mandrel_blocks_gcode`, which documents every convention,
    INCLUDING the warning that a controller with no 4th axis registered drops
    the A word silently.

    layers: whole traversals of the routed cell. On a brick a traversal lays
    the rings AND the longitudinals, two fibre families that stack where they
    cross; a re-entrant honeycomb has no crossings, so its traversal is one
    fibre thick except along the retraced struts. `layer_step` is the Z step
    between them, default the fibre diameter.

    design: a `mew_unit_cell.TubeCellDesign` already resolved onto this mandrel.
      `mandrel_diameter` is passed too and CHECKED against it, because v is arc
      length, so the same route on a different radius is a different rotation.
    """
    return generate_mandrel_blocks_gcode(
        mandrel_diameter, [TubeBlock(design, layers)], fiber_diameter, speed,
        layer_step=layer_step, speed_mode=speed_mode, start_x=start_x,
        start_angle=start_angle, start_z=start_z, precision=precision,
        exit_margin=exit_margin, rotary_feed=rotary_feed, time_base=time_base,
        surface_z=surface_z, fixed_feed=fixed_feed, copies=copies,
        copy_gap=copy_gap)


# --------------------------------------------------------------------------- #
# Print-time estimate
# --------------------------------------------------------------------------- #
# Modal feed (mm/min) assumed until the first F word. Mirrors
# gcode_to_fibers.DEFAULT_FEED / gcode_player.parse_path so this estimate lands on
# the SAME clock the deposition animation plays back on. Kept as a local literal
# (not imported) so plain g-code generation stays numpy-free -- importing it from
# gcode_to_fibers would pull in numpy, which generation otherwise never needs.
_DEFAULT_FEED = 600.0


# --------------------------------------------------------------------------- #
# Five axes -- a projected path onto a curved collector
# --------------------------------------------------------------------------- #
#: The four collector shapes the five-axis designer offers, as ids. One
#: `mew_frame` profile family covers all of them, which is the whole reason the
#: surface is a surface OF REVOLUTION: a class per shape would have been four
#: places for the curvature convention to drift.
SURFACE_SHAPES = ("dome", "bowl", "cone", "torus")


def _check_named_collector(shape, radius, ring, half_angle_deg, span):
    """Raise ValueError for a named collector that is not a surface.

    The designer page refuses the first four of these itself
    (`gui_surface._surface_check`); the engine did not, so every other door
    built them. The meridian bounds are new: a dome's or bowl's meridian runs
    pole to pole over pi*R, and past it the profile retraces the same sphere,
    so a span beyond it is surface that is not there. A torus's meridian is
    one turn of its tube, 2*pi*R; more laps the same circle."""
    u0, u1 = float(span[0]), float(span[1])
    if not radius > 0.0:
        raise ValueError("the collector radius has to be positive, got %r"
                         % (radius,))
    if not u1 > u0:
        raise ValueError("the collector span has to run forwards: 'span to' "
                         "%g is not beyond 'span from' %g" % (u1, u0))
    if shape == "torus" and not ring > radius:
        raise ValueError(
            "a torus needs a ring radius larger than its arc radius (ring %g, "
            "arc %g); at or below it the surface closes on its own axis"
            % (ring, radius))
    if shape == "cone" and not 0.0 < half_angle_deg < 90.0:
        raise ValueError(
            "a cone's half-angle has to be between 0 and 90 degrees, got %g; "
            "zero is a cylinder (use the 4-axis rotating mandrel for that) "
            "and 90 is a flat disc" % (half_angle_deg,))
    if shape in ("dome", "bowl"):
        pole_to_pole = math.pi * radius
        if u0 < 0.0 or u1 > pole_to_pole:
            raise ValueError(
                "a %s of radius %g has a meridian %.3f mm long, pole to pole; "
                "the span %g-%g mm runs off it. Shorten the span or enlarge "
                "the radius" % (shape, radius, pole_to_pole, u0, u1))
    if shape == "torus" and u1 - u0 > 2.0 * math.pi * radius:
        raise ValueError(
            "a torus tube of radius %g is %.3f mm around; the span %g-%g mm "
            "laps it" % (radius, 2.0 * math.pi * radius, u0, u1))


def surface_frame(shape, *, radius=10.0, ring=0.0, half_angle_deg=0.0,
                  span=(0.0, 12.0)):
    """The `mew_frame.DepositionFrame` a collector describes. THE ONE BUILDER.

    The datum picker and the engine's own tests come through here, so a
    collector drawn in one place and read back in another cannot be two
    different collectors
    -- the failure mode `gui_calibration` names as "two copies of the law" and
    `mew_circle_fit` exists to prevent for the circle geometry.

    `dome` and `bowl` are THE SAME CIRCLE, traversed opposite ways FROM
    OPPOSITE POLES, which is how one profile is two shapes: reversing the
    traversal flips the outward normal, so the curvature sign follows the
    normal rather than being applied on top of it where the two could disagree,
    and starting from the other pole (`t0 = pi`) puts the concave face where a
    cup's is -- looking up at the nozzle. Reversing alone was not enough and
    shipped a bowl mounted mouth-down; the comment on the branch below carries
    the measurement. A `cone`'s meridian is straight, so it is developable and
    the flat sag solver applies to it unchanged.
    """
    import mew_frame

    if shape not in SURFACE_SHAPES:
        raise ValueError("unknown collector shape %r; one of %s"
                         % (shape, ", ".join(SURFACE_SHAPES)))
    _check_named_collector(shape, radius, ring, half_angle_deg, span)
    if shape == "cone":
        prof = mew_frame.LineProfile(radius,
                                     half_angle=math.radians(half_angle_deg))
    elif shape == "torus":
        prof = mew_frame.ArcProfile(radius, centre_rho=ring)
    elif shape == "bowl":
        # A BOWL IS THE OPPOSITE CAP, NOT THE DOME'S UNDERSIDE. Reversing the
        # traversal alone (`convex=False` at `t0 = 0`) flips the normal and
        # nothing else, so the two shapes came out on the SAME cap of the same
        # sphere -- the one whose pole is at `x = +R` -- and the bowl's
        # printable face pointed away from the nozzle. Measured on the rig's
        # own preset through the player's actor matrices: dome and bowl filled the
        # identical world volume,
        # z = -28.596 .. -22.220 mm, and the outward normal at the pattern
        # centre read (0, 0, +0.825) on the dome against (0, 0, -0.825) on the
        # bowl. That is a cup mounted mouth-down, which is how it looked in
        # the viewer: the bowl sat upside down on the C axis.
        #
        # `t0 = pi` puts s = 0 at the LOWER pole instead, so the cup's bottom
        # is the part's bottom (`mount_axial` = -R, the point that rests on the
        # mount) and the meridian climbs to the rim. The normal still points at
        # the circle's centre -- concave, k1 = -1/R, untouched -- and that
        # centre is now ABOVE the surface, so the face looks at the nozzle.
        prof = mew_frame.ArcProfile(radius, t0=math.pi, convex=False)
    else:
        prof = mew_frame.ArcProfile(radius)
    return mew_frame.RevolutionFrame(prof, u0=span[0], u1=span[1])


def surface_centre_uv(frame, centre, azimuth_deg=0.0):
    """Chart `(u, v)` for a typed 'Centered at' + 'Around collector' pair.

    THE ONE PLACE `--surface-centre` / `--surface-azimuth` BECOME A CHART
    POINT, for the same reason `surface_frame` is the one builder: the CLI and
    the designer page both come through here, so a placement typed on the page
    and a placement typed on the command line resolve to the same point.

    `v = azimuth (radians) * rho(u)` is `mew_frame`'s own convention -- the
    chart puts `(u, v)` at azimuth `phi = v / rho(u)` (`mew_frame.py:1672`) --
    so azimuth 0 is `v = 0.0` exactly, whatever the sign of `rho`, and this is
    byte-for-byte the origin every caller sent before this field existed.

    AN IMPORTED COLLECTOR ANSWERS `(0, 0)`, always, and the two arguments say
    nothing. Its chart is not the collector's, it is the DESIGN's -- built
    about the design origin at construction (`mew_cad.CadFrame`) -- so the
    design is at the chart's own origin by definition and placement was
    already spent, on `surface_frame(origin=, heading_deg=)`. The CLIs refuse
    a typed centre or azimuth against a file rather than ignoring one here.
    """
    import numpy as np                 # local: numpy is not a module-level dep

    if getattr(frame, "profile", None) is None:
        return np.zeros(2, dtype=np.float64)
    _x, rho = frame.profile.point(float(centre))
    return np.array([float(centre), math.radians(float(azimuth_deg)) * rho],
                    dtype=np.float64)


def _rot2(v, deg):
    """`(p, q)` rotated by `deg` about the pattern origin."""
    if not deg:
        return (float(v[0]), float(v[1]))
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (c * float(v[0]) - s * float(v[1]),
            s * float(v[0]) + c * float(v[1]))


class SurfacePattern(object):
    """The PARAMETERS a five-axis layer's flat pattern is built from.

    `surface_layers` takes either a ready `(N, 2)` array -- the v1 path, which
    turns that ONE pattern rigidly per layer -- or one of these, which lets it
    re-derive the passes at every layer's own angle against a fixed footprint,
    the way `_emit_one_scaffold` has always done it for a flat plate. The
    difference is the whole point: a rigid turn moves the pass ENDS, so a
    rotated layer's passes run past the outermost fibre of the layer below it
    (1.00 mm at each end at the shipped 6 x 4 mm defaults), while re-derived
    passes all end on the same footprint boundary.

    `at(angle)` returns `(pattern, chart_angle_deg)` straight from
    `mew_project.scaffold_pattern`, which holds the port; no geometry lives
    here."""

    def __init__(self, length, width, spacing, *, shape="rectangle",
                 shape_sides=64, footprint_polygon=None, footprint_angle=0.0,
                 sample_step=0.5, samples_per_pass=None, turn_radius=0.0,
                 arc_segments=None):
        self.length = float(length)
        self.width = float(width)
        self.spacing = float(spacing)
        self.shape = shape or "rectangle"
        self.shape_sides = int(shape_sides)
        self.footprint_polygon = footprint_polygon
        self.footprint_angle = float(footprint_angle)
        self.sample_step = float(sample_step)
        self.samples_per_pass = samples_per_pass
        self.turn_radius = float(turn_radius)
        self.arc_segments = arc_segments

    #: The four ways into one layer's fibre set, in the order
    #: `_emit_one_scaffold` builds them, so `ENTRIES[0]` is the candidate a
    #: first layer takes there and here alike.
    ENTRIES = tuple((f, v, r) for f in (False, True)
                    for v in (False, True) for r in (False, True))

    def at(self, angle_deg, entry=(False, False, False)):
        import mew_project
        kw = ({} if self.arc_segments is None
              else {"arc_segments": int(self.arc_segments)})
        flip, vflip, reverse = entry
        return mew_project.scaffold_pattern(
            self.length, self.width, self.spacing, angle_deg=angle_deg,
            footprint_angle=self.footprint_angle, shape=self.shape,
            shape_sides=self.shape_sides,
            footprint_polygon=self.footprint_polygon,
            samples_per_pass=self.samples_per_pass,
            sample_step=self.sample_step, turn_radius=self.turn_radius,
            flip=flip, vflip=vflip, reverse=reverse, **kw)

    @property
    def n_arc(self):
        import mew_project
        return (mew_project.TURN_ARC_SEGMENTS if self.arc_segments is None
                else int(self.arc_segments))


def _pattern_ends(pattern, delta_deg=0.0):
    """`(entry, d_entry, exit, d_exit)` of a flat pattern, turned by `delta_deg`.

    `delta_deg` expresses one layer's ends in ANOTHER layer's chart: a point in
    layer k's chart points along the world direction of k's chart angle, so the
    turn between two charts is the difference of their fill angles. Any
    `footprint_angle` is in both and cancels."""
    def r(i):
        return _rot2((float(pattern[i][0]), float(pattern[i][1])), delta_deg)

    entry, nxt = r(0), r(1)
    exit_, prv = r(-1), r(-2)
    return (entry, _unit(nxt[0] - entry[0], nxt[1] - entry[1]),
            exit_, _unit(exit_[0] - prv[0], exit_[1] - prv[1]))


def _surface_link_points(pattern, prev, nxt, radius, *, link_loops="manual",
                         link_loop_angle=25.0, link_loop_dist=0.5,
                         link_loop_travel=True, n_arc=8):
    """`(arrival, departure)` flat `(p, q)` lists for one layer's link loops.

    The layer-to-layer LASSO, ported whole from the three-axis rule: the head
    arrives at a corner, rides an outboard circle tangent to both rays, and
    comes back to the SAME corner (`_link_loop_circle` / `_link_loop_points`
    above -- called, not re-derived). Because each loop starts and ends at its
    own corner, each lies entirely inside ONE layer's pattern and therefore
    inside one layer's projection; that is what makes the three-axis law port to
    a curved collector at all, since neither loop has to span two offset
    surfaces.

    `prev` and `nxt` are `(point, heading)` for the PREVIOUS layer's exit and
    the NEXT layer's entry, already expressed in THIS layer's chart by
    `_pattern_ends`, or None at the two ends of the stack. They are passed in
    rather than derived here because with per-angle pass derivation the
    neighbouring layer is a DIFFERENT pattern, not this one turned.

    THE GATES ARE THE THREE-AXIS ONES. Drawing a loop at every layer change
    unconditionally -- the equivalent of `link_loops="manual"` with
    `link_loop_travel=True` -- would not match the three-axis default, which is
    `"auto"` with travel loops OFF and draws nothing at a travel-joined change.
    The four knobs here are the same four, read the same way `_emit_one_scaffold` reads
    them:

      * DEPARTURE, at this layer's exit, turning from the last pass onto the
        hop. Only with `link_loop_travel`, and only when the hop is at least
        `link_loop_dist` long -- a shorter hop is ONE corner and a second loop
        would sit on top of the first.
      * ARRIVAL, at this layer's entry, turning off the hop onto the first
        pass. With travel loops on the lead-in is `_lead_in_chord`; with them
        off it is the previous layer's exit heading and the corner must be
        COINCIDENT (`gap <= link_loop_dist`), which is reachable now that both
        layers' passes end on one footprint boundary and was not when the
        pattern was turned rigidly.

    Either way `_link_loop_circle` still self-gates on a near-straight heading
    pair, a near U-turn, or a loop that would bulge into the part.

    `part_centre` is `(0, 0)`: the pattern is centred on the pattern origin by
    construction, for `mew_project.scaffold_pattern` as for `serpentine_pattern`.
    """
    entry, d_entry, exit_, d_exit = _pattern_ends(pattern)
    arrival, departure = [], []
    if link_loops in (None, False, "off"):
        return arrival, departure
    manual = (link_loops == "manual")

    def _turn(a, b):
        return math.degrees(math.acos(max(-1.0, min(1.0,
            a[0] * b[0] + a[1] * b[1]))))

    if prev is not None:
        prev_exit, prev_dir = prev
        gap = math.hypot(entry[0] - prev_exit[0], entry[1] - prev_exit[1])
        if link_loop_travel:
            d_out = _lead_in_chord(entry, prev_exit, prev_dir, link_loop_dist)
            at_corner = True
        else:
            d_out = prev_dir
            at_corner = gap <= link_loop_dist
        if at_corner and (manual or _turn(d_out, d_entry) >= link_loop_angle):
            pts = _link_loop_points(entry, d_out, d_entry, radius, (0.0, 0.0),
                                    n_arc, False)
            if pts:
                # `pts` ends back at the corner and `pattern[0]` IS the corner,
                # so the closing point is dropped rather than emitted twice.
                arrival = [entry] + [(x, y) for (x, y, _a) in pts[:-1]]
    if nxt is not None and link_loop_travel:
        next_entry = nxt[0]
        gap = math.hypot(next_entry[0] - exit_[0], next_entry[1] - exit_[1])
        if gap >= link_loop_dist:
            d_travel = _unit(next_entry[0] - exit_[0], next_entry[1] - exit_[1])
            if manual or _turn(d_exit, d_travel) >= link_loop_angle:
                pts = _link_loop_points(exit_, d_exit, d_travel, radius,
                                        (0.0, 0.0), n_arc, False)
                departure = [(x, y) for (x, y, _a) in pts]
    return arrival, departure


def surface_layers(frame, pattern, origin_uv, layers, fiber_diameter, *,
                   layer_rotation=0.0, per_layer_reprojection=False, step=0.25,
                   link_loops=False, link_loop_radius=None,
                   link_loop_angle=25.0, link_loop_dist=0.5,
                   link_loop_travel=True):
    """One `mew_project.ProjectedPath` per layer, at that layer's wall height.

    `per_layer_reprojection` OFF -- the five-axis DEFAULT, matching the shipped four-axis one
    (`Calibration.mandrel_per_layer_registration = False`) -- builds ONE
    projection per DISTINCT pattern -- keyed on the pattern's own bytes, up to
    traversal direction, so two layers that reach the same points by different
    routes share one projection -- each projected once on the
    BARE collector (`height=0`) and then lifted to every layer that shares it
    by `world + h * normal` -- world arithmetic, no second chart evaluation.
    `mew_frame`'s `to_world` builds the offset surface by moving `h` out along
    the bare normal (`mew_frame.py:1385-1396`), so this is an EXACT identity --
    `surf_h.point(uv) == surf_0.point(uv) + h * surf_0.normal(uv)`
    -- not an approximation: layer N sits directly above the layer at height 0
    of its OWN pattern, along the local surface normal, which is the stacking
    the design calls for and is the reason five-axis holding the nozzle
    perpendicular to the collector should make layers stack in the first
    place. `test_five_axis_gcode` pins that a plain (unrotated, loop-free)
    stack is then the single projection's file, byte for byte.

    REPEATED layers are folded to `% 180`, not `% 360`. A hatch line has no
    visual identity beyond 180 degrees of rotation -- turning it a further 180
    retraces the SAME lines -- so a layer at `li * layer_rotation = 0` and one
    two rotation STEPS above it at `180` are "the same repeated orientation".
    Handed to `project_pattern` as two INDEPENDENT rigid rotations of the one
    canonical pattern, they would not be: a rigid rotation carries each
    point's own local approach heading around with it, so whether two such
    layers' shared turnaround corners end up approached from the same heading
    or the opposite one is an accident of `mew_project.serpentine_pattern`'s
    own point order, not a choice. Measured, that splits 16 of 112 shared
    corners by up to 0.19 mm on a real dome, and it is NOT a curvature
    effect: the identical split (22/64 corners, 0.11 mm) reproduced
    on a curvature-free near-flat control, so it is not a five-axis-specific
    or dome-specific consequence, only that rotation strategy's. The flat
    three-axis generator does not have this failure mode (0 of 32 shared
    corners split, same measurement) because `_layer_passes` recomputes hatch
    lines fresh against the fixed footprint at every layer's own angle rather
    than rotating one stored copy.

    Folding to `% 180` here means the two layers get the IDENTICAL projected
    path -- not merely the same lines approached the same way, but the exact
    same commanded points -- which is a strictly sufficient fix and the
    simplest one available: it does not require porting the flat generator's
    box-reclip algorithm onto a curved chart, which has no footprint to reclip
    against. Rigid rotation by the FULL angle is not reachable from any
    parameter this module exposes -- it has no known benefit for MEW and it
    harms fibre stacking. If a future collector shape genuinely cannot
    re-derive its pattern per angle, a full, unfolded rotation could still be reintroduced
    as an INTERNAL fallback the code chooses for that shape, but it must not
    become a user-facing checkbox: the choice of "does this print stack"
    belongs to the physics, not to an export option.

    ON re-runs the projection on each layer's OFFSET surface instead. That
    matters because the offset surface is not a scaled copy: its principal
    curvatures are `k / (1 + k h)`, so its geodesics are not the base surface's
    geodesics pushed outward, and a pass that was straight on the bare collector
    is not straight on top of ten plies of fibre. It buys exact commanded
    interfibre spacing in millimetres on each layer's own surface, at the cost
    of the stacking above -- the right choice where `h / R` is not small, which
    for MEW it usually is not. It stays available, just not the default.

    A per-layer ROTATION is allowed with re-projection off. A rotated pattern
    is laid along different geodesics, so ONE shared projection across every
    layer could not express it; one projection PER rotation angle can, and
    that is what is built.

    `pattern` IS EITHER A PATTERN OR ITS PARAMETERS, and which one decides
    whether a layer rotation is honest. An `(N, 2)` array is the v1 path: the
    one pattern, turned rigidly per layer. A `SurfacePattern` re-derives the
    passes at every layer's own fill angle against a fixed footprint, which is
    what `_emit_one_scaffold` has always done for a flat plate. Rigid turning
    moves the pass ENDS, so a 90-degree layer reached `length / 2` where the
    layer below reached `width / 2` -- 1.00 mm of straight overrun at each end
    at the shipped 6 x 4 mm defaults, which showed in the g-code as a pass
    carrying on for a few mm before its turnaround began. With a spec
    every layer ends on one boundary; measured, the two layers' deposited
    extents in the footprint's frame differ by 0.0000 mm
    (`test_a_rotated_layer_ends_on_the_shared_footprint`, whose RED control is
    the same measurement on the v1 builder at 1.0000 mm).

    THE ENTRY CORNER IS CHOSEN, not taken. With a spec, each layer is entered
    from whichever of the four candidates -- `SurfacePattern.ENTRIES`, the
    three-axis `flip` x `vflip` x reversal -- starts nearest the layer below's
    exit, which is `_emit_one_scaffold:1266` ported. Without it a five-axis
    layer change is a hop across the whole footprint, a dragged fibre in
    continuous MEW; with it the gap is 0.0000 mm at 0 and at 90 degrees. That
    is also what lets the linking loop's COINCIDENCE gate fire, so it is not a
    separate nicety: it is what makes the loop control live at the flat
    plate's own defaults.

    `link_loops` adds the layer-to-layer lasso (`_surface_link_points`), OFF by
    default and byte-identical off. It takes the flat plate's own three values
    -- `"off"`, `"auto"` (draw only when the heading turns by at least
    `link_loop_angle`) and `"manual"` (no angle test) -- beside
    `link_loop_dist` and the `link_loop_travel` opt-in, read exactly as
    `_emit_one_scaffold` reads them. `True` is accepted as the v1 spelling and
    means `"manual"`. A five-axis layer change is NOT always a long hop: with
    the entry minimiser the corners coincide, so the flat plate's
    default (`"auto"`, travel off) draws at a rotated stack's shared corner and
    draws nothing at an unrotated one -- where the layer below is re-entered by
    reversal, making the change a U-turn that `_link_loop_circle` refuses on
    any collector. The ported law still self-gates on that, on a near-straight
    heading pair, and on a loop that would bulge into the part.

    `link_loop_radius` is MILLIMETRES and has no default here to fall back on:
    a bare pattern carries no turnaround radius, and a spec's is not
    necessarily the loop's. `generate_surface_gcode` is where `None` resolves
    to `--surface-turn-radius`, and asking for loops without a radius raises
    here rather than silently drawing none.
    """
    import numpy as np                 # local: numpy is not a module-level dep
    import mew_project

    n = int(layers)
    if n < 1:
        raise ValueError("layers must be at least 1, got %r" % (layers,))
    spec = pattern if isinstance(pattern, SurfacePattern) else None
    # `True` is the v1 spelling of the switch and means what it meant then:
    # every layer change looped, no angle test. The string values are the flat
    # plate's own three, so one vocabulary serves both collectors. NB "off" is
    # a TRUTHY string -- every gate below tests `mode`, never `link_loops`.
    mode = ("manual" if link_loops is True
            else ("off" if link_loops in (None, False) else str(link_loops)))
    if mode not in ("off", "auto", "manual"):
        raise ValueError("link_loops must be off/auto/manual (or a bool), "
                         "got %r" % (link_loops,))
    if mode != "off" and link_loop_radius is None:
        raise ValueError(
            "link_loops needs link_loop_radius in mm; this function is handed "
            "a pattern, not the parameters it came from, so it cannot default "
            "to the turnaround radius. generate_surface_gcode resolves None "
            "to --surface-turn-radius.")
    if mode != "off" and spec is None and len(pattern) < 2:
        raise ValueError("link_loops needs a pattern with at least two points")
    n_arc = spec.n_arc if spec is not None else mew_project.TURN_ARC_SEGMENTS

    # `% 180`, not `% 360` -- see the docstring's "REPEATED layers" paragraph.
    # With per-angle derivation it is a no-op
    # rather than a correction, because `_layer_passes` lays the same lines at
    # an angle and at that angle plus 180; it is kept so the fold cannot come
    # back as a difference if the builder ever changes.
    angles = [(li * float(layer_rotation)) % 180.0 for li in range(n)]

    built = {}

    def _built(angle, entry):
        """One layer's flat pattern for a given fill angle and entry corner."""
        key = (round(float(angle), 9), entry)
        got = built.get(key)
        if got is None:
            got = (spec.at(angle, entry) if spec is not None
                   else (np.asarray(pattern, dtype=np.float64), float(angle)))
            built[key] = got
        return got

    # -- PASS A: which corner each layer is entered from ------------------- #
    # The three-axis travel minimisation, ported (`_emit_one_scaffold:1266`):
    # four ways into the same fibre set, take whichever STARTS nearest the
    # previous layer's exit. Without it a five-axis layer change is always a
    # hop across the whole footprint, which is a dragged fibre in continuous
    # MEW and is also why the linking loop's coincidence gate could never fire.
    # Layer 0 takes `ENTRIES[0]`, the candidate the flat emitter takes there.
    entries = [SurfacePattern.ENTRIES[0]] * n
    if spec is not None and n > 1:
        for li in range(1, n):
            p_pat, _ = _built(angles[li - 1], entries[li - 1])
            # the previous exit, in THIS layer's chart
            prev_pt = _rot2((float(p_pat[-1][0]), float(p_pat[-1][1])),
                            angles[li - 1] - angles[li])
            best, best_d = SurfacePattern.ENTRIES[0], None
            for cand in SurfacePattern.ENTRIES:
                pat_c, _ = _built(angles[li], cand)
                d = ((float(pat_c[0][0]) - prev_pt[0]) ** 2
                     + (float(pat_c[0][1]) - prev_pt[1]) ** 2)
                if best_d is None or d < best_d:
                    best, best_d = cand, d
            entries[li] = best

    # -- PASS B: the loops, which need both neighbours' chosen corners ----- #
    def _ends(li, in_chart_of):
        pat_li, _ = _built(angles[li], entries[li])
        return _pattern_ends(pat_li, angles[li] - angles[in_chart_of])

    def _with_loops(li):
        """Layer `li`'s flat pattern and the angle its chart is laid at."""
        pat, chart = _built(angles[li], entries[li])
        if mode == "off":
            return pat, chart
        prev = nxt = None
        if li > 0:
            e = _ends(li - 1, li)
            prev = (e[2], e[3])                          # its exit + heading
        if li < n - 1:
            e = _ends(li + 1, li)
            nxt = (e[0], e[1])                           # its entry + heading
        arrival, departure = _surface_link_points(
            pat, prev, nxt, float(link_loop_radius), link_loops=mode,
            link_loop_angle=link_loop_angle, link_loop_dist=link_loop_dist,
            link_loop_travel=link_loop_travel, n_arc=n_arc)
        if arrival or departure:
            pat = np.vstack([np.asarray(arrival, dtype=np.float64
                                        ).reshape(-1, 2),
                             pat,
                             np.asarray(departure, dtype=np.float64
                                        ).reshape(-1, 2)])
        return pat, chart

    out = [None] * n
    bare = mew_project.FrameSurface(frame, height=0.0)
    cache = {}          # distinct-pattern key -> its ProjectedPath on `bare`
    plan = []           # (layer, pattern, chart angle, key, backwards)
    for li in range(n):
        surf = mew_project.FrameSurface(frame, height=li * float(fiber_diameter))
        pat, chart = _with_loops(li)
        if per_layer_reprojection:
            out[li] = mew_project.project_pattern(
                surf, pat, origin_uv, angle_deg=chart, step=step)
            continue
        # The loops make a layer's pattern depend on its NEIGHBOURS' angles as
        # well as its own, so the key is the triple. With loops off it reduces
        # to the angle, which is the key this cache always had.
        # TWO LAYERS SHARE A PROJECTION WHEN THEIR PATTERN IS THE SAME PATTERN,
        # so the key IS the pattern -- its bytes and its chart angle -- rather
        # than a tuple of the inputs that produced it. Keying on the inputs was
        # right while a layer was only ever `(angle, first/middle/last)`; with
        # the entry minimiser it is not, because two layers can reach the same
        # points by different routes and an input key cannot see that.
        #
        # DIRECTION IS CANONICALISED, and that is not an approximation.
        # `project_pattern` is pointwise in the rows it is handed (`_rib_jobs`
        # selects by `q` and writes back by index), so projecting a reversed
        # pattern gives the reversed projection exactly. Without this the
        # minimiser doubles the projection count on an unrotated stack, where
        # it enters each layer by reversing the one below: measured 1 -> 2
        # projections with loops off and 3 -> 4 with them on, ten layers.
        rev = pat[::-1].copy()
        fwd_b, rev_b = pat.tobytes(), rev.tobytes()
        backwards = rev_b < fwd_b
        key = ((rev_b if backwards else fwd_b), round(float(chart), 9))
        if key not in cache:
            cache[key] = (rev if backwards else pat, chart)
        plan.append((li, key, backwards))

    # EVERY DISTINCT PATTERN IN ONE PROJECTION CALL, so their marches are
    # taken together (`mew_project.project_patterns`) -- same answers as one
    # call each.
    keys = [k for k in cache if isinstance(cache[k], tuple)]
    if keys:
        done = mew_project.project_patterns(
            bare, [(cache[k][0], origin_uv, None, cache[k][1]) for k in keys],
            step=step)
        cache.update(zip(keys, done))

    for li, key, backwards in plan:
        base = cache[key]

        def _rev(a):
            return None if a is None else (a[::-1].copy() if backwards else a)

        pt = _rev(base.pattern)
        nrm = _rev(base.normal)
        # THE OFFSET LAYER IS WORLD ARITHMETIC, not a second chart evaluation.
        # `mew_frame.to_world` builds the offset surface by moving `h` out
        # along the BARE normal (`mew_frame.py:1385-1396`), so
        # `surf_h.point(uv) == surf_0.point(uv) + h * surf_0.normal(uv)` is an
        # identity rather than an approximation -- and written this way it is
        # the identity itself, with no `(u, v)` in it, which is what lets a
        # collector with no chart stack layers at all. A parallel surface also
        # shares its normals with the surface it is offset from, which is why
        # `nrm` is the bare projection's and is not recomputed.
        h = li * float(fiber_diameter)
        out[li] = mew_project.ProjectedPath(
            _rev(base.world) + h * nrm, nrm, pt,
            uv=_rev(base.uv), tangent=_rev(base.tangent))
    return out


def _nearest_turn(delta):
    """`delta` moved into (-180, 180] by whole turns -- the shortest way round."""
    return delta - 360.0 * math.floor(delta / 360.0 + 0.5)


def _collector_normal(rotaries, normal):
    """`normal` carried into the COLLECTOR's own frame and unit length, or
    `None` when it is degenerate.

    The mount comes from `machine_model.collector_mount`, the SAME function
    `rotary_solutions` applies before it solves -- calling it here rather than
    rebuilding it is what keeps the band and the branches talking about one
    normal instead of two.
    """
    import numpy as np

    got = _collector_normal_f(rotaries, normal)
    return None if got is None else np.array(got)


def _collector_normal_f(rotaries, normal):
    """`_collector_normal` as a float 3-tuple, or `None`. The one computation.

    PLAIN FLOATS because the five-axis layer solve asks it at every point, and
    numpy's per-call cost on a 3-vector was most of that solve.
    """
    nx, ny, nz = (float(c) for c in normal)
    nn = math.sqrt(nx * nx + ny * ny + nz * nz)
    if nn <= 1e-12:
        return None
    nx, ny, nz = nx / nn, ny / nn, nz / nn
    m = _mount_rows(rotaries)
    return (m[0][0] * nx + m[0][1] * ny + m[0][2] * nz,
            m[1][0] * nx + m[1][1] * ny + m[1][2] * nz,
            m[2][0] * nx + m[2][1] * ny + m[2][2] * nz)


def _mount_rows(rotaries):
    """`machine_model.collector_mount` as nested float tuples, memoised with it."""
    import machine_model

    js = tuple(rotaries)
    key = (len(js) >= 2, js[0].axis if js else None)
    got = _MOUNT_ROWS.get(key)
    if got is None:
        got = tuple(tuple(float(c) for c in row)
                    for row in machine_model.collector_mount(js))
        _MOUNT_ROWS[key] = got
    return got


_MOUNT_ROWS = {}
_UNSET = object()


def _vec3(v):
    return (float(v[0]), float(v[1]), float(v[2]))


def _tilt_deg(a1, a0, n_c, c_deg):
    """The base joint's raw reading once the inner joint stands at `c_deg`:
    `_signed_angle(rotation_matrix(a1, c_deg) @ n_c, NOZZLE_UP, a0)`, on floats.

    Rodrigues applied to the vector rather than built as a matrix -- the same
    rotation -- because this runs at every point of every banded layer.
    """
    import machine_model

    kx, ky, kz = a1
    kn = math.sqrt(kx * kx + ky * ky + kz * kz)
    kx, ky, kz = kx / kn, ky / kn, kz / kn
    vx, vy, vz = n_c
    th = math.radians(float(c_deg))
    co, si = math.cos(th), math.sin(th)
    kv = (kx * vx + ky * vy + kz * vz) * (1.0 - co)
    m = (vx * co + (ky * vz - kz * vy) * si + kx * kv,
         vy * co + (kz * vx - kx * vz) * si + ky * kv,
         vz * co + (kx * vy - ky * vx) * si + kz * kv)
    return machine_model._signed_angle(m, machine_model.NOZZLE_UP, a0)

def _perp_band_interval(a1, a0, n_c, prev_c_deg, prev_a_deg, tol_deg):
    """The FEASIBLE INTERVAL for the inner (C) reading at one point: `(lo, hi)`
    in degrees, unwrapped to the copy nearest `prev_c_deg` -- or `None` when
    EVERY C satisfies the band and the joint is free to sit anywhere.

    THE MATHS, exact and general. Write
    `m(C) = R_{a1}(C) . n_c`, the collector normal after the inner joint has
    turned by `C` but before the base joint has. The base joint rotates about
    `a0` and so cannot change a vector's component along `a0`, so the
    residual perpendicularity error once the base joint has done its best is
    exactly `abs(arccos(m(C).a0) - arccos(NOZZLE_UP.a0))`
    (`cos(alpha - beta) = cos a cos b + sin a sin b` is why it collapses this
    far -- the base joint's own freedom is a rotation of `m` about `a0`,
    which changes nothing about the angle `m` makes with `a0`).

    `p(C) = m(C).a0` is SINUSOIDAL in `C`: decomposing `n_c` into its `a1`
    component (untouched by the inner joint) and `w`, the rest, `m(C) = (n_c
    . a1) a1 + cos(C) w + sin(C) (a1 x w)`, so `p(C) = K + R cos(C - psi)`
    with `K = (n_c.a1)(a1.a0)`, `R = hypot(w.a0, (a1 x w).a0)`. A tolerance
    band on the error is therefore a bound on `cos` of an interval, i.e. a
    pair of arcs in `C` -- closed form, no search.

    `R <= 1e-12` is the pole: `n_c` runs along `a1` itself (the apex of a
    dome under an apex-centred pattern is the case that motivated this), so
    `w = 0` and turning the inner joint changes nothing at all. The same
    holds when the whole circle already satisfies the tolerance (`lo <= -1
    and hi >= 1`). Both report `None`, FREE, rather than a point: a caller
    walking point by point spends that freedom on holding still, and a
    caller routing a whole layer spends it on going where the next closure
    wants it to already be. Collapsing it to a single value here would take
    the second choice away, which is the whole of change 1.

    Of the two arcs, the one returned is whichever a greedy step would pick.
    Each is first unwrapped to the copy nearest `prev_c_deg` (a rotation is
    periodic in 360 and this maths cannot tell the difference, the same
    freedom `_five_axis_readings` already spends on the spin word). **Do not
    wrap, only unwrap** -- the caller's spin word accumulates across many
    revolutions and comparing modulo 360 would unwind it.

    THE TWO ARCS ARE THE TWO IK BRANCHES, AND THE PICK COSTS BOTH JOINTS.
    They are half a turn apart and reach the same orientation with opposite
    BASE tilt, so choosing between them on `prev_c_deg` alone prices half the
    move: a 4 deg saving on the spin joint can carry a 93 deg flip of the
    tilt joint, from leaning one way to leaning the other. That is not a
    cosmetic difference. A layer change is ONE `G0`, and a controller
    interpolates its words linearly, so a tilt that changes sign inside one
    move puts the collector momentarily LEVEL -- its pole under the nozzle --
    and the deposited travel fibre is dragged from the footprint rim, over
    the apex and back, straight across the part. Measured on an
    apex-centred 12 x 12 mm pattern on an R10 dome at the 2 deg default:
    three of nine layer hops
    flipped, each reaching polar 1.68 deg (the apex is 0) and floating
    2.3 mm off the collector, against 47.07 deg and 0.13 mm for the six that
    did not.
    `cost = |dC| + |dA|` is not a new rule invented here. It is the cost
    `_five_axis_readings` already applies at tolerance 0 -- `sum(abs(a - p)
    for a, p in zip(cand, prev_angles))` over BOTH joints -- and the exact
    solve produces zero apex crossings on the same design. Costing C alone
    was what made the tolerance path a second policy rather than the
    generalisation of the exact one its docstring claims it is. Re-measured
    across the same design after the change: 0 flipped hops, total |dA|
    15075.8 -> 14795.1 deg (-1.9%), total |dC| 32901.5 -> 32929.2 (+0.08%) --
    the tolerance's own purpose is untouched.

    `prev_a_deg` is the base (tilt) joint's current angle, in the same
    unwrapped degrees. It is REQUIRED rather than optional because both
    production callers are already gated on there being a previous pose, and
    a default would leave the half-priced pick reachable by accident.
    """
    import machine_model

    a1 = _vec3(a1)
    a0 = _vec3(a0)
    nx, ny, nz = _vec3(n_c)
    up = _vec3(machine_model.NOZZLE_UP)
    q = max(-1.0, min(1.0, up[0] * a0[0] + up[1] * a0[1] + up[2] * a0[2]))
    theta_q = math.acos(q)
    c = nx * a1[0] + ny * a1[1] + nz * a1[2]
    wx, wy, wz = nx - c * a1[0], ny - c * a1[1], nz - c * a1[2]
    k = c * (a1[0] * a0[0] + a1[1] * a0[1] + a1[2] * a0[2])
    b = wx * a0[0] + wy * a0[1] + wz * a0[2]
    d = ((a1[1] * wz - a1[2] * wy) * a0[0]
         + (a1[2] * wx - a1[0] * wz) * a0[1]
         + (a1[0] * wy - a1[1] * wx) * a0[2])
    r = math.hypot(b, d)
    e = math.radians(float(tol_deg))
    if r <= 1e-12:
        return None
    p_lo = math.cos(min(theta_q + e, math.pi))
    p_hi = math.cos(max(theta_q - e, 0.0))
    lo = max(-1.0, min(1.0, (p_lo - k) / r))
    hi = max(-1.0, min(1.0, (p_hi - k) / r))
    if lo <= -1.0 and hi >= 1.0:
        return None
    psi_deg = math.degrees(math.atan2(d, b))
    acos_lo = math.degrees(math.acos(lo))
    acos_hi = math.degrees(math.acos(hi))
    prev_c = float(prev_c_deg)
    prev_a = float(prev_a_deg)
    n_c = (nx, ny, nz)
    best, best_cost = None, None
    for lo_deg, hi_deg in ((psi_deg + acos_hi, psi_deg + acos_lo),
                           (psi_deg - acos_lo, psi_deg - acos_hi)):
        mid = 0.5 * (lo_deg + hi_deg)
        k_turn = round((prev_c - mid) / 360.0)
        s_lo, s_hi = lo_deg + 360.0 * k_turn, hi_deg + 360.0 * k_turn
        # The greedy step this arc would take -- the clamp `_perp_band_c`
        # applies -- and the base tilt that step forces, derived exactly as
        # the callers derive it so the price is the move they will make.
        c_here = min(max(prev_c, s_lo), s_hi)
        a_raw = _tilt_deg(a1, a0, n_c, c_here)
        cost = (abs(c_here - prev_c)
                + abs(_nearest_turn(a_raw - prev_a)))
        if best_cost is None or cost < best_cost:
            best, best_cost = (s_lo, s_hi), cost
    return best

def _perp_band_c(a1, a0, n_c, prev_c_deg, prev_a_deg, tol_deg):
    """The GREEDY inner (C) reading: the one closest to `prev_c_deg` that keeps
    the collector's perpendicularity error within `tol_deg` -- or `prev_c_deg`
    itself, unmoved, when every C satisfies the band.

    A thin chooser over `_perp_band_interval`, which carries the maths and is
    what a whole-layer router calls instead. The two must not drift apart:
    least-motion IS a clamp of the previous reading into the feasible interval,
    so writing it any other way here would make the greedy walk and the routed
    walk disagree about what is feasible rather than about where to sit in it.

    If `prev_c_deg` already lies inside the band the clamp is a no-op and C is
    reported held still, which is the point.

    `prev_a_deg` is only ever handed on: WHICH of the two IK branches the
    interval describes is priced on both joints there, and that decision has
    to be the same one here and in the router or the two would disagree about
    the feasible set rather than about where to sit in it.
    """
    return _clamp_band(
        _perp_band_interval(a1, a0, n_c, prev_c_deg, prev_a_deg, tol_deg),
        prev_c_deg)


def _clamp_band(band, prev_c_deg):
    """The least-motion reading inside `band`; `prev_c_deg` itself when free."""
    if band is None:
        return float(prev_c_deg)
    lo, hi = band
    return min(max(float(prev_c_deg), lo), hi)

def _five_axis_readings(rotaries, normal, prev_angles, perp_tolerance_deg=0.0,
                        band=_UNSET):
    """(angles, readings) for one point, CONTINUOUS with `prev_angles`.

    `machine_model.solve_rotaries` answers for ONE orientation and says so: its
    reading is in (-180, 180] and it picks the smaller base tilt. Neither is
    right along a path. Of the two branches a two-axis wrist admits, the one
    with the smaller tilt at this point can be the far one from the last point,
    and a spin joint that has turned dozens of times must keep counting --
    Terranova's `V` reaches -12240 degrees in `Fig3D_Wavy.g` (measured:
    39.4 revolutions of travel, and it REVERSES, so its range and
    its travel are different numbers), and wrapping it to a principal range
    would unwind the collector mid-print.

    So both branches are first UNWRAPPED to the turn nearest the previous
    reading -- free and exact, because a rotation is periodic in 360 and the
    transform cannot tell the difference -- and then the branch with the smaller
    total movement wins. `rotary_solutions` is public for exactly this; the
    alternative is `solve_rotaries` per point, which is what makes a five-axis
    post processor flip branches mid-pass.

    `perp_tolerance_deg` is the PERPENDICULARITY TOLERANCE:
    degrees the collector's normal may depart from exactly under the nozzle
    before the inner (collector-spin) joint is made to track it. **Zero, the
    default, is byte-identical to the exact solve above** -- the band walk in
    `_perp_band_c` only runs when the tolerance is positive and there is a
    previous reading to hold near, so nothing above this line is touched at
    0. Positive, it REPLACES the choice this function would otherwise make:
    the inner reading is free to sit still anywhere the error stays inside
    the band, which is exactly where a pass would otherwise whip the
    collector through a wide azimuth to serve a surface that is nearly flat
    under the nozzle (this docstring's own pole case is the E=0 special case
    of the same band -- `_perp_band_c`'s `R <= 1e-12` -- so it is a
    generalisation of the freeze above, not a second policy beside it). The
    base reading is then RE-SOLVED for the chosen inner one, never held at
    its own exact value: deferring a monotonic sweep only forces it to catch
    up in one step, which measured worse peak spin than doing nothing at all.
    """
    import machine_model

    sols = machine_model.rotary_solutions(rotaries, normal)
    if not sols:
        raise machine_model.Unreachable(
            "no rotary reading brings the surface normal %s under the nozzle; "
            "the collector cannot present this point to a fixed vertical jet"
            % ([round(float(c), 6) for c in normal],))

    # THE POLE IS A FREE SPIN, AND TAKING THE NUMBER OFFERED THERE IS THE WHIP.
    # When the surface normal lands parallel to the inner joint's own axis --
    # the apex of a dome, the bottom of a bowl, any point of a surface of
    # revolution where the normal runs along the collector's axis -- turning
    # that joint moves neither the normal nor the point, which sits ON the
    # axis. The orientation constraint is satisfied for EVERY inner reading, so
    # there is a one-parameter family of solutions and `rotary_solutions`
    # returns one arbitrary member of it (its two branches coincide, so its
    # `g > 1e-12` test collapses to a single `s`). Costing that arbitrary
    # number against the previous point is what produced a 180 deg C step in
    # one move on an apex-centred dome, measured: five of them, each
    # exactly -180.000 deg and each at A = 0.000. The sample after the pole
    # offers the continuous branch again, but by then the march has been
    # carried onto the far one and cannot afford to come back.
    #
    # So spend the freedom on continuity: hold the previous reading. The
    # emitted XYZ is derived FROM the angles (`_five_axis_point`), so the
    # commanded point follows the reading we choose and nothing downstream sees
    # an inconsistency -- and perpendicularity, which is the whole reason the
    # collector moves at all, is exact either way.
    #
    # The base joint is deliberately not given the same treatment: its own
    # degeneracy needs `m` parallel to `a0`, which is not what a surface of
    # revolution on a spin table reaches, and inventing a rule for a case no
    # collector here produces would be a guess.
    #
    # `n_c` is the SAME mounted normal `rotary_solutions` built its branches
    # from (`_collector_normal_f` applies the same mount), or this would test a
    # different normal than the one the branches were built from.
    n_c = None
    if prev_angles is not None and len(rotaries) == 2:
        n_c = _collector_normal_f(rotaries, normal)
        if n_c is not None:
            sx, sy, sz = _vec3(rotaries[0].direction())
            if 1.0 - abs(n_c[0] * sx + n_c[1] * sy + n_c[2] * sz) < 1e-12:
                sols = [(prev_angles[0], s[1]) for s in sols]

    best, best_cost = None, None
    for sol in sols:
        if prev_angles is None:
            cand = tuple(float(a) for a in sol)
            cost = sum(abs(a) for a in cand)
        else:
            cand = tuple(p + _nearest_turn(float(a) - p)
                         for a, p in zip(sol, prev_angles))
            cost = sum(abs(a - p) for a, p in zip(cand, prev_angles))
        if best_cost is None or cost < best_cost:
            best, best_cost = cand, cost

    if (perp_tolerance_deg and float(perp_tolerance_deg) > 0.0
            and n_c is not None):
        a1 = _vec3(rotaries[0].direction())
        a0 = _vec3(rotaries[1].direction())
        # `band`, when the whole-layer solve already priced this point against
        # this same previous pose, is that answer -- not a second computation
        # of it.
        if band is _UNSET:
            chosen_c = _perp_band_c(a1, a0, n_c, float(prev_angles[0]),
                                    float(prev_angles[1]),
                                    float(perp_tolerance_deg))
        else:
            chosen_c = _clamp_band(band, prev_angles[0])
        _a_raw = _tilt_deg(a1, a0, n_c, chosen_c)
        chosen_a = prev_angles[1] + _nearest_turn(_a_raw - prev_angles[1])
        best = (chosen_c, chosen_a)

    readings = tuple(j.word_from_angle(a) for j, a in zip(rotaries, best))
    return best, readings

def _five_axis_point(machine, rotaries, point, normal, prev_angles,
                     mount_axial=None, perp_tolerance_deg=0.0):
    """(xyz, readings, angles) -- `machine_model.five_axis_command`, but with the
    solution chosen for CONTINUITY along the path rather than per point.

    The transform is `machine_model`'s own (`rotary_transform`, plus the single
    joint's seat), so this does not carry a second copy of the kinematics. What
    it replaces is only the solution CHOICE, which that module documents as
    belonging to whatever emits the path.

    `mount_axial` is the COLLECTOR's own axial coordinate at its bottom edge,
    from `mew_frame.DepositionFrame.mount_axial`. It is the collector half of
    the machine's two calibrated datums and it reaches the maths only through
    `rotary_transform`, which is also where the picked g-code zero comes off.

    `perp_tolerance_deg` passes straight through to `_five_axis_readings`;
    see its docstring. Zero, the default, is byte-identical to before.
    """
    import numpy as np

    import machine_model

    angles, readings = _five_axis_readings(rotaries, normal, prev_angles,
                                           perp_tolerance_deg)
    return _five_axis_xyz(machine, rotaries, point, angles,
                          mount_axial), readings, angles


def _five_axis_xyz(machine, rotaries, point, angles, mount_axial=None):
    """The machine XYZ that presents `point` when the joints stand at `angles`.

    Split out of `_five_axis_point` so a WHOLE-LAYER solve can choose every
    angle first and place every point afterwards, without a second copy of the
    transform. The transform is still `machine_model`'s own.
    """
    return _five_axis_xyzs(machine, rotaries, [point], [angles], mount_axial)[0]


def _five_axis_xyzs(machine, rotaries, points, angles, mount_axial=None):
    """`_five_axis_xyz` for a whole layer, `(N, 3)`, through ONE batched chain.

    `machine_model.rotary_transforms` is the transform `rotary_transform` is
    the one-row case of, so a layer placed here and a move read back by the
    parser are composed by the same code.
    """
    import numpy as np

    import machine_model

    r, t = machine_model.rotary_transforms(rotaries, angles, mount_axial)
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    g = (r @ pts[:, :, None])[:, :, 0] + t
    if len(rotaries) == 1:
        g = g + rotaries[0].seat()
    free = [i for i, ax in enumerate(machine_model.AXES)
            if machine.axis_side(ax) is None]
    if free:
        bad = np.abs(g[:, free]) > 1e-9
        if bad.any():
            k = int(np.argmax(bad.any(axis=1)))
            i = free[int(np.argmax(bad[k]))]
            raise machine_model.Unreachable(
                "no body drives %s, but the path needs %.4f mm there"
                % (machine_model.AXES[i], g[k, i]))
    return g

ROUTE_ITERATIONS = 4000
ROUTE_SETTLE_DEG = 1e-9


def _route_band_c(greedy_c, bands, iterations=ROUTE_ITERATIONS,
                  settle=ROUTE_SETTLE_DEG):
    """Route one layer's inner-joint (C) trajectory through the feasible tube.

    `greedy_c` is the least-motion walk -- a feasible starting trajectory, and
    the thing this replaces. `bands[i]` is that point's feasible interval in
    the SAME unwrapped degrees: `(lo, hi)`, `(-inf, +inf)` where every C
    satisfies the tolerance, or `None` where the point is not routable at all
    and must stand where the greedy walk put it.

    WHY A SECOND PASS EXISTS. The greedy walk is myopic by construction: it
    takes the nearest feasible C at each point, so it can only react to the
    tube closing, never anticipate it. Every surface has locally-flat stretches
    where the tube is wide; a myopic walk sits wherever it happens to be while
    the tube is wide and then SNAPS when the tube closes. Measured on a 35 mm
    dome: at the 2 deg default the band already holds C
    perfectly still across the apex, and all 524-594 deg of remaining travel
    happens BEYOND 1.2 mm from the spin axis, peaking at 1.30 and 1.41 mm out
    -- exactly where a 35 mm dome's tilt reaches 2 deg and the tube closes. The
    whip is a deferred catch-up, not a singularity, which is why re-tuning the
    greedy cannot reach it and a whole-path solve can.

    THE ROUTING is iterated smoothing with projection onto the feasible set:
    replace each interior point by the mean of its neighbours, then clamp it
    back into its own interval, until nothing moves. Deliberately simple. It is
    not claimed optimal -- only that it cut peak spin at IDENTICAL tolerance,
    travel and feasible set, which is the whole finding: the tolerance was
    never the problem, the route through it was. Anything cleverer (a QP, a
    min-peak-rate formulation) must be MEASURED against this rather than
    assumed better.

    The ENDPOINTS are pinned. Point 0 is the joint's continuity with the layer
    before and moving it would break the unwrap chain; the last point is
    pinned for symmetry. Smoothing only ever moves the interior.

    Jacobi and not Gauss-Seidel -- every point reads the PREVIOUS iterate, per
    the measured baseline. Sweeping in place converges faster and to a slightly
    different trajectory, which would make that measurement unreproducible.
    """
    import numpy as np

    n = len(greedy_c)
    if n < 3:
        return list(greedy_c)
    s = np.asarray(greedy_c, float)
    lo = np.empty(n)
    hi = np.empty(n)
    for i, band in enumerate(bands):
        if band is None:
            lo[i] = hi[i] = s[i]
        else:
            lo[i], hi[i] = band
    for _ in range(int(iterations)):
        t = s.copy()
        t[1:-1] = 0.5 * (s[:-2] + s[2:])
        np.clip(t, lo, hi, out=t)
        t[0], t[-1] = s[0], s[-1]
        moved = float(np.max(np.abs(t - s)))
        s = t
        if moved <= settle:
            break
    return [float(v) for v in s]


ROUTE_GRID_DEG = 0.25
ROUTE_GRID_SPAN_DEG = 540.0

# WHEN THE LEAN ROUTE IS WORTH TAKING. Least-total-travel and least-peak-rate
# genuinely trade against each other -- the travel optimum sits on the feasible
# boundary everywhere and peaks at 742 deg/s against the smoothed route's 89.7
# -- so pass 3 is adopted on a measured balance rather than whenever it exists.
# Measured across five collectors at the 2 deg default:
#
#   dome R35   1142.4 -> 1005.2 deg  (-12.0%), peak 89.7 -> 93.9  (+4.6%)
#   dome R10   2476.3 -> 2465.2 deg  ( -0.4%)
#   torus      150.3  -> 150.3       ( 0.0% at a fine grid)
#   cone 15    1365.2 -> 1365.2      ( 0.0% at a fine grid)
#
# So the gain is real on a shallow dome and absent elsewhere, and the guard's
# job is to take it where it exists without paying for it where it does not.
# A 2% floor sits well above grid quantisation, which is what made a coarse
# grid look like a 2.6% REGRESSION on the torus until the grid was refined.
ROUTE_LEAN_MIN_GAIN = 0.02
ROUTE_LEAN_MAX_PEAK_RISE = 0.10

# Pass 3 costs about 3.5 ms per point, and the work is genuinely grid x window
# element operations -- not call overhead, which was checked: a sliding-window
# form of `_capped_envelope` is bit-identical (1.1e-13) and exactly as fast.
# So a very large layer is bounded here rather than left to run for minutes.
# 4000 points is roughly 15 s for one layer; beyond it the layer keeps the
# smoothed route, which is a weaker result and never a wrong one.
ROUTE_LEAN_MAX_POINTS = 4000


def _band_sinusoid(rotaries, normals):
    """(K, R, psi) per point and the constant theta_q.

    The band's own decomposition, lifted out of `_perp_band_interval` so a
    whole-path solver can build every point's error curve at once:

        p(C) = K + R cos(C - psi)      err(C) = |arccos(p(C)) - arccos(q)|

    A degenerate normal contributes a flat zero-error curve rather than an
    exception, because a single bad sample must not decide a whole layer.
    """
    import numpy as np

    import machine_model

    a1 = np.asarray(rotaries[0].direction(), float)
    a0 = np.asarray(rotaries[1].direction(), float)
    q = float(np.clip(np.dot(machine_model.NOZZLE_UP, a0), -1.0, 1.0))
    theta_q = math.acos(q)
    # ONE ARRAY PASS over the layer -- the same decomposition, per row.
    nrm = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
    nn = np.sqrt((nrm * nrm).sum(axis=1))
    ok = nn > 1e-12
    n_c = (nrm / np.where(ok, nn, 1.0)[:, None]) @ np.asarray(
        machine_model.collector_mount(tuple(rotaries))).T
    c = n_c @ a1
    w = n_c - c[:, None] * a1
    K = c * float(np.dot(a1, a0))
    b = w @ a0
    d = np.cross(a1, w) @ a0
    R = np.hypot(b, d)
    P = np.arctan2(d, b)
    K = np.where(ok, K, math.cos(theta_q))
    R = np.where(ok, R, 0.0)
    P = np.where(ok, P, 0.0)
    return K[:, None], R[:, None], P[:, None], theta_q

def _band_err_deg(K, R, P, theta_q, grid_deg):
    """|err| in degrees for every (point, C) on `grid_deg`."""
    import numpy as np

    p = K + R * np.cos(np.radians(grid_deg)[None, :] - P)
    return np.degrees(np.abs(np.arccos(np.clip(p, -1.0, 1.0)) - theta_q))


def _capped_envelope(f, step, cells):
    """min over j with |i-j| <= `cells` of `f[j] + |i-j| * step`.

    The L1 lower envelope of the cost-to-go, restricted to a window -- which
    is what a per-step rate cap does to the dynamic program.

    THE WINDOW IS NOT SMALL ON EVERY COLLECTOR. It is `rate_cap * ds / speed`
    in grid cells, so it grows with how hard the surface makes C turn: 2-19
    cells on the test block at up x / y, 442-997 at up z, and a fold over the
    shifts is O(grid * cells). That fold was 72 s of an 88 s generate on the
    block at up z and 325 s of 355 s on the r10 dome at up y. Above `_DILATE_FOLD_MAX` it is
    therefore two sliding-window minima (`_window_min`, O(grid) at any
    window): `f[j] - j*step` over the window's left half plus `i*step`, and
    `f[j] + j*step` over its right half minus `i*step`.

    BIT-IDENTICAL, NOT CLOSE. That rewrite is exact only where every sum is:
    finite costs that are integer multiples of a power-of-two `step`, well
    inside 2**53. A cost-to-go built from the pinned anchor (0) by adding
    `|d| * step` always is, so the solver takes the fast path; a start that
    fell back to `|grid - start_c|` is not, and keeps the fold.
    """
    import numpy as np

    n = f.size
    cells = int(min(max(cells, 0), n - 1))
    if cells <= 0:
        return f.copy()
    if cells > _DILATE_FOLD_MAX and _envelope_exact(f, step):
        j = np.arange(n) * step
        pad = np.full(cells, np.inf)
        left = _window_min(np.concatenate([pad, f - j]), cells + 1) + j
        right = _window_min(np.concatenate([f + j, pad]), cells + 1) - j
        return np.minimum(left, right)
    pad = np.full(cells, np.inf)
    a = np.concatenate([pad, f, pad])
    best = np.full(n, np.inf)
    for d in range(-cells, cells + 1):
        np.minimum(best, a[cells + d:cells + d + n] + abs(d) * step, out=best)
    return best


def _envelope_exact(f, step):
    """Is every sum `_capped_envelope`'s fast path forms exact in floating point?"""
    import numpy as np

    if math.frexp(step)[0] != 0.5 or f.size >= 2 ** 20:
        return False
    fin = f[np.isfinite(f)]
    if fin.size == 0:
        return True
    q = fin / step
    return bool(np.all(q == np.rint(q)) and np.abs(q).max() < 2.0 ** 40)


def _window_min(a, w):
    """`min(a[i:i + w])` for every `i` in `range(a.size - w + 1)`.

    van Herk / Gil-Werman: running minima forward and backward inside blocks
    of `w`, so any full-width window is one suffix and one prefix. O(a.size).
    """
    import numpy as np

    m = a.size
    nb = -(-m // w)
    b = np.concatenate([a, np.full(nb * w - m, np.inf)]).reshape(nb, w)
    pre = np.minimum.accumulate(b, axis=1).ravel()
    suf = np.minimum.accumulate(b[:, ::-1], axis=1)[:, ::-1].ravel()
    k = m - w + 1
    return np.minimum(suf[:k], pre[w - 1:w - 1 + k])


# HOW FAR BELOW THE SMOOTHED ROUTE'S OWN PEAK THE TRUE FLOOR HAS TO SIT before
# the minimax cap replaces it, how much slack the replacement is given, and how
# much it then has to actually win. The trigger exists so that a layer where
# smoothing already reaches the floor -- which is most of them -- takes the
# plain path, cap and gates included.
ROUTE_MINIMAX_TRIGGER = 0.90
ROUTE_MINIMAX_SLACK = 1.02
ROUTE_MINIMAX_MIN_DROP = 0.10
ROUTE_MINIMAX_ITERS = 14


def _route_grid(rotaries, normals, start_c, tol_deg):
    """`(ok, grid, step)` -- the FULL feasible set, as a (point, C) boolean.

    Shared by the two whole-path solvers so they cannot disagree about what is
    feasible. `ok[i, j]` is "grid C `j` keeps point `i` inside the tolerance",
    and because it is built from the error curve itself rather than from an
    interval, it structurally contains BOTH IK branches and every winding in
    reach -- which is the difference between this and the per-point
    `_perp_band_interval`, that returns one greedily-chosen arc of the two.
    """
    import numpy as np

    K, R, P, theta_q = _band_sinusoid(rotaries, normals)
    step = float(ROUTE_GRID_DEG)
    grid = np.arange(float(start_c) - ROUTE_GRID_SPAN_DEG,
                     float(start_c) + ROUTE_GRID_SPAN_DEG + step, step)
    ok = _band_err_deg(K, R, P, theta_q, grid) <= float(tol_deg) + 1e-9
    return ok, grid, step


# Below this window the shift-fold is faster than the prefix sum; above it the
# prefix sum wins by up to 60x. Measured, with the two bit-identical on 300
# random cases and a deliberately wrong window NOT matching.
_DILATE_FOLD_MAX = 16


def _dilate(reach, cells):
    """Reachability spread by `cells` either way -- a sliding-window OR.

    O(grid) through a prefix sum whatever the window, which is what makes the
    cap search affordable enough to run on every layer: the window is
    `rate_cap * ds / speed` in cells and goes to thousands on a long step,
    where a fold over the shifts is O(grid * cells).
    """
    import numpy as np

    cells = int(cells)
    if cells <= 0:
        return reach.copy()
    if cells <= _DILATE_FOLD_MAX:
        pad = np.zeros(cells, bool)
        a = np.concatenate([pad, reach, pad])
        out = np.zeros_like(reach)
        for d in range(-cells, cells + 1):
            out |= a[cells + d:cells + d + reach.size]
        return out
    n = reach.size
    cells = min(cells, n)
    c = np.concatenate(([0], np.cumsum(reach.astype(np.int64))))
    i = np.arange(n)
    return (c[np.minimum(i + cells + 1, n)] - c[np.maximum(i - cells, 0)]) > 0


def _route_cap_feasible(ok, grid, arc, start_c, cap, speed_mm_s, step):
    """Is ANY trajectory inside `ok` reachable from `start_c` under `cap`?

    The forward half of `_route_min_travel_c` with the costs dropped -- a
    reachable SET per point rather than a cost-to-go -- so the two agree about
    feasibility by construction. FLOOR here and not ceil: this answers "can a
    grid trajectory hold this cap", and being generous would report a cap the
    returned route cannot actually keep. `_route_min_travel_c`'s own ceil-plus-
    one is the opposite need and is right there for its own reason.
    """
    import numpy as np

    reach = ok[0] & (np.abs(grid - float(start_c)) <= 0.5 * step)
    if not reach.any():
        reach = ok[0].copy()
    if not reach.any():
        return False
    for i in range(1, ok.shape[0]):
        ds = float(arc[i] - arc[i - 1]) if i < arc.size else 0.0
        if cap is None or ds <= 1e-12 or speed_mm_s <= 0.0:
            cells = grid.size
        else:
            cells = int(math.floor((cap * ds / speed_mm_s) / step))
        reach = _dilate(reach, cells) & ok[i]
        if not reach.any():
            return False
    return True


def _route_minimax_cap(rotaries, normals, arc, start_c, tol_deg, speed_mm_s,
                       seed_cap):
    """The LEAST peak rate any feasible trajectory can hold, or `None`.

    WHY THIS EXISTS. Capping pass 3 at pass 2's own peak would be
    self-referential and silently wrong exactly when pass 2
    fails. Pass 2 smooths inside the tube it is handed, and that tube is
    `_perp_band_interval`'s ONE greedily-chosen arc per point, picked against
    wherever the myopic walk stood -- so when the walk snaps branch mid-pass
    the tube carries a 180 deg discontinuity that clipped smoothing cannot
    cross, and pass 2 then achieves nothing at all. Measured on the dome R10 /
    60 deg design: layers 1 and 4 smooth 1592.6 -> 603.9 deg/s while layers 2 and 3 go
    1347.8 -> 1347.8, untouched. Capping at 1347.8 then asks pass 3 to beat a
    number the defect set, and the true floor is 598.2.

    So the cap is SOLVED instead of inherited, over `_route_grid`'s full
    two-arc set: bisect the smallest per-step rate for which a trajectory
    still exists. Feasibility is monotone in the cap, which is what makes a
    bisection valid -- a route holding a cap holds every larger one.

    `seed_cap` bounds the search above and is what pass 2 achieved; a floor
    cannot be worse than an achieved route, so the seed is feasible by
    construction and a `None` return means the caller's own trajectory left
    the grid, in which case it keeps what it had.
    """
    n = len(normals)
    if n < 3 or n > ROUTE_LEAN_MAX_POINTS or not (seed_cap > 0.0):
        return None
    import numpy as np

    ok, grid, step = _route_grid(rotaries, normals, start_c, tol_deg)
    arc = np.asarray(arc, float)
    hi = float(seed_cap)
    if not _route_cap_feasible(ok, grid, arc, start_c, hi, speed_mm_s, step):
        return None
    lo = 0.0
    for _ in range(int(ROUTE_MINIMAX_ITERS)):
        mid = 0.5 * (lo + hi)
        if _route_cap_feasible(ok, grid, arc, start_c, mid, speed_mm_s, step):
            hi = mid
        else:
            lo = mid
    return hi


def _route_min_travel_c(rotaries, normals, arc, start_c, tol_deg, rate_cap,
                        speed_mm_s):
    """Least TOTAL inner-joint travel through the tube, subject to `rate_cap`.

    Returns the C trajectory, or `None` when no trajectory satisfies both the
    tolerance and the cap -- in which case the caller keeps what it had.

    WHAT THIS IS FOR, and why it is not `_route_band_c` with a different cost.
    `_route_band_c` smooths, which minimises CURVATURE: it spreads motion out,
    which is why it cut the peak 4x and why the joint afterwards never stops.
    It cannot reduce the TOTAL, because the total was already fixed when the
    greedy walk picked which of the two IK arcs, and which winding, to unwrap
    onto. That choice is where the avoidable spin lives: measured on a 35 mm
    dome at the 2 deg default, the walk spends 1142.4 deg where 858.8 is
    reachable.

    So this solves over ABSOLUTE C rather than inside a chosen arc. The
    feasible set at each point is every C whose error is inside the tolerance,
    which structurally contains both IK branches, every winding in reach, and
    the pole's full circle -- the three policies `_five_axis_readings` runs
    separately are one set here, which is what makes the result surface-
    agnostic rather than dome-shaped.

    THE CAP IS NOT OPTIONAL, and this is the part that surprised. Minimising
    total travel alone produces a route that sits ON the feasible boundary at
    every point -- maximally deferred, zero slack in the direction it will
    later need -- and its peak is 660-742 deg/s, worse than the greedy walk's
    343. Total travel and peak rate genuinely trade against each other. Nor can
    the two be composed: smoothing a travel-optimal route changes nothing at
    all, because a boundary-hugging trajectory is already a fixed point of
    smooth-and-project (measured: 858.8 deg and 660.5 deg/s before, identical
    after). The cap is how the trade is priced, and it is a RATE, converted per
    step through that step's own arc length -- never a Cartesian XYZ distance,
    which understates the whip near the pole by up to 7x.

    The grid is a real approximation and the only one here: an endpoint-only
    candidate set is exact for the UNCAPPED problem and measurably better
    (819.1 deg against a 0.25 deg grid's 861.8), but wrong once the cap couples
    the points -- the optimum then sits strictly inside an arc, and an endpoint
    solver calls a 12 deg step infeasible while `_route_band_c` already
    achieves 8.42. Feasibility is tested AT grid points, so every returned C
    satisfies the tolerance exactly; the grid costs a little travel, never
    accuracy.
    """
    import numpy as np

    n = len(normals)
    if n < 3 or n > ROUTE_LEAN_MAX_POINTS:
        return None
    ok, grid, step = _route_grid(rotaries, normals, start_c, tol_deg)
    arc = np.asarray(arc, float)

    # Point 0 is the layer's continuity with the layer before, so it is pinned
    # to where the walk already stands -- the same anchor `_route_band_c` pins.
    f = np.where(ok[0] & (np.abs(grid - float(start_c)) <= 0.5 * step),
                 0.0, np.inf)
    if not np.isfinite(f).any():
        f = np.where(ok[0], np.abs(grid - float(start_c)), np.inf)
    if not np.isfinite(f).any():
        return None
    keep = [f]
    for i in range(1, n):
        ds = float(arc[i] - arc[i - 1]) if i < arc.size else 0.0
        if rate_cap is None or ds <= 1e-12 or speed_mm_s <= 0.0:
            cells = grid.size
        else:
            # CEIL PLUS ONE CELL, not floor. The cap this is given is the peak
            # of a REAL-VALUED trajectory that must remain reachable here, and
            # two roundings stand in the way: the window in cells truncates,
            # and snapping that trajectory to the grid can cost another cell.
            # Flooring made the solver infeasible under its own seed's peak in
            # five of eight measured layers, and in a sixth it returned a
            # WORSE total than the seed -- the tell that a minimiser is being
            # handed a smaller feasible set than the one it was asked about.
            # Being a cell generous is safe because the peak of whatever comes
            # back is re-measured against the cap before it is adopted.
            cells = int(math.ceil((rate_cap * ds / speed_mm_s) / step)) + 1
        f = _capped_envelope(f, step, cells)
        f = np.where(ok[i], f, np.inf)
        if not np.isfinite(f).any():
            return None
        keep.append(f)

    cs = [0.0] * n
    cs[-1] = float(grid[int(np.nanargmin(keep[-1]))])
    for i in range(n - 2, -1, -1):
        ds = float(arc[i + 1] - arc[i]) if i + 1 < arc.size else 0.0
        cost = keep[i] + np.abs(grid - cs[i + 1])
        if rate_cap is not None and ds > 1e-12 and speed_mm_s > 0.0:
            reach = rate_cap * ds / speed_mm_s + 2.0 * step
            cost = np.where(np.abs(grid - cs[i + 1]) <= reach, cost, np.inf)
        if not np.isfinite(cost).any():
            return None
        cs[i] = float(grid[int(np.nanargmin(cost))])
    if not _route_flips_tilt(rotaries, normals, cs):
        return cs

    # PRICED ON BOTH JOINTS, and only where it matters. The solve above costs
    # C alone, so where two routes spend the same C it is free to take the one
    # that swings the base joint from leaning one way to leaning the other --
    # measured on `test collector.step` at up z: at a crease the
    # smoothed route took C -80.26 with A +17.8 and this solve took +80.33
    # with A -17.8, the same C travel for 37 deg of tilt thrown in, twice per
    # loop. `_perp_band_interval` already prices its branch pick on
    # `|dC| + |dA|`; this is the same rule, here. A route with no such flip is
    # returned above untouched, which keeps every file that never flipped
    # byte-identical.
    def cells(ds):
        if rate_cap is None or ds <= 1e-12 or speed_mm_s <= 0.0:
            return grid.size
        return int(math.ceil((rate_cap * ds / speed_mm_s) / step)) + 1

    def reach(ds):
        if rate_cap is not None and ds > 1e-12 and speed_mm_s > 0.0:
            return rate_cap * ds / speed_mm_s + 2.0 * step
        return np.inf

    priced = _route_dp(rotaries, normals, arc, start_c, grid, step,
                       lambda i: np.where(ok[i], 0.0, np.inf), cells, reach,
                       anchor_fallback=True)
    return cs if priced is None else priced


# A sign change of the base joint is a BRANCH FLIP only when it leans at least
# this far on BOTH sides. A path sweeping through level crosses zero
# continuously, a degree or so either side of it, and the band lets a nearly
# flat stretch wander +/- its tolerance; neither is a flip, and pricing them
# re-routed the block at up x -- a design with no edge near the axis -- by one
# degree of A in 6105. The reported flip leans 18 deg each way.
ROUTE_FLIP_MIN_DEG = 5.0


def _route_flips_tilt(rotaries, normals, cs):
    """Does the base joint change SIGN along `cs` while leaning at least
    `ROUTE_FLIP_MIN_DEG` on both sides? The trigger for pricing it."""
    a1 = _vec3(rotaries[0].direction())
    a0 = _vec3(rotaries[1].direction())
    prev = None
    for nm, c in zip(normals, cs):
        n_c = _collector_normal_f(rotaries, nm)
        if n_c is None:
            continue
        t = _tilt_deg(a1, a0, n_c, c)
        if (prev is not None and (t >= 0.0) != (prev >= 0.0)
                and min(abs(t), abs(prev)) >= ROUTE_FLIP_MIN_DEG):
            return True
        prev = t
    return False


_TILT_TRIG = {}


def _tilt_row(rotaries, normal, grid_deg):
    """`_tilt_deg` at every C of `grid_deg` at once.

    `m(C) = c a1 + cos(C) w + sin(C) (a1 x w)`, and the signed angle about `a0`
    carrying `m` onto NOZZLE_UP is `atan2(m . (vp x a0), m . vp)` with `vp` the
    nozzle direction projected off `a0` -- both arguments sinusoids in C, so a
    point is six scalars against one cached cos/sin of the grid. Agrees with
    `_tilt_deg` to 7e-15 deg. A normal
    along `a0` has no projection, both arguments vanish, and `atan2(0, 0)` is
    the 0 `_signed_angle` returns there.
    """
    import numpy as np

    import machine_model

    got = _TILT_TRIG.get(id(grid_deg))
    if got is None or got[0] is not grid_deg:
        th = np.radians(grid_deg)
        got = (grid_deg, np.cos(th), np.sin(th))
        _TILT_TRIG.clear()
        _TILT_TRIG[id(grid_deg)] = got
    _, co, si = got
    n_c = _collector_normal_f(rotaries, normal)
    if n_c is None:
        return np.zeros(grid_deg.size)
    a1 = np.asarray(_vec3(rotaries[0].direction()))
    a1 = a1 / np.linalg.norm(a1)
    a0 = np.asarray(_vec3(rotaries[1].direction()))
    a0 = a0 / np.linalg.norm(a0)
    n_c = np.asarray(n_c)
    c = float(n_c @ a1)
    w = n_c - c * a1
    u = np.cross(a1, w)
    up = np.asarray(machine_model.NOZZLE_UP, float)
    vp = up - float(up @ a0) * a0
    k = np.cross(vp, a0)
    cr = c * float(a1 @ k) + float(w @ k) * co + float(u @ k) * si
    dt = c * float(a1 @ vp) + float(w @ vp) * co + float(u @ vp) * si
    return np.degrees(np.arctan2(cr, dt))


def _route_dp(rotaries, normals, arc, start_c, grid, step, node, cells, reach,
              anchor_fallback=False):
    """Least `sum(node) + |dC| + |dA at a sign flip|` through `grid`.

    The dynamic program `_route_min_travel_c` runs, generalised in the two
    ways its callers need: a per-point `node(i)` cost over the grid (0/inf is
    a feasible set, a finite cost is a penalty), and the BASE joint priced
    where it changes sign. A flip from `+a` to `-b` moves that joint `a + b`,
    which is separable into one term per side, so the program stays one
    lower-envelope per branch pair instead of growing a state; a change that
    keeps its sign is continuous and not priced. `cells(ds)` is the step's
    window in grid cells and `reach(ds)` the same bound in degrees for the
    backward pass. Every cost is a multiple of `step` (tilts are rounded to
    it), which keeps `_capped_envelope` on its exact fast path.
    """
    import numpy as np

    n = len(normals)
    f = np.where(np.abs(grid - float(start_c)) <= 0.5 * step, 0.0, np.inf)
    f = f + node(0)
    if not np.isfinite(f).any() and anchor_fallback:
        f = np.where(np.isfinite(node(0)), np.abs(grid - float(start_c)),
                     np.inf)
    if not np.isfinite(f).any():
        return None
    keep = [f]
    t = _tilt_row(rotaries, normals[0], grid)
    for i in range(1, n):
        ds = float(arc[i] - arc[i - 1]) if i < arc.size else 0.0
        w = cells(ds)
        tn = _tilt_row(rotaries, normals[i], grid)
        s, sn = t >= 0.0, tn >= 0.0
        aq = step * np.rint(np.abs(t) / step)
        aqn = step * np.rint(np.abs(tn) / step)
        hp = np.where(s, f, np.inf)
        hm = np.where(s, np.inf, f)
        g = np.where(
            sn,
            np.minimum(_capped_envelope(hp, step, w),
                       _capped_envelope(hm + aq, step, w) + aqn),
            np.minimum(_capped_envelope(hm, step, w),
                       _capped_envelope(hp + aq, step, w) + aqn))
        f = g + node(i)
        if not np.isfinite(f).any():
            return None
        keep.append(f)
        t = tn
    cs = [0.0] * n
    j = int(np.nanargmin(keep[-1]))
    cs[-1] = float(grid[j])
    t_next = float(_tilt_row(rotaries, normals[-1], grid)[j])
    for i in range(n - 2, -1, -1):
        ds = float(arc[i + 1] - arc[i]) if i + 1 < arc.size else 0.0
        t = _tilt_row(rotaries, normals[i], grid)
        cost = keep[i] + np.abs(grid - cs[i + 1])
        flip = (t >= 0.0) != (t_next >= 0.0)
        cost = cost + np.where(
            flip, step * np.rint(np.abs(t) / step)
            + step * round(abs(t_next) / step), 0.0)
        cost = np.where(np.abs(grid - cs[i + 1]) <= reach(ds), cost, np.inf)
        if not np.isfinite(cost).any():
            return None
        j = int(np.nanargmin(cost))
        cs[i] = float(grid[j])
        t_next = float(t[j])
    return cs


# THE WEIGHT OF ONE DEGREE OF PERPENDICULARITY OVERRUN, in degrees of spin
# travel, inside the spin-capped route. Large on purpose: the tolerance is the
# user's and travel is not, so the route spends any amount of travel to save
# overrun and only breaks ties between equal overruns on travel.
SPIN_CAP_OVERRUN_WEIGHT = 1000.0


def _route_spin_cap_c(rotaries, normals, arc, start_c, tol_deg, cap_deg_s,
                      speed_mm_s, around):
    """The C trajectory that never turns faster than `cap_deg_s`, leaving the
    perpendicularity tolerance as little as that allows. `None` if none fits.

    WHY IT EXISTS. Every other pass here keeps the tolerance and lets the rate
    fall where it must. Where the surface normal is DISCONTINUOUS between two
    samples -- a crease, a sharp edge -- no trajectory inside the tolerance
    avoids a step, and near the spin axis that step is tens of degrees in one
    move: on `test collector.step` at up z, whose top is four cylinders meeting
    along the diagonals, 124 moves turned C by 45-72 deg at up to 3245 deg/s.
    Missing perpendicularity for a brief arc on the crease is
    acceptable, and better than a collector jittering around, which on a real
    rig would break the jet. So the cap is hard and the tolerance is the thing spent -- by
    `SPIN_CAP_OVERRUN_WEIGHT` per degree per sample, which makes the overrun
    as small and as short as the cap permits.

    The grid is `_route_grid`'s step, anchored ON `start_c` so point 0 stays
    exactly where the layer before left the joint, and spans the trajectory
    being replaced (`around`) plus a turn either way. A step's window is
    FLOORED, so every returned step keeps the cap; a zero-length step holds.
    Every C is admissible -- the overrun is a cost, not a wall -- and the base
    joint is priced at a sign flip as in `_route_dp`.
    """
    import numpy as np

    step = float(ROUTE_GRID_DEG)
    lo = min(min(around), float(start_c)) - 360.0
    hi = max(max(around), float(start_c)) + 360.0
    grid = float(start_c) + step * np.arange(
        math.floor((lo - start_c) / step), math.ceil((hi - start_c) / step) + 1)
    K, R, P, theta_q = _band_sinusoid(rotaries, normals)
    arc = np.asarray(arc, float)
    tol = float(tol_deg)

    def node(i):
        e = _band_err_deg(K[i:i + 1], R[i:i + 1], P[i:i + 1], theta_q, grid)[0]
        over = np.maximum(e - tol - 1e-9, 0.0)
        return step * np.rint(SPIN_CAP_OVERRUN_WEIGHT * over / step)

    def cells(ds):
        if ds <= 1e-12:
            return 0
        return int(math.floor((cap_deg_s * ds / speed_mm_s) / step + 1e-9))

    def reach(ds):
        return cap_deg_s * max(ds, 0.0) / speed_mm_s + 1e-9

    return _route_dp(rotaries, normals, arc, start_c, grid, step, node, cells,
                     reach)


def _perp_overrun_deg(rotaries, normals, cs, tol_deg):
    """Per point, degrees the collector's normal sits OUTSIDE the tolerance
    once the inner joint stands at `cs` and the base joint is re-solved -- the
    band's own error measure (`_band_sinusoid`), 0 inside it."""
    import numpy as np

    K, R, P, theta_q = _band_sinusoid(rotaries, normals)
    c = np.radians(np.asarray(cs, float))[:, None]
    p = np.clip(K + R * np.cos(c - P), -1.0, 1.0)
    err = np.degrees(np.abs(np.arccos(p) - theta_q))[:, 0]
    return np.maximum(err - float(tol_deg), 0.0)


def _peak_rate(cs, arc, speed_mm_s):
    """Peak |dC| per second along a trajectory, timed on COLLECTOR ARC."""
    import numpy as np

    c = np.asarray(cs, float)
    a = np.asarray(arc, float)
    peak = 0.0
    for i in range(c.size - 1):
        ds = float(a[i + 1] - a[i]) if i + 1 < a.size else 0.0
        if ds > 1e-9:
            peak = max(peak, abs(float(c[i + 1] - c[i])) * speed_mm_s / ds)
    return peak


def _five_axis_layer(machine, rotaries, pts, nrms, prev_angles,
                     mount_axial=None, perp_tolerance_deg=0.0, route=True,
                     arc=None, speed_mm_s=0.0, spin_cap_deg_s=0.0):
    """One layer's ((xyz, readings) per point, final angles) -- the routed
    solve below, then held under `spin_cap_deg_s` where it is set.

    THE SPIN CAP IS A LAST PASS, NOT A REPLACEMENT. Zero is off. Set, a layer
    whose routed inner joint already keeps it is returned untouched, so a
    design that never crosses an edge near the spin axis writes the same file
    with the cap on or off; only a layer that breaks it is re-routed, by
    `_route_spin_cap_c`, with the base joint re-solved for the new inner angle
    exactly as the routed passes do. The rate is timed on collector arc at
    `speed_mm_s`, the same clock `_peak_rate` and the spin report use.
    """
    cmds, prev = _five_axis_layer_routed(
        machine, rotaries, pts, nrms, prev_angles, mount_axial,
        perp_tolerance_deg, route, arc, speed_mm_s)
    cap = float(spin_cap_deg_s or 0.0)
    if (cap <= 0.0 or arc is None or not speed_mm_s > 0.0
            or len(rotaries) != 2 or len(nrms) < 3):
        return cmds, prev
    cs = [rotaries[0].angle_from_word(r[0]) for _, r in cmds]
    if _peak_rate(cs, arc, speed_mm_s) <= cap * (1.0 + 1e-9):
        return cmds, prev
    capped = _route_spin_cap_c(rotaries, nrms, arc, cs[0],
                               float(perp_tolerance_deg or 0.0), cap,
                               speed_mm_s, cs)
    if capped is None:
        return cmds, prev
    a1 = _vec3(rotaries[0].direction())
    a0 = _vec3(rotaries[1].direction())
    angles = [(cs[0], rotaries[1].angle_from_word(cmds[0][1][1]))]
    prev = angles[0]
    for i in range(1, len(nrms)):
        n_c = _collector_normal_f(rotaries, nrms[i])
        c = capped[i]
        if n_c is None:
            a = prev[1]
        else:
            a = prev[1] + _nearest_turn(_tilt_deg(a1, a0, n_c, c) - prev[1])
        prev = (c, a)
        angles.append(prev)
    return _layer_cmds(machine, rotaries, pts, angles, mount_axial), prev


def _five_axis_layer_routed(machine, rotaries, pts, nrms, prev_angles,
                            mount_axial=None, perp_tolerance_deg=0.0,
                            route=True, arc=None, speed_mm_s=0.0):
    """One layer's ((xyz, readings) per point, final angles), solved WHOLE.

    Pass 1 walks the layer greedily -- exactly `_five_axis_point`, point by
    point -- and collects each point's feasible interval alongside. Pass 2
    routes one trajectory through those intervals (`_route_band_c`), which
    minimises curvature and so cuts the PEAK. Pass 3, given `arc` and a speed,
    re-solves the whole layer under a rate cap (`_route_min_travel_c`); it is
    adopted only on evidence. Pass 4 re-solves the BASE joint for the chosen
    inner one and places every point.

    THE CAP IS PASS 2'S OWN PEAK, EXCEPT WHERE THAT PEAK IS THE DEFECT.
    No axis velocity limit exists to read -- the rig's `printer.cfg` carries no
    rotary section, and a plausible C limit spans 30-1800 deg/s on reduction
    ratio alone -- so a number cannot be adopted, and capping at what pass 2
    achieves needs none. That was right as far as it went, and it is
    self-referential: pass 2 smooths inside the tube pass 1 handed it, and that
    tube is one greedily-chosen arc of the two per point, so where the myopic
    walk snaps branch mid-pass the tube carries a 180 deg discontinuity that
    clipped smoothing cannot cross. Pass 2 then achieves NOTHING and the cap
    becomes the whip itself. Measured on a dome R10 / 60 deg design
    (`probe_pass3_adoption.py`): layers 1 and 4 smooth 1592.6 -> 603.9 deg/s,
    layers 2 and 3 go 1347.8 -> 1347.8, and the true floor is 598.2. The 90 deg
    rotation tested before it never showed this, because 90 is a symmetry of
    the hatch set and every layer re-enters in the same state.

    So `_route_minimax_cap` SOLVES the floor over the full two-arc set, and
    when it sits at least 10% under pass 2's peak the cap becomes the floor and
    pass 3 is adopted on PEAK rather than on travel. Above the trigger --
    where smoothing already reaches the floor -- the cap, the gates and the emitted
    file are unchanged.
    Measured at 2 deg: pass 2 spends 1142.4 deg peaking at 89.7 deg/s on a
    35 mm dome, pass 3 spends 1004.5 deg at the same peak, both untouched.

    **Tolerance 0 never reaches pass 2 and is byte-identical to the per-point
    walk**, the same property `_five_axis_readings` has and for the same
    reason: there is no band at 0, so there is no tube to route through and
    pass 1's answer is the answer. `test_five_axis_gcode` asserts it.

    The base joint is RE-SOLVED for the chosen inner angle and never held at
    its own greedy value. Measured: a policy that held both
    made peak spin WORSE, 1363 deg/s against 682, while saving no travel at
    all, because deferring a monotonic sweep forces it to catch up in one step.

    `route=False` returns pass 1 alone, which is what a caller comparing the
    two wants; it is not a mode anything ships.
    """
    banding = (bool(perp_tolerance_deg) and float(perp_tolerance_deg) > 0.0
               and len(rotaries) == 2)
    a1 = a0 = None
    if banding:
        a1 = _vec3(rotaries[0].direction())
        a0 = _vec3(rotaries[1].direction())

    # ANGLES FIRST, PLACEMENT ONCE. Every pass below chooses readings; the XYZ
    # those readings put the point at is one batched transform at the end
    # (`_layer_cmds`), rather than a transform per point per pass -- pass 1's
    # own placements were thrown away whenever the layer was routed.
    angles = []
    greedy_c = []
    bands = []
    prev = prev_angles
    for nm in nrms:
        band = None
        raw = _UNSET
        if banding and prev is not None:
            n_c = _collector_normal_f(rotaries, nm)
            if n_c is not None:
                raw = band = _perp_band_interval(
                    a1, a0, n_c, float(prev[0]), float(prev[1]),
                    float(perp_tolerance_deg))
                # FREE, not held. `_perp_band_interval` says None where EVERY
                # C satisfies the tolerance -- the pole, and any stretch flat
                # enough that the whole circle is inside the band. The greedy
                # walk spends that freedom on standing still; a router spends
                # it on already being where the next closure needs it to be,
                # which is where the improvement comes from. An unbounded
                # interval is how the clamp below expresses "anywhere".
                band = (-float("inf"), float("inf")) if band is None else band
        bands.append(band)
        # `raw` is the band this point was just priced at against this same
        # previous pose, so the readings solve takes it instead of pricing it
        # a second time.
        prev, _readings = _five_axis_readings(rotaries, nm, prev,
                                              perp_tolerance_deg, band=raw)
        angles.append(prev)
        greedy_c.append(float(prev[0]))

    if not (route and banding) or len(angles) < 3:
        return _layer_cmds(machine, rotaries, pts, angles, mount_axial), prev

    routed = _route_band_c(greedy_c, bands)

    # PASS 3. Least total travel under pass 2's own peak, over ABSOLUTE C --
    # so the solver may take either IK branch and any winding, which is where
    # the avoidable spin lives and what neither pass above can reach. Adopted
    # only on evidence: it must keep the peak and spend strictly less.
    if arc is not None and speed_mm_s > 0.0:
        import numpy as np

        cap = _peak_rate(routed, arc, speed_mm_s)
        if cap > 0.0:
            # WHAT THE CAP IS. Pass 2's own peak, unless the SOLVED floor sits
            # materially below it -- which says pass 2 was smoothing inside a
            # branch-collapsed tube and its peak is the defect rather than a
            # standard. `_route_minimax_cap`'s docstring carries the
            # measurement. Below the trigger nothing changes: same cap, same
            # two gates, same answer.
            floor = _route_minimax_cap(rotaries, nrms, arc, greedy_c[0],
                                       float(perp_tolerance_deg), speed_mm_s,
                                       cap)
            want_peak = (floor is not None
                         and floor <= ROUTE_MINIMAX_TRIGGER * cap)
            use_cap = floor * ROUTE_MINIMAX_SLACK if want_peak else cap
            lean = _route_min_travel_c(rotaries, nrms, arc, greedy_c[0],
                                       float(perp_tolerance_deg), use_cap,
                                       speed_mm_s)
            if lean is not None:
                spent = float(np.abs(np.diff(np.asarray(routed, float))).sum())
                lean_spent = float(np.abs(np.diff(np.asarray(lean,
                                                             float))).sum())
                gain = (spent - lean_spent) / spent if spent > 0.0 else 0.0
                lean_peak = _peak_rate(lean, arc, speed_mm_s)
                rise = (lean_peak - cap) / cap
                if want_peak:
                    # The trade is deliberate and it is the one asked for:
                    # this branch buys PEAK and may spend more total doing it,
                    # so the travel gate does not apply. Measured on the dome
                    # R10 / 60 deg layer 2, 1357.8 -> 618.4 deg/s for +5%
                    # travel. It must still actually win, or the smoothed
                    # route stands.
                    take = lean_peak <= cap * (1.0 - ROUTE_MINIMAX_MIN_DROP)
                else:
                    take = (gain >= ROUTE_LEAN_MIN_GAIN
                            and rise <= ROUTE_LEAN_MAX_PEAK_RISE)
                if take:
                    routed = lean
    angles = []
    prev = prev_angles
    for i, nm in enumerate(nrms):
        if bands[i] is None or prev is None:
            # Not routable: the layer's first point (its continuity with the
            # layer before), or a degenerate normal. Take the ordinary greedy
            # step from wherever the ROUTED walk now stands, rather than the
            # pass-1 angles, which were costed against a different previous.
            prev, _readings = _five_axis_readings(rotaries, nm, prev,
                                                  perp_tolerance_deg)
            angles.append(prev)
            continue
        chosen_c = routed[i]
        n_c = _collector_normal_f(rotaries, nm)
        a_raw = _tilt_deg(a1, a0, n_c, chosen_c)
        chosen_a = prev[1] + _nearest_turn(a_raw - prev[1])
        prev = (chosen_c, chosen_a)
        angles.append(prev)
    return _layer_cmds(machine, rotaries, pts, angles, mount_axial), prev


def _layer_cmds(machine, rotaries, pts, angles, mount_axial):
    """`[(xyz, readings)]` for a layer whose readings are all chosen."""
    g = _five_axis_xyzs(machine, rotaries, pts, angles, mount_axial)
    return [(g[i], tuple(j.word_from_angle(a) for j, a in zip(rotaries, ang)))
            for i, ang in enumerate(angles)]


def _collector_xyz(rotaries, poses, mount_axial=None):
    """Machine poses `[(x, y, z, *words)]` posed back into the COLLECTOR's own
    frame, `(N, 3)`.

    The exact inverse of the chain `_five_axis_xyzs` places a layer with --
    `p = R^T (g - t)` -- and the same one `_travel_waypoints` already routes
    in, so a point recovered here and a point the design was written from are
    the same number. That matters because this is what "the surface arc" is
    measured in: a layer's `world` points ARE collector-frame points, and
    running this over a layer's commands returns them to machine precision.
    """
    import numpy as np

    import machine_model

    P = np.asarray(poses, float).reshape(-1, 3 + len(rotaries))
    ang = np.array([[j.angle_from_word(x) for j, x in zip(rotaries, row[3:])]
                    for row in P]).reshape(-1, len(rotaries))
    r, t = machine_model.rotary_transforms(rotaries, ang, mount_axial)
    return np.einsum("nij,nj->ni", np.transpose(r, (0, 2, 1)), P[:, :3] - t)


def _surface_arcs(rotaries, layers, cmds_layers, mount_axial=None):
    """The surface arc of EVERY written move, travels included: how far the
    point under the nozzle walks ACROSS THE COLLECTOR. One per command, in the
    writer's order, so it lines up with `_move_durations`' clock.

    WHAT A TRAVEL'S SURFACE ARC IS, WHICH NOTHING HAD DEFINED.
    The real jet is continuous, so travel fibres are not treated differently
    from deposition. A melt-electrowritten jet never
    stops, so a travel lays fibre and the thing that decides whether it lands
    as a line or a coil is the same thing it is for a fibre move: how fast the
    contact point crosses the collector. The definition here is therefore the
    one that already holds for deposition, applied to a move that happens to
    fly at standoff -- the nozzle's own displacement IN THE COLLECTOR'S FRAME.

    It is not a new convention. A layer's `world` points are collector-frame
    points and `arc_length` is their cumulative chord, so for a fibre move
    `|P[i+1] - P[i]|` IS `diff(arc_length())` to machine precision; the fibre
    moves keep the design's own number here so the F words they already carry
    do not move by a rounding. What the collector frame adds is that the same
    quantity is defined for a move with no design under it: the lead-in, which
    starts at the machine's pre-program pose, and the layer hop, which crosses
    from one layer's last point to the next layer's first.

    A NEAR-IN-PLACE ROTATION READS AS A NEAR-ZERO ARC, AND THAT IS THE POINT.
    Where the collector turns under a hovering nozzle the contact point barely
    moves however fast the axes run, and no F word can lift its surface speed;
    `mew_kinematics.surface_feeds` leaves such a move at the plain speed and
    `_plan_feed` does not try to slow it to a floor it cannot reach. The
    measurement is still the honest one -- the floor guard reports it, and the
    remedy is to give the move some surface travel, not more feed.
    """
    import numpy as np

    poses = [[0.0] * (3 + len(rotaries))]
    for cmds in cmds_layers:
        poses += [[float(g[0]), float(g[1]), float(g[2])]
                  + [float(x) for x in r] for g, r in cmds]
    P = _collector_xyz(rotaries, poses, mount_axial)
    out = []
    k = 0
    for pp, cmds in zip(layers, cmds_layers):
        out.append(float(np.linalg.norm(P[k + 1] - P[k])))
        out += [float(x) for x in np.diff(np.asarray(pp.arc_length(), float))]
        k += len(cmds)
    return out


def _file_feeds(rotaries, layers, cmds_layers, speed, compensate,
                mount_axial=None):
    """The F word of EVERY written move, travels included: one per command.

    `speed * norm / arc` (`mew_kinematics.surface_feeds`) -- the word that
    makes the firmware, which divides F by the mm-and-degrees quadrature norm,
    take exactly `arc / speed` over the move. A TRAVEL IS NOT SPECIAL: it gets
    the same word for the same reason, because the jet does not stop for it
    (`_surface_arcs`). Uncompensated, every move keeps the plain `speed`,
    travel and fibre alike, exactly as before.
    """
    import mew_kinematics

    n = sum(len(c) for c in cmds_layers)
    if not compensate:
        return [float(speed)] * n
    norms = []
    prev = None
    for cmds in cmds_layers:
        for g, r in cmds:
            cur = [float(g[0]), float(g[1]), float(g[2])] + [float(x)
                                                             for x in r]
            base = [0.0] * len(cur) if prev is None else prev
            norms.append(mew_kinematics.machine_norm(
                [cur[k] - base[k] for k in range(3)],
                [cur[k] - base[k] for k in range(3, len(cur))]))
            prev = cur
    return mew_kinematics.surface_feeds(
        norms, _surface_arcs(rotaries, layers, cmds_layers, mount_axial),
        speed)


def _spin_stats(cmds, arc, speed_mm_s):
    """One layer's rotary cost: travel and peak rate for each rotary word.

    Read off the EMITTED readings rather than off any internal trajectory, so
    it describes the file and not the solver's opinion of it.
    """
    import numpy as np

    out = {}
    n = len(cmds[0][1]) if cmds else 0
    for j in range(n):
        w = np.array([float(r[j]) for _, r in cmds])
        out[j] = (float(np.abs(np.diff(w)).sum()) if w.size > 1 else 0.0,
                  _peak_rate(w, arc, speed_mm_s))
    return out


def _hold_threshold_deg(rotaries, normals):
    """The smallest tolerance at which ONE inner-joint reading serves this
    whole stretch -- `min over C of max over the stretch of err(C)`.

    THIS IS THE NUMBER THE DESIGN SETS, and it is why the tolerance field is a
    step rather than a dial. It is the pass's own ANGULAR MISS DISTANCE from
    the collector's spin axis -- for a geodesic pass missing the axis by `d` of
    arc on a sphere of radius `R`, exactly `d / R` in radians -- and it is
    CONSTANT along the pass rather than growing towards its ends, so under it
    the inner joint must sweep roughly 180 deg and over it the joint need not
    move at all. Measured on the dome R10 design: 2.864 deg for the d = 0.5 mm
    pass against `degrees(0.05)` = 2.865, and 34.374 at d = 6.0 against
    `degrees(0.6)` = 34.378. `asin(d / R)` is the SMALL-OFFSET form of the same
    number and agrees to 0.04% at d = 0.5, which is why it read as the closed
    form until a large-offset pass was checked; it is wrong by 2.5 deg at
    d = 6. Crossing a pass's threshold takes its travel from 93.0 deg to 5.7.

    Computed from the error curve directly, so it needs no dome, no apex and
    no chart -- an imported STEP collector gives its own number by the same
    call.
    """
    import numpy as np

    if len(normals) < 2:
        return None
    K, R, P, theta_q = _band_sinusoid(rotaries, normals)
    grid = np.arange(-180.0, 180.0, ROUTE_GRID_DEG)
    return float(_band_err_deg(K, R, P, theta_q, grid).max(axis=0).min())


def _straight_runs(pattern, min_pts=5):
    """Index ranges of the maximal COLLINEAR runs of a flat pattern.

    `ProjectedPath.pattern` is the flat source of every sample, so a hatch pass
    is exactly collinear there whatever the projection does to it on the way to
    the surface -- which the chart `(u, v)` is not, and testing collinearity
    there finds nothing at all. Turnarounds and linking loops are what falls
    between the runs.
    """
    import numpy as np

    if pattern is None:
        return []
    pat = np.asarray(pattern, float)
    if pat.ndim != 2 or pat.shape[0] < min_pts:
        return []
    runs, start = [], 0
    for i in range(1, len(pat) - 1):
        a, b, c = pat[i - 1], pat[i], pat[i + 1]
        x = ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
        if abs(x) > 1e-9:
            if i - start >= min_pts:
                runs.append((start, i))
            start = i
    if len(pat) - 1 - start >= min_pts:
        runs.append((start, len(pat) - 1))
    return runs


SPIN_REPORT_CANDIDATES = 2
SPIN_REPORT_MIN_STEP_DEG = 0.05


def _overrun_stretches(overrun, arc):
    """`[(length_mm, samples, worst_deg)]`, one per run of consecutive points
    outside the tolerance. The length runs from the last point INSIDE before
    the run to the first one inside after it -- the whole arc over which the
    nozzle leaves the band and comes back, not just between the samples that
    happen to land outside it."""
    import numpy as np

    over = np.asarray(overrun, float)
    arc = np.asarray(arc, float)
    out = []
    i, n = 0, over.size
    while i < n:
        if over[i] <= 1e-6:
            i += 1
            continue
        j = i
        while j + 1 < n and over[j + 1] > 1e-6:
            j += 1
        out.append((float(arc[min(j + 1, n - 1)] - arc[max(i - 1, 0)]),
                    j - i + 1, float(over[i:j + 1].max())))
        i = j + 1
    return out


def _overrun_lines(layers, perp_tolerance_deg, spin_cap_deg_s, perp_overrun,
                   limit_stats=None):
    """The rotary limits' cost, in the terms the trade was accepted on: how
    often the nozzle leaves the tolerance, for how long an arc, and by how much --
    and, if the limits could not be fully met, that too."""
    ls = limit_stats or {}
    accel = float(ls.get("accel") or 0.0)
    tilt = float(ls.get("tilt_accel") or 0.0)
    if perp_overrun is None or not (float(spin_cap_deg_s or 0.0) > 0.0
                                    or accel > 0.0 or tilt > 0.0):
        return []
    runs = []
    for li, (pp, over) in enumerate(zip(layers, perp_overrun)):
        runs += [(r, li) for r in _overrun_stretches(over, pp.arc_length())]
    head = "  rotary limits %s" % ", ".join(
        t for t in ("spin %.0f deg/s" % float(spin_cap_deg_s)
                    if float(spin_cap_deg_s or 0.0) > 0.0 else "",
                    "spin %.0f deg/s^2" % accel if accel > 0.0 else "",
                    "tilt %.0f deg/s^2" % tilt if tilt > 0.0 else "") if t)
    out = []
    sl = ls.get("slowed")
    if sl and sl.get("mm"):
        total = sum(float(pp.arc_length()[-1]) for pp in layers)
        out.append("  feed slowed on %.1f of %.1f mm of path to hold "
                   "perpendicularity (%.1f mm on the straight passes, the rest "
                   "on turnarounds and loops); slowest %.0f mm/min"
                   % (sl["mm"], total, sl["body_mm"], sl["min_speed"]))
    if ls.get("remaining"):
        out.append("  WARNING: %d rotary move(s) still exceed the limits"
                   % ls["remaining"])
    if not runs:
        return out + [head + ": held everywhere inside the %.2f deg tolerance"
                      % float(perp_tolerance_deg or 0.0)]
    longest = max(runs, key=lambda x: x[0][0])
    worst = max(runs, key=lambda x: x[0][2])
    total = sum(float(pp.arc_length()[-1]) for pp in layers)
    spent = sum(r[0][0] for r in runs)
    return out + [
        head + ": nozzle outside the %.2f deg tolerance on %d stretch(es), "
        "%.1f of %.1f mm of path (%.0f%%); longest %.2f mm (layer %d), worst "
        "+%.2f deg (layer %d)"
        % (float(perp_tolerance_deg or 0.0), len(runs), spent, total,
           100.0 * spent / total if total > 0 else 0.0, longest[0][0],
           longest[1] + 1, worst[0][2], worst[1] + 1)]


def surface_spin_report(machine, layers, speed, perp_tolerance_deg, spin_stats,
                        mount_axial=None, spin_cap_deg_s=0.0,
                        perp_overrun=None, limit_stats=None):
    """Lines saying what the rotaries spent, and what a wider band would save.

    The cap is 3 degrees, and the report says how many degrees of
    C/A travel each step up in tolerance would save. The thresholds are not
    invented -- each straight pass has its
    own, `_hold_threshold_deg`, the tolerance at which that pass stops needing
    the inner joint at all -- so the candidates offered are the ones the design
    itself puts on the table, and the saving beside each is MEASURED by
    re-solving, never extrapolated.

    ONE LAYER, the worst one by peak rate, is re-solved per candidate, and the
    line says so. Re-solving the whole stack would triple a run that already
    takes the better part of a minute, and the layers of a rotated stack differ
    by a few percent -- so the honest presentation is the layer's own numbers
    with the layer named, not a total quietly extrapolated from it.
    """
    import numpy as np

    rotaries = tuple(machine.rotaries)
    if not spin_stats or len(rotaries) != 2 or not layers:
        return []
    words = [j.word.upper() for j in rotaries]
    v = float(speed) / 60.0
    lines = []
    tot = [sum(st[j][0] for st in spin_stats) for j in range(len(words))]
    peak = [max(st[j][1] for st in spin_stats) for j in range(len(words))]
    lines.append("  rotary cost over %d layer(s): %s"
                 % (len(spin_stats),
                    ", ".join("%s %.0f deg travel, peak %.0f deg/s"
                              % (words[j], tot[j], peak[j])
                              for j in range(len(words)))))
    lines.extend(_overrun_lines(layers, perp_tolerance_deg, spin_cap_deg_s,
                                perp_overrun, limit_stats))
    if not (float(perp_tolerance_deg) > 0.0):
        lines.append("  perpendicularity tolerance is 0 (exact): every degree "
                     "above is spin the band could buy back")
        return lines

    worst = int(np.argmax([st[0][1] for st in spin_stats]))
    pp = layers[min(worst, len(layers) - 1)]
    nrm = list(pp.normal)
    thresholds = []
    for i0, i1 in _straight_runs(getattr(pp, "pattern", None)):
        t = _hold_threshold_deg(rotaries, nrm[i0:i1 + 1])
        if t is not None and t > float(perp_tolerance_deg):
            thresholds.append(t)
    if not thresholds:
        lines.append("  no pass on layer %d would stop spinning at any wider "
                     "band -- this design's spin is not the tolerance"
                     % (worst + 1,))
        return lines
    cand, last = [], float(perp_tolerance_deg)
    for t in sorted(thresholds):
        if t - last >= SPIN_REPORT_MIN_STEP_DEG:
            cand.append(t)
            last = t
        if len(cand) >= SPIN_REPORT_CANDIDATES:
            break

    pts = [tuple(float(c) for c in p) for p in pp.world]
    nrms = [tuple(float(c) for c in n) for n in nrm]
    arc = pp.arc_length()
    base = spin_stats[worst]
    lines.append("  layer %d is the worst; raising the perpendicularity "
                 "tolerance from %.2f deg would save, ON THAT LAYER:"
                 % (worst + 1, float(perp_tolerance_deg)))
    for t in cand:
        # A hair over the threshold: AT it the pass is only just feasible and
        # the grid can land the wrong side of the last digit.
        tol = t + 0.01
        cmds, _ = _five_axis_layer(machine, rotaries, pts, nrms, None,
                                   mount_axial, tol, arc=arc, speed_mm_s=v,
                                   spin_cap_deg_s=spin_cap_deg_s)
        st = _spin_stats(cmds, arc, v)
        # A SAVING AND A COST READ DIFFERENTLY, and both happen: buying the
        # inner joint's spin down usually costs the base joint a little more
        # travel, because holding C is exactly what makes A sweep through zero.
        # Printing "-15 deg saved" for that would be the report lying by
        # formatting.
        def _delta(j):
            d = base[j][0] - st[j][0]
            return ("%s %.0f deg %s (%.0f -> %.0f), peak %.0f -> %.0f deg/s"
                    % (words[j], abs(d), "saved" if d >= 0 else "MORE",
                       base[j][0], st[j][0], base[j][1], st[j][1]))

        lines.append("    %.2f deg -> %s"
                     % (tol, ", ".join(_delta(j)
                                       for j in range(len(words)))))
    return lines


def _perp_error_deg(rotaries, normals, angles):
    """Degrees between each point's surface normal, carried by the joints at
    `angles`, and the nozzle -- the perpendicularity error of what is WRITTEN,
    whatever chose the angles."""
    import numpy as np

    import machine_model

    r, _t = machine_model.rotary_transforms(rotaries, angles, None)
    n = np.asarray(normals, float).reshape(-1, 3)
    n = n / np.linalg.norm(n, axis=1)[:, None]
    m = np.einsum("kij,kj->ki", r, n)
    up = np.asarray(machine_model.NOZZLE_UP, float)
    return np.degrees(np.arccos(np.clip(m @ up, -1.0, 1.0)))


def _move_durations(rotaries, layers, cmds_layers, speed, feed_compensation,
                    mount_axial=None, max_feed=0.0):
    """Seconds per written move, on the controller's clock: `60 * norm / F`
    with rotary degrees in quadrature with XYZ mm, from word 0 at the start --
    exactly what `rotary_limits.py` reads back off the file. The F is the one
    the writer below puts on that line, travels included (`_file_feeds`).

    `max_feed` (mm/min, 0 = no ceiling) is the largest F the file may carry.
    It is applied HERE, before any joint is limited, because it is a property
    of the clock and not of a joint: capping F lengthens a move, which is
    exactly what every limit downstream is asking for, so a file planned on
    the capped clock needs no second pass. Only a move whose F would have
    exceeded the ceiling is touched -- `min(f, max_feed)` -- so a ceiling no
    move reaches leaves every duration, and therefore every written word,
    alone.

    Returns `(durations, capped)`, `capped` being True when the ceiling moved
    at least one duration; the caller needs that to know the file must be
    written to the returned clock even when both joints were already legal.
    """
    import numpy as np

    ceiling = float(max_feed or 0.0)
    feeds = _file_feeds(rotaries, layers, cmds_layers, speed,
                        feed_compensation, mount_axial)
    out = []
    capped = False
    prev = None
    k = 0
    for cmds in cmds_layers:
        for g, r in cmds:
            cur = [float(g[0]), float(g[1]), float(g[2])] + [float(x)
                                                            for x in r]
            base = [0.0] * len(cur) if prev is None else prev
            norm = math.sqrt(sum((a - b) ** 2 for a, b in zip(cur, base)))
            f = float(feeds[k])
            k += 1
            if ceiling > 0.0 and f > ceiling and norm > 0.0:
                f = ceiling
                capped = True
            out.append(60.0 * norm / f if f > 0.0 else float("inf"))
            prev = cur
    return np.asarray(out, float), capped


def _limit_violations(seq, dt, rate, accel):
    """How many moves of one joint's WORD sequence break `rate` or `accel`,
    by `rotary_limits.check`'s definitions (unrounded)."""
    import numpy as np

    x = np.concatenate(([0.0], np.asarray(seq, float)))
    dt = np.asarray(dt, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        v = np.diff(x) / dt
    bad = 0
    if rate > 0.0:
        bad += int(np.sum(np.abs(v) > rate * (1.0 + 1e-9) + 1e-9))
    if accel > 0.0 and v.size > 1:
        m = 0.5 * (dt[1:] + dt[:-1])
        with np.errstate(divide="ignore", invalid="ignore"):
            a = np.abs(np.diff(v)) / m
        bad += int(np.sum(np.isfinite(a) & (a > accel * (1.0 + 1e-9) + 1e-9)))
    return bad


def _limit_track(target, dt, rate, accel, x0):
    """Follow `target` from `x0` as closely as a joint limited to `rate` and
    `accel` can, one move at a time. `target[i]` is where move `i` should end
    and `dt[i]` its duration; the result is feasible by construction.

    Each move asks for the velocity that lands on its target, no faster than
    stopping on the target at `accel` allows (`sqrt(2 a |e|)`, so it does not
    overshoot a still target), then clips it into what this move can reach
    from the last one's velocity -- `accel` over the mean of the two moves'
    durations, the definition the checker uses -- and into `rate`. The first
    move carries no acceleration from rest, as the checker charges none.
    """
    n = len(target)
    out = [0.0] * n
    px, pv = float(x0), None
    inf = float("inf")
    for i in range(n):
        d = float(dt[i])
        if not d > 0.0 or not math.isfinite(d):
            out[i] = px
            continue
        e = float(target[i]) - px
        want = e / d
        cap = rate if rate > 0.0 else inf
        if accel > 0.0:
            cap = min(cap, math.sqrt(2.0 * accel * abs(e)))
        want = math.copysign(min(abs(want), cap), want)
        if pv is not None and accel > 0.0:
            m = 0.5 * (float(dt[i - 1]) + d)
            want = min(max(want, pv - accel * m), pv + accel * m)
        if rate > 0.0:
            want = min(max(want, -rate), rate)
        px = px + want * d
        pv = want
        out[i] = px
    return out


def _limit_two_sided(target, dt, rate, accel, x0):
    """`_limit_track` run BACKWARDS first and then forwards along that.

    A forward limiter alone lags: it starts turning only once the target has
    moved, so every reorientation lands late. Run on the reversed sequence it
    leads instead, and it is just as feasible, because a rate and an
    acceleration bound read the same in either direction. The forward pass
    then follows the leading copy from the true start, which centres the
    turn on the feature. Measured against a forward-only limiter and two
    band-tracking variants:
    this spent the least path outside the tolerance on both the reported
    crease case (76% against 79-83%) and the smooth dome (52% against 55-84%).
    """
    rt = list(target)[::-1]
    rd = list(dt)[::-1]
    back = _limit_track(rt, rd, rate, accel, rt[0])[::-1]
    return _limit_track(back, dt, rate, accel, x0)


# The fraction of each rotary limit the planners aim at (see its use).
LIMIT_PLAN_MARGIN = 0.995

# The feed planner's passes. Each one only ever LENGTHENS a move, bounded by
# the floor, so it cannot oscillate; this bounds its cost on a design whose
# demand no floor can meet, where the angle limiter takes the rest.
FEED_PLAN_PASSES = 200


def _plan_feed(seqs, dt, arcs, speed, min_speed, limits):
    """Durations that let the ROUTED angles be followed exactly, slowing the
    fibre moves no further than `min_speed` (mm/min). Returns a new array.

    What to spend first: falling below the CTS is unacceptable,
    the acceleration limit and the CTS are the hard limits, and of what is left
    perpendicularity is preferred. So time is spent before tilt, down to the floor, and only then does `_limit_two_sided` give up
    perpendicularity for what is left.

    A move's rotary change is fixed here; only its duration moves. Rate over
    a limit lengthens that move to the limit. Acceleration over a limit at a
    vertex lengthens BOTH moves meeting there by `sqrt(a / limit)`, which
    divides that vertex's acceleration by the same squared factor; the
    neighbours are then re-checked on the next pass. Every change is a
    lengthening and every move is capped at `arc / min_speed`, so the passes
    only climb towards the floor, and a slowdown spreads into the moves either
    side of a demand as a ramp rather than a step.

    A TRAVEL IS NOT EXEMPT. A hop arriving here with `arcs[k] is None` would
    keep its time whatever the limits asked, as if a travel laid nothing. The
    jet is continuous, so it
    lays fibre on a travel exactly as on a fibre move (`_surface_arcs`), and
    the floor is the same floor: exempt, nine layer hops on the library dome run
    at 11 mm/min of surface speed against a 528 floor, which is the knot at
    every layer boundary. Every move carries a surface arc and every move is
    held to `arc / min_speed`.

    The one move still passed over is the one with NO surface arc at all -- a
    rotation that turns the collector under a stationary contact point. It has
    no surface speed for a floor to raise, so lengthening it buys nothing;
    `mew_kinematics.surface_feeds` leaves it at the plain speed for the same
    reason. The floor guard still reports it.
    """
    import numpy as np

    dt = np.asarray(dt, float).copy()
    v_floor = float(min_speed) / 60.0
    fibre = np.array([a is not None and a > 0.0 for a in arcs])
    # NO FLOOR, NO CAP. `_limit_rotary_motion` only calls this with a floor;
    # `_plan_piece_feeds` calls it for a routed travel's pieces whatever the
    # floor, because their angles are fixed and time is the only thing that
    # keeps them inside the limits. It divided by the zero floor there.
    cap = np.array([((float(a) / v_floor if v_floor > 0.0 else np.inf)
                     if a is not None and a > 0.0
                     else d) for a, d in zip(arcs, dt)])
    cap = np.maximum(cap, dt)
    deltas = [np.diff(np.concatenate(([0.0], np.asarray(s, float))))
              for s in seqs]
    for _ in range(FEED_PLAN_PASSES):
        grow = np.ones_like(dt)
        for d, (rate, accel) in zip(deltas, limits):
            with np.errstate(divide="ignore", invalid="ignore"):
                r = d / dt
            if rate > 0.0:
                need = np.abs(r) / rate
                grow = np.maximum(grow, np.where(fibre & (need > 1.0), need,
                                                 1.0))
            if accel > 0.0 and dt.size > 1:
                m = 0.5 * (dt[1:] + dt[:-1])
                with np.errstate(divide="ignore", invalid="ignore"):
                    a = np.abs(np.diff(r)) / m
                f = np.where(np.isfinite(a) & (a > accel),
                             np.sqrt(a / accel), 1.0)
                grow[1:] = np.maximum(grow[1:], f)
                grow[:-1] = np.maximum(grow[:-1], f)
        grow = np.where(fibre, grow, 1.0)
        new = np.minimum(dt * grow * (1.0 + 1e-9), cap)
        if np.all(new <= dt * (1.0 + 1e-12)):
            break
        dt = np.maximum(dt, new)
    return dt


def _limit_rotary_motion(machine, rotaries, layers, cmds_layers, speed,
                         feed_compensation, mount_axial, rate, accel,
                         tilt_accel, min_speed=0.0, tilt_rate=0.0,
                         max_feed=0.0):
    """Hold both joints of the whole file under their own rate (deg/s) and
    acceleration (deg/s^2) limits, and every F under `max_feed` (mm/min).
    Returns `(cmds_layers, stats, durations)`. Any limit at 0 is not applied.

    A LIMIT PER JOINT BECAUSE THE JOINTS ARE NOT THE SAME MACHINE. The 720
    deg/s^2 measured on the rig is the C axis only, and A is not
    measured. So the spin's numbers and the
    tilt's are four separate fields, and the Machine tab ships the one
    measured acceleration on both joints and both rates off
    (`rotary_limits.RIG_LIMIT_FIELDS`). A tilt limited only by acceleration
    is still bounded along any real path, which is why the rate being off
    costs nothing today.

    WHY. The software must never command a move that is not merely bad for the
    jet but physically impossible for the joint. Staying
    perpendicular through a curve needs rotary acceleration that grows with
    the SQUARE of the print speed -- a tilt of T around a loop of radius r at
    speed v needs about T (v/r)^2 -- so at 800 mm/min a 0.75 mm turnaround asks
    ~4700 deg/s^2 of a joint recorded at 720. The file cannot have both. This
    keeps the motion possible and lets the nozzle leave the perpendicularity
    tolerance where it must; the run log says how much.

    HOW. The spin joint is limited against the route already chosen, with
    `_limit_two_sided` on the file's own clock (`_move_durations`). The tilt
    joint is then RE-SOLVED for the spin it was given -- the same re-solve
    every pass in this module makes -- and limited in turn. Nothing is
    changed when the file is already inside both limits, so a gentle design
    writes the same bytes with the limits on.

    THE CLOCK IS FIXED FIRST AND THE FILE IS WRITTEN TO HOLD IT. A move lasts
    `60 * norm / F`, and `norm` counts the rotary degrees -- so limiting the
    joints changes how long every move whose F is not compensated takes,
    above all the layer hops, which are written at the plain speed. Planning
    on the durations of the file being replaced and then writing that file's
    own F words left moves over the limit on the reported case (a hop shrank
    to 3 ms and the next move read back at 4686 deg/s^2), and re-planning on
    the new durations did not settle: 6 over after eight passes, 27 when the
    shortest duration seen was used instead. So `durations` is returned, the
    ORIGINAL clock, and the writer sets every move's F to keep it
    (`F = 60 * norm / duration`). A feed-compensated fibre move already runs
    on exactly that clock -- its duration is its arc over the speed -- so
    only the hops' and an uncompensated file's F words differ from what the
    writer would have put there, and only in a file this pass changed.

    The spin cap here is not `_route_spin_cap_c`'s: that pass chooses WHERE
    the overrun falls under a rate cap, and runs first; this one makes the
    result physically possible, rate and acceleration together, on both
    joints.
    """
    import numpy as np

    stats = {"rate": rate, "accel": accel, "tilt_rate": tilt_rate,
             "tilt_accel": tilt_accel, "max_feed": max_feed,
             "ran": False, "remaining": 0}
    limits = ((rate, accel), (tilt_rate, tilt_accel))
    # PLANNED A HAIR INSIDE. A limiter that lands exactly on a limit writes a
    # file that reads back a hair over it once words and F are rounded to the
    # written decimals: 720.1 against 720 on 8 moves of the reported case.
    plan = tuple((r * LIMIT_PLAN_MARGIN, a * LIMIT_PLAN_MARGIN)
                 for r, a in limits)
    words = [[[float(r[k]) for _, r in cmds] for cmds in cmds_layers]
             for k in range(2)]
    flat = [sum(w, []) for w in words]
    dt, capped = _move_durations(rotaries, layers, cmds_layers, speed,
                                 feed_compensation, mount_axial, max_feed)
    before = sum(_limit_violations(f, dt, *lim)
                 for f, lim in zip(flat, limits))
    stats["before"] = before
    stats["feed_capped"] = capped
    if before == 0:
        # NOTHING TO RE-SOLVE, BUT THE CLOCK MAY STILL HAVE MOVED. A feed
        # ceiling lengthens moves without touching an angle, so the words are
        # already right and only the F words are not; hand the writer the
        # capped clock and let it write them. With no ceiling, or one no move
        # reached, `capped` is False and the file is byte-identical.
        return cmds_layers, stats, (dt if capped else None)
    stats["ran"] = True
    counts = [len(c) for c in cmds_layers]
    # EVERY move's surface arc, the travels' included -- see `_surface_arcs`.
    # Measured on the UNLIMITED solve, which is the same basis the fibre arcs
    # have: `layers` is the design and no pass here moves it.
    arcs = _surface_arcs(rotaries, layers, cmds_layers, mount_axial)
    nrms = [tuple(float(c) for c in n) for pp in layers for n in pp.normal]
    j0, j1 = rotaries
    a1 = _vec3(j0.direction())
    a0 = _vec3(j1.direction())

    def solve(dt):
        """One limit pass on the clock `dt`; returns the new commands."""
        c = _limit_two_sided([j0.angle_from_word(w) for w in flat[0]], dt,
                             plan[0][0], plan[0][1], j0.angle_from_word(0.0))
        a_star = []
        pa = None
        for n, ci in zip(nrms, c):
            n_c = _collector_normal_f(rotaries, n)
            t = (pa if pa is not None else 0.0) if n_c is None else _tilt_deg(
                a1, a0, n_c, ci)
            pa = t if pa is None else pa + _nearest_turn(t - pa)
            a_star.append(pa)
        a = _limit_two_sided(a_star, dt, plan[1][0], plan[1][1],
                             j1.angle_from_word(0.0))
        out, i = [], 0
        for pp, cnt in zip(layers, counts):
            pts = [tuple(float(q) for q in p) for p in pp.world]
            ang = [(c[i + m], a[i + m]) for m in range(cnt)]
            out.append(_layer_cmds(machine, rotaries, pts, ang, mount_axial))
            i += cnt
        return out

    if 0.0 < float(min_speed) < float(speed):
        dt0 = dt
        dt = _plan_feed(flat, dt, arcs, speed, min_speed, plan)
        stats["slowed"] = _slowdown_stats(layers, arcs, dt0, dt)
    # RE-TIME AND SOLVE AGAIN, FOR THE FEED CEILING. A travel carries a
    # compensated F like every other move (`_file_feeds`), so the writer holds the
    # clock planned here on
    # the whole file and the only override left is the feed ceiling, which
    # `_file_clock` applies against the norms these words -- not the planned
    # ones -- produce. The loop stays for that, and because the best pass seen
    # is kept an oscillating design is no worse than one pass.
    out = solve(dt)
    best = None
    for _ in range(LIMIT_CLOCK_PASSES):
        # The clock the FILE will have, not the one this pass planned on: the
        # travels take the durations the words just written give them at
        # `--speed`. Judging the pass on its own planning clock instead scored
        # a file that was never written and reported 0 while the library dome
        # came back with 13 moves over 720 at its nine layer hops.
        dt_file = _file_clock(out, dt, max_feed)
        n = _remaining(out, dt_file, limits)
        if best is None or n < best[0]:
            best = (n, out, dt_file)
        if not n:
            break
        dt = dt_file
        out = solve(dt)
    stats["remaining"], out, dt = best
    return out, stats, dt


#: How many times `_limit_rotary_motion` re-times the travels and solves again
#: (see its note). Four: on every case of `test_five_axis_matrix` the count of
#: moves still over a limit reaches zero by the second.
LIMIT_CLOCK_PASSES = 4


def _remaining(cmds_layers, dt, limits):
    """Moves still over a limit in `cmds_layers`, on the clock `dt`."""
    seqs = [[float(r[q]) for cmds in cmds_layers for _, r in cmds]
            for q in range(2)]
    return sum(_limit_violations(f, dt, *lim) for f, lim in zip(seqs, limits))


def _file_clock(cmds_layers, dt, max_feed=0.0):
    """The durations the WRITER will actually give `cmds_layers`, which is not
    `dt`: it is `dt` with the writer's one remaining override applied, against
    the norms these words -- not the ones the clock was planned on -- produce.

    That override is the feed ceiling: a move's `F` is clamped to `max_feed`,
    so its duration can only grow, `max(dt, 60 * norm / max_feed)`.

    A travel carries a compensated F like any other move, so the writer holds
    this clock everywhere and only the ceiling can move it. That is still
    worth re-reading off the words rather than the plan: a ceiling can read
    honoured on the plan while the file's peak F rises past it (14598 to 15883
    against a ceiling of 12000, on the dome).
    """
    import numpy as np

    out = np.asarray(dt, float).copy()
    ceiling = float(max_feed or 0.0)
    if not ceiling > 0.0:
        return out
    prev = None
    k = 0
    for cmds in cmds_layers:
        for g, r in cmds:
            cur = [float(g[0]), float(g[1]), float(g[2])] + [float(x)
                                                             for x in r]
            base = [0.0] * len(cur) if prev is None else prev
            norm = math.sqrt(sum((a - b) ** 2 for a, b in zip(cur, base)))
            if norm > 0.0:
                out[k] = max(out[k], 60.0 * norm / ceiling)
            prev = cur
            k += 1
    return out


def _slowdown_stats(layers, arcs, dt0, dt):
    """What the feed planner spent: path slowed, the slowest surface speed, and
    how much of the slowed path lies on the design's straight passes (the
    scaffold body, `_straight_runs`) rather than its turnarounds and loops,
    where a thicker fibre sits outside the part."""
    body = []
    for pp in layers:
        n = len(pp.arc_length())
        flags = [False] * n
        for i0, i1 in _straight_runs(getattr(pp, "pattern", None)):
            for k in range(i0 + 1, i1 + 1):
                if k < n:
                    flags[k] = True
        body += flags
    slowed = body_mm = 0.0
    vmin = None
    for k, a in enumerate(arcs):
        if a is None or not a > 0.0 or not dt[k] > dt0[k] * (1.0 + 1e-6):
            continue
        slowed += a
        if body[k]:
            body_mm += a
        v = 60.0 * a / float(dt[k])
        vmin = v if vmin is None else min(vmin, v)
    return {"mm": slowed, "body_mm": body_mm, "min_speed": vmin}


# Travel routing (`_travel_waypoints`). A travel is sampled no coarser than
# this in XYZ and in either rotary, the density `collector_checks` judges it at.
TRAVEL_STEP_MM = 0.25
TRAVEL_STEP_DEG = 1.0
# How far a sample must sit inside the part (or the fibre stack its two ends
# are laid on) before the travel is routed at all, mm. Above the words' own
# 1e-4 rounding, so a travel that only grazes -- a plate hop at layer height --
# is left byte-identical.
TRAVEL_TRIGGER_MM = 1.0e-3
# Clearance a routed travel keeps over the part and that stack, mm: margin for
# the surface between two samples.
TRAVEL_CLEARANCE_MM = 0.25
# How far apart the WAYPOINTS may stand. The requirement is measured at the
# sample step above; a waypoint every sample traced the need curve exactly and
# spent 63 lines on one lead-in over a spinning dome. Each waypoint instead
# carries the largest requirement within one spacing either side, which is what
# makes the line between two of them clear everything between them.
TRAVEL_WAYPOINT_MM = 1.0
TRAVEL_WAYPOINT_DEG = 4.0


def _upper_hull(pts):
    """The upper convex hull of `(s, z)` points sorted by s, as a sub-list."""
    out = []
    for q in pts:
        while len(out) >= 2:
            (s0, z0), (s1, z1) = out[-2], out[-1]
            if (s1 - s0) * (q[1] - z0) - (z1 - z0) * (q[0] - s0) >= 0.0:
                out.pop()
            else:
                break
        out.append(q)
    return out


# The lead-in's approach (`_leadin_approach`). `LEADIN_CLEAR_MM` is the first
# offset tried outside the footprint; `LEADIN_FOOTPRINT_TOL_MM` is how much
# laid fibre may still cross the footprint BEFORE the final approach, in mm of
# fibre -- five diameters at the shipped 0.02 mm, which is not a fibre lying
# under the scaffold. `LEADIN_PASSES` bounds the search, because every sample
# of it is a kernel ray.
LEADIN_CLEAR_MM = 0.5
LEADIN_FOOTPRINT_TOL_MM = 0.1
LEADIN_PASSES = 3


def _chart_landing(rotaries, frame, mount_axial, words, w):
    """Where the jet would put material from commanded words `w`: the chart
    coordinates the nozzle's own axis lands on, or None when it misses the
    collector. The landing is the frame's `_touchdown`, which is what
    `DepositionFrame.lay_lead_in` lays the travel fibre on."""
    import numpy as np

    import machine_model

    ang = [[j.angle_from_word(x) for j, x in zip(rotaries, w[3:])]]
    R, t = machine_model.rotary_transforms(rotaries, ang, mount_axial)
    P = R[0].T @ (np.asarray(w[:3], float) - t[0])
    hit = frame._touchdown(P, -R[0].T[:, 2])
    if hit is None:
        return None
    u, v, _h = frame.from_world(*np.asarray(hit[1], float))
    return float(u), float(v)


def _footprint_fibre_mm(rotaries, frame, mount_axial, words, legs, box):
    """How much fibre the travel `legs` lay ON the footprint `box`, mm.

    `legs` is the travel as word vectors, consecutive; each leg is sampled the
    way the controller runs it, linearly in every word, and each sample is
    dropped onto the collector by `_chart_landing`. The chart is
    arc-length-parametrised, so a step between two landings is fibre.

    THE LAST LEG IS NOT COUNTED once there is more than one, because that leg
    IS the approach: it runs from just outside the footprint to the design's
    own first point, which on a stack of rotated layers sits INSIDE the
    footprint the whole stack covers (1.26 mm in on the block at 12 x 12). What
    the measurement is about is the fibre laid crossing the footprint on the
    way there.

    SAMPLED ON THE WAYPOINT GRID, not the finer routing grid: every sample is a
    kernel ray on an imported collector and this runs several times over a
    ~45 mm lead-in. At the routing step it cost 17 s of that block's solve, and
    millimetres of stray fibre do not hide between samples a millimetre apart.
    """
    import numpy as np

    u0, u1, v0, v1 = box
    pairs = list(zip(legs[:-1], legs[1:]))
    if len(pairs) > 1:
        pairs = pairs[:-1]
    total = 0.0
    for a, b in pairs:
        lin = math.dist(a[:3], b[:3])
        rot = max((abs(x - y) for x, y in zip(a[3:], b[3:])), default=0.0)
        n = max(2, int(math.ceil(max(lin / TRAVEL_WAYPOINT_MM,
                                     rot / TRAVEL_WAYPOINT_DEG))))
        prev = None
        for i in range(n + 1):
            f = i / float(n)
            w = [x + f * (y - x) for x, y in zip(a, b)]
            hit = _chart_landing(rotaries, frame, mount_axial, words, w)
            inside = (hit is not None
                      and u0 < hit[0] < u1 and v0 < hit[1] < v1)
            if inside and prev is not None:
                total += float(np.linalg.norm(np.asarray(hit, float)
                                              - np.asarray(prev, float)))
            prev = hit if inside else None
    return total


def _leadin_approach(machine, rotaries, frame, mount_axial, words, layers,
                     w0, w1, angles):
    """One word vector to come into the first point THROUGH, or None.

    WHY. The lead-in flies in from the picked home, which is off the part, and
    in MEW the jet never stops -- so whatever the nozzle passes over on the way
    is fibre lying on the collector. Straight from home the approach runs
    ACROSS the design's own footprint and the whole scaffold is then printed on
    top of it (measured: 13.1 mm of laid fibre over the footprint on the
    block at 12 x 12).

    HOW. The design's footprint is read off the layers' own chart coordinates,
    never re-derived from the CLI. The approach point is the first point pushed
    OUTWARD -- away from the footprint's centre, so an edge start is approached
    square to its edge and a corner start diagonally -- and posed
    perpendicular to the surface there, exactly as a deposition point is. The
    offset then GROWS until the fibre laid on the way in stops crossing the
    footprint (`_footprint_fibre_mm`), because how far out is far enough
    depends on how the rotaries swing on the way in, which is the collector's
    business and not a constant's.

    None when the design has no chart, when the approach point falls off the
    collector's own chart, or when the straight lead-in already lays nothing
    inside the footprint -- and then the file keeps the travel it had.
    """
    import numpy as np

    uv = [np.asarray(pp.uv, float) for pp in layers
          if getattr(pp, "uv", None) is not None]
    if len(uv) != len(layers) or not len(uv[0]):
        return None
    allu = np.concatenate(uv)
    box = (float(allu[:, 0].min()), float(allu[:, 0].max()),
           float(allu[:, 1].min()), float(allu[:, 1].max()))
    if _footprint_fibre_mm(rotaries, frame, mount_axial, words, [w0, w1],
                           box) <= LEADIN_FOOTPRINT_TOL_MM:
        return None
    start = uv[0][0]
    centre = np.array([0.5 * (box[0] + box[1]), 0.5 * (box[2] + box[3])])
    e = np.asarray(start, float) - centre
    n = float(np.linalg.norm(e))
    if not n:
        return None
    e = e / n
    out = None
    step = LEADIN_CLEAR_MM
    for _ in range(LEADIN_PASSES):
        q = np.asarray(start, float) + e * step
        try:
            p = np.asarray(frame.to_world(q[0], q[1], 0.0), float).reshape(3)
            nrm = np.asarray(frame.normal(q[0], q[1], 0.0),
                             float).reshape(3)
        except Exception:       # noqa: BLE001 -- off the chart is an answer
            return out
        g, readings, _a = _five_axis_point(machine, rotaries, p, nrm, angles,
                                           mount_axial)
        cand = [float(g[0]), float(g[1]), float(g[2])] + [
            float(x) for x in readings]
        crossed = _footprint_fibre_mm(rotaries, frame, mount_axial, words,
                                      [w0, cand, w1], box)
        out = cand
        if crossed <= LEADIN_FOOTPRINT_TOL_MM:
            return out
        # DOUBLED, not stepped by what was measured: the crossing is a LENGTH
        # of fibre and says nothing about how much further out the approach has
        # to start, so the search widens instead of guessing.
        step = 2.0 * step + LEADIN_CLEAR_MM
    return out


def _piece_feeds(rotaries, mount_axial, start, pieces, v_surf, fallback):
    """One F word per routed-travel piece, holding the whole travel's own
    surface speed `v_surf` (mm/min) across every one of them.

    `_travel_waypoints` splits a travel into pieces that follow the straight
    move's rotary path exactly and leave its line only in XYZ, so the pieces do
    not share a shape: one can be almost pure rotation, crossing almost no
    collector, while another is almost pure nozzle motion. One F for all of
    them therefore does not mean one speed for all of them, which is what left
    the routed lead-in running at 11 mm/min on some of its pieces and 358 on
    others. `v * norm / arc` per piece is the same word
    `mew_kinematics.surface_feeds` writes for a fibre move, and it makes every
    piece take `arc / v`.

    A piece with no surface arc -- the collector turning under a stationary
    contact point -- has no surface speed to hold and keeps `fallback`, the
    straight move's own F, exactly as `surface_feeds` does.

    Returns `(feeds, arcs)`; the arcs are what `_plan_piece_feeds` then plans
    the chain's own rotary vertices against.
    """
    import numpy as np

    poses = [list(start)] + [list(w) for w in pieces]
    P = _collector_xyz(rotaries, poses, mount_axial)
    out, arcs = [], []
    for i in range(len(pieces)):
        a, b = poses[i], poses[i + 1]
        norm = _machine_norm_of(a, b)
        arc = float(np.linalg.norm(P[i + 1] - P[i]))
        arcs.append(arc)
        out.append(float(v_surf) * norm / arc
                   if v_surf > 0.0 and arc > 1e-12 and norm > 1e-12
                   else float(fallback))
    return out, arcs


def _machine_norm_of(a, b):
    """`mew_kinematics.machine_norm` between two machine poses."""
    import mew_kinematics

    return mew_kinematics.machine_norm([b[k] - a[k] for k in range(3)],
                                       [b[k] - a[k] for k in range(3, len(a))])


def _plan_piece_feeds(start, pieces, feeds, arcs, limits, min_speed):
    """`_plan_feed` run over ONE routed travel's pieces, so a rotary vertex
    INSIDE a travel is planned the way every other vertex in the file is.

    THE LIMITER NEVER SEES A PIECE. `_limit_rotary_motion` plans a travel as
    the single straight move it is at that point; `_travel_waypoints` splits it
    afterwards, and `_leadin_approach` puts a second leg in front of that whose
    rotary path the straight move never had. While every piece carried one
    shared F that cost nothing -- a piece turned the joints no faster than the
    straight move did, which is the guarantee `_travel_waypoints` documents.
    Holding the SURFACE speed instead makes a rotation-heavy piece short, and
    on the library dome that put the lead-in's last leg at -210 deg/s next to a
    piece at +33, a 1931 deg/s^2 reversal against a 720 limit.

    So the same pass runs here on the same terms: lengthen only, cap every
    piece at `arc / min_speed`, and let an acceleration that will not fit
    inside the floor stand and be counted rather than break the floor for it.
    That is the order because falling below the CTS is unacceptable.

    ONLY THE VERTICES BETWEEN PIECES ARE PLANNED. The two at the ends of the
    chain belong to moves the limiter already timed, and lengthening one of
    those here would contradict a plan the whole file's angles were solved
    against. The words are passed relative to `start`, so the first piece's own
    change is the first change seen.
    """
    import numpy as np

    dt = [60.0 * _machine_norm_of(a, b) / f if f > 0.0 else 0.0
          for a, b, f in zip([start] + list(pieces[:-1]), pieces, feeds)]
    if len(pieces) < 2 or not all(x > 0.0 for x in dt):
        return list(feeds)
    seqs = [[float(w[3 + j]) - float(start[3 + j]) for w in pieces]
            for j in range(len(start) - 3)]
    out = _plan_feed(seqs, np.asarray(dt, float), arcs, 0.0, min_speed, limits)
    return [(60.0 * _machine_norm_of(a, b) / t if t > 0.0 else f)
            for a, b, t, f in zip([start] + list(pieces[:-1]), pieces, out,
                                  feeds)]


def _travel_waypoints(rotaries, frame, mount_axial, w0, w1):
    """Route one travel over the collector: `[(s, words)]`, or None to leave
    the straight move alone.

    WHY. MEW's jet never stops, so a travel lays fibre, and a travel through
    the part is a nozzle crash on the rig and fibre drawn inside the collector.
    The controller interpolates every word linearly, rotaries included, so the
    straight `G0` from `w0` to `w1` swings the collector under a nozzle moving
    in a straight line, and nothing ever asked whether the two met: every
    lead-in from the picked home dived 0.1-5.4 mm into the part, and layer hops
    on a block 0.72 mm. Moving where that fibre is DRAWN does not fix it;
    this moves the machine.

    HOW. The travel is sampled (`TRAVEL_STEP_MM`, `TRAVEL_STEP_DEG`) and each
    sample's commanded point is posed into the collector at THAT instant's
    rotary words, through `machine_model.rotary_transforms` -- the chain the
    file is read back by. `frame.outermost_along` then says how far the point
    must rise along the nozzle axis to stand over everything under it. Machine
    +Z is that axis in collector coordinates at every pose, so lifting the Z
    word by an amount lifts the posed point by exactly it.

    Where a stretch of samples sits inside the part, or inside the fibre stack
    the travel's two ends are laid on, by more than `TRAVEL_TRIGGER_MM`, that
    stretch is lifted to clear both by `TRAVEL_CLEARANCE_MM`: the waypoints are
    the upper hull of the required lifts, returning to the straight line on
    either side. A travel that never enters is returned as None, and the file
    keeps its one straight move.

    THE ROTARY PATH IS NOT TOUCHED. Every waypoint carries the rotary words the
    straight move had at that fraction `s`, so the joints pass through the same
    poses in the same order with no re-pose; only XYZ leaves the line. Each
    piece runs at the straight move's own F (the travel speed the user set,
    `fbb19c8`), so a lifted piece takes longer than its share and the joints
    turn SLOWER through it, never faster: no travel rate rises above what
    `_limit_rotary_motion` planned. What changes is a rate step at a waypoint,
    bounded by the travel's own rate -- at most F/60 deg/s on a pure turn.
    """
    import numpy as np

    import machine_model

    if frame is None or getattr(frame, "_touchdown", None) is None:
        return None
    w0 = np.asarray(w0, float)
    w1 = np.asarray(w1, float)
    dw = w1 - w0
    lin = float(np.linalg.norm(dw[:3]))
    rot = float(np.max(np.abs(dw[3:]))) if dw.size > 3 else 0.0
    n = max(2, int(math.ceil(max(lin / TRAVEL_STEP_MM, rot / TRAVEL_STEP_DEG))))
    s = np.linspace(0.0, 1.0, n + 1)
    W = w0[None, :] + s[:, None] * dw[None, :]
    ang = np.array([[j.angle_from_word(x) for j, x in zip(rotaries, row[3:])]
                    for row in W])
    R, t = machine_model.rotary_transforms(rotaries, ang, mount_axial)
    RT = np.transpose(R, (0, 2, 1))
    P = np.einsum("nij,nj->ni", RT, W[:, :3] - t)
    up = RT[:, :, 2]
    top = np.full(n + 1, -np.inf)
    for k in range(n + 1):
        o = frame.outermost_along(P[k], up[k])
        if o is not None:
            top[k] = o
    # The stack the travel runs over: an end on layer k sits k*d over the bare
    # collector. The LOWER end's height, because the rise from one layer to
    # the next is the travel itself and not a pass through material.
    stack = max(0.0, min(-float(top[0]) if np.isfinite(top[0]) else 0.0,
                         -float(top[-1]) if np.isfinite(top[-1]) else 0.0))
    need = top + stack
    need[0] = need[-1] = -np.inf
    if not np.any(need > TRAVEL_TRIGGER_MM):
        return None
    req = np.where(np.isfinite(need), need + TRAVEL_CLEARANCE_MM, 0.0)
    req = np.maximum(req, 0.0)
    req[0] = req[-1] = 0.0
    # THE WAYPOINT GRID, and the dilation that makes it safe. Each candidate
    # carries the largest requirement within one spacing either side, so the
    # straight line between two neighbours clears everything between them --
    # including a wall that steps up between two samples, which is what put the
    # slab's own 0.023 mm inside. The two ends cannot rise: they are the points
    # the travel joins.
    coarse = max(2, int(math.ceil(max(lin / TRAVEL_WAYPOINT_MM,
                                      rot / TRAVEL_WAYPOINT_DEG))))
    g = max(1, int(math.ceil(n / float(coarse))))
    cand = sorted(set(list(range(0, n + 1, g)) + [n]))
    wide = {}
    for k in cand:
        wide[k] = (0.0 if k in (0, n) else
                   float(np.max(req[max(0, k - g):min(n, k + g) + 1])))
    hot = [k for k in cand if wide[k] > TRAVEL_TRIGGER_MM]
    if not hot:
        return None
    # One hull per stretch that needs lifting, bracketed by the grid point
    # either side, so the route returns to the straight move between stretches
    # rather than tenting over the whole travel.
    runs, a = [], hot[0]
    for b0, b1 in zip(hot[:-1], hot[1:]):
        if cand.index(b1) != cand.index(b0) + 1:
            runs.append((a, b0))
            a = b1
    runs.append((a, hot[-1]))
    lift = {}
    for a, b in runs:
        ia, ib = cand.index(a), cand.index(b)
        keys = cand[max(0, ia - 1):ib + 2]
        pts = [(float(s[k]), wide[k]) for k in keys]
        for sk, zk in _upper_hull(pts):
            lift[sk] = max(zk, lift.get(sk, 0.0))
    out = []
    for sk in sorted(lift):
        if sk <= 0.0:
            continue
        w = w0 + sk * dw
        w[2] += lift[sk]
        out.append((sk, w))
    if not out or out[-1][0] < 1.0:
        out.append((1.0, w1.copy()))
    else:
        out[-1] = (1.0, w1.copy())
    return out


#: Decimals every five-axis word is written to. ONE SOURCE, because reading
#: the file back has to round the same way the writer did: `rotary_limits`
#: grants a move exactly one last-digit unit of slack per endpoint, and a
#: reader that assumed a different precision would grant the wrong slack.
SURFACE_PRECISION = 4


def generate_surface_gcode(machine, path, *, speed, feed_compensation=False,
                           precision=SURFACE_PRECISION, frame=None,
                           perp_tolerance_deg=0.0,
                           spin_stats=None, spin_cap_deg_s=0.0,
                           perp_overrun=None, accel_cap_deg_s2=0.0,
                           tilt_accel_cap_deg_s2=0.0, min_speed=0.0,
                           tilt_cap_deg_s=0.0, max_feed=0.0,
                           limit_stats=None, surface_speeds=None):
    """Return five-axis MEW g-code (str) for a projected path on a collector.

    SECTION 5 RULE 4: this consumes a PATH and not a formula. `path` is one
    `mew_project.ProjectedPath`, or one per layer as `surface_layers` builds
    them; the projection, and therefore the surface, is somebody else's problem
    by the time the file is written. That is what lets an arbitrary-STL
    collector arrive later with nothing in here changing.

    `machine` is a `machine_model.MachineProfile` carrying two rotary joints.
    THE WORDS COME FROM THE JOINTS, never from a constant in this module: a
    rig's words are a property of the rig. ISO 6983 names A/B/C for rotations
    about X/Y/Z and U/V/W for secondary LINEAR axes, so Terranova's `U`/`V` are
    their machine's own convention and not a standard to hardcode. Only the
    joints know.

    Every emitted move is a `G1` but the approach and the layer hops, which are
    `G0` so the parser scores them as travel rather than as fibre; the first
    carries `F` because `F` is modal and a consumer holding none runs at its own
    default. There is no `E` word: MEW is continuous flow, and neither our files
    nor any of Terranova's six carry one.

    `feed_compensation` holds the SURFACE speed at `speed` where the rotaries
    swing the tool point, through `mew_kinematics.surface_feeds`. Switched off
    the file carries a single constant `F`. Read that function's note for what
    the F word on such a machine actually divides, and for why Terranova's own
    per-segment F is a comparator for the CONVENTION and not for this physics.

    `frame` is the collector the path was projected onto, and it is optional for
    one reason only: it is needed to place the collector on the machine (its
    `mount_axial`), and a profile that has not been calibrated does not place it
    anyway. Passing it is what lets the emitter honour the Machine page's two
    picked datums -- where the collector stands, and where the nozzle zeroes
    against it. Without either pick the file is byte-identical whether it is
    passed or not, which `test_five_axis_gcode` asserts.

    `spin_stats`, when a list is passed, collects one `_spin_stats` dict per
    layer as the layers are solved -- the rotary travel and peak rate the file
    actually carries, for free, because every layer is solved here anyway.
    `surface_spin_report` turns it into the run summary. It is an out-parameter
    and not a second return value so that no caller has to change.

    `perp_tolerance_deg` is the perpendicularity-tolerance band
    (`_five_axis_readings`'s docstring has the maths): degrees the inner
    (collector-spin) joint may let the surface normal depart from exactly
    under the nozzle before it is made to track it, which is what stops an
    apex-centred pattern whipping that joint through a wide azimuth to serve
    a surface that barely tilts there. **Zero, the default, is byte-identical
    to today** -- this only ever reaches the readings function, which does
    nothing different at 0.

    `spin_cap_deg_s` is the MAX COLLECTOR SPIN, deg/s on collector arc at
    `speed`: the inner joint is held under it, and where that cannot be done
    inside `perp_tolerance_deg` the tolerance is overrun instead, as briefly
    and as little as the cap allows (`_route_spin_cap_c`). **Zero, the
    default, is off and byte-identical.** `perp_overrun`, when a list is
    passed, collects one array per layer of each point's overrun in degrees,
    measured on the angles actually written, for `surface_spin_report`.

    `accel_cap_deg_s2` and `tilt_accel_cap_deg_s2` are the MAX SPIN and TILT
    ACCELERATIONS. With any of the three limits set, the whole file's rotary
    motion -- both joints, the hops included -- is held under them on the clock the controller
    will run (`_limit_rotary_motion`), spending perpendicularity wherever the
    surface asks for more than the rig can do. A file already inside both is
    untouched. `min_speed` (mm/min) is how far the feed may be slowed first
    to keep perpendicularity (`_plan_feed`); 0 does not slow it. The GUI
    sends the critical translation speed plus a margin, never less.
    `limit_stats`, when a dict is passed, receives what that did.

    `surface_speeds`, when a list is passed, receives one entry per move (one
    per projected point, layers in order): the surface speed in mm/min the
    limiter's clock holds that move at, or `None` where no limiter clock was
    held and the move runs at the plain `speed`. It is how the five-axis
    preview draws the feed planner's slowdown on the file's own clock rather
    than a second estimate of it (`gui_surface._laid_surface_paths`).
    """
    if not speed > 0:
        raise ValueError("speed must be positive, got %r" % (speed,))
    rotaries = tuple(machine.rotaries)
    if len(rotaries) != 2:
        raise ValueError(
            "a five-axis collector needs exactly two rotary joints, got %d. "
            "One joint is a mandrel and `generate_mandrel_gcode` emits it."
            % (len(rotaries),))
    layers = [path] if hasattr(path, "uv") else list(path)
    if not layers:
        raise ValueError("no path to emit")

    def fmt(v):
        return format_coord(v, precision)

    import numpy as np

    words = [j.word.upper() for j in rotaries]
    mount_axial = None if frame is None else frame.mount_axial()
    lines = ["G21", "G90"]
    current_f = None
    prev_angles = None
    solved = []
    held = None
    move = 0
    last = None
    for li, pp in enumerate(layers):
        pts = [tuple(float(c) for c in p) for p in pp.world]
        nrms = [tuple(float(c) for c in n) for n in pp.normal]
        if len(pts) < 2:
            raise ValueError("a layer needs at least two points, got %d"
                             % (len(pts),))
        cmds, prev_angles = _five_axis_layer(
            machine, rotaries, pts, nrms, prev_angles, mount_axial,
            perp_tolerance_deg, arc=pp.arc_length(),
            speed_mm_s=float(speed) / 60.0, spin_cap_deg_s=spin_cap_deg_s)
        solved.append(cmds)
    # SOLVE EVERY LAYER, THEN LIMIT THE WHOLE FILE, THEN WRITE. The limits are
    # a property of the machine's motion, and the hop between two layers is a
    # move like any other, so they cannot be honoured one layer at a time.
    if (float(spin_cap_deg_s or 0.0) > 0.0
            or float(accel_cap_deg_s2 or 0.0) > 0.0
            or float(tilt_cap_deg_s or 0.0) > 0.0
            or float(tilt_accel_cap_deg_s2 or 0.0) > 0.0
            or float(max_feed or 0.0) > 0.0):
        solved, stats, held = _limit_rotary_motion(
            machine, rotaries, layers, solved, speed, feed_compensation,
            mount_axial, float(spin_cap_deg_s or 0.0),
            float(accel_cap_deg_s2 or 0.0),
            float(tilt_accel_cap_deg_s2 or 0.0),
            min_speed=float(min_speed or 0.0),
            tilt_rate=float(tilt_cap_deg_s or 0.0),
            max_feed=float(max_feed or 0.0))
        if limit_stats is not None:
            limit_stats.update(stats)
    # ONE F PER MOVE FOR THE WHOLE FILE, travels included (`_file_feeds`).
    feeds = _file_feeds(rotaries, layers, solved, speed, feed_compensation,
                        mount_axial)
    arcs_f = _surface_arcs(rotaries, layers, solved, mount_axial)
    if surface_speeds is not None:
        surface_speeds.extend(
            (60.0 * float(a) / float(held[k])
             if held is not None and held[k] > 0.0 and a is not None
             and a > 0.0 else None)
            for k, a in enumerate(arcs_f))
    # The same limits `_limit_rotary_motion` plans on, for the vertices inside
    # a routed travel that it never sees (`_plan_piece_feeds`).
    piece_limits = tuple(
        (float(r or 0.0) * LIMIT_PLAN_MARGIN,
         float(a or 0.0) * LIMIT_PLAN_MARGIN)
        for r, a in ((spin_cap_deg_s, accel_cap_deg_s2),
                     (tilt_cap_deg_s, tilt_accel_cap_deg_s2)))
    _mark(lines, "group", group=0)
    for li, (pp, cmds) in enumerate(zip(layers, solved)):
        _mark(lines, "layer", layer=li)
        if perp_overrun is not None:
            perp_overrun.append(np.maximum(_perp_error_deg(
                rotaries, pp.normal,
                [tuple(j.angle_from_word(x) for j, x in zip(rotaries, r))
                 for _, r in cmds]) - float(perp_tolerance_deg or 0.0), 0.0))
        if spin_stats is not None:
            spin_stats.append(_spin_stats(cmds, pp.arc_length(),
                                          float(speed) / 60.0))
        for i, (g, readings) in enumerate(cmds):
            body = "X%s Y%s Z%s" % (fmt(g[0]), fmt(g[1]), fmt(g[2]))
            for w, r in zip(words, readings):
                body += " %s%s" % (w, fmt(r))
            if li == 0 and i == 0:
                # NO `G92` HERE, AND THE LEAD-IN IS THE REASON. One datuming the
                # parser's assumed pre-program position to this first target
                # would collapse the approach to zero length. That does two jobs
                # and only one is wanted: it keeps `mew_frame.unwrap_path` from
                # being handed a point 35 mm off the collector, but it also
                # DELETES the approach -- no travel fibre, and a converter clock
                # that opens at this point while the machine is still 3.58 s
                # away from it, so the reveal runs ahead of the printhead.
                #
                # `gcode_to_fibers.take_travel` does the first job on its own,
                # clipping a travel chain to the stretch that is actually over
                # the collector (`mew_frame.DepositionFrame.covers`), so the
                # solver never sees the off-collector part and the approach
                # survives. Without that clip, Generate exits 2 with no `.depo`.
                #
                # The file's zero is unchanged either way -- that is
                # `machine_model.rotary_transform` subtracting
                # `collector_home_offset`, and no line here touches it.
                pass
            # The feed BELONGS TO THE MOVE ARRIVING at this point, and a
            # travel's belongs to it for the same reason a fibre move's does:
            # the jet is continuous, so the layer hop and the lead-in lay
            # fibre and are compensated and floored like any other move
            # (`_file_feeds`, `_surface_arcs`). Not the NEXT fibre segment's
            # compensated F: F on a line times that line's own move, and the
            # R10 dome's 48 mm lead-in would run at F15198.
            f = feeds[move]
            cur = [float(g[0]), float(g[1]), float(g[2])] + [
                float(x) for x in readings]
            base = [0.0] * len(cur) if last is None else last
            norm = math.sqrt(sum((p - q) ** 2 for p, q in zip(cur, base)))
            if held is not None:
                # The limiter's clock, held (`_limit_rotary_motion`) -- on
                # EVERY move, travels included: the limiter plans a travel's
                # duration like any other move's, and this is where that
                # duration is written.
                if held[move] > 0.0 and norm > 0.0:
                    f = 60.0 * norm / float(held[move])
            move += 1
            legs = None
            if i == 0 and norm > 0.0 and frame is not None:
                # THE LEAD-IN GETS ONE MORE QUESTION than a layer hop: it flies
                # in from off the part, so where it comes in MATTERS as well as
                # how high (`_leadin_approach`). A hop already starts and ends
                # on the design.
                mid = None
                if li == 0:
                    mid = _leadin_approach(
                        machine, rotaries, frame, mount_axial, words, layers,
                        base, cur,
                        [j.angle_from_word(x)
                         for j, x in zip(rotaries, readings)])
                legs = [base] + ([] if mid is None else [mid]) + [cur]
                routed = [_travel_waypoints(rotaries, frame, mount_axial, a, b)
                          for a, b in zip(legs[:-1], legs[1:])]
                if mid is None and routed[0] is None:
                    legs = None                 # one straight move, as before
            last = cur
            if legs is not None:
                # EVERY PIECE AT THE WHOLE TRAVEL'S SURFACE SPEED, not at its
                # F. Routing splits one travel into pieces that share its
                # rotary path but not its shape, so a piece that is nearly all
                # rotation crosses almost no collector: written at one shared
                # F the lead-in's pieces ran at 11-358 mm/min of surface speed
                # against a 528 floor while the straight move they came from
                # read 358. So the surface speed the straight
                # move was planned at is taken, and each piece is given the F
                # that holds it -- `v * norm / arc`, the same arithmetic
                # `mew_kinematics.surface_feeds` does for a fibre move. Every
                # piece then runs at the speed the plan asked for, and the
                # routed travel takes longer than the straight one by exactly
                # the extra collector it crosses.
                pieces = []
                for (a, b), r in zip(zip(legs[:-1], legs[1:]), routed):
                    pieces += [b] if r is None else [w for _s, w in r]
                arc_k = arcs_f[move - 1]
                # UNCOMPENSATED, EVERY PIECE KEEPS THE STRAIGHT MOVE'S F, as
                # every other line in such a file does: switched off, the
                # convention is one constant word over the 5-vector and there
                # is no surface speed being held for a piece to inherit.
                v_surf = 0.0
                if feed_compensation:
                    v_surf = (60.0 * arc_k / float(held[move - 1])
                              if held is not None and held[move - 1] > 0.0
                              and arc_k > 0.0
                              else (f * arc_k / norm if norm > 0.0 else 0.0))
                pf, parcs = _piece_feeds(rotaries, mount_axial, base,
                                         pieces, v_surf, f)
                if v_surf > 0.0 and any(x > 0.0 for p in piece_limits
                                        for x in p):
                    pf = _plan_piece_feeds(base, pieces, pf, parcs,
                                           piece_limits,
                                           float(min_speed or 0.0))
                for w_k, f_k in zip(pieces, pf):
                    line = "G0 " + " ".join(
                        "%s%s" % (w, fmt(x)) for w, x in
                        zip(("X", "Y", "Z") + tuple(words), w_k))
                    if current_f is None or fmt(f_k) != fmt(current_f):
                        line += " F%s" % fmt(f_k)
                        current_f = f_k
                    lines.append(line)
                continue
            code = "G0" if i == 0 else "G1"
            line = "%s %s" % (code, body)
            if current_f is None or fmt(f) != fmt(current_f):
                line += " F%s" % fmt(f)
                current_f = f
            lines.append(line)
    return "\n".join(lines) + "\n"

def _arc_length(sx, sy, ex, ey, i, j, cw):
    """Length (mm) of a G2/G3 arc from (sx, sy) to (ex, ey) about centre
    (sx + i, sy + j) -- the I/J offset-from-start convention. Mirrors
    gcode_to_fibers.arc_xy_points' sweep handling (G2/CW sweeps negative, G3/CCW
    positive); the length is R * |sweep|."""
    r = math.hypot(i, j)
    if r < 1e-12:
        return 0.0
    cx, cy = sx + i, sy + j
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    sweep = a1 - a0
    if cw:                              # G2: sweep must be negative (clockwise)
        while sweep >= 0.0:
            sweep -= 2.0 * math.pi
    else:                              # G3: sweep must be positive
        while sweep <= 0.0:
            sweep += 2.0 * math.pi
    return r * abs(sweep)


def estimate_print_time(gcode):
    """Estimate total print time (seconds) for a g-code string. The time half
    of `print_path_stats`, which carries the full account."""
    return print_path_stats(gcode)[0]


def print_path_stats(gcode):
    """(seconds, mm): the print time and the path length of a g-code string,
    integrating path length / feedrate over every move.

    The length is every move's, travels included, because a continuous-flow
    jet lays fiber on all of them -- the same moves the time counts.

    In continuous-flow MEW the head holds ONE commanded feed for the whole print
    and never truly rapids, so travels (G0) move at the same modal F as the
    deposition passes (G1/G2/G3) -- the convention
    gcode_to_fibers.parse_gcode(with_times=True) and gcode_player already use, so
    this estimate matches their playback clock. Feed is `_DEFAULT_FEED` until the
    first F word (only the leading positioning G0 is affected). G2/G3 arcs
    contribute their true arc length; the head starts at the origin, as the
    player assumes.

    Deliberately re-walks the text rather than reusing parse_gcode: that lives in
    gcode_to_fibers.py (which imports numpy) and returns per-fiber deposition
    times that omit the trailing exit travel. A self-contained walk keeps plain
    g-code generation numpy-free and counts every move, the off-plate exit
    included.

    A SPLIT-FEED mandrel file (`rotary_feed="split"`, Aerotech) is timed as
    Aerotech times it: F over the linear axes for any move that has one, E over
    the A degrees for a rotation-only move, and both read per second after a
    G76. That needs no A in `seg` for the first kind -- the linear length over
    F already IS the move's time there. A combined file has no E and no G76,
    and its total is what it always was."""
    x = y = z = a = 0.0
    feed = _DEFAULT_FEED               # mm/min, modal
    rot_feed = 0.0                     # deg/min, modal; split files only
    per = 1.0                          # 60 after G76: words are per second
    absolute = True                    # G90 / G91
    total = 0.0                        # seconds
    length = 0.0                       # mm

    def words(tokens):
        out = {}
        for tok in tokens[1:]:
            a = tok[0].upper()
            if a in ("X", "Y", "Z", "I", "J", "F", "E", "A"):
                try:
                    out[a] = float(tok[1:])
                except ValueError:
                    pass
        return out

    for raw in gcode.splitlines():
        line = raw.split(";", 1)[0].strip()        # tolerate inline comments
        if not line:
            continue
        tokens = line.split()
        code = tokens[0].upper()

        if code == "G90":
            absolute = True
            continue
        if code == "G91":
            absolute = False
            continue
        if code in ("G75", "G76"):                 # Aerotech time base
            per = 60.0 if code == "G76" else 1.0
            continue
        if code == "G92":                          # set position, no motion
            w = words(tokens)
            x = w.get("X", x)
            y = w.get("Y", y)
            z = w.get("Z", z)
            a = w.get("A", a)
            continue
        if code not in ("G0", "G1", "G00", "G01", "G2", "G3", "G02", "G03"):
            continue                       # G21, legacy M30, etc. -- no motion

        w = words(tokens)
        if w.get("F", 0.0) > 0:
            feed = w["F"] * per
        if w.get("E", 0.0) > 0:
            rot_feed = w["E"] * per
        nx = (w["X"] if absolute else x + w["X"]) if "X" in w else x
        ny = (w["Y"] if absolute else y + w["Y"]) if "Y" in w else y
        nz = (w["Z"] if absolute else z + w["Z"]) if "Z" in w else z
        na = (w["A"] if absolute else a + w["A"]) if "A" in w else a

        if code in ("G2", "G3", "G02", "G03"):
            seg = _arc_length(x, y, nx, ny, w.get("I", 0.0), w.get("J", 0.0),
                              cw=code in ("G2", "G02"))
            seg = math.hypot(seg, nz - z)          # Z helix (planar arcs -> dz 0)
        else:
            seg = math.sqrt((nx - x) ** 2 + (ny - y) ** 2 + (nz - z) ** 2)

        if feed > 0 and seg > 1e-12:
            total += seg / (feed / 60.0)
        elif rot_feed > 0 and abs(na - a) > 1e-12:
            total += abs(na - a) / (rot_feed / 60.0)
        length += seg
        x, y, z, a = nx, ny, nz, na
    return total, length


def format_duration(seconds):
    """A duration in seconds as a compact 'Xh Ym Zs' string, dropping units above
    the largest non-zero one ('2h 5m 30s', '5m 30s', '45s')."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
# Generation is a small fraction of a full parameters -> g-code -> STL run (the
# sag solve alone is 75-90 % of it), so it owns the front 3 % of the bar and
# hands the rest to the chain. That 3 % is written down once, in
# gcode_to_fibers.PROGRESS_WEIGHTS["generate"].
def _progress_say(enabled, frac, label):
    if enabled:
        import gcode_to_fibers
        print("%s %.4f %s" % (gcode_to_fibers.PROGRESS_MARKER, frac, label),
              flush=True)


def _progress_handover(enabled, gtf_argv):
    """Give the converter the rest of the bar, and tell it to report on it.

    Called on the two paths that chain, right before `gcode_to_fibers.main`.
    The span is a module global on that side for the same reason the fibre
    transform is: the chain is a function call, not a subprocess."""
    import gcode_to_fibers
    w = gcode_to_fibers.PROGRESS_WEIGHTS["generate"]
    gcode_to_fibers.set_progress_span(w, 1.0 - w)
    if enabled:
        gtf_argv.append("--progress")


# Axial origin of the print when --start-x is not given. Two numbers because a
# flat collector and a mandrel are zeroed differently: a slide sits in from the
# bed corner, a rod is zeroed under the nozzle at the print's near end.
_PLATE_START_X = 40.0
_TUBE_START_X = 0.0

#: What a `--tube-blocks` entry may set: the `--mandrel` flags that shape one
#: block's pattern, by their argparse names, plus the block's own three.
_TUBE_BLOCK_KEYS = ("family", "layers", "step", "speed", "winding_angle",
                    "tube_eta", "tube_rm", "tube_pivots", "cell_width",
                    "cell_height", "cell_fit", "cell_n_around", "cell_n_along",
                    "rib_angle", "cell_amplitude", "cell_graph", "shift")


def _cli_tube_cell(ns):
    """The `TubeCellDesign` the `--mandrel` flags in `ns` describe.

    `mew_unit_cell._resolve_params` REJECTS a knob the family does not take,
    so exactly ONE of the three family knobs reaches it."""
    if ns.tube_family == "reentrant":
        kw = {"rib_angle_deg": ns.rib_angle}
    elif ns.tube_family == "custom":
        kw = {"graph": ns.cell_graph}
    else:
        kw = {"amplitude": ns.cell_amplitude}
    return mew_unit_cell.design_tube_cells(
        ns.tube_family, ns.mandrel, ns.tube_length, ns.cell_width,
        ns.cell_height, fit=ns.cell_fit, n_around=ns.cell_n_around,
        n_axial=ns.cell_n_along, **kw)


#: The keys of a `--tube-blocks` entry that shape its PATTERN. An entry with
#: none of them is the `--mandrel` flags' own tube.
_TUBE_DESIGN_KEYS = tuple(k for k in _TUBE_BLOCK_KEYS
                          if k not in ("layers", "step", "speed", "shift"))

#: What one LAYER of a tube may set for itself -- the G-code page's Layer
#: parameters for a tube: the pattern and its size, its speed, and how far
#: the nozzle steps out to it.
#: "pore" (a side, micrometres) is the page's: it resolves a winding layer's
#: pore into `tube_pivots` before an entry reaches the worker. "shift" (mm
#: along the rod from the tube's own start) is what the page's per-layer
#: Position on mandrel / Offset resolve to, by the tube's own placement rule.
TUBE_LAYER_KEYS = ("family", "winding_angle", "pore", "cell_width",
                   "cell_height", "rib_angle", "cell_amplitude", "speed",
                   "step", "shift")


def tube_layer_blocks(layers, overrides):
    """Per-layer tube settings -> the `--tube-blocks` entries that lay them.

    `overrides` is {layer, 0-based: {TUBE_LAYER_KEYS key: value}}; a layer
    with none is the tube's own. Consecutive layers that ask for the same
    thing are one block, so "layers 5-8 re-entrant" on a 12-layer winding is
    three entries. `step` is the step OUT TO that layer, so the first layer's
    has nothing to step from and is ignored; a block's step is its second
    layer's when it starts the tube. Layers past `layers` are ignored."""
    layers = int(layers)
    out = []
    cur = None
    for k in range(layers):
        o = dict(overrides.get(k) or {})
        bad = sorted(set(o) - set(TUBE_LAYER_KEYS))
        if bad:
            raise ValueError("layer %d: unknown tube setting(s) %s; a layer "
                             "takes %s" % (k + 1, ", ".join(bad),
                                           ", ".join(TUBE_LAYER_KEYS)))
        step = o.pop("step", None)
        if cur is not None and o == cur["keys"] and (
                cur["open"] or step == cur["step"]):
            cur["layers"] += 1
            if cur["open"]:
                cur["step"], cur["open"] = step, False
            continue
        cur = dict(keys=o, layers=1, step=step, open=(k == 0))
        out.append(cur)
    entries = []
    for c in out:
        e = dict(c["keys"], layers=c["layers"])
        if c["step"] is not None and not (c["open"] and c is out[0]):
            e["step"] = c["step"]
        entries.append(e)
    return entries


def tube_blocks_from_entries(entries, base, base_design=None):
    """`--tube-blocks` entries -> ([TubeBlock], one line per block).

    `base` is an argparse.Namespace of the `--mandrel` flags (their dest
    names: `tube_family`, `winding_angle`, `cell_width`...). Each entry is
    those flags with its own keys laid over them, so a key left out takes the
    flag's value. An entry that names no pattern key IS the flags' tube:
    `base_design` when given (the caller already resolved it), else resolved
    from `base`. One that does is resolved AT THAT TUBE'S LENGTH -- every
    block shares it -- so a `fit="fit"` cell or an end-turn winding comes out
    exactly as long, and anything else is refused by the emitter."""
    if not isinstance(entries, list) or not entries:
        raise ValueError("--tube-blocks must hold a non-empty JSON list of "
                         "blocks, got %r" % (type(entries).__name__,))
    families = ("zigzag",) + mew_unit_cell.MANDREL_FAMILIES
    if base_design is None:
        base_design = _cli_tube_design(base)
    blocks, lines = [], []
    for bi, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError("--tube-blocks entry %d is not an object"
                             % (bi + 1,))
        unknown = sorted(set(entry) - set(_TUBE_BLOCK_KEYS))
        if unknown:
            raise ValueError("--tube-blocks entry %d: unknown key(s) %s; a "
                             "block takes %s" % (bi + 1, ", ".join(unknown),
                                                 ", ".join(_TUBE_BLOCK_KEYS)))
        ns = argparse.Namespace(**vars(base))
        for key, value in entry.items():
            setattr(ns, "tube_family" if key == "family" else key, value)
        if ns.tube_family not in families:
            raise ValueError("--tube-blocks entry %d: unknown family %r; "
                             "expected one of %s"
                             % (bi + 1, ns.tube_family, ", ".join(families)))
        if not any(k in entry for k in _TUBE_DESIGN_KEYS):
            design = base_design
        else:
            ns.tube_length = base_design.length
            design = _cli_tube_design(ns)
        if ns.tube_family == "zigzag":
            what = ("zigzag at %.1f deg, N=%d pivots, pore %.4f mm^2"
                    % (design.omega_deg, design.n_pivots, design.pore_area))
            if design.end_turn_deg > 0.0:
                what += ", end turn %.3f deg" % design.end_turn_deg
        else:
            what = ("%s, %d x %d cells of %.4f x %.4f mm"
                    % (design.family, design.n_axial, design.n_around,
                       design.cell_w, design.cell_h))
        block = TubeBlock(design, entry.get("layers", base.layers),
                          step=entry.get("step"), speed=entry.get("speed"),
                          turn_out=getattr(base, "tube_turn_out", 0.0),
                          shift=entry.get("shift") or 0.0)
        blocks.append(block)
        lines.append("  block %d: %s layer(s) of %s" % (bi + 1, block.layers,
                                                        what))
    return blocks, lines


def _cli_tube_design(ns):
    """The design the `--mandrel` flags in `ns` describe, either family."""
    if ns.tube_family == "zigzag":
        return mandrel_design(ns.mandrel, ns.winding_angle, ns.tube_length,
                              eta=ns.tube_eta, rm=ns.tube_rm,
                              pivots=ns.tube_pivots)
    return _cli_tube_cell(ns)


#: What one `--tube-scaffolds` entry may set: one tube's own flags, by their
#: argparse dest names, plus where it sits. Everything else -- the rod, the
#: speed policy, the feeds, the start angle, the exit -- is the program's.
TUBE_SCAFFOLD_KEYS = ("tube_family", "tube_length", "winding_angle",
                      "tube_eta", "tube_rm", "tube_pivots", "tube_strategy",
                      "cell_width", "cell_height", "cell_fit", "cell_n_around",
                      "cell_n_along", "rib_angle", "cell_amplitude",
                      "cell_graph", "layers", "speed", "fiber_diameter",
                      "tube_layer_step", "tube_turn_out", "tube_blocks",
                      "start_x", "start_z")


def _cli_tube_scaffolds(args, ap):
    """`--tube-scaffolds` -> ([TubeScaffold], summary lines).

    Each entry is a WHOLE tube: a key left out takes the flag's default
    (`ap.get_default`), never the command line's, so tube 2 does not
    inherit tube 1's blocks or pivots by accident. A value may be a string,
    as the GUI sends it; it is converted by the flag's own type."""
    spec = args.tube_scaffolds.strip()
    if spec.startswith("["):
        raw = json.loads(spec)
    else:
        with open(args.tube_scaffolds) as fh:
            raw = json.load(fh)
    if not isinstance(raw, list) or not raw:
        raise ValueError("--tube-scaffolds must hold a non-empty JSON list of "
                         "tubes")
    types = {a.dest: a.type for a in ap._actions}
    tubes = []
    lines = ["Wrote %s: %d tube(s) on a d%.3f mm mandrel"
             % (args.output, len(raw), args.mandrel)]
    for ti, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError("--tube-scaffolds entry %d is not an object"
                             % (ti + 1,))
        unknown = sorted(set(entry) - set(TUBE_SCAFFOLD_KEYS))
        if unknown:
            raise ValueError("--tube-scaffolds entry %d: unknown key(s) %s; a "
                             "tube takes %s" % (ti + 1, ", ".join(unknown),
                                                ", ".join(TUBE_SCAFFOLD_KEYS)))
        ns = argparse.Namespace(**vars(args))
        for key in TUBE_SCAFFOLD_KEYS:
            if key in ("start_x", "start_z"):
                setattr(ns, key, 0.0)
                continue
            setattr(ns, key, ap.get_default(key))
        for key, value in entry.items():
            conv = types.get(key)
            if value is not None and conv is not None and isinstance(
                    value, str) and key != "tube_blocks":
                value = conv(value)
            elif key in ("start_x", "start_z") and value is not None:
                value = float(value)
            setattr(ns, key, value)
        if ns.tube_strategy not in MANDREL_STRATEGIES:
            raise ValueError("tube %d: unknown mandrel strategy %r"
                             % (ti + 1, ns.tube_strategy))
        if ns.tube_blocks:
            blk = ns.tube_blocks
            if isinstance(blk, str):
                blk = json.loads(blk)
            blocks, blines = tube_blocks_from_entries(blk, ns)
        else:
            design = _cli_tube_design(ns)
            blocks = [TubeBlock(design, ns.layers,
                                turn_out=ns.tube_turn_out)]
            blines = []
        tube = TubeScaffold(
            blocks, ns.fiber_diameter, ns.speed, layer_step=ns.tube_layer_step,
            start_x=ns.start_x,
            start_z=ns.start_z + args.start_z + args.mandrel_surface_z)
        tubes.append(tube)
        d = blocks[0].design
        what = ("zigzag at %.1f deg, N=%d pivots" % (d.omega_deg, d.n_pivots)
                if ns.tube_family == "zigzag" else
                "%s, %d x %d cells" % (d.family, d.n_axial, d.n_around))
        lines.append("  tube %d: %s x %.4f mm from X %.4f, %d layer(s), "
                     "starts %.4f mm out"
                     % (ti + 1, what, d.length, ns.start_x,
                        sum(int(b.layers) for b in blocks), ns.start_z))
        lines += ["  " + ln for ln in blines]
    return tubes, lines


def _cli_tube_blocks(args):
    """`--tube-blocks` -> ([TubeBlock], summary lines). The flag is a JSON
    file, or the JSON itself when it starts with ``[`` (what the GUI sends,
    so the argv it compares to decide on a regeneration carries the
    blocks)."""
    spec = args.tube_blocks.strip()
    if spec.startswith("["):
        raw = json.loads(spec)
    else:
        with open(args.tube_blocks) as fh:
            raw = json.load(fh)
    blocks, lines = tube_blocks_from_entries(raw, args)
    lines.insert(0, "Wrote %s: tube d%.3f x %.4f mm, %d block(s), %d layer(s)"
                 % (args.output, args.mandrel, blocks[0].design.length,
                    len(blocks), sum(int(b.layers) for b in blocks)))
    return blocks, lines


def main(argv=None):
    """`_main`, with a path it cannot open or write reported in one line.

    Stdout is a PIPE when the GUI runs this, and a pipe on Windows takes the
    ANSI code page, which cannot encode a Cyrillic or CJK path: the first
    `print` of one killed the worker. Unencodable characters are escaped
    instead (`gcode_to_fibers.main` does the same)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):     # not a TextIOWrapper
            pass
    try:
        return _main(argv)
    except OSError as exc:
        print("error: %s" % (exc,), file=sys.stderr)
        return 2


def _write_gcode_output(args, gcode, groups=None):
    """Write `gcode` to `-o`, plus the `--line-map` beside it, and with
    `--gcode-doc` the document's PARSE form instead of the bare program.
    Returns what was written to `-o`.

    `groups` is the `--scaffold-groups` list; None (a tube, a surface) means
    one group per "group" mark, each with the layer marks that follow it."""
    if _LINE_MARKS is None:
        with open(args.output, "w") as fh:
            fh.write(gcode)
        return gcode
    import mew_gcode
    if groups is None:
        # one group per "group" mark, counting the layers after it: a tube
        # laid as side-by-side copies is one scaffold group per copy
        groups = []
        for m in _LINE_MARKS:
            if m["kind"] == "group" or not groups:
                groups.append({"name": "scaffold%d" % (len(groups) + 1),
                               "layers": 0})
            if m["kind"] == "layer":
                groups[-1]["layers"] += 1
        groups = groups or [{"name": "scaffold1", "layers": 0}]
    line_map = mew_gcode.LineMap(_LINE_MARKS, len(gcode.splitlines()),
                                 groups, sha1=mew_gcode.text_sha1(gcode))
    if args.line_map:
        line_map.save(args.line_map)
    text = gcode
    if args.gcode_doc:
        try:
            doc = mew_gcode.Document.load(args.gcode_doc)
        except (OSError, ValueError, KeyError) as exc:
            raise SystemExit("could not read --gcode-doc: %s" % (exc,))
        doc.rebase(gcode, line_map)
        text = doc.parse_text()
    with open(args.output, "w") as fh:
        fh.write(text)
    return text


def _main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate MEW scaffold G-code (and optionally its sagged "
                    "fiber STL) from print parameters.")
    ap.add_argument("-o", "--output", default="scaffold.gcode",
                    help="output .gcode file (default: scaffold.gcode)")
    ap.add_argument("--stl", default=None,
                    help="also render the sagged fiber STL to this path "
                         "(chains into gcode_to_fibers.py)")
    ap.add_argument("--line-map", default=None, metavar="PATH",
                    help="also write the g-code's STRUCTURE to this JSON file: "
                         "which scaffold, layer and pass every line belongs to "
                         "(`mew_gcode.LineMap`). Out of band -- the g-code "
                         "itself is byte-identical with or without it")
    ap.add_argument("--gcode-doc", default=None, metavar="DOC.json",
                    help="apply a saved G-code page document (its hand edits, "
                         "rules and transforms; `mew_gcode.Document`) to the "
                         "generated g-code and write its PARSE form to -o, so "
                         "the STL, .depo and player see the edits. Off by "
                         "default")
    ap.add_argument("--progress", action="store_true",
                    help="emit machine-readable progress lines on stdout for a "
                         "GUI to drive a determinate bar with, covering THIS "
                         "program's generation and, when --stl chains into it, "
                         "the converter's parse/sag/mesh/write. See "
                         "gcode_to_fibers.py --progress. Off by default")
    ap.add_argument("--no-mesh", action="store_true",
                    help="with --stl: run the sag solve and write the .depo cache "
                         "but SKIP building the STL mesh (forwarded to the "
                         "converter). The viewers render the cached centrelines as "
                         "wireframe/tubes; the exact STL is built later on demand")

    ap.add_argument("--scaffolds-json", default=None,
                    help="path to a JSON file holding a LIST of scaffold "
                         "parameter dicts; emits them all as one continuous "
                         "program (stacked or side by side, exactly as listed). "
                         "When given, the single-scaffold geometry flags "
                         "(--width, --height, ... --start-z) are IGNORED -- each "
                         "scaffold carries its own. The optional stabilization, "
                         "build-plate and STL flags still apply to the whole file. "
                         "Each dict: width, height, stepover, rotation, "
                         "fiber_diameter, layers, speed, start_x, start_y, "
                         "start_z, start_angle, arc_segments, and optional "
                         "arc_radius_mult and loop_speed")

    ap.add_argument("--width", type=float, default=30.0,
                    help="scaffold footprint width (X) in mm; also the "
                         "diameter for --shape circle/polygon (default: 30)")
    ap.add_argument("--height", type=float, default=30.0,
                    help="scaffold footprint height (Y) in mm; ignored for "
                         "--shape circle/polygon (default: 30)")
    ap.add_argument("--shape", choices=_SHAPES, default="rectangle",
                    help="footprint outline: rectangle (default; --width x "
                         "--height box), circle (diameter --width), ellipse "
                         "(axes --width x --height), polygon (regular "
                         "--shape-sides-gon, diameter --width), or custom "
                         "(--footprint-polygon). Non-rectangle shapes must be "
                         "CONVEX")
    ap.add_argument("--shape-sides", type=int, default=64,
                    help="circle/ellipse smoothness (vertex count) or regular "
                         "polygon side count (default: 64; min 3)")
    ap.add_argument("--footprint-polygon", default=None,
                    help="[--shape custom] outline vertices, e.g. "
                         "'0,0; 10,0; 10,8; 0,8' -- ';'-separated ABSOLUTE (x, y) "
                         "mm pairs, recentred on their bounding box. Must be convex")
    ap.add_argument("--footprint-angle", type=float, default=0.0,
                    help="rotate the WHOLE footprint (outline + fill) about its "
                         "centre by this many degrees (default: 0 -> byte-identical)")
    ap.add_argument("--stepover", type=float, default=0.5,
                    help="centre-to-centre spacing between adjacent fibers in "
                         "mm (default: 0.5)")
    ap.add_argument("--rotation", type=float, default=90.0,
                    help="per-layer rotation of the fiber direction in degrees "
                         "(square 90, triangle 60, decagon 36; default: 90)")
    ap.add_argument("-d", "--fiber-diameter", type=float, default=0.02,
                    help="fiber diameter in mm; also the layer height "
                         "(default: 0.02)")
    ap.add_argument("--layers", type=int, default=10,
                    help="layers of wall (default: 10). On a mandrel a layer "
                         "is one WHOLE sheet of the pattern -- a zigzag "
                         "circuit out and back over every pivot, or one "
                         "traversal of a cell -- and the nozzle steps out "
                         "--tube-layer-step (default one fibre diameter) "
                         "between them")
    ap.add_argument("--speed", type=float, default=600.0,
                    help="print feedrate in mm/min, emitted as F (default: 600)")
    ap.add_argument("--loop-speed", type=float, default=None,
                    help="feedrate in mm/min for the turnaround links joining "
                         "parallel fibers (default: inherit --speed). Lets the "
                         "linking loops print at a different speed than the main "
                         "scaffold passes")
    # ONE FLAG, TWO COLLECTORS, TWO DEFAULTS. A plate's 40 mm puts the slide in
    # the middle of the bed; a TUBE's axial origin is 0, because you zero the
    # nozzle on the rod and the print starts there. Sharing the plate's default
    # put every mandrel program 40 mm along its own collector, and because the
    # parser starts from an assumed origin that showed up downstream as a 40 mm
    # approach fibre running off the end of the rod. Resolved per collector below
    # (`_PLATE_START_X` / `_TUBE_START_X`) rather than shared; an explicit
    # --start-x still wins for both.
    ap.add_argument("--start-x", type=float, default=None,
                    help="X of the footprint's lower-left corner (default: 40 "
                         "on a flat collector, 0 on a mandrel -- a tube is "
                         "zeroed on its own rod)")
    ap.add_argument("--start-y", type=float, default=10.0,
                    help="Y of the footprint's lower-left corner (default: 10)")
    ap.add_argument("--start-z", type=float, default=0.0,
                    help="Z offset of the first layer in mm (default: 0). Added on "
                         "top of the plate-top datum (--plate-top-z), so 0 prints "
                         "directly on the plate and a positive value raises it")
    ap.add_argument("--start-angle", type=float, default=0.0,
                    help="fiber direction of the first layer in degrees "
                         "(default: 0)")
    ap.add_argument("--angle-pattern", default=None,
                    help="explicit per-layer fiber angles, e.g. '0,60,90': layer "
                         "0 takes the first angle, layer 1 the second, ..., "
                         "cycling when there are more layers than entries. When "
                         "given, OVERRIDES --rotation and --start-angle. Accepts "
                         "commas and/or spaces. Ignored in --scaffolds-json mode "
                         "(each scaffold carries its own 'angle_pattern')")
    ap.add_argument("--angle-stepover-pattern", default=None,
                    help="per-layer stepover to pair with --angle-pattern, e.g. "
                         "'0.3,,0.5': slot i overrides layer i's pass pitch, an "
                         "EMPTY slot keeps --stepover for that layer. Split on "
                         "commas only (blank slots are positional); cycles with "
                         "--angle-pattern. Ignored without --angle-pattern and in "
                         "--scaffolds-json mode (each scaffold carries its own "
                         "'angle_stepovers')")
    ap.add_argument("--angle-offset-pattern", default=None,
                    help="per-layer period offset (mm) to pair with "
                         "--angle-pattern, e.g. '0,,0.25': slot i shifts layer i's "
                         "fibre grid that far PERPENDICULAR to its fibres, an EMPTY "
                         "slot leaves layer i unshifted. Same comma-only / cycling "
                         "rules as --angle-stepover-pattern. Ignored without "
                         "--angle-pattern and in --scaffolds-json mode (each "
                         "scaffold carries its own 'angle_offsets')")
    ap.add_argument("--linear-pattern", default=None,
                    help="wave each straight fiber through a shape, e.g. "
                         "'0,0; 0,1; 1,1; 1,0' for a unit square. Each "
                         "';'-separated point is an ABSOLUTE (x, y) vertex in mm in "
                         "the fiber-local frame -- x runs along the fiber, y is "
                         "perpendicular. The fiber traces the vertices in order and "
                         "the shape repeats every --linear-wavelength + "
                         "--linear-spacing mm. Requires --linear-wavelength > 0. "
                         "Ignored in --scaffolds-json mode (each scaffold carries "
                         "its own 'linear_pattern'/'linear_wavelength'/"
                         "'linear_spacing')")
    ap.add_argument("--linear-wavelength", type=float, default=0.0,
                    help="travel distance in mm of one repeat of --linear-pattern "
                         "(the shape footprint along the fiber; default: 0 = off)")
    ap.add_argument("--linear-spacing", type=float, default=0.0,
                    help="flat gap in mm inserted after each --linear-pattern "
                         "repeat, so shapes tile without touching (default: 0)")
    ap.add_argument("--arc-radius-mult", type=float, default=1.5,
                    help="radius of the ballooning turnaround loops joining "
                         "adjacent passes, as a MULTIPLE of the stepover "
                         "(default: 1.5; the loop is wider than the pitch). "
                         "Scaling with stepover keeps the loops sane at any pore "
                         "size -- a fixed mm radius makes giant overlapping loops "
                         "at tight stepovers. 0 = straight connectors")
    ap.add_argument("--arc-segments", type=int, default=8,
                    help="line segments per half-turn of arc (default: 8). "
                         "Ignored when --g2g3-arcs is set (real arcs need no "
                         "chord subdivision)")
    ap.add_argument("--g2g3-arcs", action="store_true",
                    help="emit each turnaround loop as one true circular arc "
                         "(G3 CCW / G2 CW with I/J centre offsets) instead of a "
                         "fan of G1 chords -- for machines that interpolate arcs. "
                         "Negates --arc-segments; the loop geometry is identical. "
                         "In --scaffolds-json mode this applies to every scaffold")
    ap.add_argument("--flat-layers", action="store_true",
                    help="drop the per-layer Z step: command every layer at its "
                         "start Z instead of stacking layer i at start Z + i * "
                         "fiber diameter, leaving the nozzle height to the "
                         "machine. The layer pattern is unaffected. In "
                         "--scaffolds-json mode this applies to every scaffold; "
                         "the stabilization base goes flat either way")
    ap.add_argument("--link-loops", choices=("off", "auto", "manual"),
                    default="off",
                    help="add a lead-in/lead-out LOOP at near-coincident sharp "
                         "layer changes (e.g. a square corner, where one layer's "
                         "exit and the next layer's entry share a point) so the jet "
                         "rounds the turn out in the loop instead of on the "
                         "scaffold. 'off' (default) keeps the straight hop; 'auto' "
                         "loops when the turn is at least --link-loop-angle; "
                         "'manual' drops that test. A layer change reached along a "
                         "TRAVEL fibre is looped only with --link-loop-travel. "
                         "Loop radius = the turnaround --arc-radius-mult. In "
                         "--scaffolds-json mode each scaffold carries its own "
                         "'link_loops'")
    ap.add_argument("--link-loop-angle", type=float, default=25.0,
                    help="[--link-loops auto] minimum heading change in degrees "
                         "for a layer change to get a loop (default: 25)")
    ap.add_argument("--link-loop-dist", type=float, default=0.5,
                    help="[--link-loops] how far back along the travelled path "
                         "the loop's lead-in heading is measured, in mm; also the "
                         "max exit->entry gap treated as a coincident corner "
                         "without --link-loop-travel (default: 0.5)")
    # The flag is spelled BOTH ways on purpose. `--link-loop-travel` is the live
    # one (the default is off); `--no-link-loop-travel` is kept because saved
    # presets from when the default was on carry it, and a command line that does must
    # keep meaning what it said rather than becoming an unrecognised-argument
    # error. Both write the same dest, so the last one on the line wins.
    ap.add_argument("--link-loop-travel", dest="link_loop_travel",
                    action="store_true", default=False,
                    help="[--link-loops] ALSO loop a layer change joined by a "
                         "TRAVEL fibre, not only one landing on a shared corner. "
                         "Off by default: it is a real corner, but rounding it "
                         "lays a lasso out in open space")
    ap.add_argument("--no-link-loop-travel", dest="link_loop_travel",
                    action="store_false",
                    help="[--link-loops] the explicit off, and the default. Only "
                         "loop where the previous layer's exit and the next "
                         "layer's entry share a corner (within --link-loop-dist). "
                         "Kept so a command line written while travel loops were "
                         "the default still parses")

    ap.add_argument("--perimeter-travel", dest="perimeter_travel",
                    action="store_true", default=False,
                    help="route every automatic travel (lead-in, layer changes, "
                         "the hop to the next scaffold, the exit) along the "
                         "scaffold's perimeter --perimeter-travel-offset mm "
                         "clear of its outermost fibre, instead of round its "
                         "bounding box. In --scaffolds-json mode each scaffold "
                         "carries its own 'perimeter_travel'")
    ap.add_argument("--perimeter-travel-offset", type=float, default=1.0,
                    help="[--perimeter-travel] how far outside the outermost "
                         "fibre the travel runs, mm (default: 1)")
    ap.add_argument("--stepover-end", type=float, default=None,
                    help="grade the pore size WITHIN each layer: the fiber "
                         "spacing runs from --stepover at the footprint's start "
                         "edge to this at its far edge, mm, along "
                         "--grade-angle (default: none, even spacing). In "
                         "--scaffolds-json mode each scaffold carries its own "
                         "'stepover_end'")
    ap.add_argument("--grade-angle", type=float, default=0.0,
                    help="[--stepover-end] the direction the spacing grades "
                         "along, degrees: 0 = left to right along X, 90 = "
                         "front to back along Y (default: 0)")

    # --- optional stabilization scaffold ----------------------------------- #
    # Printed before the main scaffold, in the SAME g-code file / continuous
    # fiber. Inherits --speed and --arc-segments; always a square grid (90 deg
    # layer rotation, 0 deg start angle) regardless of the main --rotation /
    # --start-angle.
    ap.add_argument("--stabilization", action="store_true",
                    help="prepend a stabilization scaffold (same file, one "
                         "continuous fiber) before the main scaffold")
    ap.add_argument("--stab-width", type=float, default=15.0,
                    help="[--stabilization] footprint width (X) in mm (default: 15)")
    ap.add_argument("--stab-height", type=float, default=15.0,
                    help="[--stabilization] footprint height (Y) in mm (default: 15)")
    ap.add_argument("--stab-stepover", type=float, default=1.7,
                    help="[--stabilization] fiber spacing in mm (default: 1.7)")
    ap.add_argument("--stab-fiber-diameter", type=float, default=None,
                    help="[--stabilization] fiber diameter / layer height in mm "
                         "(default: inherit the main --fiber-diameter)")
    ap.add_argument("--stab-layers", type=int, default=10,
                    help="[--stabilization] number of layers (default: 10)")
    ap.add_argument("--stab-arc-radius-mult", type=float, default=None,
                    help="[--stabilization] turnaround loop radius as a multiple "
                         "of stab-stepover (default: 1.5)")
    ap.add_argument("--stab-start-x", type=float, default=10.0,
                    help="[--stabilization] X of the footprint's lower-left "
                         "corner (default: 10)")
    ap.add_argument("--stab-start-y", type=float, default=10.0,
                    help="[--stabilization] Y of the footprint's lower-left "
                         "corner (default: 10)")

    # --- pass-through controls for the optional STL step ------------------- #
    ap.add_argument("-f", "--faces", type=int, default=12,
                    help='[--stl] faces on the fiber cross-section (default: 12)')
    ap.add_argument("--no-sag", action="store_true",
                    help="[--stl] disable sagging (render straight bars)")
    ap.add_argument("--sag-approx", action="store_true",
                    help="[--stl] use the APPROXIMATE sag solver instead of the "
                         "exact contact solve (see gcode_to_fibers --sag-approx). "
                         "Forwarded verbatim; ignored with --no-sag")
    ap.add_argument("--sag-multiplier", type=float, default=1.0,
                    help="[--stl] scale the droop the algorithm computes from "
                         "pore size and fiber diameter (1.0 = algorithm default, "
                         ">1 more, <1 less, 0 none). The only sag knob")
    ap.add_argument("--mesh-tolerance", type=float, default=1.0, metavar="MULT",
                    help="[--stl] thin the sagged centreline before tubing it, "
                         "within MULT x the tube's own faceting error (default: "
                         "1.0; 0 restores the previous byte-identical mesh). See "
                         "gcode_to_fibers --mesh-tolerance")
    # 0 = never, matching gcode_to_fibers._DEFAULT_SAG_RESET_LAYERS (kept a literal
    # here: this module only imports the converter inside main()). The period is part
    # of the .depo cache key, so these defaults must not drift apart.
    ap.add_argument("--render-travel", action="store_true",
                    help="[--stl] render travel (G0) moves as deposited fibre -- the "
                         "jet never stops, so it lays material while travelling "
                         "(entry/exit tails, and the linking fibre at a layer "
                         "change). Forwarded to the converter; off = byte-identical.")
    # The travel fibre is always SOLVED with the print (the jet never stops);
    # this is the separate question of whether it is tubed into the exported
    # mesh. It exists here only to be forwarded -- `gcode_to_fibers` owns the
    # behaviour -- but it has to exist, because the GUI puts it on the scaffold
    # worker's argv and `parse_args` rejects a flag it has never heard of. It
    # did: every plate run with "Render travel (G0) fibre in the exported STL"
    # ticked died with "unrecognized arguments: --travel-in-stl", in both
    # physics modes (mew_export forwards it verbatim in `rest`).
    ap.add_argument("--travel-in-stl", action="store_true",
                    help="[--stl --render-travel] tube the deposited travel fibre "
                         "into the exported STL as well. Off (the default) the "
                         "travel is still solved with the print -- it holds the "
                         "fibres above it up -- but is cut out of the mesh. "
                         "Forwarded to the converter.")
    ap.add_argument("--sag-reset-layers", type=int, default=0,
                    help="[--stl] lay every Nth layer straight and let sagging "
                         "re-accumulate from it -- the observed 'fibres go "
                         "straight again every so many layers' behaviour "
                         "(default: 0 = never reset; 20 is a typical period)")
    ap.add_argument("--sag-resolution", type=float, default=1.0,
                    metavar="FACTOR",
                    help="[--stl] sample the sagged centreline FACTOR times "
                         "more densely (default 1 = shipped density). Forwarded "
                         "to the converter (see gcode_to_fibers)")
    ap.add_argument("--layer-by-deposition", action="store_true",
                    help="[--stl] sag layers in deposition (g-code) order instead "
                         "of by global height, so a multi-scaffold stack emitted by "
                         "scaffold number rests each scaffold on top of the last "
                         "rather than interleaving by height. Forwarded to the "
                         "converter; off = byte-identical (see gcode_to_fibers)")
    # --- build plate (the support the print sits on) ----------------------- #
    # A circular or rectangular plate, positioned
    # by its centre relative to home; the print starts on its top surface
    # (--plate-top-z, computed by the GUI from the machine floor + thickness) and
    # the end-of-print exit move clears its edge. --plate (draw the grey STL) is
    # forwarded to the converter; the geometry/datum args apply regardless.
    ap.add_argument("--plate", action="store_true",
                    help="[--stl] draw the build-plate slab beneath the fibers")
    ap.add_argument("--plate-shape", choices=("rectangular", "circular"),
                    default="rectangular", help="build-plate shape")
    ap.add_argument("--plate-size-x", type=float, default=76.2,
                    help="[rectangular] plate size X in mm (default: 76.2 = 3 in)")
    ap.add_argument("--plate-size-y", type=float, default=50.8,
                    help="[rectangular] plate size Y in mm (default: 50.8 = 2 in)")
    ap.add_argument("--plate-diameter", type=float, default=50.0,
                    help="[circular] plate diameter in mm (default: 50)")
    ap.add_argument("--plate-thickness", type=float, default=1.0,
                    help="plate thickness (Z) in mm (default: 1.0)")
    ap.add_argument("--plate-center-x", type=float, default=38.1,
                    help="plate centre X in mm, relative to home (default: 38.1)")
    ap.add_argument("--plate-center-y", type=float, default=25.4,
                    help="plate centre Y in mm, relative to home (default: 25.4)")
    ap.add_argument("--plate-top-z", type=float, default=0.0,
                    help="Z of the plate's top surface = the print-start datum "
                         "(machine floor + thickness); added to every scaffold's "
                         "start Z (default: 0)")
    ap.add_argument("--origin-offset", type=float, nargs=3,
                    default=(0.0, 0.0, 0.0), metavar=("DX", "DY", "DZ"),
                    help="translate the whole exported g-code by this offset to "
                         "re-base the coordinate origin (e.g. onto the collector "
                         "corner). Forwarded to the converter so the plate STL "
                         "shifts to match. Default: no shift")

    # --- end-of-print exit move -------------------------------------------- #
    # After the last deposition the polymer keeps flowing; left over the part it
    # piles onto the finished scaffold. By default the program ends with one
    # travel that runs the nozzle off the build plate (the plate footprint,
    # centred at --plate-center-x/y) so that trailing flow lands off the part. On
    # by default; needs no tuning.
    ap.add_argument("--exit-margin", type=float, default=gcode_exit.DEFAULT_MARGIN,
                    help="how far past the nearest plate edge to run the nozzle "
                         f"at the end, in mm (default: {gcode_exit.DEFAULT_MARGIN:g})")
    ap.add_argument("--no-exit-move", action="store_true",
                    help="do not append the end-of-print move off the plate")
    ap.add_argument("--travel-offset", type=float, default=0.0,
                    help="mm to run the nozzle clear of each finished scaffold's "
                         "footprint before traveling to the next one, so the "
                         "continuous-flow drag leaves the part instead of crossing "
                         "it (default: 0 = straight hop). Applies between "
                         "scaffolds in --scaffolds-json mode")
    # -- mandrel mode ------------------------------------------------------ #
    # A separate branch, not extra knobs on the planar flags: a tube shares no
    # parameter with a footprint scaffold except the fibre diameter, the layer
    # count and the speed. Every flag above is IGNORED under --mandrel, which is
    # why the branch returns before any of them is read.
    ap.add_argument("--mandrel", type=float, default=None, metavar="DIAM",
                    help="MANDREL MODE: print a tube on a rotating collector of "
                         "this DIAMETER in mm, emitting X + the rotary A instead "
                         "of X/Y. Needs --tube-length and --winding-angle. The "
                         "planar footprint flags are ignored")
    ap.add_argument("--tube-length", type=float, default=10.0,
                    help="[--mandrel] requested tube length in mm; the emitted "
                         "tube is the nearest length that CLOSES (default: 10)")
    ap.add_argument("--winding-angle", type=float, default=40.0,
                    help="[--mandrel] winding angle in degrees FROM THE MANDREL "
                         "AXIS -- 0 is axial, 90 is a ring (default: 40)")
    ap.add_argument("--tube-eta", type=int, default=None,
                    help="[--mandrel] pin McColl's eta (the fraction-multiplier "
                         "divisor) instead of solving for --tube-length. The "
                         "pivot count N follows from it (Eq 4.2)")
    ap.add_argument("--tube-rm", type=int, default=0,
                    help="[--mandrel] extra WHOLE revolutions per length "
                         "iteration, McColl's Rm (default: 0)")
    ap.add_argument("--tube-pivots", type=int, default=None,
                    help="[--mandrel] close by an END TURN with this many "
                         "pivot points (even): the tube is exactly "
                         "--tube-length at exactly --winding-angle, and the "
                         "mandrel turns at each end onto the next pivot. "
                         "Not with --tube-eta")
    ap.add_argument("--tube-strategy", choices=MANDREL_STRATEGIES,
                    default="zigzag",
                    help="[--mandrel] pattern family (default: zigzag)")
    ap.add_argument("--tube-speed-mode", choices=MANDREL_SPEED_MODES,
                    default="surface",
                    help="[--mandrel] 'surface' holds the deposition-surface "
                         "speed constant on every layer (default); 'omega' holds "
                         "the mandrel's angular rate constant, so the surface "
                         "speed drifts up by (R0+h)/R0 as the wall builds")
    ap.add_argument("--rotary-feed", choices=MANDREL_ROTARY_FEEDS,
                    default="combined",
                    help="[--mandrel] 'combined' (default): one F word, the "
                         "resultant surface speed (Klipper). 'split': "
                         "Aerotech's F (linear axes) plus E (rotary, deg) on "
                         "every move, both derived from --speed so the "
                         "surface speed is still the one commanded")
    ap.add_argument("--feed-time-base", choices=FEED_TIME_BASES,
                    default="min",
                    help="[--mandrel --rotary-feed split] F and E per minute "
                         "(G75, default) or per second (G76)")
    ap.add_argument("--fixed-feed", type=float, nargs=2, default=None,
                    metavar=("F", "E"),
                    help="[--mandrel --rotary-feed split] your own F and E, "
                         "in the controller's units, written on every move "
                         "in place of the rates worked out from --speed")
    ap.add_argument("--tube-start-angle", type=float, default=0.0,
                    help="[--mandrel] the A value the program starts from, "
                         "degrees (default: 0)")
    ap.add_argument("--tube-layer-step", type=float, default=None,
                    help="[--mandrel] how far the nozzle steps OUT between "
                         "layers, mm (default: the fiber diameter). A layer "
                         "is one whole sheet of the pattern, and a winding's "
                         "crossings stack about two diameters")
    ap.add_argument("--tube-turn-out", type=float, default=0.0,
                    help="[--mandrel] PERIMETER TRAVEL: a helix closed by an "
                         "end turn runs this far (mm) past the tube's end, "
                         "turns there, and comes back, instead of turning in "
                         "place at the end (default: 0 = off)")
    ap.add_argument("--tube-blocks", default=None, metavar="JSON",
                    help="[--mandrel] a JSON file of LAYER BLOCKS laid in "
                         "order, outward, e.g. [{\"family\": \"zigzag\", "
                         "\"layers\": 4, \"winding_angle\": 30, "
                         "\"tube_pivots\": 20}, {\"family\": \"reentrant\", "
                         "\"layers\": 4, \"cell_fit\": \"fit\"}]. A key is "
                         "a --mandrel flag's name with underscores (family is "
                         "--tube-family), plus layers, step and speed; one left "
                         "out takes the flag's value. Every block shares "
                         "--tube-length. The JSON itself may be given in "
                         "place of a file")
    ap.add_argument("--tube-scaffolds", default=None, metavar="JSON",
                    help="[--mandrel] several TUBES on one rod, laid in order, "
                         "each its own scaffold group: a JSON list (or a file "
                         "of one) whose entries are the per-tube --mandrel "
                         "flags by their dest names (%s). A key left out takes "
                         "the FLAG'S DEFAULT, not this command line's: each "
                         "entry is a whole tube. The rod, the speed policy, "
                         "the feeds, the start angle and the exit are this "
                         "command line's, for every tube. start_x is the "
                         "tube's near end, start_z its first layer out from "
                         "the bare surface. Not with --tube-copies"
                    % ", ".join(TUBE_SCAFFOLD_KEYS))
    ap.add_argument("--tube-copies", type=int, default=1, metavar="N",
                    help="[--mandrel] lay N copies of the tube SIDE BY SIDE "
                         "along the rod, each its own scaffold group, joined "
                         "by a travel on bare rod (default 1). From the third "
                         "copy on, each join crosses the copy before: one "
                         "axial fiber over its wall")
    ap.add_argument("--tube-copy-gap", type=float, default=TUBE_COPY_GAP,
                    metavar="MM",
                    help="[--mandrel] bare rod between two copies, extreme "
                         "fiber to extreme fiber (default %g mm). The exit "
                         "parks in it, so it may not be shorter than the "
                         "exit distance" % TUBE_COPY_GAP)
    # -- unit-cell families (mew_unit_cell) -------------------------------- #
    # `zigzag` is the DEFAULT and dispatches to the legacy McColl emitter
    # untouched, so an argv that does not name --tube-family produces exactly
    # the bytes it did before this flag existed.
    ap.add_argument("--tube-family",
                    choices=("zigzag",) + mew_unit_cell.MANDREL_FAMILIES,
                    default="zigzag",
                    help="[--mandrel] unit-cell family. 'zigzag' (default) is "
                         "the McColl diamond helix and uses --winding-angle / "
                         "--tube-eta / --tube-rm; every other family is a "
                         "literature cell tiled on the unwrapped plane and "
                         "uses --cell-width / --cell-height / --cell-fit "
                         "instead, ignoring the winding angle")
    ap.add_argument("--cell-width", type=float, default=2.0,
                    help="[--mandrel, cell families] requested cell size ALONG "
                         "THE AXIS in mm -- McCosker's longitudinal spacing "
                         "(default: 2, their published value)")
    ap.add_argument("--cell-height", type=float, default=4.18,
                    help="[--mandrel, cell families] requested cell size AROUND "
                         "the circumference in mm of arc -- McCosker's radial "
                         "spacing (default: 4.18, their published value, which "
                         "is 6 cells around an 8 mm mandrel)")
    ap.add_argument("--cell-fit", choices=mew_unit_cell.FIT_MODES,
                    default="scale",
                    help="[--mandrel, cell families] how to reconcile the "
                         "requested cell with an integer number of cells "
                         "around the circumference (Devlin 2024 sec 2.3): "
                         "'scale' keeps the aspect ratio and moves the tube "
                         "length (default), 'fit' keeps the length exactly and "
                         "distorts the aspect ratio")
    # The designer page's COUNT LADDER pin. Absent (the default) is the
    # engine's own rounding, so an argv that does not name these two produces
    # exactly the bytes it did before the ladder existed.
    ap.add_argument("--cell-n-around", type=int, default=None,
                    help="[--mandrel, cell families] pin the number of cells "
                         "AROUND the circumference instead of rounding "
                         "--cell-height into it. The resolved cell height is "
                         "then the circumference divided by this count")
    ap.add_argument("--mandrel-surface-z", type=float, default=0.0,
                    metavar="MM",
                    help="[--mandrel] the g-code Z that means THE BARE MANDREL "
                         "SURFACE -- the radial half of a 4-axis zero. 0 (the "
                         "default) is a machine zeroed on the rod's surface, so "
                         "a Z word is wall height; zero on the rod's AXIS "
                         "instead and it is the rod radius. Added to every "
                         "emitted Z and forwarded to the converter as the "
                         "cylindrical frame's surface_z, so the two cannot "
                         "disagree about which Z is the surface")
    ap.add_argument("--cell-n-along", type=int, default=None,
                    help="[--mandrel, cell families] pin the number of cells "
                         "ALONG the axis instead of rounding --tube-length "
                         "into it")
    ap.add_argument("--rib-angle", type=float, default=30.0,
                    help="[--mandrel, reentrant] rib angle from the mandrel "
                         "axis in degrees, 0 < a < 90 (default: 30)")
    ap.add_argument("--cell-amplitude", type=float, default=0.0,
                    help="[--mandrel, brick/chevron families] wave amplitude in "
                         "mm for the circumferential fibres. 0 (default) is "
                         "straight; the chevron family requires a positive "
                         "value")
    ap.add_argument("--cell-graph", default=None,
                    help="[--mandrel, custom family] the drawn cell, as "
                         "'nodes | edges': node u,v pairs as FRACTIONS of the "
                         "cell (u along the axis, v around it) and 1-based "
                         "index pairs 'i-j', a ':free' suffix marking a member "
                         "the route may retrace cheaply. A node on the u=1 or "
                         "v=1 edge joins the neighbouring cell, so a honeycomb "
                         "is '0.25,0; 0.75,0; 0,0.5; 1,0.5; 0.25,1; 0.75,1 | "
                         "1-2:free; 3-1; 2-4; 3-5; 4-6'. Required by, and only "
                         "read by, --tube-family custom")

    args = ap.parse_args(argv)
    global _LINE_MARKS
    _LINE_MARKS = [] if (args.line_map or args.gcode_doc) else None

    # argparse's float() takes 'nan' and 'inf'; every coordinate, angle and
    # margin would then be written into the g-code as `Xnan` / `Zinf`
    for act in ap._actions:
        val = getattr(args, act.dest, None)
        if _nonfinite_numbers(val):
            ap.error(f"{'/'.join(act.option_strings) or act.dest} must be a "
                     f"finite number, got {val!r}")

    # Open the bar before any work, so a slow generation shows 0 % under a
    # name. Both collector branches below pass through here.
    _progress_say(args.progress, 0.0, "Generating g-code")

    if args.mandrel is not None:
        # A tube is zeroed ON its rod, so its axial origin is 0 unless the caller
        # says otherwise -- never the flat collector's 40 mm (see --start-x).
        # The tube's end-of-print exit, sharing the plate's two switches: the
        # same --no-exit-move turns it off and the same --exit-margin sets how
        # far past the end it parks. There is no plate edge to run off here, so
        # the axis it leaves along is the mandrel's own -- see
        # `gcode_exit.mandrel_exit_x` for which end and why.
        _tube_exit_margin = None if args.no_exit_move else args.exit_margin
        _tube_start_x = (_TUBE_START_X if args.start_x is None
                         else args.start_x)
        # Start Z is the nozzle's height above the rod's surface: below zero
        # it is driven into the mandrel
        if args.start_z < 0:
            ap.error("start Z is %g mm, below the mandrel's surface: the "
                     "nozzle would be driven into the mandrel. Use 0 to print "
                     "on it, or a positive height to start above it"
                     % args.start_z)
        # RETURNS EARLY, before any planar flag is touched. The converter
        # solves on the cylinder, so --stl chains -- but only with
        # `--mandrel-diameter` forwarded: without it the converter reads the file
        # as a flat zigzag, because the rotary word is not in its axis whitelist
        # and vanishes with no error.
        summary = []
        scaffold_groups = None
        if args.tube_scaffolds is not None:
            if args.tube_copies > 1:
                ap.error("--tube-scaffolds and --tube-copies do not go "
                         "together: lay the copies as scaffolds")
            try:
                tubes, summary = _cli_tube_scaffolds(args, ap)
                joins = []
                gcode = generate_mandrel_scaffolds_gcode(
                    args.mandrel, tubes, speed_mode=args.tube_speed_mode,
                    start_angle=args.tube_start_angle,
                    exit_margin=_tube_exit_margin,
                    rotary_feed=args.rotary_feed,
                    time_base=args.feed_time_base,
                    surface_z=args.mandrel_surface_z,
                    fixed_feed=args.fixed_feed, joins=joins)
                for k, _x0, _x1, crossed in joins:
                    if crossed:
                        summary.append(
                            "  NOTE the join to scaffold %d runs over "
                            "scaffold %s and lays one axial fiber there: a "
                            "tube ends where it started, so only two tubes "
                            "in a row join on bare rod alone"
                            % (k + 1, ", ".join(str(j + 1)
                                                for j in crossed)))
            except (ValueError, TypeError, mew_tube.TubeDesignError,
                    mew_unit_cell.UnitCellError) as exc:
                ap.error(str(exc))
            # the converter renders each tube at ITS fibre, as a plate's
            # scaffolds are (`path_diameters_from_groups`)
            args.fiber_diameter = tubes[0].fiber_diameter
            scaffold_groups = [
                {"name": "scaffold%d" % (k + 1),
                 "layers": sum(int(b.layers) for b in t.blocks),
                 "fiber_diameter": t.fiber_diameter}
                for k, t in enumerate(tubes)]
        elif args.tube_blocks is not None:
            try:
                blocks, summary = _cli_tube_blocks(args)
                gcode = generate_mandrel_blocks_gcode(
                    args.mandrel, blocks, args.fiber_diameter, args.speed,
                    layer_step=args.tube_layer_step,
                    speed_mode=args.tube_speed_mode, start_x=_tube_start_x,
                    start_angle=args.tube_start_angle,
                    start_z=args.start_z + args.mandrel_surface_z,
                    exit_margin=_tube_exit_margin,
                    rotary_feed=args.rotary_feed,
                    time_base=args.feed_time_base,
                    surface_z=args.mandrel_surface_z,
                    fixed_feed=args.fixed_feed, copies=args.tube_copies,
                    copy_gap=args.tube_copy_gap)
            except (ValueError, mew_tube.TubeDesignError,
                    mew_unit_cell.UnitCellError) as exc:
                ap.error(str(exc))
        elif args.tube_family == "zigzag":
            try:
                design = mandrel_design(args.mandrel, args.winding_angle,
                                        args.tube_length, eta=args.tube_eta,
                                        rm=args.tube_rm,
                                        pivots=args.tube_pivots)
            except (ValueError, mew_tube.TubeDesignError) as exc:
                ap.error(str(exc))
            # the program's own refusals (speed, fibre diameter, layers, layer
            # step, copies) are the user's input too: a sentence, not a trace
            try:
                gcode = generate_mandrel_gcode(
                    args.mandrel, args.tube_length, args.winding_angle,
                    args.fiber_diameter, args.layers, args.speed,
                    design=design, strategy=args.tube_strategy,
                    speed_mode=args.tube_speed_mode,
                    start_x=_tube_start_x, start_angle=args.tube_start_angle,
                    start_z=args.start_z + args.mandrel_surface_z,
                    exit_margin=_tube_exit_margin,
                    rotary_feed=args.rotary_feed,
                    time_base=args.feed_time_base,
                    surface_z=args.mandrel_surface_z,
                    fixed_feed=args.fixed_feed,
                    layer_step=args.tube_layer_step, copies=args.tube_copies,
                    copy_gap=args.tube_copy_gap, turn_out=args.tube_turn_out)
            except (ValueError, mew_tube.TubeDesignError) as exc:
                ap.error(str(exc))
            summary.append(
                "Wrote %s: tube d%.3f x %.4f mm at %.1f deg, %d layer(s), "
                "N=%d pivots, alpha=%.2f deg, pore %.4f mm^2"
                % (args.output, args.mandrel, design.length,
                   args.winding_angle, int(args.layers), design.n_pivots,
                   design.pivot_angle, design.pore_area))
            if design.end_turn_deg > 0.0:
                summary.append(
                    "  (closes by turning %.3f deg at each end, no axial "
                    "move)" % design.end_turn_deg)
            if abs(design.length - args.tube_length) > 1e-9:
                summary.append(
                    "  (requested %.4f mm; %.4f mm is the nearest length that "
                    "closes -- a length that does not close leaves the helix "
                    "out of sync)" % (args.tube_length, design.length))
        else:
            # A cell family ignores --winding-angle / --tube-eta / --tube-rm
            # entirely: the pattern is tiled on the unwrapped plane, so there
            # is no helix to close and McColl's pivot ladder does not apply.
            try:
                cell = _cli_tube_cell(args)
                gcode = generate_mandrel_cell_gcode(
                    args.mandrel, cell, args.fiber_diameter, args.layers,
                    args.speed, speed_mode=args.tube_speed_mode,
                    start_x=_tube_start_x, start_angle=args.tube_start_angle,
                    start_z=args.start_z + args.mandrel_surface_z,
                    exit_margin=_tube_exit_margin,
                    rotary_feed=args.rotary_feed,
                    time_base=args.feed_time_base,
                    surface_z=args.mandrel_surface_z,
                    fixed_feed=args.fixed_feed,
                    layer_step=args.tube_layer_step, copies=args.tube_copies,
                    copy_gap=args.tube_copy_gap)
            except (ValueError, mew_unit_cell.UnitCellError) as exc:
                ap.error(str(exc))
            summary.append(
                "Wrote %s: %s tube d%.3f x %.4f mm, %d x %d cells of "
                "%.4f x %.4f mm (fit=%s), %d layer(s), one traversal each"
                % (args.output, args.tube_family, args.mandrel, cell.length,
                   cell.n_axial, cell.n_around, cell.cell_w, cell.cell_h,
                   cell.fit, int(args.layers)))
            summary.append(
                # `n_retraced` counts MEMBERS laid twice, so it is spelled that
                # way: the line above now reports the wall's traversals and two
                # different "traversals" on one screen read as one number.
                "  route: %d members, %.4f mm of %.4f mm laid twice (%.1f%% "
                "retraced, %d retraced members), %d rotation reversals"
                % (cell.n_members, cell.retraced_length, cell.total_length,
                   100.0 * cell.retraced_length / cell.total_length,
                   cell.n_retraced, cell.reversals))
            if cell.unfold_strain is not None:
                summary.append(
                    "  unfolds at %.1f%% longitudinal / %.1f%% radial strain "
                    "(derived kinematics, rigid ribs and hinged nodes -- not a "
                    "mechanics prediction)"
                    % (100.0 * cell.unfold_strain["longitudinal"],
                       100.0 * cell.unfold_strain["radial"]))
            if (abs(cell.length - args.tube_length) > 1e-9
                    or abs(cell.cell_h - args.cell_height) > 1e-9
                    or abs(cell.cell_w - args.cell_width) > 1e-9):
                summary.append(
                    "  (requested %.4f mm of %.4f x %.4f mm cells; an integer "
                    "number of cells has to close the %.4f mm circumference, "
                    "so '%s' resolved them to %.4f mm of %.4f x %.4f mm)"
                    % (args.tube_length, args.cell_width, args.cell_height,
                       cell.period_v, cell.fit, cell.length, cell.cell_w,
                       cell.cell_h))
        if args.tube_copies > 1:
            laid = (blocks if args.tube_blocks is not None else
                    [TubeBlock(design if args.tube_family == "zigzag"
                               else cell, 1, turn_out=args.tube_turn_out)])
            summary.append(
                "  %d copies side by side, %.4f mm apart near end to near end "
                "(%.4f mm of bare rod between)"
                % (args.tube_copies, tube_copy_pitch(laid, args.tube_copy_gap),
                   args.tube_copy_gap))
            crossed = tube_copy_crossings(args.tube_copies)
            if crossed:
                summary.append(
                    "  NOTE the join to copy %s crosses the copy before it and "
                    "lays one axial fiber over its wall: a copy ends where it "
                    "started, so only two copies join on bare rod alone"
                    % ", ".join(str(c) for c in crossed))
        # Re-base the coordinate origin, exactly as the planar branch does
        # below. On a tube the offset is AXIAL ONLY: a tube program emits no Y
        # word (the datum is the mandrel axis by construction) and its radial
        # datum is `--mandrel-surface-z`, which is already in the Z words above
        # rather than being a translation -- a Z shift here would be removed a
        # second time by the frame that unrolls the file.
        gcode = translate_gcode(gcode, *args.origin_offset)
        _write_gcode_output(args, gcode)
        a_max = max(float(w[1:]) for ln in gcode.splitlines()
                    for w in ln.split() if w.startswith("A"))
        # the raw total is what a controller's travel limit is checked
        # against; the turns are what a reader pictures
        summary[0] += ", A ends at %.1f deg (%.4g turns)" % (a_max, a_max / 360.0)
        for line in summary:
            print(line)
        print("  NOTE the rotary word A is DROPPED SILENTLY by a controller "
              "with no 4th axis registered (Klipper: MANUAL_STEPPER "
              "STEPPER=<name> GCODE_AXIS=A) -- the tube then prints flat with "
              "no error. See pi_sim/config/mandrel_probe.cfg")
        print(f"[GCODE-READY] {args.output}", flush=True)
        if args.stl:
            try:
                import gcode_to_fibers
            except ImportError as exc:
                print("Cannot render STL -- gcode_to_fibers.py not importable: "
                      "%s" % exc, file=sys.stderr)
                return 1
            gtf_argv = [args.output, "-o", args.stl,
                        "-d", str(args.fiber_diameter),
                        "-f", str(args.faces), "-m", "g1",
                        "--mandrel-diameter", str(args.mandrel),
                        # which Z is the bare surface -- the converter's
                        # CylinderFrame unrolls with it, so a program zeroed on
                        # the rod's axis is not read as a tube one radius thick
                        "--mandrel-surface-z", str(args.mandrel_surface_z),
                        "--sag-multiplier", str(args.sag_multiplier),
                        "--sag-reset-layers", str(args.sag_reset_layers),
                        *_sag_resolution_argv(args),
                        "--mesh-tolerance", str(args.mesh_tolerance)]
            if args.no_sag:
                gtf_argv.append("--no-sag")
            if args.sag_approx:
                # With its periodic analytic contacts, the fast solver is valid
                # on the cylindrical frame as well as a plate.
                # Keep the page/CLI choice honest by forwarding it verbatim.
                gtf_argv.append("--sag-approx")
            if args.no_mesh:
                gtf_argv.append("--no-mesh")
            # The continuous jet is not a property of the collector. A tube's
            # travels are the approach from the g-code datum to the print start
            # -- real fibre laid along the bare rod whenever an end margin or a
            # wave family's overhang insets the print -- plus the per-layer
            # radial hop, which is a pure Z move at one (u, v) and so deposits
            # nothing visible. The converter says which of the two it found.
            if args.render_travel:
                gtf_argv.append("--render-travel")
            if args.travel_in_stl:
                gtf_argv.append("--travel-in-stl")
            # Not forwarded: --plate (a flat-collector datum the converter refuses
            # on a mandrel), --arc-segments (a tube carries no G2/G3 -- they are
            # refused under a rotary frame), and the footprint/linking-loop flags,
            # which are planar pass machinery a tube shares nothing with.
            # Several tubes: each at its own fibre, and a sibling STL each,
            # exactly as a plate's scaffolds
            if scaffold_groups:
                gtf_argv += ["--scaffold-groups", json.dumps(scaffold_groups)]
            _progress_handover(args.progress, gtf_argv)
            return gcode_to_fibers.main(gtf_argv)
        _progress_say(args.progress, 1.0, "Done")
        return 0

    # The loop radius is entered as a MULTIPLE of stepover; convert to the
    # absolute mm the geometry functions take. Scaling with stepover keeps the
    # turnaround loops proportional at any pore size (a fixed mm radius balloons
    # into huge overlapping loops at tight stepovers). A multiplier of 0 -> a 0mm
    # radius -> straight connectors.
    arc_radius = args.arc_radius_mult * args.stepover

    try:
        angle_pattern = parse_angle_pattern(args.angle_pattern)
    except ValueError:
        ap.error("--angle-pattern must be a comma/space separated list of "
                 "numbers, e.g. '0,60,90'")

    try:
        angle_stepovers = parse_layer_value_list(args.angle_stepover_pattern)
    except ValueError:
        ap.error("--angle-stepover-pattern must be a comma-separated list of "
                 "numbers (blank slots allowed), e.g. '0.3,,0.5'")
    if angle_stepovers and any(x is not None and x <= 0 for x in angle_stepovers):
        ap.error("--angle-stepover-pattern values must be positive")
    try:
        angle_offsets = parse_layer_value_list(args.angle_offset_pattern)
    except ValueError:
        ap.error("--angle-offset-pattern must be a comma-separated list of "
                 "numbers (blank slots allowed), e.g. '0,,0.25'")

    try:
        linear_pattern = parse_linear_pattern(args.linear_pattern)
    except ValueError as exc:
        ap.error(f"--linear-pattern must be ';'-separated (x, y) vertex pairs, "
                 f"e.g. '0,0; 0,1; 1,1; 1,0' ({exc})")
    if linear_pattern and args.linear_wavelength <= 0:
        ap.error("--linear-pattern needs --linear-wavelength > 0")
    if args.linear_spacing < 0:
        ap.error("--linear-spacing must be >= 0")

    # custom footprint outline (reuses the linear-pattern vertex parser). Ignored
    # for the parametric shapes, which build their outline from --width/--height.
    try:
        footprint_polygon = parse_linear_pattern(args.footprint_polygon)
    except ValueError as exc:
        ap.error(f"--footprint-polygon must be ';'-separated (x, y) pairs, e.g. "
                 f"'0,0; 10,0; 10,8; 0,8' ({exc})")
    if args.shape == "custom" and not footprint_polygon:
        ap.error("--shape custom needs --footprint-polygon with >= 3 vertices")
    if args.shape in ("circle", "ellipse", "polygon") and args.shape_sides < 3:
        ap.error(f"--shape-sides must be >= 3 for a {args.shape} footprint")
    # Start Z is a height ABOVE the plate's top (--plate-top-z is added to it
    # below), so a negative one drives the nozzle into the plate: refused
    # here, in the user's terms, rather than written as a crash.
    if args.start_z < 0:
        ap.error("start Z is %g mm, below the top of the plate: the "
                 "nozzle would be driven into the plate. Use 0 to print on "
                 "the plate, or a positive height to start above it"
                 % args.start_z)

    stabilization = None
    if args.stabilization:
        stab_arc_radius = (args.stab_arc_radius_mult * args.stab_stepover
                           if args.stab_arc_radius_mult is not None else None)
        stabilization = dict(
            width=args.stab_width, height=args.stab_height,
            stepover=args.stab_stepover,
            fiber_diameter=(args.stab_fiber_diameter
                            if args.stab_fiber_diameter is not None
                            else args.fiber_diameter),
            layers=args.stab_layers, arc_radius=stab_arc_radius,
            start_x=args.stab_start_x, start_y=args.stab_start_y)

    # The fiber diameter handed to the STL step as its global default -- the tube
    # width for any fiber NOT covered by a per-scaffold group. In multi-scaffold
    # mode it comes from the FIRST scaffold's diameter; each scaffold's own
    # diameter rides along in --scaffold-groups so the converter renders every
    # scaffold's fibers at its OWN diameter (the global value is the fallback /
    # the diameter the sag solve uses).
    stl_fiber_diameter = args.fiber_diameter
    # End-of-print exit + datum geometry from the build plate. plate_extent is the
    # rectangle the nozzle exits past (a circle uses its bounding square); None
    # when --no-exit-move. plate_top_z (the plate's top surface) is the print
    # start datum added to every scaffold's own start Z below, so the print rests
    # on the plate.
    if args.plate_shape == "circular":
        _half_x = _half_y = args.plate_diameter / 2.0
    else:
        _half_x, _half_y = args.plate_size_x / 2.0, args.plate_size_y / 2.0
    plate_extent = (None if args.no_exit_move else
                    (args.plate_center_x - _half_x, args.plate_center_x + _half_x,
                     args.plate_center_y - _half_y, args.plate_center_y + _half_y))
    # per-scaffold parts for the viewer (g-code order: stabilization then each
    # scaffold). Built only in multi-scaffold mode; None -> the STL is one mesh.
    scaffold_groups = None
    try:
        if args.scaffolds_json:
            try:
                with open(args.scaffolds_json) as fh:
                    specs = json.load(fh)
            except (OSError, ValueError) as exc:
                ap.error(f"could not read --scaffolds-json: {exc}")
            if not isinstance(specs, list) or not specs:
                ap.error("--scaffolds-json must hold a non-empty list of "
                         "scaffold dicts")
            # the file's own words, before `_scaffold_from_spec` does
            # arithmetic on them: a missing key read back as `'stepover'` and
            # a string number as a Python operand error
            for i, s in enumerate(specs):
                if not isinstance(s, dict):
                    ap.error("--scaffolds-json scaffold %d is not a dict of "
                             "parameters: %r" % (i + 1, s))
                for key in _SPEC_REQUIRED:
                    if key not in s:
                        ap.error("--scaffolds-json scaffold %d is missing "
                                 "'%s'" % (i + 1, key))
                    v = s[key]
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        ap.error("--scaffolds-json scaffold %d %s must be a "
                                 "number, got %r" % (i + 1, key, v))
                if float(s.get("start_z", 0.0) or 0.0) < 0:
                    ap.error("scaffold %d starts at Z %g mm, below the top of "
                             "the plate: the nozzle would be driven "
                             "into the plate. Use 0 to print on the plate"
                             % (i + 1, float(s["start_z"])))
            scaffolds = [_scaffold_from_spec(s) for s in specs]
            # raise every scaffold onto the plate top (its own start_z is kept as
            # the relative stacking offset); the stabilization base reads
            # scaffolds[0]["start_z"], so it rides up onto the plate too
            for sc in scaffolds:
                sc["start_z"] = sc.get("start_z", 0.0) + args.plate_top_z
            if args.g2g3_arcs:                  # CLI flag forces it on every one
                for sc in scaffolds:
                    sc["g2g3"] = True
            if args.flat_layers:                # ditto -- a machine-wide setting
                for sc in scaffolds:            # (the stab base reads scaffolds[0])
                    sc["stack_layers"] = False
            stl_fiber_diameter = scaffolds[0]["fiber_diameter"]
            scaffold_groups = []
            if args.stabilization:
                scaffold_groups.append(
                    {"name": "stab", "layers": int(args.stab_layers),
                     "fiber_diameter": stabilization["fiber_diameter"]})
            for k, sc in enumerate(scaffolds):
                scaffold_groups.append(
                    {"name": f"scaffold{k + 1}", "layers": int(sc["layers"]),
                     "fiber_diameter": sc["fiber_diameter"]})
            gcode = generate_multi_scaffold_gcode(
                scaffolds, stabilization=stabilization,
                plate_extent=plate_extent,
                exit_margin=args.exit_margin, travel_offset=args.travel_offset,
                lead_in_from=(-args.origin_offset[0], -args.origin_offset[1]))
        else:
            gcode = generate_scaffold_gcode(
                args.width, args.height, args.stepover, args.rotation,
                args.fiber_diameter, args.layers, args.speed,
                start_angle=args.start_angle, arc_radius=arc_radius,
                arc_segments=args.arc_segments,
                start_x=(_PLATE_START_X if args.start_x is None
                         else args.start_x),
                start_y=args.start_y,
                start_z=args.start_z + args.plate_top_z,
                stabilization=stabilization, loop_speed=args.loop_speed,
                plate_extent=plate_extent,
                exit_margin=args.exit_margin, g2g3=args.g2g3_arcs,
                stack_layers=not args.flat_layers,
                angle_pattern=angle_pattern, angle_stepovers=angle_stepovers,
                angle_offsets=angle_offsets, linear_pattern=linear_pattern,
                linear_wavelength=args.linear_wavelength,
                linear_spacing=args.linear_spacing,
                shape=args.shape, shape_sides=args.shape_sides,
                footprint_polygon=footprint_polygon,
                footprint_angle=args.footprint_angle,
                link_loops=args.link_loops,
                link_loop_angle=args.link_loop_angle,
                link_loop_dist=args.link_loop_dist,
                link_loop_travel=args.link_loop_travel,
                perimeter_travel=args.perimeter_travel,
                perimeter_travel_offset=args.perimeter_travel_offset,
                stepover_end=args.stepover_end, grade_angle=args.grade_angle,
                lead_in_from=(-args.origin_offset[0], -args.origin_offset[1]))
    except (ValueError, KeyError, TypeError) as exc:
        ap.error(str(exc))

    # Re-base the coordinate origin: shift the whole program so g-code zero lands
    # where the GUI asked (the chosen collector corner / face-centre). The converter
    # gets the same offset, so the plate STL shifts to match.
    gcode = translate_gcode(gcode, *args.origin_offset)

    _write_gcode_output(args, gcode, scaffold_groups or (
        ([{"name": "stab", "layers": int(args.stab_layers)}]
         if args.stabilization else [])
        + [{"name": "scaffold1", "layers": int(args.layers)}]))
    # Count layer/scaffold starts by the Z-bearing G0s (each layer entry sets Z);
    # plain "G0 " would also count the inter-layer travel waypoints that route
    # around a non-rectangular footprint, inflating the number.
    n_layers_emitted = sum(1 for ln in gcode.splitlines()
                           if ln.startswith("G0 ") and " Z" in ln)
    n_moves = gcode.count("G1 ")
    stab_note = (f" + stabilization {args.stab_width}x{args.stab_height}mm "
                 f"@({args.stab_start_x},{args.stab_start_y})"
                 if args.stabilization else "")
    if args.scaffolds_json:
        print(f"Wrote {args.output}: {n_layers_emitted} layer(s), {n_moves} "
              f"G1 moves ({len(scaffolds)} scaffold(s) from "
              f"{args.scaffolds_json}{stab_note})")
    else:
        angle_note = (f"angle pattern {args.angle_pattern}" if angle_pattern
                      else f"rotation {args.rotation} deg")
        shape_note = {
            "circle": f"circle d{args.width}mm",
            "ellipse": f"ellipse {args.width}x{args.height}mm",
            "polygon": f"{args.shape_sides}-gon d{args.width}mm",
            "custom": "custom outline",
        }.get(args.shape, f"{args.width}x{args.height}mm")
        print(f"Wrote {args.output}: {n_layers_emitted} layer(s), {n_moves} "
              f"G1 moves ({shape_note}, stepover "
              f"{args.stepover}mm"
              + (f" to {args.stepover_end}mm across the layer"
                 if args.stepover_end is not None else "")
              + f", {angle_note}, "
              f"d {args.fiber_diameter}mm{stab_note})")

    # Print-time estimate -- integrates path length / feedrate over every move (a
    # MEW print holds one feed throughout, so the g-code's F words make this
    # exact). Printed to stdout only; the g-code file itself stays comment-free.
    print(f"Estimated print time: "
          f"{format_duration(estimate_print_time(gcode))} (at the g-code feedrates)")

    # Tell the GUI the g-code is on disk and ready to play / simulate -- emitted
    # BEFORE the (slow) STL build below so the simulation buttons enable the moment
    # the g-code exists instead of waiting out the whole sag/mesh. Stdout only (the
    # g-code file stays comment-free); the GUI output reader watches for this exact
    # prefix. Keep it in sync with GCODE_READY_MARKER in gui_run.py.
    print(f"[GCODE-READY] {args.output}", flush=True)

    if args.stl:
        try:
            import gcode_to_fibers
        except ImportError as exc:
            print(f"Cannot render STL -- gcode_to_fibers.py not importable: {exc}",
                  file=sys.stderr)
            return 1
        # how finely the STL re-expands G2/G3 arcs (g2g3 mode only; harmless for
        # G1 files, which carry no arcs). In G2/G3 mode we DOUBLE the G1-chord
        # fidelity so an arc-mode preview reads visibly smoother than the chord
        # one -- the machine still interpolates the exact arc; this only sets the
        # preview facets / sag input density. Kept out of the g-code itself, which
        # must stay comment-free.
        stl_arc_segments = (args.arc_segments * 2 if args.g2g3_arcs
                            else args.arc_segments)
        gtf_argv = [args.output, "-o", args.stl,
                    "-d", str(stl_fiber_diameter),
                    "-f", str(args.faces), "-m", "g1",
                    "--sag-multiplier", str(args.sag_multiplier),
                    "--sag-reset-layers", str(args.sag_reset_layers),
                    *_sag_resolution_argv(args),
                    "--mesh-tolerance", str(args.mesh_tolerance),
                    "--arc-segments", str(stl_arc_segments)]
        if args.no_sag:
            gtf_argv.append("--no-sag")
        if args.sag_approx:                 # approximate solver (see gcode_to_fibers)
            gtf_argv.append("--sag-approx")
        if args.render_travel:              # deposit the G0s (see gcode_to_fibers)
            gtf_argv.append("--render-travel")
        if args.travel_in_stl:              # ... and tube them into the mesh
            gtf_argv.append("--travel-in-stl")
        if args.layer_by_deposition:        # sag in g-code order, not by height
            gtf_argv.append("--layer-by-deposition")
        if args.no_mesh:                    # sag + .depo only; skip the mesh build
            gtf_argv.append("--no-mesh")
        # the build plate (drawn as a grey sibling STL by the converter) at the
        # same top-surface Z the print rests on
        if args.plate:
            gtf_argv += ["--plate", "--plate-shape", args.plate_shape,
                         "--plate-thickness", str(args.plate_thickness),
                         "--plate-center-x", str(args.plate_center_x),
                         "--plate-center-y", str(args.plate_center_y),
                         "--plate-top-z", str(args.plate_top_z)]
            if args.plate_shape == "circular":
                gtf_argv += ["--plate-diameter", str(args.plate_diameter)]
            else:
                gtf_argv += ["--plate-size-x", str(args.plate_size_x),
                             "--plate-size-y", str(args.plate_size_y)]
        # same origin re-base as the g-code above, so the converter shifts the
        # plate STL to match the translated fibres
        ox, oy, oz = args.origin_offset
        if ox or oy or oz:
            gtf_argv += ["--origin-offset", str(ox), str(oy), str(oz)]
        # multi-scaffold: ask the converter to also drop per-scaffold sibling
        # STLs (viewing aids) so the viewer can toggle/colour each scaffold
        if scaffold_groups:
            gtf_argv += ["--scaffold-groups", json.dumps(scaffold_groups)]
        _progress_handover(args.progress, gtf_argv)
        return gcode_to_fibers.main(gtf_argv)

    _progress_say(args.progress, 1.0, "Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
