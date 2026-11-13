#!/usr/bin/env python3
"""preview_sag.py -- PHYSICS BUILD, MODERN-UI ONLY: the fast approximate sag
("drape") engine behind the Scaffold-designer preview's "Sagging (approximate)"
toggle. `gui_preview.preview_sag` (the wired seam) calls `drape_sag` here.

Why this exists: the exact solver (`gcode_to_fibers.apply_sag`) profiled 12-25 s
per rebuild at the preview's targets, far over the live preview's 3 s budget, and
the cost is inherent to its exact segment-segment contact geometry. This engine
reproduces the solver's MECHANISM with a rasterized contact query instead, at
~1000x speed:

  1. fibres are processed in DEPOSITION ORDER; a 2D heightmap `H` accumulates the
     laid centreline tops (virgin ground = bed_c - diameter, where bed_c is the
     bare surface plus one radius -- a fibre rests its UNDERSIDE on the bed);
  2. the floor under a new fibre is what it would rest on over what was
     genuinely laid beneath: the exact contact CAP, `z + sqrt(d^2 - o^2)` at XY
     offset `o` from a strand's centreline. NO grid/regularity assumption
     anywhere. A flat bed reads it off `G`, `H`'s twin holding each cell's top
     strand as two solved segments (`_segment_floor`), and so does a MANDREL,
     off a periodic twin of `G`; any other CURVED collector asks the exact
     `_SegIndex.rest_heights` over the already solved fibres -- same index
     this engine already keeps for its crossing anchors. Both replaced
     `H + diameter`, a TOP-HAT one cell wide (see `drape_sag`'s floor block);
  3. pass 1, contact finding: z0 = max-plus dilation of f along arclength with the
     REAL droop law as the kernel, so a point rests (z0 == f) where nothing holds it;
  4. pass 2, span rebuild: hang the REAL span parabola between consecutive contact
     samples and clamp onto the floor. Pass 1 alone leaves fat quadratic touchdown
     skirts, a fixed kernel not knowing the span; misclassified contacts are
     harmless, the clamp keeping z >= floor either way;
  5. the sagged fibre is stamped into H, so the whole-stack collapse emerges layer
     by layer as in the solver.

Every droop goes through `gcode_to_fibers.sag_depth`, so a recalibration reshapes
this preview automatically. This engine must NEVER feed anything but the preview.
Pure numpy -- no Tk, no VTK; it imports gcode_to_fibers and nothing imports it back.

Validated against `apply_sag`: mean RMS 4-8 um on a 20 um fibre across grid /
triangle / wave / circle footprints, 0.27-0.38 x diameter across diameters
0.02-0.10 mm and multipliers 0.5-5; runtime 33-127 ms at 10x10 and 0.4-0.8 s at
30x30. Known approximations, which are why the toggle says "approximate": a
planar fibre's own crossings are found exactly but their contacts are written
down straight-strand (`_crossing_self_pairs`), and a same-fibre contact that is not a
crossing is still
not seen; a flat bed's contacts are read off the TOP strand of each cell
(cell = `_raster_cell`, one diameter until the print outgrows the budget) only, so a lower strand sharing a cell
with a higher one out of reach is missed (0.12% of samples by more than 0.2 d
on the 10 mm 60-degree default), and over a planar fibre's own lifted crossing later fibres
read the cap off `self_idx` instead; and the physical droop-law seam and
per-point dynamic diameters are ignored. FUSION is not among them -- see
`drape_sag`.
"""

import math

import numpy as np

import gcode_to_fibers as g2f

# display-density guard: after sagging, a fibre keeps every sample whose Z step
# exceeds _SIMPLIFY_TOL_DIAM fibre-diameters (the peaks and cliffs) plus every
# _SIMPLIFY_STRIDE-th sample of the flats, so the blind display decimation in
# gui_preview never has to stride away whole crossover peaks. Tuned:
# stride 3 / tol 0.02 keeps the engine-output RMS at the validated ~7 um
# (coarser settings chord across the curved touchdown skirts and double it)
# while still thinning the resting flats.
_SIMPLIFY_TOL_DIAM = 0.02
_SIMPLIFY_STRIDE = 3

_MAX_GRID = 1400          # heightmap cells per axis on a SQUARE print; the
                          # memory budget is its square (`_raster_cell`)
_DILATE_BLOCK = 8192      # bounded sliding-window memory (block x (2W+1))
_W_CAP = 2048             # kernel half-width cap (samples); safety bound

# Planar same-fibre crossings (`_crossing_self_pairs`): a crossing's reach is
# `d / sin(theta)` of arc, which grows without bound as the strands graze, so
# `sin` is floored here -- a reach of at most 20 diameters.
_SELF_SIN_FLOOR = 0.05

# The drape's two budgets, past which it REFUSES rather than runs. A design
# never reaches them; a hand-edited text can, by a lot (on the G-code page,
# `G1 X100 Y100` repeated after every move of a 15 mm scaffold).
#
# Samples: the drape lays one sample per `h` of fibre, and its time grows with
# them -- 0.6 M samples took 0.9 s, 11.6 M (50 mm / 0.25 / x20, physics on)
# 26 s. Past 16 M the picture is no longer live; that edit was 21 M.
#
# Self-pair candidates (`g2f._self_pair_load`): the same-fibre crossing finder
# (`_SegIndex.seg_pairs`)
# emits one candidate per CELL two segments share before it deduplicates, so
# long legs retraced along one corridor cost (pairs x shared cells), not
# pairs. That edit's 904 segments asked for 290 M candidates -- 2.2 GB in one
# array, then 7 GB -- where the largest design measured asks for 0.5 M; 31 M
# peaked at 1.5 GB, so 20 M holds the drape near 1.2 GB.
#
# Raster scale: the heightmap cell grows past one diameter once the print's
# raster outgrows `_MAX_GRID ** 2` cells (`_raster_cell`), so one far coordinate
# still coarsens it for the whole print -- by its AREA now, not its longest
# side, which is what let one axial `X100` on a 12 mm tube (an AREA of 100 x
# 9.4 mm, well inside the budget) coarsen it to 3.3 diameters. A cell many
# diameters wide cannot see a contact, and the dilation's plan grows with it:
# one `G1 X1000000 Y1000000` (a 714 mm cell) passed 3 GB. Designs sit at 1-4
# diameters (a 50 mm scaffold is 1.8 at 20 um).
_MAX_DRAPE_SAMPLES = 16_000_000
_MAX_SELF_CANDIDATES = 20_000_000
_MAX_CELL_DIAMETERS = 10.0


class BuildSuperseded(Exception):
    """Raised by `drape_sag` (and the preview builders around it) when their
    `should_stop()` says a newer edit has replaced this build: its picture
    would be discarded on arrival, so the seconds it has left are not spent.
    Never a result -- the caller drops the build."""


class DrapeBudgetExceeded(RuntimeError):
    """The drape refused this input (see the budgets above); the message says
    why, in words the preview's status line shows. The caller draws the fibre
    undraped. `samples` is the drape's own estimate of the samples it would
    have taken, when the sample budget is what refused it."""

    def __init__(self, message, samples=None):
        super().__init__(message)
        self.samples = samples


def _crossing_self_pairs(s, xy, p, raw_cum, hits, diameter, fuse_sink=0.0,
                         fuse_wall=None, band=None):
    """The same pairs `g2f._self_contact_pairs` builds at a FLAT fibre's own
    crossings, written down from each crossing's geometry instead of searched.

    Within reach of a crossing -- `d / sin(theta)` of arc -- both strands are
    straight to within the loop's curvature (an arc radius of 1.5-2 stepovers
    is 17-45 diameters), so a later-strand sample `u` of arc past the crossing
    sits `|u| sin(theta)` off the earlier strand, and its foot is `u cos(theta)`
    of arc from the crossing along it. The lift is the builder's own
    `sqrt(d^2 - h^2)` over that offset, the foot is resolved onto the earlier
    strand's sample segment as the builder resolves it, and a split
    wall/junction sink is `_wall_weight` of the same `sin`. Nothing is
    searched: the crossings (`g2f._self_intersections` on the INPUT polyline)
    already say where every pair is.

    `s`/`xy` are the fibre's resampled arc position and XY per sample; `p`/`raw_cum` the
    input polyline and its arc, which give each strand's direction. A crossing
    whose strands are closer in arc than the builder's own `_SELF_CONTACT_ARC`
    admissibility plus the reach is skipped, as the builder would skip its
    pairs. Returns a `g2f._SelfPairs`, or None."""
    n = len(s)
    if not hits or n < 3:
        return None
    xy_of = lambda i: (xy[i, 0], xy[i, 1])  # noqa: E731
    H = np.asarray(hits, dtype=np.float64)
    a_lo, a_hi, sin_t = H[:, 0], H[:, 1], np.clip(H[:, 2], 0.0, 1.0)
    reach = diameter / np.maximum(sin_t, _SELF_SIN_FLOOR)
    ok = (a_hi - reach) - (a_lo + reach) >= g2f._SELF_CONTACT_ARC * diameter
    if not ok.any():
        return None
    a_lo, a_hi, sin_t, reach = a_lo[ok], a_hi[ok], sin_t[ok], reach[ok]
    seg = np.diff(p[:, :2], axis=0)
    seg_len = np.maximum(np.hypot(seg[:, 0], seg[:, 1]), 1e-300)
    def tangent(a):
        k = np.clip(np.searchsorted(raw_cum, a, side="right") - 1, 0, len(seg) - 1)
        return seg[k] / seg_len[k][:, None]
    cos_t = np.einsum("ij,ij->i", tangent(a_lo), tangent(a_hi))
    # every later-strand sample within reach of its crossing
    q_lo = np.searchsorted(s, a_hi - reach, side="left")
    q_hi = np.searchsorted(s, a_hi + reach, side="right")
    cnt = q_hi - q_lo
    if not cnt.sum():
        return None
    ci = np.repeat(np.arange(len(cnt)), cnt)
    q = np.repeat(q_lo, cnt) + (np.arange(int(cnt.sum()))
                                - np.repeat(np.cumsum(cnt) - cnt, cnt))
    u = s[q] - a_hi[ci]
    near = np.abs(u) * sin_t[ci] < diameter
    q, ci, u = q[near], ci[near], u[near]
    foot = a_lo[ci] + u * cos_t[ci]
    k0 = np.clip(np.searchsorted(s, foot, side="right") - 1, 0, n - 2)
    # The straight-strand foot only NAMES the segment; the offset and the lift
    # are the builder's own closest approach to it and its two neighbours. A
    # strand that turns a corner within reach (a line meeting a curved
    # footprint's rim) otherwise put its foot on the wrong segment and lifted
    # a point that touches nothing: +6 d by layer 9 on a 10 mm / 0.25 circle,
    # climbing a diameter a layer.
    best_h2 = np.full(len(q), np.inf)
    k = k0.copy()
    t = np.zeros(len(q))
    for dk in (-1, 0, 1):
        kk = np.clip(k0 + dk, 0, n - 2)
        x0, y0 = xy_of(kk)
        x1, y1 = xy_of(kk + 1)
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        qx, qy = xy_of(q)
        tt = np.where(L2 > 1e-30, np.clip(((qx - x0) * dx + (qy - y0) * dy)
                                          / np.where(L2 > 1e-30, L2, 1.0),
                                          0.0, 1.0), 0.0)
        hx, hy = qx - (x0 + dx * tt), qy - (y0 + dy * tt)
        h2 = hx * hx + hy * hy
        better = h2 < best_h2
        best_h2 = np.where(better, h2, best_h2)
        k = np.where(better, kk, k)
        t = np.where(better, tt, t)
    touch = best_h2 < diameter * diameter
    q, ci, k, t, best_h2 = q[touch], ci[touch], k[touch], t[touch], best_h2[touch]
    if not len(q):
        return None
    lift = np.sqrt(diameter * diameter - best_h2)
    sink = None
    if fuse_wall is not None and band is not None:
        w = g2f._wall_weight(sin_t[ci], band)
        sink = fuse_wall * w + fuse_sink * (1.0 - w)
    order = np.argsort(q, kind="stable")
    return g2f._SelfPairs(q[order], k[order], t[order], lift[order],
                          None if sink is None else sink[order])


def _periodic_anchor_floors(xy, s, anchors, prior, diameter, known=None):
    """Exact solved-floor heights at periodic crossings over earlier fibres.

    The raster remains the general floor.  This narrow correction is only for
    analytical crossing anchors, where the contact law is unambiguous: sample
    the already solved earlier centreline at the same unrolled point and rest
    one diameter above it.

    `known`, when given, is `prior.rest_heights(xy[:, 0], xy[:, 1],
    0.5 * diameter, -inf)` already taken over EVERY sample of this fibre (the
    drape's own floor query). `rest_heights` answers each point from its own
    cell's candidates by an exact max, so reading it at the anchors is the
    same number as asking again.
    """
    if not len(anchors) or not prior:
        return ()
    arcs = np.unique(np.asarray(anchors, dtype=np.float64))
    # Intersection arithmetic can report the same physical anchor a few
    # picometres apart.  `_resample` keeps both values, so floor every matching
    # sample; otherwise display simplification can retain the unfloored twin.
    #
    # One vectorised bracket for every anchor, not a Python `for` calling
    # `searchsorted` twice each: on the 88 210-point tube that loop was 110 000
    # scalar searches. Same index set, built as one run-length expansion.
    lo = np.searchsorted(s, arcs - 1e-11)
    hi = np.searchsorted(s, arcs + 1e-11, side="right")
    counts = np.maximum(hi - lo, 0)
    total = int(counts.sum())
    if total:
        starts = np.repeat(lo, counts)
        where = np.unique(starts
                          + (np.arange(total, dtype=np.int64)
                             - np.repeat(np.cumsum(counts) - counts, counts)))
    else:
        where = np.zeros(0, dtype=np.int64)
    if not len(where):
        return ()
    # `_SegIndex` splits every unrolled segment at its periodic boundaries and
    # wraps the query coordinates too.  A hand-written +/- one-period search
    # misses an ordinary tube's supports several revolutions away.
    floors = (known[where] if known is not None
              else prior.rest_heights(xy[where, 0], xy[where, 1],
                                      0.5 * diameter, -np.inf))
    out = [(int(k), float(floor)) for k, floor in zip(where, floors)
           if np.isfinite(floor)]
    return out


def _causal_supports(pairs, si, s, diameter, sag_multiplier, fuse_sink):
    """Drop every self support that would rest on its OWN shoulder.

    A support at sample `q` rests on an earlier strand point at arc `s_k` and
    stands `rise = lift - sink` above it. `_span_rebuild` then drapes the
    support's shoulder both ways along the fibre, `sag_depth(2 |s_q - s|)` below
    its top. When that shoulder is still above the strand at `s_k` --
    `sag_depth(2 (s_q - s_k)) < rise` -- the support lifts the very point it
    rests on, the next pass puts it `rise` higher again, and the convergence loop
    runs away by exactly `rise` per pass until its pass limit (seen on a
    hairpin where the laid lead-in doubles back on the default 5-layer tube --
    one support climbed 0.0174 mm = 0.8 d per pass for 1310 passes, 115 s).
    Physically the strand underneath was laid FIRST and its height is final
    before `q` arrives, so such a support must not drive the shoulder iteration.
    It is still lifted clear by the full-pair `_self_contact_apply` that follows,
    so no crossing loses its clearance."""
    if not len(si):
        return si
    q, k, t, lift, sink = pairs
    on = np.isin(q, si)
    if not on.any():
        return si
    qq, kk, tt, ll = q[on], k[on], t[on], lift[on]
    if sink is not None:
        ss = sink[on]
        rise = np.where(ss != 0.0, ll - ss, ll)
    else:
        rise = ll - fuse_sink
    s = np.asarray(s, dtype=np.float64)
    s_k = s[kk] + (s[kk + 1] - s[kk]) * tt
    reach = g2f.sag_depth(2.0 * np.abs(s[qq] - s_k), diameter, sag_multiplier)
    feeds = (rise > 0.0) & (reach < rise)
    if not feeds.any():
        return si
    return si[~np.isin(si, qq[feeds])]


def _support_closure_pairs(pairs, si, n, max_rounds=64):
    """The sub-set of `pairs` whose print-order sweep can move `z[si]`.

    The periodic support loop carries exactly ONE thing between passes: the
    heights `z[si]`.  Everything else is rebuilt from the raster floor and the
    supports.  So a pair whose query point cannot reach a support -- through
    "point q reads the strand height on segment k, so k and k+1 matter" -- is
    swept for nothing inside the loop, and dropping it is EXACT rather than an
    approximation: for every point that CAN reach a support, the reduced sweep
    sees the identical pairs, in the identical print order, with identical
    incoming heights, so it produces the identical height.

    Measured on the 88 210-point re-entrant tube: 216 381 of 556 484 pairs,
    38.9%.  `_self_contact_apply` is linear in the pairs it is handed however it
    sweeps them, so the reduction still pays after that sweep was vectorised.

    This asks nothing of HOW the contacts were found -- it reads the dependency
    structure of whatever pairs it is given -- so it survives a change to the
    detection rule.  Returns None (meaning "use them all") if the fixed point
    has not closed within `max_rounds`, so the fallback is the current
    behaviour rather than a wrong answer.
    """
    if pairs is None:
        return None
    q = np.asarray(pairs[0], dtype=np.int64)
    k = np.asarray(pairs[1], dtype=np.int64)
    need = np.zeros(n, dtype=bool)
    need[np.asarray(si, dtype=np.int64)] = True
    count = int(need.sum())
    for _ in range(max_rounds):
        m = need[q]
        kk = k[m]
        need[kk] = True
        need[kk + 1] = True
        grown = int(need.sum())
        if grown == count:
            break
        count = grown
    else:
        return None                      # did not close; sweep everything
    keep = need[q]
    if keep.all():
        return None
    # `subset` rather than five list comprehensions: the reduced set is a
    # `_SelfPairs` in its own right, so it carries its own level plan and the
    # loop's sweep stays vectorised (see `gcode_to_fibers._self_pair_plan`).
    return pairs.subset(np.flatnonzero(keep))


_NEIGH = tuple((ox, oy) for ox in (-1, 0, 1) for oy in (-1, 0, 1))


def _stamp_segments(G, occ, H, xy, z, ix, iy, idx, org, period=None):
    """Record, in the flat drape's segment raster `G`, the strand on top of each
    cell: for every sample in `idx` that now holds its cell's `H` maximum, its
    previous / own / next sample -- the two segments of the solved centreline
    either side of it; `occ` (flat, per cell) marks the cell laid. A fibre's
    end sample repeats itself, so its missing segment is a point.

    float32, and only DIFFERENCES are stored in it: XY relative to the cell's
    own corner (`org` = raster origin x, y and cell) -- an absolute coordinate
    at 30 mm is good to 2 nm in float32, and a grazing contact's
    `sqrt(d^2 - o^2)` turned that into 0.9 um -- and Z relative to the
    winner's, whose own Z is `H` itself, in float64. The fusion tests hold the
    drape to the exact solver at 1e-9 mm, below a float32 Z's resolution.
    Row: prev x, y, dz; own x, y; next x, y, dz.

    `period` (None -> the flat raster, byte for byte) is a mandrel's: the v
    rows are then `period / ny` wide from v = 0, and every stored v offset is
    folded into half a period either side, since the fibre's own v is unrolled
    by whole turns and the cell's corner is not."""
    idx = np.asarray(idx, dtype=np.int64)
    if not len(idx):
        return
    won = idx[z[idx] >= H[ix[idx], iy[idx]] - 1e-12]
    if not len(won):
        return
    n = len(z)
    pm = np.maximum(won - 1, 0)
    pp = np.minimum(won + 1, n - 1)
    cx = org[0] + ix[won] * org[2]
    if period is None:
        cy = org[1] + iy[won] * org[2]
        rel = lambda v: v - cy                             # noqa: E731
    else:
        cy = iy[won] * (period / G.shape[1])
        rel = lambda v: _fold(v - cy, period)              # noqa: E731
    occ[ix[won] * G.shape[1] + iy[won]] = True
    G[ix[won], iy[won]] = np.column_stack(
        [xy[pm, 0] - cx, rel(xy[pm, 1]), z[pm] - z[won],
         xy[won, 0] - cx, rel(xy[won, 1]),
         xy[pp, 0] - cx, rel(xy[pp, 1]), z[pp] - z[won]]).astype(np.float32)


def _segment_floor(G, occ, H, qx, qy, ix, iy, diameter, org, period=None):
    """The flat drape's floor: the highest centre height a fibre can rest at
    over the strands in `G` in the 3x3 cells around each query -- the exact
    solver's contact (`_SegIndex.rest_heights`' flat branch, ported: closest
    point on each segment, then `z + sqrt(d^2 - o^2)` for an XY offset
    `o < d`). The offset is found in float32 on cell-relative XY (see
    `_stamp_segments`), the height in float64 off `H`. Returns (height, or
    -inf with no contact; the winning neighbour's index into `_NEIGH`).

    `period` as `_stamp_segments` takes it: the v neighbour wraps round the
    mandrel and the query's v offset is folded the same way."""
    nx, ny = G.shape[:2]
    Gf = G.reshape(nx * ny, G.shape[2])   # views: both are contiguous
    Hf = H.reshape(nx * ny)
    d2 = diameter * diameter
    best = np.full(len(qx), -np.inf)
    arg = np.zeros(len(qx), dtype=np.int8)
    # Flat cell indices and `np.take` rather than 2-D fancy indexing: the
    # same numbers at 0.64x the time. A sample is always >= 2 cells in from
    # the raster's edge (`pad`), so a neighbour never needs clipping.
    lin0 = ix * ny + iy
    cyp = None if period is None else period / ny
    for k, (ox, oy) in enumerate(_NEIGH):
        if period is None:
            lin = lin0 + (ox * ny + oy)
        else:
            jy = (iy + oy) % ny         # u is padded like a flat bed; v wraps
            lin = (ix + ox) * ny + jy
        # most cells between a scaffold's strands hold nothing: only the laid
        # ones are worth the segment test
        q = np.flatnonzero(occ[lin])
        if not len(q):
            continue
        lq = lin[q]
        g = np.take(Gf, lq, axis=0)
        hw = np.take(Hf, lq)
        px = (qx[q] - (org[0] + (ix[q] + ox) * org[2])).astype(np.float32)
        py = ((qy[q] - (org[1] + (iy[q] + oy) * org[2])) if period is None
              else _fold(qy[q] - jy[q] * cyp, period)).astype(np.float32)
        c = np.full(len(q), -np.inf)
        # (segment start, end, and the Z step along t from the winner's Z):
        # prev -> own drops from prev's dz to 0, own -> next rises 0 to dz
        for (i0, i1, dzc, rise) in ((0, 3, 2, False), (3, 5, 7, True)):
            x0, y0 = g[:, i0], g[:, i0 + 1]
            dx = g[:, i1] - x0
            dy = g[:, i1 + 1] - y0
            ex = px - x0
            ey = py - y0
            L2 = dx * dx + dy * dy
            L2[L2 <= 0.0] = 1.0            # a fibre end's point "segment"
            t = (ex * dx + ey * dy) / L2
            np.clip(t, 0.0, 1.0, out=t)
            ex -= dx * t
            ey -= dy * t
            h2 = (ex * ex + ey * ey).astype(np.float64)
            dz = g[:, dzc] * (t if rise else 1.0 - t)
            cc = hw + dz + np.sqrt(np.maximum(d2 - h2, 0.0))
            cc[h2 >= d2] = -np.inf          # out of reach: no contact
            np.maximum(c, cc, out=c)
        up = c > best[q]
        best[q[up]] = c[up]
        arg[q[up]] = k
    return best, arg


def _fold(a, period):
    """`a` folded into [-period/2, period/2): a v offset measured round the
    mandrel the short way."""
    return (a + 0.5 * period) % period - 0.5 * period


def _resample(p, h, anchors=(), coalesce_tol=0.0):
    """Densify polyline `p` to XY sample spacing <= h, keeping every original
    vertex. Returns (xy, s) with `s` the XY arclength at each sample -- the
    drape needs a roughly uniform sampling so the heightmap sees every cell the
    fibre passes through and the dilation window is meaningful in mm."""
    p = np.asarray(p, dtype=np.float64)
    seg = np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    if cum[-1] <= 0.0:
        return p[:, :2].copy(), cum
    grid = np.arange(0.0, cum[-1], h)
    # Retain exact periodic-contact arcs and nearby crest points.  A crossing
    # between regular samples otherwise cannot become a support at all.
    anchors = np.asarray(tuple(anchors), dtype=np.float64)
    anchors = anchors[(anchors > 0.0) & (anchors < cum[-1])]
    s = np.unique(np.concatenate([cum, grid, anchors]))
    if coalesce_tol:
        # Periodic intersection arithmetic can report one physical crossing
        # twice, separated by floating-point noise.  Keeping both yields a
        # zero-length VTK tube segment.  Flat previews do not opt in.
        s = s[np.r_[True, np.diff(s) > coalesce_tol]]
    xy = np.column_stack([np.interp(s, cum, p[:, 0]),
                          np.interp(s, cum, p[:, 1])])
    return xy, s


class _MetricDilator:
    """The metric (nonuniform-`s`) max-plus dilation, with its workspace kept.

    Exact periodic anchors make the resampling nonuniform.  Index*h is then no
    longer a physical offset, so the same sag law is evaluated over the actual
    (metric) arclength instead of against a precomputed index kernel.  Every
    part of that -- which samples are in reach of which, how far apart they are,
    and the droop over that distance -- depends on `s` alone, while `f` enters
    only in the final gather.

    `reach` comes from the original uniform h grid, not from the post-anchor
    samples: inserting an exact crossing must not lengthen a physically
    identical support search.  Out-of-reach entries carry a kernel of +1e30, so
    `f[idx] - K` puts them at about -1e30 and they cannot win a row -- the
    sample's own zero-distance entry is always in reach, so no row is empty.

    ONE call per fibre.  The kernel matrix is not retained from the second
    call on, as the periodic support loop would re-dilate the whole floor once
    per convergence pass.  The floor is `max(f, S)` and the
    dilation splits into a dense `f` term that never changes and a sparse `S`
    term (`sparse_plan` / `sparse`), so the dense half is evaluated once and
    reused directly.  The block workspace is therefore streamed and dropped,
    and every temporary below is sized to one block, not to the fibre.
    """

    __slots__ = ("n", "_s", "_reach", "_diameter", "_mult")

    def __init__(self, s, reach, diameter, sag_multiplier):
        self._s = np.asarray(s, dtype=np.float64)
        self.n = len(self._s)
        self._reach = float(reach)
        self._diameter = diameter
        self._mult = sag_multiplier

    def __call__(self, f):
        """z_i = max_j [f_j - sag_depth(2|s_i - s_j|)] over j within reach.

        Written with in-place numpy so a block costs a handful of
        block x width arrays rather than a dozen: on the 88 210-point tube
        that matrix is 8192 x ~250 per block and the allocation traffic, not
        the arithmetic, was the measured cost.
        """
        s, n, reach = self._s, self.n, self._reach
        out = np.empty(n, dtype=np.float64)
        for b0 in range(0, n, _DILATE_BLOCK):
            b1 = min(n, b0 + _DILATE_BLOCK)
            sb = s[b0:b1]
            lo = np.searchsorted(s, sb - reach, side="left")
            hi = np.searchsorted(s, sb + reach, side="right")
            width = int(np.max(hi - lo))
            idx = lo[:, None] + np.arange(width, dtype=np.int64)
            keep = idx < hi[:, None]
            np.minimum(idx, n - 1, out=idx)
            dist = np.take(s, idx)
            dist -= sb[:, None]
            np.abs(dist, out=dist)
            keep &= dist <= reach + 1e-12
            dist *= 2.0
            row = g2f.sag_depth(dist, self._diameter, self._mult)
            np.negative(row, out=row)
            row += np.take(f, idx)
            # out of reach: the same effectively-minus-infinity `f - 1e30`
            # the explicit kernel sentinel produced, and it can never win a
            # row, because a sample's own zero-distance entry is in reach
            row[~keep] = -1e30
            out[b0:b1] = row.max(axis=1)
        return out

    def sparse_plan(self, wi):
        """A REUSABLE plan for dilating a field that is -inf outside `wi`.

        Max-plus dilation distributes over max, so a floor written as
        `max(f, S)` dilates to `max(dilate(f), dilate(S))`.  On a periodic
        fibre `S` is a few support windows out of tens of thousands of samples
        and it is the ONLY term the convergence loop rewrites between passes --
        `f`, `s`, the reach and therefore the whole kernel stand still.  So the
        geometry is planned ONCE per fibre here and each pass is one gather.

        Duplicate entries in `wi` are allowed: two supports whose windows
        overlap both write the cell and the max absorbs them, exactly as the
        dense floor's running maximum did.

        Rows are the SOURCE samples and columns the queries, the transpose of
        `_plan`, because a support's window reaches far fewer samples than the
        whole fibre reaches supports -- measured on the 88 210-point tube, the
        source-major plan is a third the size of the query-major one.

        Bit-identical to dilating the dense `max(f, S)`, not an approximation:
        the reach test is symmetric in `s`, `|a-b| == |b-a|` is exact in IEEE,
        and a max over the identical set of floats selects the identical float.
        Out-of-reach entries carry the same `+1e30` kernel `_plan` gives them,
        so they land near -1e30 and cannot win a query -- a query's own
        zero-distance entry in `dilate(f)` is always in reach of itself.
        """
        s, n, reach = self._s, self.n, self._reach
        wi = np.asarray(wi, dtype=np.int64)
        if not len(wi):
            return None
        sw = s[wi]
        lo = np.searchsorted(s, sw - reach, side="left")
        hi = np.searchsorted(s, sw + reach, side="right")
        width = max(1, int(np.max(hi - lo)))
        blocks = []
        for b0 in range(0, len(wi), _DILATE_BLOCK):
            b1 = min(len(wi), b0 + _DILATE_BLOCK)
            idx = lo[b0:b1, None] + np.arange(width, dtype=np.int64)
            valid = idx < hi[b0:b1, None]
            idx = np.minimum(idx, n - 1)
            dist = np.abs(s[idx] - sw[b0:b1, None])
            K = g2f.sag_depth(2.0 * dist, self._diameter, self._mult)
            K[~(valid & (dist <= reach + 1e-12))] = 1e30
            blocks.append((b0, b1, idx.astype(np.int32).ravel(), K))
        return blocks

    def sparse(self, plan, wv):
        """Dilate the sparse field `plan` was built for, at values `wv`."""
        out = np.full(self.n, -1e30)
        if plan is None:
            return out
        wv = np.asarray(wv, dtype=np.float64)
        for b0, b1, idx, K in plan:
            np.maximum.at(out, idx, (wv[b0:b1, None] - K).ravel())
        return out


def _dilate(f, kern, W):
    """Blockwise max-plus dilation: z_i = max_{|j-i|<=W} (f_j - kern[j-i+W]).
    The blocks bound the sliding-window scratch to _DILATE_BLOCK x (2W+1).
    Uniform sampling only -- a nonuniform `s` goes through `_MetricDilator`."""
    n = len(f)
    z = np.empty(n)
    fp = np.full(n + 2 * W, -1e30)
    fp[W:W + n] = f
    for b0 in range(0, n, _DILATE_BLOCK):
        b1 = min(n, b0 + _DILATE_BLOCK)
        win = np.lib.stride_tricks.sliding_window_view(
            fp[b0:b1 + 2 * W], 2 * W + 1)
        z[b0:b1] = (win - kern).max(axis=1)
    return z


def _span_rebuild(f, z0, s, diameter, sag_multiplier, eps=1e-6,
                  chart=None, xy=None, supports=()):
    """Pass 2 (see module docstring): contacts = samples where the dilation
    touches the floor; between consecutive contacts hang the real span parabola
    from the contact heights, clamped onto the floor. Before the first / after
    the last contact the dilation result stands (a free end drooping away from
    its last support)."""
    contact = z0 <= f + eps
    floor = f.copy()
    if len(supports):
        # An analytical periodic self-crossing is a point support.  It has no
        # raster cell of its own, so mark it explicitly while retaining the
        # ordinary raster floor everywhere else.  The values are already in
        # print order and read from the final earlier strand.
        si, sh = supports
        si = np.asarray(si, dtype=np.int64)
        floor[si] = np.maximum(floor[si], np.asarray(sh, dtype=np.float64))
        contact[si] = True
    c = np.flatnonzero(contact)
    if len(c) < 2:
        return z0
    z = z0.copy()
    z[contact] = floor[contact]
    j = np.flatnonzero(~contact)
    j = j[(j > c[0]) & (j < c[-1])]
    if len(j):
        k = np.searchsorted(c, j) - 1
        a, b = c[k], c[k + 1]
        sa, sb = s[a], s[b]
        u = (s[j] - sa) / np.maximum(sb - sa, 1e-12)
        depth = g2f.sag_depth(sb - sa, diameter, sag_multiplier)
        if chart is not None and xy is not None:
            # `s` is already the chart-metric span (term C).  The chord of
            # that span still falls below a curved collector (term B), which
            # is another parabolic mid-span depth in the preview's rebuild.
            #
            # The DIRECTION handed to term B is the one the surface sees, not
            # the chart's: `chord_drop` takes `(eu, ev)` in the principal frame
            # and reads the local curvature there.  A chart angle is only the
            # surface angle where the chart is unsheared AND its two stretches
            # match, which is a plane and nothing else.  `chart.principal_step`
            # is what turns one into the other, and on a developable collector
            # it is the same two expressions written inline.
            du = xy[b, 0] - xy[a, 0]
            dv = xy[b, 1] - xy[a, 1]
            raw = np.hypot(du, dv)
            Ls = np.where(raw > 0.0, raw, 1.0)
            um = 0.5 * (xy[a, 0] + xy[b, 0])
            vm = 0.5 * (xy[a, 1] + xy[b, 1])
            hm = 0.5 * (f[a] + f[b])
            cu, cv = du / Ls, dv / Ls
            eu, ev = chart.principal_step(um, vm, hm, cu, cv)
            chord = chart.chord_drop(sb - sa, eu, ev, um, vm, hm)
            depth = depth + np.where(raw > 0.0, chord, 0.0)
        hang = (floor[a] * (1.0 - u) + floor[b] * u
                - 4.0 * depth * u * (1.0 - u))
        z[j] = np.maximum(f[j], hang)
    return z


def _simplify(xy, z, t, diameter, s=None, protected=()):
    """Thin the drape's dense output for display: keep both ends of every Z
    step above _SIMPLIFY_TOL_DIAM diameters (peaks, touchdown cliffs), every
    _SIMPLIFY_STRIDE-th sample of the flats, and the endpoints. Without this
    the blind stride in gui_preview.decimate_paths (150 k-point display cap)
    would sample 30x30 scaffolds coarser than a crossover peak is wide."""
    n = len(z)
    if n <= 2:
        return xy, z, t
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    keep[::_SIMPLIFY_STRIDE] = True
    # A curved preview has exact commanded vertices plus exact periodic
    # crossing/crest samples.  They are geometry, not merely display density:
    # dropping one can chord across a crossing and move its UV arclength.
    if s is not None and len(protected):
        p = np.asarray(tuple(protected), dtype=np.float64)
        ss = np.asarray(s)
        # Both arrays are sorted and anchors were inserted into `s`; do not
        # materialise the N-sample by M-anchor comparison for dense helices.
        right = np.searchsorted(ss, p)
        left = np.maximum(right - 1, 0)
        where = np.concatenate([left, np.minimum(right, len(ss) - 1)])
        want = np.concatenate([p, p])
        where = where[np.isclose(ss[where], want, rtol=0.0, atol=1e-11)]
        keep[where] = True
    big = np.abs(np.diff(z)) > _SIMPLIFY_TOL_DIAM * diameter
    keep[:-1] |= big
    keep[1:] |= big
    return xy[keep], z[keep], (t[keep] if t is not None else None)


def _raster_cell(diameter, x_span, y_span):
    """The drape raster's cell (mm): one fibre diameter, unless the print's
    raster would then outgrow the memory budget of `_MAX_GRID ** 2` cells.

    THE BUDGET IS AN AREA, NOT A SIDE. A cell of
    `max(d, longest side / _MAX_GRID)` spends the budget on a SQUARE
    of the longest side however thin the print is. One hand-typed axial move
    to X100 on the 12 mm default tube (the G-code page; a 9.4 mm circumference)
    raised the cell from 21.8 um to 71 um, 3.3 diameters, for every ply: two
    stacked wall fibres then share a cell and the drape tilts and wobbles the
    whole tube, though the raster it needed (4 600 x 430 cells) was inside the
    budget the square rule reserved.

    The cell is the smallest at which `(x_span/c + 6) * (y_span/c + 6)` -- the
    raster's two sides at `c`, the 2-cell pads and the +2 of their rounding
    included -- fits the budget, so the bound holds for a thin print too. It
    is never coarser than the old rule's (`min`), which bounded memory by its
    own square already: every print that drew at one diameter still does, and
    the fix can only make a raster finer."""
    budget = float(_MAX_GRID) ** 2
    a, b = float(x_span), float(y_span)
    q = budget - 36.0
    area_cell = (6.0 * (a + b) + math.sqrt(36.0 * (a + b) ** 2
                                           + 4.0 * q * a * b)) / (2.0 * q)
    return max(float(diameter), min(max(a, b) / _MAX_GRID, area_cell))


def drape_sag(paths, ptimes, diameter, sag_multiplier=1.0, cell=None,
              sag_reset_layers=0, fuse_sink=0.0, fuse_wall=None, band=None,
              frame=None, max_samples=None, should_stop=None):
    """Approximate-sag every fibre (see module docstring for the algorithm).

    `max_samples`: a caller's own, tighter sample budget (None is
    `_MAX_DRAPE_SAMPLES`, the runaway guard). An in-app preview passes the
    number its process can afford; over it, `DrapeBudgetExceeded` carries the
    estimate and nothing is allocated.

    paths: deposition-ordered 3D polylines (parse order IS deposition order);
    ptimes: parallel per-point print-time arrays or None; diameter: the scalar fibre
    diameter (mm); sag_multiplier: the same dimensionless knob `apply_sag` takes
    (0 or negative -> no sag, passed through). Returns (sagged_paths, sagged_times),
    resampled with times interpolated by arc length -- monotonic, since only Z moves.

    sag_reset_layers: `apply_sag`'s period (0 = never). Here the whole reset is
    `H[:] = H.max()`, the twin of `_SagState.maybe_reset` re-datuming onto the peaks;
    layers are clustered with the exact solver's own `_layer_ids` at the same
    tolerance, so the preview resets on exactly the layers the STL does.

    fuse_sink: the fusion centre-sink in mm (0 = none), ALREADY EVALUATED by the
    caller so this engine stays free of any physics import. At one magnitude it is
    not an approximation: the exact solve's two twin sink sites are this engine's
    single floor, so the drape reproduces its stacked-peak compaction to numerical
    precision (pinned by test_preview_sag). fuse_wall + band, the COLLINEAR sink and
    the (sin_lo, sin_hi) blend band, split it by crossing angle exactly as the exact
    solver does, through the `A` raster carrying the DIRECTION of whatever fibre is
    on top of each cell; equal sinks are byte-for-byte the pre-split drape.

    frame: a deposition frame or None, and it decides what `paths` CARRIES.
    `None` (a flat bed) and the four-axis `CylinderFrame` take the chart columns
    they always took -- world `(x, y, z)` on the bed, the frame's own unrolled
    `(u, v, h)` on the mandrel -- and a `CylinderFrame` still folds the raster at
    the frame period, measures spans on the offset surface, applies the chord drop
    and resolves pass-crossings through the exact periodic index. EVERY OTHER
    frame takes **world XYZ in the collector's own frame**: a dome, bowl or torus,
    where the raster is binned in `sag_approx.PolarChart` (the pole's own chart,
    which is where the apex stops being a refusal), and a CONE
    too, which is developable and so still MEASURES in `GlobalChart` but is not
    holding chart columns and must be converted. `sag_approx.chart_for` picks the
    chart and `sag_approx.holds_chart` decides the currency; both read the frame
    alone, and they are different questions.

    should_stop: None, or a no-argument callable asked before each fibre; True
    raises `BuildSuperseded`. The live preview passes "a newer edit is waiting"."""
    if sag_multiplier <= 0.0 or not paths:
        return paths, ptimes
    if not math.isfinite(diameter) or diameter <= 0:
        return paths, ptimes
    # The chart has to be a RULER where the pattern goes: this engine bins the
    # floor into a raster in (u, v), so a chart that overstates a distance
    # loses a support silently. `mew_frame.require_chart` refuses the disc
    # around the axis of revolution where `v = rho*phi` gives out, not the
    # whole surface.
    #
    # `ruler="approx"`, and a RASTER cannot earn "local" the way the exact solver
    # does. Slice 25 gave `apply_sag` a per-QUERY local chart; there is no
    # per-query anything here -- the raster IS the chart, one grid for the whole
    # part -- so this engine keeps measuring globally, and its band is not a
    # tolerance it satisfies by construction but a record of how far it has
    # actually been MEASURED (`mew_frame.CHART_APPROX_BAND`), and no further.
    # Against the exact solver on the shipped-default dome at
    # 67.0% its mean disagreement is 11.977 um, LOWER than the 18.119 um it
    # already shows on a cone at 4.9%, so the shear is not what dominates it
    # (measured through `sag_bench.compare_outputs`). The obvious mitigation was tried and is NOT
    # applied: dividing the raster cell by `1 + overstatement` made it worse,
    # 16.973 um against 11.977 -- a finer raster resolves the chart better and
    # the drape's own approximation worse, and the second effect is bigger.
    # Refusing the design loop's sag toggle on a collector the exporter accepts
    # is the worse failure, and this engine's own docstring already says never to
    # cite it as a solver result.
    import sag_approx
    chart = sag_approx.chart_for(frame)
    paths = chart.from_world(paths)
    if frame is not None:
        _pts = np.concatenate([np.asarray(p, dtype=np.float64) for p in paths])
        chart.require_ruler("the preview drape", _pts, diameter, ruler="approx")
    period = chart.period
    curved = chart.curved
    if period is not None and period <= 0.0:
        raise ValueError("a periodic preview frame needs a positive period")
    all_pts = np.concatenate([np.asarray(p) for p in paths], axis=0)
    lo = all_pts[:, :2].min(axis=0)
    hi = all_pts[:, :2].max(axis=0)
    # Unrolled v grows by every revolution, while the physical raster repeats
    # after one circumference.  Using the former coarsens a fixed tube prefix
    # as later turns are appended.
    x_extent = float(max(hi[0] - lo[0], 1e-6))
    y_extent = float(max(hi[1] - lo[1], 1e-6))
    # Keep the planar domain exactly.  Only a periodic
    # collector replaces unwrapped v with one physical circumference.
    extent = max(x_extent, float(period) if period is not None else y_extent,
                 1e-6)
    # the solver's bed convention: the lowest commanded layer Z is the bare
    # SURFACE, and a fibre laid on it rests its UNDERSIDE there -- so the lowest
    # CENTRELINE sits one radius up (see apply_sag's `bed_at`). `bed_c` is that
    # centreline level, and it is what every floor below is measured against.
    bed = min(float(np.median(np.asarray(p)[:, 2])) for p in paths)
    bed_c = bed + 0.5 * diameter
    if cell is None:
        cell = _raster_cell(diameter, x_extent,
                            float(period) if period is not None else y_extent)
    if cell > _MAX_CELL_DIAMETERS * diameter:
        raise DrapeBudgetExceeded(
            f"the print spans {extent:,.0f} mm, too wide to drape at this "
            f"fiber's scale")
    laid = sum(float(np.linalg.norm(np.diff(np.asarray(q)[:, :2], axis=0),
                                    axis=1).sum()) for q in paths)
    est = laid / (0.5 * cell)
    if est > (_MAX_DRAPE_SAMPLES if max_samples is None
              else min(int(max_samples), _MAX_DRAPE_SAMPLES)):
        raise DrapeBudgetExceeded(
            f"{laid / 1000.0:,.1f} m of fiber is too much to drape live",
            samples=int(est))
    pad = 2.0 * cell
    nx = int((hi[0] - lo[0] + 2.0 * pad) / cell) + 2
    ny = (max(1, int(math.ceil(period / cell))) if period is not None
          else int((hi[1] - lo[1] + 2.0 * pad) / cell) + 2)
    H = np.full((nx, ny), bed_c - diameter)
    # A FLAT bed's floor is read off `G`, `H`'s twin holding the solved
    # centreline of whatever strand is on top of each cell (`_stamp_segments`),
    # as the exact solver's contact cap (`_segment_floor`) -- not as `H + d`.
    # See the floor block below. float32 x 8 per cell, read where `occ`.
    G = (None if curved
         else np.zeros((nx, ny, 8), dtype=np.float32))
    occ = None if curved else np.zeros(nx * ny, dtype=bool)
    g_org = (float(lo[0]) - pad, float(lo[1]) - pad, cell)
    # A MANDREL reads the same top-strand cap, off its own periodic twin of
    # `G` (`period=` through `_stamp_segments` / `_segment_floor`). Asking
    # the exact index for the whole floor is slow: every ply of a
    # tube retraces the one below, so each query sifts the whole stack under
    # it: half the drape on the Mandrel default preset. Against the exact
    # solver on five tubes (zigzag 20/40 mm, brick, chevron, re-entrant) the
    # RMS is unchanged to 0.001 d, for -20-25% of the drape. Separate from `G`
    # because `G is None` is how the rest of this function tells a flat bed
    # from a curved one; a dome/bowl/cone/torus keeps the exact index.
    pG = (np.zeros((nx, ny, 8), dtype=np.float32) if period is not None
          else None)
    pocc = None if pG is None else np.zeros(nx * ny, dtype=bool)
    # Wall/junction split: `A` is `H`'s twin -- the DIRECTION (as an angle, radians)
    # of the fibre currently on top of each cell, stamped wherever a fibre wins the
    # H max. One float32 raster, not a (dx, dy) pair: the classifier only ever wants
    # |sin(support - query)|, which reads straight off two angles and is invariant to
    # the pi flip a direction's sign would introduce. Allocated only when the split is
    # actually armed, so the ordinary drape pays nothing (no raster, no tangent pass).
    angular = (fuse_wall is not None and band is not None
               and fuse_wall != fuse_sink)
    A = np.zeros((nx, ny), dtype=np.float32) if angular else None
    fuse_any = fuse_sink > 0.0 or (fuse_wall or 0.0) > 0.0
    h = 0.5 * cell                        # arclength sample spacing
    # Periodic sag reset: which layer each fibre belongs to, so the drape can
    # flatten the heightmap on the same layers the exact solve resets on. Off (0)
    # -> no clustering pass at all, byte-for-byte the old drape.
    reset_n = int(sag_reset_layers or 0)
    layer_of = (g2f._layer_ids(paths, layer_tol=0.5 * diameter)[0]
                if reset_n >= 1 else None)
    cur_layer = None
    reset_floor = -math.inf           # raised by a sag reset; see the reset below
    # THE VISIT SEAM, ported from `_SagState._solve_layer`'s `seam_prev`. A
    # fibre whose first INPUT vertex is the previous fibre's last one (the
    # exact solver's 0.25-diameter test) is the same filament carrying on:
    # the layer change of a continuous-flow print, or a physics-relaid one,
    # whose next layer opens on the last one's touchdown. The raster read
    # below would seat that head a full diameter ON the tail just stamped,
    # and every layer then climbs a diameter at the seam instead of one per
    # two layers -- a stepped tent at a corner where nothing crosses, and a
    # break in the drawn tube where tail and head disagree. So the head
    # reads `H` as it stood BEFORE the tail stamped. The exact solver's rule
    # is filament ARC, `_SELF_CONTACT_ARC` diameters: tail = the last that
    # much of the previous fibre, head = the first that much of this one.
    # Head only, not the whole fibre: a linking loop's return pass crosses
    # that corner again and must still land on it, because this raster does
    # not see the fibre's own earlier strand (the module's documented
    # same-fibre departure). Planar only -- a curved frame reads its floor off
    # `prior_solved`, not `H`.
    seam = None
    seam_arc = g2f._SELF_CONTACT_ARC * diameter
    out_paths = []
    out_times = [] if ptimes is not None else None
    # Same class as the dilation gate above, applied here too: `_SegIndex`
    # documents `period=None` as its general mode ("takes the flat code path
    # untouched, byte for byte"), so a curved-non-periodic frame gets a correct,
    # non-wrapping index by passing its own `period` (None) through -- no
    # special-casing needed, only widening WHEN this index gets built at all.
    prior_cross = g2f._SegIndex(cell, period=period) if curved else None
    #
    # `prior_solved` is only ever asked for rest heights, so its hash cell is a
    # SPEED knob alone (`_SegIndex`: extras never change a result) and it takes
    # the exact solver's own `_hash_cell`. At the drape's `cell` = one diameter
    # the contact reach is one diameter too, so `rest_heights` widened every
    # query to a 5x5 block (`k = ceil(1.25)`); at 1.5 d it is 3x3. Drape
    # byte-identical on five tubes and the five-axis default, -20-30%.
    prior_solved = (g2f._SegIndex(g2f._hash_cell(diameter), period=period)
                    if curved else None)
    # A FLAT fibre's self-lifted runs (the strand riding its own earlier
    # strand) and the cells within reach of them: the exact cap the next
    # layers read over those peaks instead of the raster's top-hat (see the
    # stamp at the end of the loop). Built on the first such run, so a print
    # with no self-crossing never allocates either.
    self_idx = None
    self_near = None
    self_top = -math.inf
    # A flat fibre's own crossings are a function of its XY alone (`raw_cum` and
    # the finder's cell both derive from it), and a plate scaffold repeats: every
    # layer at the same angle is the same XY on a higher z -- bitwise so in the
    # live preview, whose relay lays each repeated layer once
    # (`mew_deposition._RepeatLays`). So the finder runs once per distinct XY.
    planar_seen = {}
    for fi, p in enumerate(paths):
        # `should_stop` (None: never) is asked once per fibre, so a build a
        # newer edit has replaced stops within one fibre instead of running
        # to the end; it never changes what a finished drape returns
        if should_stop is not None and should_stop():
            raise BuildSuperseded("a newer edit replaced this build")
        if layer_of is not None and layer_of[fi] != cur_layer:
            cur_layer = layer_of[fi]
            # Entering layer `cur_layer`: reset on every Nth, matching the batch
            # solver's "layers_committed % N == 0" (so layer 0, already flat on the
            # plate, is not a reset -- the period starts counting from it).
            if cur_layer and cur_layer % reset_n == 0:
                # the structure below becomes a flat plate -- at the peaks,
                # which include the self-lifted ones `H` holds unlifted
                H[:] = max(float(H.max()), self_top)
                self_idx = self_near = None
                self_top = -math.inf
                # Neither the index floor nor the flat segment floor below
                # reads `H` as a plate, so carry the reset datum itself: a
                # flattened raster is exactly `f = H.max() + diameter`
                # everywhere, which is a scalar lower bound on the floor from
                # here on. Fibres laid AFTER the reset are seen as usual.
                reset_floor = float(H[0, 0]) + diameter
                if occ is not None:
                    occ[:] = False        # the strands are under the plate now
                if pocc is not None:
                    pocc[:] = False
                seam = None               # its pre-tail snapshot is pre-flatten
        p = np.asarray(p, dtype=np.float64)
        if len(p) < 2:
            # still the filament passing through (`seam_prev`'s one-point
            # member): the seam moves to it, the tail stays the last stamped
            if seam is not None and len(p):
                seam["xy"] = p[0, :2].copy()
            # No segment, so no droop to solve -- but it is still a fibre laid
            # on a surface, and its Z is that surface. Seat it the way every
            # solved point is seated (`bed_c`), or it passes through a radius
            # low and becomes the lowest thing in the preview.
            p = p + np.array([0.0, 0.0, 0.5 * diameter])
            out_paths.append(p.copy())
            if out_times is not None:
                out_times.append(np.asarray(ptimes[fi], dtype=np.float64))
            continue
        raw_cum = np.concatenate([[0.0], np.cumsum(
            np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1))])
        anchors = ()
        exact_floors = ()
        # A flat fibre's own crossings (a linking loop over its own lead-in, one
        # loop over the next): found on the INPUT vertices, which is cheap, and
        # turned straight into contacts (`_crossing_self_pairs`).
        # No anchors are inserted for them, so `s` stays the uniform grid the
        # fast kernel below needs; a crossing lands within h/2 of a sample.
        # The finder's hash cell only sets its SPEED -- the crossings it
        # returns were identical at every cell from 0.022 to 2 mm -- and the
        # fast one tracks the spacing between the fibre's own strands, which
        # its bounding area over its length estimates whatever the sampling.
        # The optimum sat at ~1x that on commanded paths and ~0.4x on
        # physics-relaid ones, so half: 10 mm / 0.25 commanded went 0.32 ->
        # 0.06 s from the drape's own cell, 30 mm / 0.5 relaid 0.47 -> 0.2 s.
        # Thinning the input first
        # (Douglas-Peucker at 0.05 d) was tried and is NOT applied: a relaid
        # path is curved almost everywhere and kept 57% of its vertices, the
        # thinning cost more than it saved, and it lost 9 of 396 crossings.
        planar_hits = ()
        if not curved and len(p) >= 4 and raw_cum[-1] > 0.0:
            ext = p[:, :2].max(axis=0) - p[:, :2].min(axis=0)
            xcell = max(cell, 0.5 * float(ext[0] * ext[1]) / float(raw_cum[-1]))
            xy_key = p[:, :2].tobytes()
            if xy_key not in planar_seen:
                if g2f._self_pair_load(p, xcell) > _MAX_SELF_CANDIDATES:
                    raise DrapeBudgetExceeded(
                        "one fiber retraces its own path too many times to "
                        "drape live")
                planar_seen[xy_key] = g2f._self_intersections(p, raw_cum, xcell)
            planar_hits = list(planar_seen[xy_key])
        if curved:
            # Seed every true self-crossing -- same-fibre (a link loop
            # crossing itself is exactly this) or periodic -- plus a short
            # crest neighbourhood, before computing the drape and support
            # lift. `_self_intersections`/`_self_pairs_xy` document
            # `period=None` as the FLAT identity case, not a periodic-only
            # mode, so a non-wrapping curved frame (RevolutionFrame) finds its
            # own same-fibre crossings correctly here -- this was gated on
            # `period is not None` and so never ran for a dome/bowl/cone/torus,
            # which is why link-loop crossovers were not detected on those
            # pages.
            # counted unwrapped: a periodic chart's seam crossings come on
            # top, so this undercounts there, but a retraced corridor shows
            if len(p) >= 4 and g2f._self_pair_load(p, cell) > _MAX_SELF_CANDIDATES:
                raise DrapeBudgetExceeded(
                    "one fiber retraces its own path too many times to "
                    "drape live")
            hits = g2f._self_intersections(p, raw_cum, cell,
                                           period=period)
            if hits:
                anchors = np.asarray(
                    [(q - diameter, q, q + diameter)
                     for pair in hits for q in pair[:2]], dtype=np.float64
                ).ravel()
            # Raster bins only approximate inter-fibre contacts.  Keep exact
            # crossing arcs from already deposited paths as display/support
            # samples too, so a later layer cannot slip between bins.
            between = g2f._crossing_contacts(p, raw_cum, prior_cross)
            if len(between):
                anchors = np.concatenate([np.asarray(anchors), between])
        # Same coalesce concern as the periodic case: `anchors` now also comes
        # from the widened self/cross-fibre exactness above for ANY curved
        # frame, and that arithmetic can report one physical crossing twice a
        # hair apart, same as the periodic case's own note.
        xy, s_raw = _resample(p, h, anchors=anchors,
                              coalesce_tol=(1e-11 if curved else 0.0))
        t = None
        if ptimes is not None:
            seg = np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1)
            cum = np.concatenate([[0.0], np.cumsum(seg)])
            t = np.interp(s_raw, cum, np.asarray(ptimes[fi], dtype=np.float64))
        if curved:
            # The chart is the BARE surface; the fibre rides the parallel one h
            # out, stretched by `mew_frame.metric`'s `1 + k_i*h` per principal
            # direction.  On a cylinder k1 = 0 and only the v term moves.
            h_cmd = np.interp(s_raw, raw_cum, p[:, 2])
            dxy = np.diff(xy, axis=0)
            eu, ev = chart.principal_step(0.5 * (xy[:-1, 0] + xy[1:, 0]),
                                          0.5 * (xy[:-1, 1] + xy[1:, 1]),
                                          0.5 * (h_cmd[:-1] + h_cmd[1:]),
                                          dxy[:, 0], dxy[:, 1])
            s = np.concatenate([[0.0], np.cumsum(np.hypot(eu, ev))])
        else:
            s = s_raw
        ix = np.clip(((xy[:, 0] - lo[0] + pad) / cell).astype(np.int64),
                     0, nx - 1)
        if period is not None:
            iy = np.floor(np.mod(xy[:, 1], period) * ny / period).astype(np.int64)
            iy = np.clip(iy, 0, ny - 1)
        else:
            iy = np.clip(((xy[:, 1] - lo[1] + pad) / cell).astype(np.int64),
                         0, ny - 1)
        phi_q = None
        if angular:
            # This fibre's own direction at each sample, over a ~1-DIAMETER arc
            # window -- the same scale, and for the same reason, as the exact
            # solver's `_tangent_at(window=diameter)`: a contact's melt is smeared
            # over about a fibre width, so the angle it crosses at is only
            # meaningful averaged over that width, and a raw per-segment reading
            # would let a sharp turnaround corner flick the sink across the whole
            # wall->junction range within one sample.
            tx, ty = g2f._tangent_at(np.column_stack([xy, np.zeros(len(xy))]),
                                     s, s, window=diameter)
            phi_q = np.arctan2(ty, tx)
        # What each sample finds under it, BEFORE this fibre stamps -- with the
        # seam head reading past its own predecessor's tail (see `seam`).
        #
        # A FLAT BED READS A CAP, NOT A TOP-HAT. `H + diameter` hands a query
        # the full one-diameter rest anywhere in a support's cell and nothing
        # one cell over, and the cell is a diameter wide -- the contact's
        # whole reach. A fibre laid BESIDE an earlier strand at 0.5-1.2 d
        # (a 60-degree triangle's same-direction layers drift apart by that
        # much as they run into their loops) steps on and off it cell by
        # cell: a ledge ending in a one-sample cliff, a spike, a raised run
        # over a strand it never touches -- the preview's wrinkled fibres.
        # The curved branch fixes the same top-hat with the exact index; that costs
        # 9x the whole drape here (1.5 -> 13.4 s at 30 mm), so `G` keeps the
        # top strand's two segments per cell and `_segment_floor` reads the
        # exact contact off those alone. `A` is read at the cell that won.
        def floor_read(sel):
            if G is None:
                return (H[ix[sel], iy[sel]] + diameter,
                        A[ix[sel], iy[sel]] if angular else None)
            ft, k = _segment_floor(G, occ, H, xy[sel, 0], xy[sel, 1],
                                   ix[sel], iy[sel], diameter, g_org)
            ft = np.maximum(ft, bed_c)
            if not angular:
                return ft, None
            off = np.asarray(_NEIGH, dtype=np.int64)[k]
            return ft, A[np.clip(ix[sel] + off[:, 0], 0, nx - 1),
                         np.clip(iy[sel] + off[:, 1], 0, ny - 1)]
        f, Aq = floor_read(slice(None))   # the floor, BEFORE this fibre stamps
        head = None
        if seam is not None and math.hypot(p[0, 0] - seam["xy"][0],
                                           p[0, 1] - seam["xy"][1]) \
                < 0.25 * diameter:
            head = s_raw < seam_arc
            if head.any():
                tcell = (seam["ix"], seam["iy"])
                keep = [H[tcell].copy()]
                H[tcell] = seam["pre"]
                if G is not None:
                    tlin = tcell[0] * ny + tcell[1]
                    keep.append(G[tcell].copy())
                    keep.append(occ[tlin].copy())
                    G[tcell] = seam["gpre"]
                    occ[tlin] = seam["opre"]
                if angular:
                    keep.append(A[tcell].copy())
                    A[tcell] = seam["apre"]
                fh, ah = floor_read(head)
                f[head] = fh
                if angular:
                    Aq[head] = ah
                    A[tcell] = keep.pop()
                if G is not None:
                    occ[tlin] = keep.pop()
                    G[tcell] = keep.pop()
                H[tcell] = keep.pop()
        laid_exact = None
        if G is not None:
            if reset_floor > -math.inf:
                np.maximum(f, reset_floor, out=f)
            # the cap is `bed_c` with nothing in reach and strictly more with a
            # contact, exactly as `rest_heights` reports it below
            laid_exact = f > bed_c + 1e-12
        if prior_solved is not None:
            # THE RASTER FLOOR IS A TOP-HAT, and that is the preview's fibre
            # ripple. `H` holds one
            # stamped cell per sample, so `H + diameter` hands a query the FULL
            # one-diameter rest anywhere inside the support's own cell and
            # nothing at all one cell away. The real contact is a cap: two
            # tubes touch at centre separation d, so an XY offset `o` still
            # leaves a vertical gap sqrt(d^2 - o^2) (`_SegIndex.rest_heights`,
            # and it is what the exact solver rests on). A fibre crossing a
            # support at anything but a steep angle therefore reads the
            # top-hat as a DASHED line -- support, bed, support -- and the
            # measured excess sits exactly in the 1-2 diameter band the cell
            # size sets, 6.2x the exact solver's power there.
            #
            # A curved frame already BUILDS and feeds `prior_solved` for its
            # crossing anchors, so asking it for the whole floor costs the
            # query and no index: +33% on the shipped dome preview
            # (0.72 s -> 0.95 s), and it takes the drape's height high-pass
            # from 0.0232 rms to 0.0137 against `apply_sag`'s own 0.0131.
            # A flat bed reads the same cap off `G` instead (the floor above):
            # the index costs it 9x its whole drape.
            #
            # A mandrel reads it off `pG` instead (see its allocation). The
            # exact query elsewhere is asked with no floor and floored at
            # `bed_c` after: the same numbers (`rest_heights` is an exact
            # max over `floor` and the contacts), and the raw contacts are
            # what the crossing anchors below read, instead of asking again.
            solved_raw = None
            if pG is not None:
                f = np.maximum(_segment_floor(
                    pG, pocc, H, xy[:, 0], xy[:, 1], ix, iy, diameter, g_org,
                    period=period)[0], bed_c)
            else:
                solved_raw = np.asarray(prior_solved.rest_heights(
                    xy[:, 0], xy[:, 1], 0.5 * diameter, -np.inf),
                    dtype=np.float64)
                f = np.maximum(solved_raw, bed_c)
            if reset_floor > -math.inf:
                np.maximum(f, reset_floor, out=f)
            # "something is below" is the contact itself now, not a stamped
            # cell: `rest_heights` returns `bed_c` verbatim with nothing under
            # the query and strictly more with a contact.
            laid_exact = f > bed_c + 1e-12
        # The sink this fibre's contacts take, kept in scope for the curved
        # anchor override below: that override rewrites the same floor and owes
        # the same compaction (see its comment).
        sink = 0.0
        if fuse_any:
            # Fusion: a fibre landing ON another coalesces INTO it, so its resting
            # centre sits `sink` below the geometric rest-on-top height. Only
            # fibre-on-fibre contacts fuse -- the bare bed does not -- and nothing
            # sinks below the bed, matching `_sink_fused_contacts`.
            # "Something is below" is exact here, not an epsilon guess: an unstamped
            # cell is still at the H init (bed_c - diameter) while any stamped one
            # is >= bed_c, a full diameter apart. The sink then accumulates through H on
            # its own, exactly as it does through `all_lower` in the exact solve --
            # so stacked peaks come down layer on layer while the suspended spans
            # keep their droop and just translate with their supports.
            laid = laid_exact     # both branches above set it
            if angular:
                # Classify each contact by the angle between this fibre and whatever
                # is on top of the cell under it: collinear (a wall, or a fibre
                # settling into a sag valley onto the parallel layer below) takes
                # the wall sink, a crossing takes the junction sink, and the band
                # blends between. |sin(support - query)| straight from the two
                # angles -- no vectors, and the pi ambiguity cancels.
                sin_t = np.abs(np.sin(Aq.astype(np.float64) - phi_q))
                w = g2f._wall_weight(np.minimum(sin_t, 1.0), band)
                sink = fuse_wall * w + fuse_sink * (1.0 - w)
            else:
                sink = fuse_sink
            f = np.where(laid, np.maximum(f - sink, bed_c), f)
        if self_idx is not None:
            # Over an earlier fibre's SELF-LIFTED run the raster holds that
            # strand unlifted, and the lift is read here as the exact cap the
            # curved branch reads everywhere (`prior_solved` above): rest
            # `sqrt(d^2 - o^2)` over the run at XY offset `o`, less the sink a
            # fibre-on-fibre contact takes. As a raster stamp the peak was a
            # TOP-HAT, and a turnaround loop crosses the strand below at ~26
            # degrees, so a later layer rode a full diameter up for `3 cells /
            # sin(theta)` ~ 7 d of arc and fell off its end in one sample: the
            # kinked rise over a loop crossing, compounding layer on layer.
            # Only samples in a cell
            # next to a run are asked, so a print without one pays nothing. The
            # seam head keeps reading past its predecessor's tail (`seam`).
            m = self_near[ix, iy]
            if head is not None:
                m &= ~head
            if m.any():
                cap = self_idx.rest_heights(xy[m, 0], xy[m, 1],
                                            0.5 * diameter, -1e30)
                sk = sink if np.ndim(sink) == 0 else np.asarray(sink)[m]
                cap = np.where(cap > -1e29, np.maximum(cap - sk, bed_c), -1e30)
                f[m] = np.maximum(f[m], cap)
        if curved:
            # At a true inter-fibre crossing the raster may have chosen a
            # neighbouring bin.  Override only that anchor with the solved
            # earlier centreline's exact one-diameter rest height.
            # `_periodic_anchor_floors` is named for its original (mandrel)
            # motivation but is general: it only reads `prior.rest_heights`,
            # a `_SegIndex` query with no periodicity requirement of its own.
            #
            # IT OWES THE FUSION SINK. The
            # raster floor above has already had `sink` taken off every
            # fibre-on-fibre cell; `rest_heights` returns the bare one-diameter
            # rest, so an unsunk override always won the `max` and every
            # crossing on a CURVED collector was floored a full diameter above
            # the strand below instead of `d - sink`. One sink per layer,
            # compounding: on the shipped dome preview the layer-10 centreline
            # stood at 0.1178 mm above the surface against `apply_sag`'s 0.0676,
            # and 0.0327 of that 0.0502 gap was this line. Flat never enters the
            # branch, which is why only the four- and five-axis previews showed
            # it. Clamped at `bed_c` for the reason the raster floor is.
            exact_floors = _periodic_anchor_floors(
                    xy, s_raw, between, prior_solved, diameter,
                    known=solved_raw)
            _sink_k = (sink if np.isscalar(sink) or np.ndim(sink) == 0
                       else np.asarray(sink))
            # SINK ONCE, HERE, and hand the sunk heights on. The post-rebuild
            # clamp below re-applies these same anchors to `z`, and must not
            # re-apply them RAW: every crossing anchor would be
            # pushed back up by the full `sink` after the span rebuild had
            # already seated it and its neighbours at `floor - sink`. That is a
            # 0.35-diameter spike at isolated points -- a bump, and the second
            # half of the preview's fibre ripple. Same class as the pre-rebuild
            # line beside it.
            exact_floors = [(k, max(floor - (float(_sink_k)
                                             if np.ndim(_sink_k) == 0
                                             else float(_sink_k[k])), bed_c))
                            for k, floor in exact_floors]
            for k, floor in exact_floors:
                f[k] = max(f[k], floor)
        self_support = ()
        support_top = -np.inf
        def support_windows(si):
            # The heightmap's cell contact has one cell of extent. Use physical
            # frame arclength, because exact anchors make the resampling
            # nonuniform on a cylinder. `s` is sorted, so each support's window is
            # one contiguous run -- bracket it rather than testing the whole fibre
            # once per support, which is quadratic in a tube's supports.
            #
            # SPARSE and GEOMETRY-ONLY. Returning the whole floor `max(f, S)` as a
            # dense array instead makes the convergence loop re-dilate it end to
            # end once per pass, though only the support HEIGHTS moved. Returning
            # the raised samples and which support each came from lets the
            # dilation split and be planned once (`_MetricDilator.sparse_plan`):
            # the same number by a cheaper route.
            sk = s[np.asarray(si, dtype=np.int64)]
            lo = np.searchsorted(s, sk - cell - 1e-12, side="left")
            hi = np.searchsorted(s, sk + cell + 1e-12, side="right")
            counts = hi - lo
            starts = np.repeat(lo, counts)
            wi = starts + (np.arange(int(counts.sum()), dtype=np.int64)
                           - np.repeat(np.cumsum(counts) - counts, counts))
            # windows can overlap; duplicate sample indices are harmless -- the
            # dilation's row max absorbs them just as the dense floor's running
            # maximum did
            return wi, np.repeat(np.arange(len(counts), dtype=np.int64), counts)
        # A flat fibre's own crossings take the SAME support path as a curved
        # one's below; only the pairs are built differently. `self_contact`
        # stays False for a flat fibre that never crosses itself, which is then
        # the plain drape byte for byte.
        self_pairs = (_crossing_self_pairs(
            s_raw, xy, p, raw_cum, planar_hits, diameter,
            fuse_sink=fuse_sink, fuse_wall=fuse_wall, band=band)
            if planar_hits else None)
        self_contact = curved or self_pairs is not None
        self_raised = None       # which samples their own strand lifted
        if self_contact:
            # A same-fibre crossing -- periodic (a tube) or a plain link loop
            # on a dome/bowl/cone -- is a support, not merely a final
            # clearance clamp.  Find the analytical local maxima of the
            # deposition-ordered self-rest field and hand those point supports
            # to `_span_rebuild`.  Its physical `s` coordinate then rebuilds
            # the neighbouring shoulders with the same law as earlier-fibre
            # contacts.  The post-rebuild call remains the exact clearance
            # guard between support samples. `_self_contact_pairs` ->
            # `_self_pairs_xy` documents `period=None` as its FLAT identity
            # case, not periodic-only, so this widens correctly without
            # touching that function.
            # A tube's self-contact is solved three or more times over the SAME
            # fibre (seed, post-rebuild, then once per convergence pass), and all
            # of it but the print-order sweep is a function of the fibre's XY
            # geometry alone. Build that half once and re-sweep it.
            if curved:
                self_pairs = g2f._self_contact_pairs(
                    s_raw, xy[:, 0], xy[:, 1], diameter, cell,
                    fuse_sink=fuse_sink, fuse_wall=fuse_wall, band=band,
                    period=period)
            self_floor = g2f._self_contact_apply(self_pairs, f,
                                                 fuse_sink=fuse_sink)
            raised = self_floor > f + 1e-9
            if not curved:
                self_raised = raised
            peak = np.zeros(len(f), dtype=bool)
            if len(f) > 2:
                peak[1:-1] = (raised[1:-1]
                               & (self_floor[1:-1] >= self_floor[:-2])
                               & (self_floor[1:-1] > self_floor[2:]))
            if peak.any():
                si = _causal_supports(self_pairs, np.flatnonzero(peak), s,
                                      diameter, sag_multiplier, fuse_sink)
            if peak.any() and len(si):
                self_support = (si, self_floor[si])
                # Let the ordinary dilation create the free shoulder between
                # this discrete support and the neighbouring raster floor.
                # `f` remains untouched: it describes only physical raster
                # contact, while `_span_rebuild` receives the support height.
                support_top = float(np.max(self_support[1]))
        # Kernel half-width: wide enough that the droop law can bridge the
        # fibre's full relief, so no reachable support is outside the window.
        # The relief is measured over the COMPLETE floor -- the raster plus
        # whatever full diameter of relief the self supports above just added.
        # `max(f, S)`'s maximum is `max(f.max(), S.max())`, so the sparse
        # supports give the same bound the dense array did.
        relief = float(max(f.max(), support_top) - f.min()) + 1e-9
        W = 4
        while (g2f.sag_depth(2.0 * W * h, diameter, sag_multiplier) < relief
               and W < _W_CAP):
            W *= 2
        offs = np.arange(-W, W + 1, dtype=np.float64) * h
        kern = g2f.sag_depth(2.0 * np.abs(offs), diameter, sag_multiplier)
        # Gate on CURVATURE, not periodicity: `curved` (frame.is_curved()) is
        # exactly what decides whether `s` above came from `frame.metric` and
        # can therefore be non-uniform -- a periodic frame is always curved
        # (period_v is not None is one of is_curved's own disjuncts), but a
        # curved frame need not be periodic. `RevolutionFrame` (dome/bowl/cone)
        # never wraps (`period_v` is always None) yet its metric varies with
        # meridian position, so `period is not None` alone left every
        # non-mandrel five-axis design dilating with a uniform-index kernel
        # against a non-uniform arclength.
        # `_MetricDilator` itself has no periodicity assumption baked in (no
        # modulo/wraparound anywhere in `__call__`/`sparse_plan`/`sparse`): it
        # just dilates over whatever monotonic `s` it is handed, so widening
        # this gate needs no change to that class.
        dilate = (_MetricDilator(s, W * h, diameter, sag_multiplier)
                  if curved
                  else (lambda field: _dilate(field, kern, W)))
        # The support term's sparse dilation. A flat fibre keeps the uniform
        # kernel for its dense floor -- the metric one was 0.30 s of a 0.70 s
        # default-preset drape, for the same number on a uniform `s` -- and
        # plans only its few self supports
        # through `_MetricDilator`, whose `sag_depth(2|ds|)` over the same
        # `W * h` reach is that kernel's own law. Its constructor stores `s`
        # and nothing else, so an unused one is free.
        sparse_dil = (dilate if curved
                      else _MetricDilator(s, W * h, diameter, sag_multiplier))
        # The dense floor's dilation, split into the part that never changes
        # and the part the support loop rewrites every pass.  `dilate(f)` is
        # computed at most once per fibre and reused; the support term is a
        # sparse scatter over ~a handful of samples per support.  See
        # `_MetricDilator.scatter` for why the split is exact.
        cache = {"base": None, "plan": None, "src": None}
        def dilate_floor(supports):
            if cache["base"] is None:
                cache["base"] = dilate(f)
            if not len(supports) or not len(supports[0]):
                return cache["base"]
            if cache["plan"] is None:
                wi, src = support_windows(supports[0])
                cache["src"] = src
                cache["plan"] = sparse_dil.sparse_plan(wi)
            lift = sparse_dil.sparse(cache["plan"],
                                 np.asarray(supports[1],
                                            dtype=np.float64)[cache["src"]])
            return np.maximum(cache["base"], lift)
        z0 = dilate_floor(self_support)
        z = _span_rebuild(f, z0, s, diameter, sag_multiplier,
                          chart=(chart if curved else None), xy=xy,
                          supports=self_support)
        for k, floor in exact_floors:
            z[k] = max(z[k], floor)
        if self_contact:
            # The heightmap is deliberately queried before the fibre stamps
            # itself, so it never sees the fibre's own earlier strand. Resolve
            # that in print order, after the fast raster floor pass -- a tube's
            # lattice from a pass crossing its own earlier revolutions, a
            # dome/bowl/cone pattern's own link loop crossing itself, and
            # a flat fibre's crossings too
            # (`_crossing_self_pairs`).
            z = g2f._self_contact_apply(self_pairs, z, fuse_sink=fuse_sink)
            if len(self_support):
                # A later crossing can rest on an earlier shoulder itself lifted
                # by a still-earlier crossing, so rebuild from print-order-final
                # support heights until that causal chain settles. The support
                # count is a defensive finite cap; the tolerance is the
                # convergence condition. The test is at the TOP because `z` was
                # ALREADY solved from `solved_h`: a bottom test rebuilds the `z`
                # in hand before breaking on it, which is the ordinary case on the
                # mandrel fixtures and paid for one redundant whole-fibre solve per
                # fibre. Inside the loop the sweep is handed only the pairs that
                # can reach a support; the heights it tests are bit-identical and
                # everything else is thrown away by the next rebuild. The pass
                # whose result is KEPT is re-run once below over the full set.
                si, solved_h = self_support
                loop_pairs = _support_closure_pairs(self_pairs, si, len(z))
                if loop_pairs is None:
                    loop_pairs = self_pairs
                pre = None
                for _pass in range(len(si)):
                    support_h = z[si]
                    if np.allclose(support_h, solved_h, rtol=0.0, atol=1e-10):
                        break
                    solved_h = support_h.copy()
                    self_support = (si, solved_h)
                    z0 = dilate_floor(self_support)
                    pre = _span_rebuild(f, z0, s, diameter, sag_multiplier,
                                        chart=(chart if curved else None),
                                        xy=xy, supports=self_support)
                    z = g2f._self_contact_apply(loop_pairs, pre,
                                                fuse_sink=fuse_sink)
                if pre is not None and loop_pairs is not self_pairs:
                    # the kept pass, over every pair: the supports have already
                    # converged, so this reproduces the whole-fibre solve the
                    # loop would have ended on
                    z = g2f._self_contact_apply(self_pairs, pre,
                                                fuse_sink=fuse_sink)
        # later fibres rest on this one. The tail stamps LAST, over a snapshot
        # of what it covers, so a fibre continuing from it can read past it
        # (`seam`); the max is order-free, so `H` itself is unchanged by this.
        zs = z                       # what the raster holds of this fibre
        if curved:
            np.maximum.at(H, (ix, iy), z)
            if pG is not None:
                _stamp_segments(pG, pocc, H, xy, z, ix, iy,
                                np.arange(len(z)), g_org, period=period)
        else:
            if self_raised is not None and self_raised.any():
                # A flat fibre's own crossing is a PEAK about one sample wide on
                # a diagonal, and a raster cell cannot hold it: one stamped cell
                # sits where the next layer, following the lower strand's cells,
                # never reads it (22 later strands straight through a loop
                # crossing on a 10 mm / 0.5 square), and a widened stamp is a
                # top-hat a shallow crossing rides for many diameters. So the
                # raster holds the strand WITHOUT its own lift -- the floor's
                # pass-1 dilation, which carries no self support, capped at
                # the solved height -- and the lifted runs go into
                # `self_idx`, read back as an exact cap by every later fibre in
                # a cell next to them (see the floor above). A run is padded by
                # one sample each side so its segments reach the foot of the
                # rise. Tried and not kept, against the exact solver: stamping
                # the lifted samples plus their four edge neighbours (kinked
                # rises; the previous build), a 3x3 stamp (less accurate). The
                # unlifted height scored the same over the 20-scaffold matrix
                # whether it was this, the bare floor, or a whole second
                # rebuild without self supports (+0.28 s at 30 mm), so it is
                # the one that costs nothing.
                zs = z.copy()
                zs[self_raised] = np.minimum(z[self_raised],
                                             cache["base"][self_raised])
                if self_idx is None:
                    self_idx = g2f._SegIndex(cell)
                    self_near = np.zeros((nx, ny), dtype=bool)
                run_m = self_raised.copy()
                run_m[1:] |= self_raised[:-1]
                run_m[:-1] |= self_raised[1:]
                r = np.flatnonzero(run_m)
                j = np.flatnonzero(run_m[:-1] & run_m[1:])   # segments in a run
                self_idx.add_segments(
                    np.column_stack([xy[j], z[j], xy[j + 1], z[j + 1]]),
                    np.full(len(j), 0.5 * diameter))
                for ox in (-1, 0, 1):
                    for oy in (-1, 0, 1):
                        self_near[np.clip(ix[r] + ox, 0, nx - 1),
                                  np.clip(iy[r] + oy, 0, ny - 1)] = True
                self_top = max(self_top, float(z[self_raised].max()))
            tail = s_raw >= s_raw[-1] - seam_arc
            np.maximum.at(H, (ix[~tail], iy[~tail]), zs[~tail])
            _stamp_segments(G, occ, H, xy, zs, ix, iy, np.flatnonzero(~tail),
                            g_org)
            tcell = (ix[tail], iy[tail])
            seam = {"xy": p[-1, :2].copy(), "ix": tcell[0], "iy": tcell[1],
                    "pre": H[tcell].copy(), "gpre": G[tcell].copy(),
                    "opre": occ[tcell[0] * ny + tcell[1]].copy(),
                    "apre": A[tcell].copy() if angular else None}
            np.maximum.at(H, tcell, zs[tail])
            _stamp_segments(G, occ, H, xy, zs, ix, iy, np.flatnonzero(tail),
                            g_org)
        if angular:
            # Stamp this fibre's direction into `A` wherever it is now the TOP of
            # the cell -- read back off H after the max, so `won` cannot disagree
            # with the height a later fibre will actually rest on. Ties (equal
            # heights) hand the cell to this fibre, which is right: it IS on top.
            won = zs >= H[ix, iy] - 1e-12
            A[ix[won], iy[won]] = phi_q[won].astype(np.float32)
        # `anchors` is populated above for ANY curved frame now, not only a
        # periodic one -- the display thinning below must protect those exact
        # crossing samples on a dome/bowl/cone/torus too, or `_simplify` can
        # stride away the very points the self/cross-fibre exactness above
        # just found.
        protected = (np.concatenate([raw_cum, np.asarray(anchors)])
                     if curved else ())
        xy, z, t = _simplify(xy, z, t, diameter, s=s_raw,
                             protected=protected)
        out_paths.append(np.column_stack([xy, z]))
        if prior_cross is not None:
            prior_cross.add_fiber(p)
            prior_solved.add_fiber(np.column_stack([xy, z]), 0.5 * diameter)
        if out_times is not None:
            out_times.append(t)
    # Back to WORLD, the currency this engine was handed. Identity on a flat bed
    # and on a developable collector, so neither can move a byte.
    return chart.to_world(out_paths), out_times
