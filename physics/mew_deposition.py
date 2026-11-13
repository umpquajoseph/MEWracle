"""Physics-shaped deposition geometry for the MEW jet.

The seam where the reduced-order jet model (`mew_physics`) reshapes the DEPOSITED
SCAFFOLD, not just the animated jet: the molten jet trails the moving head and
touches down behind it, so each commanded centreline is replaced with the locus of
touchdown points through time and everything downstream (sag, tube, STL) sees the
fibre as it is actually laid.

Pure module, in the `mew_physics` spirit: no VTK, no file I/O, no GUI. It returns
centrelines in the SAME structure `gcode_to_fibers.parse_gcode(with_times=True)`
produces, so it drops into the `apply_sag` -> tube -> STL pipeline as a
`(paths, ptimes) -> (paths, ptimes)` transform (`gcode_to_fibers`'s
`_FIBER_TRANSFORM` seam, and `mew_export.py`).

Contracts:
  * CENTRELINE ONLY, and XY only -- the commanded Z rides through and the sag solver
    downstream owns the Z drop. The fibre DIAMETER is left to the caller.
  * The touchdown path is Hrynevich 2020's own ray / V_JCP step
    (`integrate_contact_path_ray`), so the steady trailing offset is exactly the
    calibrated lag distance and the corner transient follows his recede-speed rule.
  * The steady lag is a constant along-travel offset: a straight run shifts
    uniformly and the pore PITCH survives, so a lag error is LOCAL to starts,
    turnarounds and speed changes. The rounding radius scales with the lag, so a
    faster print really does round its corners more.
  * BELOW CTS THE FIBRE COILS -- opt-in, default OFF. On, the fibre is reshaped into
    the trailing touchdown plus a length-conserving tangent-angle oscillation
    (`_coil_excursion`) whose excursion VANISHES at CTS, continuous with the straight
    fibre above it; off, a below-CTS fibre is a passthrough of its commanded
    centreline, the straight-fibre model not holding there. Either way it is REPORTED
    as coiling in `info`. Not modelled: the coil's own diameter change and the
    multifrequency / hysteretic sub-bands.
  * Fibre count is preserved one-to-one -- reshaped, coiled and passthrough fibres
    all stay one path -- because a caller's per-scaffold grouping keys off the count.
  * The locked export, the live re-lay (`mew_live`) and the Phase-4 march
    (`mew_incremental`) produce the SAME centreline (`test_mew_incremental`).

Units follow `mew_physics`: length mm, speed mm/s, pressure Pa, temperature degC,
voltage kV, time s.
"""

from __future__ import annotations

import math

import numpy as np

from mew_physics import (ProcessParams, coil_offset_series,
                         integrate_contact_path_ray)
from mew_coil_wave import below_cts_runs, coil_wave_reshape, overlay_wave_runs
# loop_entry_flags is pure geometry (numpy only -- gcode_to_fibers pulls no VTK at
# import), so importing it keeps a SINGLE source of truth for "does this fibre begin
# with a linking loop" shared by the sag solver and this deposition transform.
from gcode_to_fibers import (_span_max_dist, _span_max_turn,
                             loop_entry_flags, merge_travels,
                             trailing_travel)

# Accel-limited motion planning re-times the head onto its real trapezoidal
# trajectory, so the deposited lag + diameter track the head's REAL ramped speed
# (slower into corners / tight turnaround arcs) instead of the commanded feed.
# Optional + off by default (callers opt in with max_accel) so a missing module --
# or a plain call -- is the byte-identical pre-kinematics deposition.
try:
    from mew_kinematics import plan_trajectory, chart_seg_accels
except Exception:  # noqa: BLE001
    plan_trajectory = chart_seg_accels = None


def _seg_accel(p, metric_p, frame, max_accel, rotary_accel):
    """Per-segment accelerations under a separate rotary limit, or None (one
    limit, byte-identical) -- `mew_kinematics.chart_seg_accels`. `p` is the
    fibre in its chart, `metric_p` the same fibre as the planner sees it."""
    if chart_seg_accels is None:
        return None
    return chart_seg_accels(p, metric_p, frame, max_accel, rotary_accel)


__all__ =["laid_fibers", "diameter_by_time", "make_transform",
           "junction_speeds", "surface_ptimes", "landing_speeds"]

# A direction change sharper than this (degrees) at an interior vertex is a
# "corner": the touchdown rounds/whips it over a few tau, so the time grid is
# refined there to resolve the transient. Gentle arc steps (the generator emits
# ~2 deg chords) fall below it and keep their own sampling -- no fill needed.
_CORNER_DEG = 20.0
# A fractional speed change this large between adjacent intervals also opens a
# transient window (e.g. a turnaround printed at a slower loop speed).
_SPEED_CHANGE_FRAC = 0.1
# Per-corner floor for the ADAPTIVE point budget (see `_lay_one`). In continuous MEW
# one layer is ONE fibre, so a fine-stepover serpentine is a single path with
# thousands of corners and a FLAT point cap thins the corner-rounding fill until the
# touchdown arc reverts to a blocky polygon; the budget instead scales as
# n_vertices + n_corners * this. The floor must FOLLOW the Corner-resolution knob --
# with a fixed one, a fibre with hundreds of corners (a 4-axis unit-cell tube is ONE
# fibre of ~290 vertices, every one a corner) was refined and then thinned straight
# back to the flat cap, which is why raising `fill_per_tau` past 12 once stopped
# changing the drawn crest. `_lay_one` budgets each corner at the most its window can
# hold, `transient_taus * fill_per_tau`, with this as the lower floor; 12 sits above
# the grid's natural per-corner density (~6-9) at the default fill_per_tau of 6.
_MIN_FILL_PER_CORNER = 12

# --- the adaptive corner grid ------------------------------------------------ #
# Everything above places the refine grid in TIME, which fails on a tube: a tube is
# corners nearly end to end (287 of 289 commanded vertices on a re-entrant cell tube)
# where a plate is long straight passes, and those corners sit closer together than
# the lag, so EVERY window is clamped to the next corner and the fill collapses into
# one uniform time grid. Measured on the GUI-default re-entrant tube: correlation
# with the interval duration 1.000, with the TURN ANGLE 0.011 -- a 120 deg junction
# sampled exactly like a straight run. The marched VERTICES are already on the
# converged curve; it is the CHORDS BETWEEN THEM that cut the corner, so this is a
# SAMPLING defect and not an integration one, which is what makes refining safe:
# adding times samples the same curve, it does not move it.

# What that costs, at the shipped fill_per_tau of 6 -- drawn chord error, then the
# error at the marched vertices:
#     chevron       31.7 um  (1.6 d)       1.71 um  (0.09 d)
#     re-entrant     7.6 um  (0.4 d)       0.96 um  (0.05 d)
#     zigzag helix   4.5 um  (0.2 d)       0.73 um  (0.04 d)

# The rule: march, ask each interior vertex how far it sits off the chord of its
# neighbours, and re-march with a midpoint in both adjacent intervals wherever that
# is too far. The error measure is `gcode_to_fibers.decimate_keep`'s, IMPORTED rather
# than restated -- centreline deviation PLUS r*theta of tube wall, because a tube
# swept along a bend moves further than its axis does and a crest is a bend.
# Refining is ADD-ONLY: the seed stays the grid the march has always used.

# The vertex sagitta spans TWO intervals and goes as h^2, so each half carries about
# a quarter of it, which argues for a trigger at 4x the tolerance the halves are held
# to. Against the fill_per_tau 96 reference on the 10-ply families, at 4.0/3.0/2.5:
#     chevron       0.16 / 0.16 / 0.12 d      8.6k /  9.0k /  9.3k points
#     re-entrant    0.07 / 0.05 / 0.04 d     43.1k / 50.7k / 53.1k
#     brick-closed  0.09 / 0.09 / 0.09 d     23.5k / 23.5k / 23.7k
#     brick-open    0.13 / 0.13 / 0.13 d     21.8k / 22.1k / 22.4k
#     zigzag        0.14 / 0.12 / 0.11 d      8.6k / 10.1k / 10.5k
# 4.0 is kept: tightening buys the chevron 0.04 d and costs the re-entrant 23% more
# points for it. The chevron and brick-open numbers are FLOORS that do not move with
# the ratio -- recorded, not explained, and inside the 0.25 d the suite holds.
_REFINE_SPAN_RATIO = 4.0
# Passes are geometric (each halves h, so quarters the error): 4 passes is 256x,
# against the 16x the worst family above needs. It is a bound, not a schedule --
# the loop stops as soon as nothing is over tolerance.
_REFINE_MAX_PASSES = 4
# Most samples one pass may put inside a single interval. The split is predicted
# from a smoothness assumption, so a pathological reading must not be able to
# allocate an unbounded grid in one step; the loop's next pass picks up whatever
# this clamp left behind.
_REFINE_MAX_SPLIT = 16
# A pass must cut the worst deviation to at most this fraction of the previous
# pass's, or the loop stops. Genuine under-sampling improves as h^2 (a pass aims
# to land inside tolerance in one go, so the drop is large); a true corner in the
# laid path -- which is what a below-CTS fibre is made of, the lag being zero
# there -- improves only linearly and never arrives. 0.7 is loose enough that a
# legitimate pass which merely undershot still gets its next go.
_REFINE_MIN_PROGRESS = 0.7
#: Deliberate over-split. The prediction is first-order, so splitting to exactly
#: the estimate leaves a residue that costs another whole re-march to clear.
#: MEASURED on the 10-ply families (margin 1.0 / 1.5 / 2.0): the re-entrant tube
#: runs 1.60 / 1.10 / 1.21 s and lands 0.11 / 0.07 / 0.06 fibre diameters, for
#: 41.6k / 43.1k / 50.3k points. 1.5 buys the second re-march back for 4 % more
#: points; 2.0 buys nothing further and costs 17 %.
_REFINE_SPLIT_MARGIN = 1.5
# Pathology bound for the refined grid, in points per fibre DIAMETER of path. The
# per-fibre point cap cannot serve here: it counts corners, and this rule exists
# precisely because a tube's sampling need does not follow its corner count. Four
# points per diameter is finer than anything downstream can express (the sag
# solver's own step is half a diameter), so it bounds a runaway without ever
# being the thing that sets fidelity.
_REFINE_POINTS_PER_D = 4.0

# TRIED AND REJECTED: allocating the corner samples from a CLOSED FORM instead of
# measuring the output -- n = C*sqrt(theta*L/tol) across each corner, placed at equal
# increments of accumulated turn. It is worse on every axis. At C = 1.5, seed only,
# the chevron took 22.1k points to reach 0.14 d where measuring reaches 0.16 d with
# 8.6k, and brick-closed STUCK at 0.35 d however large C grew, because the allocation
# only fires at vertices `_corner_vertices` flagged and brick-closed's worst chord is
# not at one. Used as a SEED it was worse again -- pre-densifying a region shrinks the
# two-interval sagitta the trigger reads, so the loop stops early and the residual
# chord error goes UP (re-entrant 1.13 -> 6.75 um at C = 0.5). Do not rebuild it: the
# measurement is what makes this rule work.

#: CURVE QUALITY -- the one user-facing dial, in fibre diameters of allowed chord
#: error, plus the integration budget that makes each tolerance meaningful. Two
#: numbers per tier, because the tolerance decides where OUTPUT samples go while the
#: tracker is first-order in its sub-step: below about 0.1 d the answer is limited by
#: the integration (`mew_physics.RAY_SUBSTEPS`, 0.08-0.22 d on a 20 um fibre), so a
#: finer chord without a finer integration buys noise. MEASURED on the five 4-axis
#: families, 10 plies / 3 mm mandrel / 20 um fibre, vs fill_per_tau 96, worst family:
#:     (none)     0.22-1.58 d   x1           --        <- what this replaces
#:     draft      0.21-0.45 d   x1.1-1.9     +0..12 %
#:     standard   0.11-0.16 d   x2.0-3.0     +0..37 %
#:     fine       0.05-0.11 d   x2.7-4.2     +24..141 %
#: `standard` is the export default and `draft` the live preview's.
CURVE_QUALITY = {
    "draft":    (0.25, 96.0),
    "standard": (0.10, 192.0),
    "fine":     (0.03, 384.0),
}
DEFAULT_CURVE_QUALITY = "standard"

#: Kept as the name the rest of the file reads, and it is `standard`'s tolerance.
#: Ported, not picked: 0.1 d is `gcode_player._DEPO_DECIMATE_D`, the bound the
#: viewer already holds its drawn tube to, so the relay stops handing the viewer
#: a path coarser than the viewer's own promise.
CHORD_TOL_D = CURVE_QUALITY[DEFAULT_CURVE_QUALITY][0]


def auto_corner_grid(fiber_diameter, quality=None):
    """``(tol_mm, radius_mm, steps_per_tau)`` for one Curve-quality tier.

    THE definition, so the preview, the export, the CLI and any test cannot
    disagree about what a tier means. `laid_fibers(corner_chord=...)` takes
    exactly this triple; None there is the fixed-`fill_per_tau` grid this build
    has always used, which every flat caller still gets.
    """
    d = float(fiber_diameter)
    if not d > 0.0:
        raise ValueError("fiber diameter must be positive to size the corner grid")
    name = (quality or DEFAULT_CURVE_QUALITY).strip().lower()
    try:
        tol_d, per_tau = CURVE_QUALITY[name]
    except KeyError:
        raise ValueError("unknown curve quality %r; expected one of %s"
                         % (quality, ", ".join(sorted(CURVE_QUALITY)))) from None
    return tol_d * d, 0.5 * d, per_tau


def _refine_cap(cap, seg_len, corner_chord):
    """The refined grid's own ceiling: whichever is larger of the fibre's existing
    point cap and `_REFINE_POINTS_PER_D` samples per fibre diameter of path."""
    tol, radius = corner_chord[0], corner_chord[1]
    d = 2.0 * radius if radius > 0.0 else tol / CHORD_TOL_D
    path = float(np.sum(seg_len))
    return int(max(cap, math.ceil(_REFINE_POINTS_PER_D * path / max(d, 1e-9))))


def _vertex_deviation(xyz, radius):
    """Per interior vertex, how far the drawn tube would move if its neighbours'
    chord replaced it. Index k of the result belongs to vertex k+1.

    The measure is `decimate_keep`'s, term for term (`gcode_to_fibers.py:1258`):
    the centreline distance plus `radius * theta` of wall displacement, because a
    tube swept along a bend moves further than its axis does and every place this
    fires is a bend. Imported rather than restated so the sampler and the
    decimator downstream of it cannot drift apart.
    """
    n = len(xyz)
    if n < 3:
        return np.zeros(0, dtype=np.float64)
    seg = xyz[1:] - xyz[:-1]
    slen = np.sqrt(np.einsum("ij,ij->i", seg, seg))
    slen[slen <= 1e-24] = 1.0
    seg_unit = seg / slen[:, None]
    lo = np.arange(0, n - 2, dtype=np.int64)
    hi = np.arange(2, n, dtype=np.int64)
    dev = _span_max_dist(xyz, lo, hi)
    if radius > 0.0:
        dev = dev + radius * _span_max_turn(seg_unit, xyz, lo, hi)
    return dev


def _oversize_vertices(xyz, tol, radius):
    """Interior vertices where the sampling is too coarse -- the deviation above
    over `_REFINE_SPAN_RATIO * tol`."""
    dev = _vertex_deviation(xyz, radius)
    return np.flatnonzero(dev > _REFINE_SPAN_RATIO * tol) + 1


def _refine_corner_grid(rt, xyz, max_coil, end_state, march, corner_chord, cap):
    """Re-march on a grid refined wherever the drawn polyline is over tolerance.

    Returns the last grid marched and its results. Stops on the first pass that
    finds nothing over tolerance, that would exceed `cap`, that adds no new time,
    or that FAILS TO MAKE PROGRESS -- so a fibre already fine enough pays one
    deviation scan and nothing else.

    The progress test is not a nicety. A chord tolerance is a statement about a
    CURVE, and not every large deviation is a curve: near CTS the lag collapses
    towards zero, the touchdown barely rounds anything, and the commanded path's
    sharp corners survive into the laid path as TRUE corners. A corner reads as a
    large deviation at EVERY sampling and converges only linearly, where the
    split predictor assumes h^2 -- so without this the loop spends every pass and
    the point cap on geometry that was already exact. Measured on the app's own
    boot state (10-ply tube at 400 mm/min): 63.9 s and 282 854 points, against
    6.2 s with no grid and 3.7 s with the test in.
    """
    tol, radius = corner_chord[0], corner_chord[1]
    prev_worst = None
    for _ in range(_REFINE_MAX_PASSES):
        dev = _vertex_deviation(xyz, radius)
        want = _REFINE_SPAN_RATIO * tol
        bad = np.flatnonzero(dev > want) + 1
        if len(bad) == 0:
            break
        worst = float(dev.max())
        if prev_worst is not None and worst > _REFINE_MIN_PROGRESS * prev_worst:
            break                      # a cusp, not under-sampling -- see above
        prev_worst = worst
        # PREDICT the split rather than halving and looking again. The sagitta of
        # a smooth curve goes as h^2, so an interval reading `dev` needs about
        # sqrt(dev / want) times its sample count -- one re-march instead of the
        # four a repeated halving spends. Halving first was measured at 1.9-5.0x
        # the relay time; predicting lands it at ~2x, and the loop still runs
        # again if the estimate came up short (it is a smoothness assumption, not
        # a guarantee).
        k = np.ceil(_REFINE_SPLIT_MARGIN
                    * np.sqrt(dev[bad - 1] / want)).astype(np.int64)
        k = np.clip(k, 2, _REFINE_MAX_SPLIT)
        # both intervals at an over-tolerance vertex: the chord that misses is the
        # pair of them, and refining one side leaves the other carrying the error.
        adds = []
        for side_lo, side_hi in ((bad - 1, bad),
                                 (bad, np.minimum(bad + 1, len(rt) - 1))):
            a = rt[side_lo]
            b = rt[side_hi]
            for i in range(1, int(k.max())):
                live = k > i
                if not live.any():
                    break
                f = i / k[live]
                adds.append(a[live] + (b[live] - a[live]) * f)
        if not adds:
            break
        new = np.unique(np.concatenate([rt] + adds))
        if len(new) <= len(rt) or len(new) > cap:
            break
        rt = new
        xyz, max_coil, end_state = march(rt)
    return rt, xyz, max_coil, end_state


def _metric_path(points, frame=None):
    """A local Euclidean development of ``points`` for speed calculations.

    ``CylinderFrame`` stores circumferential position as bare-mandrel arc length
    ``v = R0*phi`` so that helices remain straight for the geometry solver.  The
    jet, however, is drawn by the *local deposition surface*: a chart step at
    wall height ``h`` travels ``(a*du, b*(dv - c*du))`` there, with
    ``mew_frame.metric`` supplying the three factors.  On a cylinder ``a`` is 1
    and ``c`` is 0, so only the circumferential term moves; on a doubly-curved
    collector all three do.  This helper keeps
    that distinction out of the tracker coordinates while giving speed, heading,
    and the kinematic planner their physical surface metric.  The constructed
    positions are only a metric development; their origin is arbitrary.

    ``frame=None`` returns the input unchanged, preserving the flat path exactly.

    THE STEP COMES FROM THE CHART, NOT FROM ``frame.metric`` DIRECTLY, since the
    five-axis work: on a developable collector ``sag_approx.GlobalChart`` is
    literally the two expressions this function would inline, in the same
    order, so a cylinder's development cannot move a bit; on a doubly-curved one
    the columns are the pole's own polar chart and the scales are that chart's.
    Either way ``points`` must already be IN the chart the frame selects -- see
    ``surface_ptimes`` and ``laid_fibers``, which are the two places that convert.

    A FIVE-AXIS COLLECTOR HAS NO CHART HERE (`_world_surface`): ``points`` are
    world and the answer is the fibre's `_Development`, whose steps are the
    world chords' tangential lengths and whose turns are measured in the
    tangent plane. The chart development above steps in the principal frame
    without transporting that frame along the path, so it misreads how much a
    path turns wherever the frame rotates: a great circle off the dome's pole
    laid through it drifts 3-30 um off its own plane, where the development
    stays on it to 1e-14 mm (geodesic).
    """
    p = np.asarray(points, dtype=np.float64)
    if frame is None or len(p) < 2:
        return p
    if _world_surface(frame) is not None:
        return _development_of(p, frame).q
    chart = chart_of(frame)
    q = np.empty_like(p)
    q[0] = p[0]
    for k in range(len(p) - 1):
        d = p[k + 1] - p[k]
        eu, ev = chart.principal_step(0.5 * (p[k, 0] + p[k + 1, 0]),
                                      0.5 * (p[k, 1] + p[k + 1, 1]),
                                      0.5 * (p[k, 2] + p[k + 1, 2]),
                                      d[0], d[1])
        q[k + 1] = q[k] + (float(eu), float(ev), d[2])
    return q


_CHART_CACHE = {}


def chart_of(frame):
    """The chart a frame's paths are measured in -- `sag_approx.chart_for`.

    ONE OWNER FOR THE CHART, and it is the approximate solver's module rather
    than this one, because `sag_approx` and `preview_sag` need the same object
    and neither may import physics (`sag_approx`'s own docstring is the rule).
    Imported lazily so this module's import graph is unchanged and no cycle can
    form through `gcode_to_fibers`.

    Cached per frame OBJECT, weakly by id: a chart holds nothing but a reference
    to its frame, and `_metric_path` asks for one per fibre.
    """
    key = id(frame)
    hit = _CHART_CACHE.get(key)
    if hit is not None and hit[0] is frame:
        return hit[1]
    import sag_approx
    chart = sag_approx.chart_for(frame)
    if len(_CHART_CACHE) > 64:
        _CHART_CACHE.clear()
    _CHART_CACHE[key] = (frame, chart)
    return chart


# --- the world-native relay ------------------------------------------------ #
# A five-axis collector's paths arrive as WORLD points and are laid without a
# chart. The contact tracker (`integrate_contact_path_ray`) is a 2D law, and the
# nozzle is held normal to the surface, so the flat law holds in the local
# tangent plane at every point. Each fibre is therefore DEVELOPED: rolled out
# flat with its true segment lengths and the turn it makes at each vertex
# measured in the tangent plane there. The tracker runs on that development
# unchanged, and each laid offset from the nozzle is carried back to the
# collector as a step along the surface from the nozzle, settled at the
# nozzle's own height. The collector is asked only through `frame.surface()`
# (`mew_surface`), never what shape it is, so a library dome and an imported
# one run the same code.
#
# What the development keeps is what the tracker reads: arc length (so speed,
# tau and the lag), turn angles (so corners), and a heading per segment that
# maps an offset back to a world direction. It does not keep absolute position:
# a development's origin is arbitrary and two fibres' developments do not share
# one, which is why a contact state crossing from one segment into the next is
# re-expressed (`_transfer_state`).

_SURFACE_CACHE = {}
_DEV_CACHE = {}


def _world_surface(frame):
    """The collector's `mew_surface` query object when `frame`'s paths are laid
    world-native, else None.

    World-native exactly where the caller holds world and not chart columns
    (`sag_approx.holds_chart`): the flat bed and the 4-axis mandrel keep the
    chart relay and their pinned digests; every five-axis collector, library or
    imported, is laid here. Cached per frame object, as `chart_of` is.
    """
    if frame is None or bool(getattr(frame, "chart_currency", False)):
        return None
    key = id(frame)
    hit = _SURFACE_CACHE.get(key)
    if hit is not None and hit[0] is frame:
        return hit[1]
    surface = frame.surface()
    if len(_SURFACE_CACHE) > 64:
        _SURFACE_CACHE.clear()
    _SURFACE_CACHE[key] = (frame, surface)
    return surface


def _development_of(p, frame):
    """`_Development` of world polyline `p` on `frame`'s collector, cached per
    array object (the cache holds the array, so its id cannot be reused while
    the entry lives). `laid_fibers` asks for the same fibre's development from
    `junction_speeds`, the lay itself and the corner grid."""
    surface = _world_surface(frame)
    key = (id(p), id(surface))
    hit = _DEV_CACHE.get(key)
    if hit is not None and hit[0] is p and len(p) == len(hit[1].q):
        return hit[1]
    dev = _Development(surface, p)
    if len(_DEV_CACHE) > 4096:
        _DEV_CACHE.clear()
    _DEV_CACHE[key] = (p, dev)
    return dev


def _unit_rows(v):
    n = np.linalg.norm(v, axis=1)
    ok = n > 0.0
    out = np.zeros_like(v)
    out[ok] = v[ok] / n[ok, None]
    return out, n, ok


class _Development(object):
    """One world polyline on a collector, rolled out flat.

    `q` (n, 3): the development -- columns x, y and the height along the
    outward normal `h`. Segment k has the tangential length of the world chord
    (the normal part removed at the segment's own foot) and heading `theta[k]`;
    `theta` turns at vertex k by the signed angle from `T[k-1]` to `T[k]` about
    the normal there. `T[k]` is the segment's world unit tangent.

    The first vertex and heading are the world XY position and XY heading, so on
    the flat bed the development IS the world path and the plane control
    reproduces the flat relay.
    """

    def __init__(self, surface, p):
        p = np.asarray(p, dtype=np.float64)
        self.surface = surface
        self.p = p
        n = len(p)
        h, _Q, N, _f = surface.height(p)
        self.h = h
        self.N = N
        if n < 2:
            self.T = np.zeros((0, 3))
            self.theta = np.zeros(0)
            self.q = np.column_stack([p[:, :2], h])
            self.mid = None
            return
        mids = 0.5 * (p[:-1] + p[1:])
        Qm, Nm, fm = surface.settle(mids)
        self.mid = (Qm, Nm, fm)
        d = np.diff(p, axis=0)
        tan = d - np.sum(d * Nm, axis=1)[:, None] * Nm
        T, L, ok = _unit_rows(tan)
        # A segment with no tangential length (a pure hop along the normal, or a
        # repeated point) has no heading of its own: it inherits its neighbour's.
        # With no heading anywhere, any tangent will do -- nothing is laid off it.
        if not ok.all():
            good = np.nonzero(ok)[0]
            if len(good):
                src = good[np.clip(np.searchsorted(good, np.arange(n - 1),
                                                   side="right") - 1,
                                   0, len(good) - 1)]
                src = np.where(np.arange(n - 1) < good[0], good[0], src)
                T = T[src]
            else:
                ref = np.where(np.abs(Nm[:, :1]) < 0.9, [[1.0, 0.0, 0.0]],
                               [[0.0, 1.0, 0.0]])
                T = _unit_rows(np.cross(Nm, ref))[0]
            L = np.where(ok, L, 0.0)
        self.T = T
        turn = np.zeros(n - 1)
        if n > 2:
            Nv = N[1:-1]
            a = T[:-1] - np.sum(T[:-1] * Nv, axis=1)[:, None] * Nv
            b = T[1:] - np.sum(T[1:] * Nv, axis=1)[:, None] * Nv
            turn[1:] = np.arctan2(np.sum(np.cross(a, b) * Nv, axis=1),
                                  np.sum(a * b, axis=1))
        self.theta = math.atan2(T[0, 1], T[0, 0]) + np.cumsum(turn)
        steps = L[:, None] * np.column_stack([np.cos(self.theta),
                                              np.sin(self.theta)])
        xy = p[0, :2] + np.concatenate([[[0.0, 0.0]], np.cumsum(steps, axis=0)])
        self.q = np.column_stack([xy, h])

    def principal(self, k, per_layer):
        """`(k1, k2, du, dv, d1, d2)` at segment k's foot: the principal
        curvatures (at the offset surface of the segment's height when
        `per_layer`), the segment's heading resolved on the principal
        directions, and those directions. Batched over every segment once."""
        if not hasattr(self, "_principal"):
            Qm, Nm, fm = self.mid
            k1, k2, d1, d2 = self.surface.curvature(Qm, Nm, fm)
            # `CurvedPlacement` hands a patch with a zero FIRST curvature to the
            # cylinder's Eq 4 (its class docstring). The surface orders k1 >= k2,
            # so a cylinder arrives as (1/R, 0): put the zero first, where the
            # placement looks for it.
            swap = (k2 == 0.0) & (k1 != 0.0)
            k1, k2 = np.where(swap, k2, k1), np.where(swap, k1, k2)
            d1, d2 = (np.where(swap[:, None], d2, d1),
                      np.where(swap[:, None], d1, d2))
            self._principal = (k1, k2, d1, d2)
        k1, k2, d1, d2 = self._principal
        a, b = float(k1[k]), float(k2[k])
        if per_layer:
            hm = 0.5 * (self.h[k] + self.h[k + 1])
            a, b = a / (1.0 + a * hm), b / (1.0 + b * hm)
        return (a, b, float(self.T[k] @ d1[k]), float(self.T[k] @ d2[k]),
                d1[k], d2[k])

    def offset_to_dev(self, k, w):
        """World tangent vector `w` at segment k -> development vector."""
        Nm = self.mid[1][k]
        T = self.T[k]
        B = np.cross(Nm, T)
        a, b = float(w @ T), float(w @ B)
        c, s = math.cos(self.theta[k]), math.sin(self.theta[k])
        return (a * c - b * s, a * s + b * c)

    def to_world(self, t, rt, dev_xy, nozzle_xy):
        """World points for development points `dev_xy` laid at times `rt`
        while the nozzle was at development points `nozzle_xy`.

        Each offset from the nozzle is resolved on its segment's heading into
        along- and cross-track parts, rebuilt on the world tangent plane at the
        nozzle, stepped along the collector and settled at the nozzle's height.
        The step is `tan(k*L)/k` for an offset of length L on normal curvature k
        of the offset surface, which lands a point L along the surface exactly
        on a sphere or along a cylinder's principal directions; a straight
        tangent step followed by settling would fall short by L^3 k^2 / 3.
        """
        t = np.asarray(t, dtype=np.float64)
        rt = np.asarray(rt, dtype=np.float64)
        n = len(self.p)
        s = np.clip(np.searchsorted(t, rt, side="right") - 1, 0, n - 2)
        Pn = np.column_stack([np.interp(rt, t, self.p[:, i]) for i in range(3)])
        hn = np.interp(rt, t, self.h)
        o = np.asarray(dev_xy, dtype=np.float64) - np.asarray(nozzle_xy,
                                                              dtype=np.float64)
        c, sn = np.cos(self.theta[s]), np.sin(self.theta[s])
        a = o[:, 0] * c + o[:, 1] * sn
        b = -o[:, 0] * sn + o[:, 1] * c
        _h, Qn, Nn, fn = self.surface.height(Pn)
        T = self.T[s]
        T = _unit_rows(T - np.sum(T * Nn, axis=1)[:, None] * Nn)[0]
        B = np.cross(Nn, T)
        W = a[:, None] * T + b[:, None] * B
        L = np.linalg.norm(W, axis=1)
        moved = L > 0.0
        if not moved.any():
            return Pn
        kn = np.zeros(len(rt))
        kn[moved] = self.surface.normal_curvature(Qn[moved], Nn[moved],
                                                  fn[moved], W[moved])
        den = 1.0 + kn * hn
        ke = np.abs(np.where(den > 1e-9, kn / np.where(den > 1e-9, den, 1.0),
                             kn))
        x = np.minimum(ke * L, 1.2)
        scale = np.ones(len(rt))
        bent = x > 1e-12
        scale[bent] = np.tan(x[bent]) / x[bent]
        X = Pn + scale[:, None] * W
        Q, N, _f = self.surface.settle(X[moved])
        out = Pn.copy()
        out[moved] = Q + hn[moved, None] * N
        return out


def _transfer_state(prev, cur, state):
    """A contact state `(c, w[, K])` from the end of development `prev` into
    development `cur`, which starts at the vertex `prev` ends on.

    The two developments are each rigid images of the tangent plane at that
    vertex, rotated and shifted differently. The rotation is the difference of
    the two headings less the world turn between `prev`'s last segment and
    `cur`'s first, so a direction keeps its angle to the surface across the seam.
    """
    if state is None or prev is None or cur is None or len(prev.T) == 0 \
            or len(cur.T) == 0:
        return state
    Nv = cur.N[0]
    a = prev.T[-1] - float(prev.T[-1] @ Nv) * Nv
    b = cur.T[0] - float(cur.T[0] @ Nv) * Nv
    turn = math.atan2(float(np.cross(a, b) @ Nv), float(a @ b))
    phi = float(cur.theta[0] - prev.theta[-1] - turn)
    c, s = math.cos(phi), math.sin(phi)
    p0 = prev.q[-1, :2]
    q0 = cur.q[0, :2]
    cx, cy = float(state[0][0]) - p0[0], float(state[0][1]) - p0[1]
    wx, wy = float(state[1][0]), float(state[1][1])
    out = ([c * cx - s * cy + q0[0], s * cx + c * cy + q0[1]],
           [c * wx - s * wy, s * wx + c * wy])
    return out + tuple(state[2:])


def surface_ptimes(paths, frame, speed_mm_min, t0=0.0):
    """Per-point times for chart paths that have no g-code clock yet.

    THE FIVE-AXIS PREVIEW'S CLOCK.  A `mew_project.ProjectedPath` carries `uv`,
    `world` and `normal` and no time at all -- the projector lays geometry, not
    a program -- while `laid_fibers` wants `paths` and `ptimes` on one modal
    clock and sizes the lag at the per-interval speed.  This is the bridge, and
    it is the ONE place that says what the preview's clock means.

    `paths` are WORLD XYZ in the collector's own frame -- what
    `mew_project.ProjectedPath.world` carries -- the same currency
    `laid_fibers` consumes; `frame` is the deposition frame; `speed_mm_min` is
    the commanded print speed.  Returns one non-decreasing time array per path,
    on a SINGLE clock continued across paths from `t0`.

    IT USED TO BE `(u, v, h)` AND THAT IS THE FIVE-AXIS DEFECT. On a dome's pole
    `v = rho*phi` collapses, so a chart path there is not a position at all: the
    relay fed one to the tracker and drew a path winding 5.47 turns about the
    collector axis where the projector turns 1.02. `chart_of(frame)` converts once, here,
    and it is the identity only where the caller is already holding chart
    columns (`sag_approx.holds_chart`) -- the 4-axis cylinder keeps handing its
    own unrolled chart in and nothing moves. It is NOT the identity on every
    DEVELOPABLE frame: a CONE's world path read here as `(u, v, h)` would be
    laid 0.28-0.38 mm inside its own collector.

    ON A FIVE-AXIS COLLECTOR the paths are not converted at all (`_world_surface`)
    and the length is the world development's (`_Development`): the tangential
    length of each world chord. It sits ~1e-4 below the chart arc on a 0.5 mm
    step of the R10 dome, and it is the length `laid_fibers` reads, which is
    what the next paragraph requires.

    THE BASIS IS HELD SURFACE SPEED, and the length is `_metric_path`'s, never a
    second definition: `laid_fibers` recovers `v_seg` from
    `|diff(_metric_path(p, frame))| / dt`, so dividing that same development by
    the speed is what makes the speed the physics sees equal the speed the user
    typed, exactly, rather than approximately.  A world-chord length would
    disagree with it wherever the sampling is coarse.

    WHY NOT UNIFORM TIME.  Terranova's files are sampled uniformly in time
    and it would be the easier clock to write, but
    the projector's sample spacing is a PROJECTION artefact -- `mew_project`
    marches ribs at the flat pattern's `sample_step` -- so the drawn lag would
    halve when a user halved Sample step with nothing physical changed.

    WHAT THIS CLOCK IS NOT.  It is the `--surface-feed-comp` ON convention:
    `scaffold_gcode._file_feeds` holds the surface at `speed` only in that
    mode, while OFF emits a constant F over the 5-vector and the surface speed
    then varies by `arc/norm`.  Computing `norm` needs the rig's two rotary
    joints, so the OFF basis is not computable without a machine profile.  A
    caller showing this to a user must SAY which basis it drew, rather than let
    two different surface speeds look like one.

    THE LAYER HOP CARRIES NO TIME.  The clock continues from the previous
    path's last value.  Nothing here runs `simulate_travel`, so every fibre
    re-ignites at rest and `junction_speeds` returns 0/0; the only consumer of
    the absolute cross-path offset is the diameter-vs-time lookup.  A
    fabricated hop duration would be a number with no source.
    """
    v = float(speed_mm_min) / 60.0
    if not v > 0.0:
        raise ValueError("speed_mm_min must be positive, got %r"
                         % (speed_mm_min,))
    if _world_surface(frame) is None:
        paths = chart_of(frame).from_world(paths)
    out = []
    t = float(t0)
    for p in paths:
        p = np.asarray(p, dtype=np.float64)
        if len(p) < 2:
            out.append(np.full(len(p), t, dtype=np.float64))
            continue
        seg = np.linalg.norm(np.diff(_metric_path(p, frame), axis=0), axis=1)
        times = t + np.concatenate(([0.0], np.cumsum(seg / v)))
        out.append(times)
        t = float(times[-1])
    return out


def junction_speeds(paths, ptimes, loop_entry, frame=None):
    """Per-fibre (entry_v, exit_v) in mm/s for the accel-limited plan.

    Both 0 (the fibre starts/ends AT REST) everywhere EXCEPT across a layer-change
    LINKING LOOP. A linking loop's lead-out keeps the previous fibre's exit heading,
    so the head flows DEAD STRAIGHT through that junction -- the loop exists precisely
    to keep it moving. So the loop fibre carries speed IN at its start, and the fibre
    before it carries speed OUT at its end; each is the MIN cruise of the two adjoining
    segments (a junction can't be crossed faster than either side runs). Without this,
    the per-fibre planner stops the head at the junction and the v->0 diameter law
    beads a fat lump exactly at the loop entry. Off-junction stays 0, so a non-loop
    print plans byte-for-byte as before. `loop_entry` is `loop_entry_flags(paths)`."""
    n = len(paths)
    ev = [0.0] * n
    xv = [0.0] * n
    if not loop_entry:
        return ev, xv

    def _edge_speed(i, last):
        p = _metric_path(paths[i], frame)
        t = np.asarray(ptimes[i], dtype=np.float64)
        rng = range(len(p) - 2, -1, -1) if last else range(len(p) - 1)
        for k in rng:
            L = float(np.linalg.norm(p[k + 1] - p[k]))
            dt = float(t[k + 1] - t[k])
            if L > 1e-12 and dt > 0.0:
                return L / dt
        return 0.0

    for i in range(1, n):
        if loop_entry[i]:                    # fibre i begins with a loop
            jv = min(_edge_speed(i - 1, last=True), _edge_speed(i, last=False))
            xv[i - 1] = jv                   # previous fibre flows OUT into the loop
            ev[i] = jv                       # the loop fibre flows IN from it
    return ev, xv


def _effective_seg_speeds(p, seg_len, v_naive, max_accel, square_corner_velocity,
                          entry_v=0.0, exit_v=0.0, seg_accel=None):
    """Accel-limited mean speed per commanded interval (mm/s) -- the head's REAL
    speed once the trapezoidal plan (mew_kinematics: Klipper accel + SCV + the arc
    curvature limit) ramps fibre ends and slows through corners / tight turnaround
    arcs -- versus the naive commanded feed `v_naive` (= seg_len/dt).

    Returns `v_naive` UNCHANGED when kinematics is off (`max_accel` is None or <= 0)
    or unavailable, so it is a drop-in for v_seg and the deposition is byte-identical
    to the pre-kinematics build. Cruise interiors are unchanged either way (the plan
    cruises them entry==exit -> duration = length/feed -> v_eff == feed), so only
    fibre ends / corners / arcs differ: the diameter beads and the lag tracks the
    slowdown EXACTLY where the head really slows, never on the steady straight runs
    the build is calibrated on.

    `entry_v` / `exit_v` (mm/s, default 0 = at rest) carry head speed THROUGH a
    boundary the fibre flows across -- a linking loop's straight junction -- so the
    plan does not insert a stop (and its bead) there. See `plan_trajectory`.
    `seg_accel` is its per-segment ramp under a rotary limit (`_seg_accel`)."""
    if plan_trajectory is None or max_accel is None or max_accel <= 0.0:
        return v_naive
    traj = plan_trajectory(p, v_naive * 60.0, max_accel=max_accel,
                           square_corner_velocity=square_corner_velocity,
                           entry_v=entry_v, exit_v=exit_v, seg_accel=seg_accel)
    kt = np.asarray(traj.times, dtype=np.float64)
    if kt.size != len(p):                    # planner bailed (degenerate) -> naive
        return v_naive
    dur = np.diff(kt)
    return np.divide(seg_len, dur, out=v_naive.astype(np.float64, copy=True),
                     where=dur > 0.0)


def _buffer_speed(arcs, speeds, buffer_len):
    """Low-pass the accel-limited speed profile along ARC LENGTH with length constant
    `buffer_len` (the jet's lag distance) -- the viscoelastic jet BUFFERS the deposited
    diameter, so it does not change instantly with the head speed but relaxes over ~the
    lag distance. Hrynevich 2020 measured exactly this -- "the viscoelastic jet acts as
    a buffer that does not allow instant diameter change," the transition "distributed
    over a fiber segment up to 10 mm long." A sharp sub-buffer dip (a corner's tens of
    um) is therefore heavily damped -- the jet cannot follow it -- so a sharp corner
    barely beads while a long feed grade passes through. `buffer_len` is the calibrated
    jet-lag DISTANCE (model.lag_distance), NOT a new fitted constant: the smoothing
    scale rides on the lag law's external (Hrynevich) anchor, so it needs no print
    sweep of its own.

    Returns (arcs2, speeds2). The grid is DENSIFIED first (no gap exceeds buffer_len/3)
    because the resample is deliberately sparse on cruise -- the relaxation needs points
    to land on, else one long cruise step would under-relax (the diameter would read
    biased) and a feature would smear across the whole gap. The relaxed interior is
    collapsed back out downstream (diameter_by_time / diameter_schedule drop knots equal
    to both neighbours), so the extra knots survive only in the ~few*buffer_len tail
    after each speed change. The pole is the exact zero-order-hold response
    a = 1 - exp(-ds/buffer_len), so a long step relaxes fully (a->1) and a short one
    lightly (a->ds/buffer_len). buffer_len <= 0 -> identity (the pointwise bead)."""
    arcs = np.asarray(arcs, dtype=np.float64)
    speeds = np.asarray(speeds, dtype=np.float64)
    n = len(speeds)
    if n < 2 or buffer_len <= 0.0:
        return arcs, speeds
    step = buffer_len / 3.0
    grid = [float(arcs[0])]
    for j in range(1, n):
        gap = float(arcs[j] - arcs[j - 1])
        if gap > step:
            k = min(int(gap / step), 64)           # cap insertions per gap (safety)
            grid.extend(arcs[j - 1] + gap * i / (k + 1) for i in range(1, k + 1))
        grid.append(float(arcs[j]))
    grid = np.asarray(grid, dtype=np.float64)
    v = np.interp(grid, arcs, speeds)              # raw speed on the dense grid
    out = v.copy()
    for j in range(1, len(grid)):
        ds = grid[j] - grid[j - 1]
        if ds <= 0.0:
            out[j] = out[j - 1]
            continue
        a = 1.0 - math.exp(-ds / buffer_len)
        out[j] = out[j - 1] + a * (v[j] - out[j - 1])
    return grid, out


def _diam_speed_samples(p, t, seg_len, v_seg, max_accel, square_corner_velocity,
                        entry_v=0.0, exit_v=0.0, buffer_len=0.0, seg_accel=None):
    """Yield (naive_time, speed) diameter samples for one fibre.

    Kinematics OFF (`max_accel` None/<=0) or unavailable -> one sample per MOVING
    commanded segment at its midpoint time, at the commanded feed `v_seg`:
    byte-identical to the plain one-knot-per-segment diameter. ON -> sample the
    trapezoidal velocity profile (`Trajectory.resample`: dense through the accel /
    decel RAMPS, a single point per cruise) and map each sample's ARC LENGTH back
    onto the naive print clock, so a long ramping move beads at its slow ENDS instead
    of carrying one mean diameter over its whole length. The cruise sampling stays
    coarse, so a constant-feed run still collapses to its boundaries downstream.
    Non-advancing samples (v<=0: hard stops, null moves) are skipped.

    `buffer_len` (mm, default 0 = off) low-passes the kinematic speed profile along
    arc length before it becomes a diameter, modelling the jet's buffering of the
    diameter transition (see `_buffer_speed`); pass the jet lag distance. It is
    applied ONLY on the kinematics-on path, so kinematics-off stays byte-identical.

    The accel-limited plan parameterises the SAME polyline as the naive clock, so the
    two share an arc-length axis -- that is the bridge that puts the kinematic speed
    profile back onto the naive print-time the diameter lookup is keyed on."""
    def _per_segment():
        for k in range(len(seg_len)):
            if v_seg[k] > 0.0:
                yield 0.5 * (float(t[k]) + float(t[k + 1])), float(v_seg[k])

    if plan_trajectory is None or max_accel is None or max_accel <= 0.0:
        yield from _per_segment()
        return
    traj = plan_trajectory(p, v_seg * 60.0, max_accel=max_accel,
                           square_corner_velocity=square_corner_velocity,
                           entry_v=entry_v, exit_v=exit_v, seg_accel=seg_accel)
    if np.asarray(traj.times).size != len(p):      # planner bailed -> per-segment
        yield from _per_segment()
        return
    _kt, _pts, speeds, arcs, _seg = traj.resample()
    arcs = np.asarray(arcs, dtype=np.float64)
    speeds = np.asarray(speeds, dtype=np.float64)
    if buffer_len > 0.0:                            # jet buffers the diameter change
        arcs, speeds = _buffer_speed(arcs, speeds, buffer_len)
    cum_s = np.concatenate(([0.0], np.cumsum(seg_len)))
    naive_t = np.interp(arcs, cum_s, t)
    for j in range(len(naive_t)):
        if speeds[j] > 0.0:
            yield float(naive_t[j]), float(speeds[j])


#: How far apart two incoming contact points may sit (mm) and still count as the
#: same segment for `_RepeatLays`. Measured on a 30x30 / 36 deg / 30-layer plate:
#: the contact a repeated layer inherits differs from its earlier twin by at most
#: 2.3e-5 mm, float residue of the whole chain before it, which an exact match
#: misses. 1e-4 mm is 0.005 of a 20 um fibre. What reuse moves the relaid path
#: by, measured on that plate against laying every segment: 0.0004 d RMS and
#: 0.021 d (0.46 um) at worst, the worst being where the corner grid's refine
#: test lands one side of its threshold for the twin and the other for the copy.
_REPEAT_CONTACT_TOL = 1e-4


class _RepeatLays:
    """Reuse a laid segment for a later segment that is the same move.

    A plate scaffold repeats: every layer at the same angle is the same commanded
    XY on a later clock and a higher z, so at 0/90 the relay of layers 3..N is a
    copy of layers 1-2, and at 36 deg of layers 1-5 (measured: twins agree to
    1e-7 mm or better, 1e-4 mm at the worst layer). This
    lays each distinct segment once. Opt-in (`laid_fibers(reuse_repeats=True)`),
    which only the live preview asks for, so every export stays as it was.

    A segment is the same when its XY is bitwise equal, its z and times are equal
    up to a constant offset, its effective speeds match (the accel-limited plan
    can differ by neighbour), its flags match, and the contact it inherits lies
    within `_REPEAT_CONTACT_TOL`. Only a flat, above-CTS lay is offered: a curved
    collector's placement and the coil grid both read absolute position or time.
    The reused result is the earlier one moved by the z and time offsets."""

    def __init__(self):
        self._seen = {}

    @staticmethod
    def _close_state(a, b):
        if a is None or b is None:
            return a is None and b is None
        ca, cb = np.asarray(a[0], float), np.asarray(b[0], float)
        ka = float(a[2]) if len(a) > 2 else 0.0
        kb = float(b[2]) if len(b) > 2 else 0.0
        return (ca.shape == cb.shape
                and float(np.abs(ca - cb).max()) <= _REPEAT_CONTACT_TOL
                and abs(ka - kb) <= 1e-6 * max(1.0, abs(ka)))

    def lay(self, p, t, v_eff, flags, init, run):
        """`run()` lays the segment; called only when no earlier twin matches."""
        key = (len(p), p[:, :2].tobytes(), flags)
        z_rel, t_rel = p[:, 2] - p[0, 2], t - t[0]
        for z0, t0, zr, tr, ve, st, res in self._seen.get(key, ()):
            if (np.allclose(zr, z_rel, rtol=0.0, atol=1e-9)
                    and np.allclose(tr, t_rel, rtol=0.0, atol=1e-9)
                    and np.allclose(ve, v_eff, rtol=1e-9, atol=1e-12)
                    and self._close_state(st, init)):
                xyz = res[0].copy()
                xyz[:, 2] += p[0, 2] - z0
                shift = t[0] - t0
                return ((xyz, res[1] + shift) + tuple(res[2:-1])
                        + (res[-1] + shift,))
        res = run()
        self._seen.setdefault(key, []).append(
            (float(p[0, 2]), float(t[0]), z_rel, t_rel, np.array(v_eff, float),
             init, res))
        return res


def laid_fibers(paths, ptimes, model, *, pressure, voltage, temperature,
                speed_scale=1.0, zeta=1.0, coil=False,
                coil_wave_amp=0.0, coil_wave_len=0.3, max_accel=None,
                square_corner_velocity=6.0, rotary_accel=0.0,
                settle_taus=6.0, fill_per_tau=6.0,
                transient_taus=8.0, max_points_per_fiber=20000, loop_entry=None,
                entry_vs=None, exit_vs=None,
                travels=None, simulate_travel=False, placement=None, frame=None,
                corner_chord=None, reuse_repeats=False):
    """Reshape commanded fibre centrelines into the touchdown paths the jet lays.

    Contracts: the module docstring. Defense: PHYSICS_DEPENDENCY_GRAPH.md, "Laid
    touchdown centreline". Expected, not defects: high-speed whip (~3.4 mm lag at 1200
    mm/min against ~1 mm at 600, bowing the touchdown inside each turnaround) and
    pass-direction alternation on triangle footprints.

    `paths`/`ptimes` are `parse_gcode(with_times=True)` output on one modal clock;
    `model` supplies CTS and the lag law, its `working_distance` being the gap H;
    `pressure`/`voltage`/`temperature` are the fixed knobs, speed read per interval.
    `speed_scale` shapes the geometry as if printed at v*scale, leaving the g-code
    timeline alone. `zeta` is RETIRED: accepted, ignored, reaching no geometry, kept
    because the GUI, `lite_params` and `mew_export --zeta` pass it. `coil` is the
    below-CTS coiling model, `coil_wave_amp`/`_len` (mm) the coiling-WAVE indicator
    that takes precedence below CTS. `max_accel`/`square_corner_velocity` re-time onto
    the real ramp (None/<=0 byte-identical) but OUTPUT times stay on the naive clock;
    `rotary_accel` (deg/s^2, 0 = none) adds the mandrel's rotary limit (`_seg_accel`).
    The grid knobs are `_MIN_FILL_PER_CORNER`'s and `_REFINE_SPAN_RATIO`'s;
    `travels`/`simulate_travel` and `placement`/`frame` are seams `_lay_one` owns.
    `reuse_repeats` lays each repeated segment once (`_RepeatLays`; the live
    preview's switch, off everywhere else).

    Returns (out_paths, out_ptimes, info): one (Mi,3) path per input, in order, with
    parallel non-decreasing times. `info`: n_fibers, n_coiling, coiling_fibers,
    max_lag_distance (the ALONG-track trail under a placement, not the flat lag),
    `sag_times` (the SEED grid the sag solve runs on, the drawn fibre keeping every
    sample -- `carry_sag_onto_drawn`), and travel_* with `simulate_travel`.
    """
    # THE ONE CONVERSION, and the whole of this function's share of the
    # chart-free path. `paths`/`travels` arrive as WORLD XYZ in the collector's
    # own frame and every line below reads the CHART the frame selects; the
    # output is converted back at the return. On a developable collector both
    # directions are the identity (`sag_approx.GlobalChart`), so a flat print and
    # a 4-axis tube hand their own coordinates straight through and the pinned
    # digests are unmoved. `chart_of` is deterministic in the frame alone, which
    # is what lets `surface_ptimes` time a path in the same chart this lays it in.
    #
    # A FIVE-AXIS collector takes no chart at all (`_world_surface`): `paths`
    # stay world, every metric below is the fibre's development
    # (`_Development`), and `_lay_one` carries the laid offsets back onto the
    # collector itself. `chart` is None there and nothing converts.
    world = _world_surface(frame) is not None
    if world and placement is not None \
            and not hasattr(placement, "at_curvatures"):
        raise ValueError("a world-native lay needs a per-point placement "
                         "(mew_mandrel.CurvedPlacement), got %r" % (placement,))
    chart = None if world else chart_of(frame)
    if not world:
        paths = chart.from_world(paths)
    if travels is not None and not world:
        # `travels` is take_travel()'s (points, times) pairs (or None), NOT a
        # list of plain point arrays like `paths` above -- handing the tuples
        # straight to `chart.from_world` reads each pair as two ragged
        # elements. Transform the points only and carry times through
        # untouched, matching the five-axis unwrap's own handling of this
        # exact shape (gcode_to_fibers.py, around the frame-unwrap block).
        travels = [None if tv is None else (chart.from_world([tv[0]])[0], tv[1])
                   for tv in travels]
    # Flow rate Q and viscosity eta are fixed across the print (pressure/temperature
    # are the fixed knobs), so CTS is a single value. Pass both so a CALIBRATED CTS
    # (nonzero flow/viscosity exponents) gates the regime correctly; with the
    # default exponents they are ignored and CTS is voltage&gap only as before.
    cts = model.critical_speed(
        voltage, model.working_distance,
        flow_rate=model.flow_rate(pressure, temperature),
        viscosity=model.viscosity(temperature))
    info = {"n_fibers": len(paths), "n_coiling": 0, "coiling_fibers": [],
            "max_lag_distance": 0.0, "max_coil_amplitude": 0.0}
    # A non-positive CTS means a misconfigured calibration (cts_ref is a measured,
    # positive landing speed): the lag law has no anchor, so reshaping would emit
    # runaway geometry. Leave every fibre as commanded rather than guess.
    if not (cts > 0.0):
        info["cts_invalid"] = True
        out_t = [np.asarray(t, dtype=np.float64).copy() for t in ptimes]
        info["sag_times"] = out_t
        copies = [np.asarray(p, dtype=np.float64).copy() for p in paths]
        return (copies if world else chart.to_world(copies)), out_t, info

    # steady lag DISTANCE (mm) at a given EFFECTIVE speed, cached by rounded speed --
    # a single-feed MEW print hits the cache after the first interval. _lay_one forms
    # tau = lag / v_naive so the touchdown trails by this distance on the naive clock
    # the integrator runs (with kinematics off v_eff == v_naive and tau == the model's
    # lag_time, byte-for-byte the old tau_and_lag).
    lag_cache = {}

    def lag_of(v_eff):
        key = round(v_eff, 6)
        hit = lag_cache.get(key)
        if hit is None:
            hit = model.solve(ProcessParams(
                pressure=pressure, voltage=voltage,
                speed=max(v_eff, 1e-9), temperature=temperature)).lag_distance
            lag_cache[key] = hit
        return hit

    # Per-fibre boundary speeds: 0 (at rest) except where a linking loop lets the
    # head flow straight through the layer-change junction (see `junction_speeds`).
    # A caller relaying ONE layer in isolation (the live march's export-relay branch)
    # passes the boundary speeds it computed over the WHOLE commanded set, so the
    # single-fibre reshape still matches the whole-set export at a loop junction; the
    # default (None) computes them here, byte-identical to before.
    if entry_vs is None or exit_vs is None:
        entry_vs, exit_vs = junction_speeds(paths, ptimes, loop_entry,
                                            frame=frame)

    # The head's whole motion in program order. Without the continuous-jet mode this
    # is exactly the input fibres, walked with `state` pinned to None -- i.e. every
    # fibre ignites at rest, byte-for-byte the plain per-path behaviour.
    segs = _program_segments(paths, ptimes, travels, simulate_travel)
    chain_v_eff = (_chain_effective_speeds(segs, max_accel,
                                           square_corner_velocity,
                                           entry_vs, exit_vs, frame=frame,
                                           rotary_accel=rotary_accel)
                   if simulate_travel else None)

    out_paths = [None] * len(paths)          # the fibres: index-aligned to `paths`
    out_ptimes = [None] * len(paths)
    # the laid travels: reported alongside, plus the trailing one (the exit)
    # at index len(paths) when the program ends in a travel, as `travels` has it
    n_tv = max([len(paths)] + [s[1] + 1 for s in segs if s[0]])
    tv_paths = [None] * n_tv
    tv_ptimes = [None] * n_tv
    out_sag = [None] * len(paths)            # per fibre: the grid to SAG on
    tv_sag = [None] * n_tv
    state = None                             # contact (c, w) carried across segments
    prev_dev = None                          # world: the development `state` is in
    # flat lays only: a placement or a curved frame reads absolute position
    repeats = (_RepeatLays() if reuse_repeats and placement is None
               and frame is None and not world else None)

    for si, (is_tv, idx, p, t) in enumerate(segs):
        dev = _development_of(p, frame) if world and len(p) >= 2 else None
        def _emit(xyz, times, sag_times=None):
            # `sag_times` is the SEED grid this fibre was marched on before the
            # adaptive corner grid refined it -- what the sag solve should run on.
            # None (every passthrough branch, and every fibre when the grid is off)
            # means "the whole grid", so the plain behaviour is unchanged.
            st = times if sag_times is None else sag_times
            if is_tv:
                tv_paths[idx], tv_ptimes[idx] = xyz, times
                tv_sag[idx] = st
            else:
                out_paths[idx], out_ptimes[idx] = xyz, times
                out_sag[idx] = st

        # The jet re-ignites at rest wherever the head cannot have arrived
        # continuously: the program start, a fibre with no inbound travel, and every
        # fibre when the mode is off.
        if (not simulate_travel or si == 0
                or not _segs_contiguous(segs[si - 1], segs[si])):
            state = None
        elif world:
            # the state was left in the previous segment's development
            state = _transfer_state(prev_dev, dev, state)
        prev_dev = dev
        # Settle only at the end of a CHAIN. Mid-chain the head travels on instead of
        # stopping, so the next segment lays that material (see `_lay_one`'s `settle`).
        settle = (not simulate_travel or si == len(segs) - 1
                  or not _segs_contiguous(segs[si], segs[si + 1]))

        if len(p) < 2:                       # nothing to reshape; pass through
            _emit(p.copy(), t.copy())
            continue

        # per-interval NAIVE nozzle speed from geometry / time (mm/s) -- the speed the
        # integrator's clock implies; and the accel-limited EFFECTIVE speed (the real
        # ramped speed), which equals v_seg when kinematics is off. The REAL speed
        # decides the regime, sizes the lag, and (in diameter_by_time) the thickness.
        metric_p = _metric_path(p, frame)
        seg_len = np.linalg.norm(np.diff(metric_p, axis=0), axis=1)
        dt = np.diff(t)
        v_seg = np.divide(seg_len, dt, out=np.zeros_like(seg_len),
                          where=dt > 0.0)
        if chain_v_eff is not None and chain_v_eff[si] is not None:
            v_eff = chain_v_eff[si]          # planned across the whole chain
        else:
            v_eff = _effective_seg_speeds(metric_p, seg_len, v_seg, max_accel,
                                          square_corner_velocity,
                                          entry_v=(0.0 if is_tv
                                                   else entry_vs[idx]),
                                          exit_v=(0.0 if is_tv
                                                  else exit_vs[idx]),
                                          seg_accel=_seg_accel(
                                              p, metric_p, frame, max_accel,
                                              rotary_accel))
        # Fold the live print-speed what-if into the EFFECTIVE (physics) speed only:
        # the lag law, regime gate and coil clock all read v_eff, while the integrator
        # clock keeps v_seg (the naive feed) -- so tau = lag(v_eff*scale)/v_seg, the
        # touchdown trailing by the scaled lag distance, exactly as mew_live._tau_at
        # does. scale == 1.0 leaves v_eff untouched (byte-identical to before).
        if speed_scale != 1.0:
            v_eff = v_eff * speed_scale
        total_len = float(seg_len.sum())
        mean_v = (float((v_eff * seg_len).sum() / total_len)
                  if total_len > 0.0 else 0.0)

        # A truly non-moving fibre (zero mean speed) has no deposition regime; pass
        # it through rather than divide by zero classifying it. `state` is left
        # untouched: nothing happened, so the next segment inherits what it would
        # have inherited anyway.
        if mean_v <= 0.0:
            _emit(p.copy(), t.copy())
            continue

        # A fibre whose COMMANDED feed crosses CTS is judged run by run
        # (`below_cts_runs`): a continuous 4-axis CTS ladder is one fibre, and a
        # single verdict coiled every rung or none. Such a fibre is laid by the
        # tracker end to end -- the lag law floors below CTS, so the touchdown
        # relaxes onto the nozzle through a slow run exactly as the live relay
        # does -- and the coil wave is added over its slow runs only. Chart
        # path only: a five-axis world lay keeps the one verdict (below).
        runs = (None if world else
                below_cts_runs(v_seg, v_eff, seg_len, cts))
        if runs is not None and len(runs) > 1:
            if not is_tv:
                info["n_coiling"] += 1
                info["coiling_fibers"].append(idx)
                info["n_coiling_partly"] = info.get("n_coiling_partly", 0) + 1
            wave = coil_wave_amp > 0.0
            res = _lay_one(
                p, t, seg_len, dt, v_seg, v_eff, lag_of, zeta,
                settle_taus, fill_per_tau, transient_taus, max_points_per_fiber,
                model=model, corner_chord=corner_chord, cts=cts,
                coiling=bool(coil and not wave),
                is_loop=(bool(loop_entry[idx])
                         if (loop_entry and not is_tv) else False),
                init=state, settle=settle, with_state=simulate_travel,
                placement=placement, frame=frame, chart=chart, dev=dev)
            if simulate_travel:
                xyz, times, max_lag, max_coil, state, seed_times = res
            else:
                xyz, times, max_lag, max_coil, seed_times = res
            if wave:
                xyz, times = overlay_wave_runs(xyz, times, p, t, runs,
                                               coil_wave_amp, coil_wave_len)
                max_coil = max(max_coil, float(coil_wave_amp))
            info["max_lag_distance"] = max(info["max_lag_distance"], max_lag)
            info["max_coil_amplitude"] = max(info["max_coil_amplitude"],
                                             max_coil)
            _emit(xyz, times, seed_times)
            continue

        # Regime by mean speed vs CTS. The small (1-1e-9) margin makes the
        # classification DETERMINISTIC for a fibre whose every segment sits exactly
        # at CTS -- where ~1e-14 of length-weighted-mean rounding would otherwise
        # coin-flip it, the root of the old 0deg/90deg asymmetry and wavelength flip.
        below_cts = mean_v < cts * (1.0 - 1e-9)
        if below_cts:
            if not is_tv:                    # a travel is not one of the fibres
                info["n_coiling"] += 1
                info["coiling_fibers"].append(idx)
            if coil_wave_amp > 0.0:
                # visual coiling-wave INDICATOR (mew_coil_wave): reshape the
                # below-CTS fibre into a tight lateral sine so the regime is
                # unmistakable on screen, in place of the straight passthrough
                # below. Fixed user amplitude/wavelength -- an indicator, not the
                # physical (length-conserving) coil the `coil` flag lays. The
                # state still advances exactly as the passthrough does.
                if world:
                    # the wave is drawn on the development and carried back
                    wx, wt = coil_wave_reshape(metric_p, t, coil_wave_amp,
                                               coil_wave_len)
                    noz = np.column_stack([np.interp(wt, t, metric_p[:, 0]),
                                           np.interp(wt, t, metric_p[:, 1])])
                    wx = dev.to_world(t, wt, wx[:, :2], noz)
                else:
                    wx, wt = coil_wave_reshape(p, t, coil_wave_amp,
                                               coil_wave_len)
                info["max_coil_amplitude"] = max(info["max_coil_amplitude"],
                                                 float(coil_wave_amp))
                _emit(wx, wt)
                if simulate_travel:
                    state = _end_pin_state(metric_p if world else p, dt)
                continue
            if not coil:
                # honest passthrough (the default): the straight-fibre model does
                # not hold below CTS and the coil is opt-in, so leave the commanded
                # centreline unchanged rather than fake a straight rounded fibre.
                # The STATE still advances: below CTS the steady lag is zero (theta
                # is pinned to the CTS boundary), so the contact rides the nozzle --
                # which is exactly the state `_end_pin_state` reports, i.e. what the
                # tracker itself would have produced here.
                _emit(p.copy(), t.copy())
                if simulate_travel:
                    state = _end_pin_state(metric_p if world else p, dt)
                continue

        # Reaching here: either above CTS (no coil), or below CTS with coiling ON.
        # Above CTS `coiling` is False and `_lay_one` reduces to the straight-fibre
        # lag + corner-rounding model, byte-for-byte its pre-coiling behaviour.
        is_loop = (bool(loop_entry[idx])
                   if (loop_entry and not is_tv) else False)

        def run(p=p, t=t, seg_len=seg_len, dt=dt, v_seg=v_seg, v_eff=v_eff,
                below_cts=below_cts, is_loop=is_loop, state=state,
                settle=settle, dev=dev):
            return _lay_one(
                p, t, seg_len, dt, v_seg, v_eff, lag_of, zeta,
                settle_taus, fill_per_tau, transient_taus, max_points_per_fiber,
                model=model, corner_chord=corner_chord, cts=cts,
                coiling=below_cts, is_loop=is_loop,
                init=state, settle=settle, with_state=simulate_travel,
                placement=placement, frame=frame, chart=chart, dev=dev)
        if repeats is not None and not below_cts:
            res = repeats.lay(np.asarray(p, dtype=np.float64),
                              np.asarray(t, dtype=np.float64), v_eff,
                              (is_tv, is_loop, settle), state, run)
        else:
            res = run()
        if simulate_travel:
            xyz, times, max_lag, max_coil, state, seed_times = res
        else:
            xyz, times, max_lag, max_coil, seed_times = res
        info["max_lag_distance"] = max(info["max_lag_distance"], max_lag)
        info["max_coil_amplitude"] = max(info["max_coil_amplitude"], max_coil)
        _emit(xyz, times, seed_times)

    info["sag_times"] = out_sag
    if simulate_travel:
        info["travel_paths"] = tv_paths
        info["travel_ptimes"] = tv_ptimes
        info["travel_sag_times"] = tv_sag
    # Did the CONTACT ARC saturate at the tangency bound anywhere in this run?
    # The placement accumulates it (`mew_mandrel.cap_circumferential_lag`); it is
    # reported rather than clamped in silence because past that bound the
    # unclamped deviation turns over and FALLS, and a saturating number that
    # looks like a prediction is exactly what went unnoticed before.
    sat = getattr(placement, "saturation", None)
    if sat:
        info["lag_arc_saturated"] = dict(sat)
    # Did any lag radius come from a CONCAVE patch? Same channel, and the same
    # reason: nothing published measures jet lag on a concave collector, so an
    # admitted number there is a prediction and must be labelled one wherever
    # it is shown (`mew_mandrel.CurvedPlacement.concave`, five-axis spec 8).
    conc = getattr(placement, "concave", None)
    if conc:
        info["lag_concave_prediction"] = dict(conc)
    # Back to WORLD, and None-safe: `travel_paths` is index-aligned to the
    # fibres, so every index that is not a travel holds None.
    if not world:
        out_paths = _to_world_sparse(chart, out_paths)
        if simulate_travel:
            info["travel_paths"] = _to_world_sparse(chart,
                                                    info["travel_paths"])
    return out_paths, out_ptimes, info


def _to_world_sparse(chart, paths):
    """`chart.to_world` over a list that may hold None at some indices."""
    idx = [i for i, p in enumerate(paths) if p is not None]
    if not idx:
        return paths
    done = chart.to_world([paths[i] for i in idx])
    out = list(paths)
    for i, p in zip(idx, done):
        out[i] = p
    return out


def _program_segments(paths, ptimes, travels, simulate_travel):
    """The segments the jet lays, in PROGRAM order, as [is_travel, idx, p, t].

    Without the continuous-jet mode this is just the fibres (each laid in isolation).
    With it, each fibre is preceded by its inbound travel,
    so walking this list IS the head's whole motion. `idx` is always the FIBRE index
    the segment belongs to (a travel carries the index of the fibre it leads into),
    which is what keeps every output index-aligned to `paths`.

    travels[0] -- the LEADING travel (origin/home to the first part) -- is deposited
    too: in continuous MEW the jet is already flowing on that first approach. It
    ignites at rest (`si == 0` in the caller), so the first fibre inherits the lag it
    built. Its start is `parse_gcode`'s ASSUMED origin (the head's true pre-program
    position is not in the g-code); for generator output that IS the g-code frame."""
    segs = []
    for i in range(len(paths)):
        if simulate_travel and travels and 0 <= i < len(travels):
            tv = travels[i]
            if tv is not None:
                tvp, tvt = tv
                if tvp is not None and tvt is not None and len(tvp) >= 2:
                    segs.append([True, i, np.asarray(tvp, dtype=np.float64),
                                 np.asarray(tvt, dtype=np.float64)])
        segs.append([False, i, np.asarray(paths[i], dtype=np.float64),
                     np.asarray(ptimes[i], dtype=np.float64)])
    # ...and the TRAILING travel, the end-of-print exit, after the last fibre:
    # the head travels on out of it, so the last fibre no longer settles at its
    # end and the exit lays that material. Its index is len(paths), one past
    # the fibres, as `parse_gcode` reports it.
    tv = trailing_travel(paths, travels) if simulate_travel else None
    if tv is not None and paths:
        segs.append([True, len(paths), np.asarray(tv[0], dtype=np.float64),
                     np.asarray(tv[1], dtype=np.float64)])
    return segs


def _segs_contiguous(a, b, tol=1e-9):
    """True when segment b continues a with no jump -- same point, same instant.

    `parse_gcode(with_travels=True)` shares the junction VERTEX between a fibre and
    its travel exactly (each starts where the last ended, on the one global clock),
    so this is an exact identity test, not a proximity guess. False means the head
    could not have got there continuously, and the jet must re-ignite at rest."""
    if len(a[2]) < 1 or len(b[2]) < 1:
        return False
    return (float(np.linalg.norm(a[2][-1] - b[2][0])) <= tol and
            abs(float(a[3][-1] - b[3][0])) <= tol)


def _chain_effective_speeds(segs, max_accel, square_corner_velocity,
                            entry_vs, exit_vs, frame=None, rotary_accel=0.0):
    """Accel-limited per-interval speeds for every segment, planned across whole
    contiguous CHAINS rather than segment-by-segment. Returns a list parallel to
    `segs`, or None when kinematics is off (the caller then plans per segment).

    Why chains: with the continuous jet the head does NOT stop at a fibre's end --
    it travels straight on. Planning each segment alone would ramp it to a halt and
    back up at every junction, inventing a stop the machine never makes (and blunting
    the very entry state this mode exists to model). A chain is one continuous
    polyline, so handing the WHOLE chain to `plan_trajectory` lets the planner apply
    its own junction-deviation / centripetal corner rules at the junctions -- the
    same rules it uses everywhere else -- instead of this module re-deriving them.
    The chain's outer boundaries keep the caller's `entry_vs`/`exit_vs` (normally 0:
    the program really does start and end at rest)."""
    if plan_trajectory is None or max_accel is None or max_accel <= 0.0:
        return None
    out = [None] * len(segs)
    start = 0
    while start < len(segs):
        end = start                                  # [start, end] = one chain
        while (end + 1 < len(segs)
               and _segs_contiguous(segs[end], segs[end + 1])):
            end += 1
        chain = segs[start:end + 1]
        pts = [chain[0][2]] + [s[2][1:] for s in chain[1:]]   # drop shared vertices
        tms = [chain[0][3]] + [s[3][1:] for s in chain[1:]]
        P = np.concatenate(pts, axis=0)
        T = np.concatenate(tms)
        metric_p = _metric_path(P, frame)
        sl = np.linalg.norm(np.diff(metric_p, axis=0), axis=1)
        dtm = np.diff(T)
        vn = np.divide(sl, dtm, out=np.zeros_like(sl), where=dtm > 0.0)
        head, tail = chain[0], chain[-1]
        ve = _effective_seg_speeds(
            metric_p, sl, vn, max_accel, square_corner_velocity,
            entry_v=(0.0 if head[0] else entry_vs[head[1]]),
            exit_v=(0.0 if tail[0] else exit_vs[tail[1]]),
            seg_accel=_seg_accel(P, metric_p, frame, max_accel, rotary_accel))
        off = 0
        for k, s in enumerate(chain):
            m = len(s[2]) - 1                        # intervals owned by s
            out[start + k] = np.asarray(ve[off:off + m], dtype=np.float64)
            off += m
        start = end + 1
    return out


def _end_pin_state(p, dt):
    """Contact state (c, w) for a fibre whose contact RIDES the nozzle (tau == 0).

    With no lag the touchdown sits under the head, so the state handed to the next
    segment is simply the last point and the last MOVING interval's velocity."""
    c = [float(p[-1, 0]), float(p[-1, 1])]
    w = [0.0, 0.0]
    for k in range(len(dt) - 1, -1, -1):
        if dt[k] > 0.0:
            w = [float((p[k + 1, 0] - p[k, 0]) / dt[k]),
                 float((p[k + 1, 1] - p[k, 1]) / dt[k])]
            break
    return c, w


def _relax_offsets(off_seg, off_of, rt, tau_seg):
    """The mandrel cross-track pre-offset, RELAXED onto the refined grid.

    `off_seg[k]` is Eq 5's lateral deviation for commanded interval k -- a STATIONARY
    equilibrium (Neuhaus 2025 solves a curvature balance, not a trajectory), so it is
    where the contact ends up on a long straight run at that heading, not where it is
    one sample after the heading changed. Handed the raw step, the ray tracker
    PURSUED that one-sample sideways jump and closed a small loop around it -- 144 per
    layer on the re-entrant unit cell.

    So the offset relaxes towards each interval's target on that interval's own tau
    -- the lag's time scale, the only one in the problem -- integrated exactly:
    off <- target + (off - target) * exp(-dt / tau). Two properties it is chosen for:
      * STEADY STATE IS UNCHANGED: exp() -> 0 down a long pass, so far from a vertex
        the offset is still exactly Eq 5's deviation -- the settled cross-track on a
        45 deg / 60 mm helix pass is 0.4581673632557135 mm before and after.
      * IT IS GRID-INDEPENDENT: the target is piecewise constant per commanded
        interval and every commanded time is in `rt`, so each block below sees a
        CONSTANT target and the exact exponential composes, which the sparse straight
        interior the refiner leaves behind depends on.

    Only the TRANSIENT after a heading change differs from the piecewise-constant
    rule; a fibre still starts at its first interval's offset and the settle tail
    inherits the last, both because `off_of` is clipped.
    """
    n_s = len(rt)
    out = np.empty((n_s, 2), dtype=np.float64)
    out[0] = off_seg[off_of[0]]
    cur = (float(out[0, 0]), float(out[0, 1]))
    # one block per run of samples sharing a commanded interval; within a block
    # the target is constant, so the whole block is one closed-form decay from
    # the block's entry sample (which is the commanded vertex itself).
    starts = np.concatenate([[0], np.flatnonzero(np.diff(off_of)) + 1])
    for bi in range(len(starts)):
        s = int(starts[bi])
        e = int(starts[bi + 1]) if bi + 1 < len(starts) else n_s
        lo = s + 1 if s == 0 else s              # sample 0 is the initial value
        if lo >= e:
            continue
        g = int(off_of[s])
        tgt_x, tgt_y = float(off_seg[g, 0]), float(off_seg[g, 1])
        tau = float(tau_seg[g])
        t0 = rt[lo - 1]
        if tau > 0.0:
            w = np.exp(-(rt[lo:e] - t0) / tau)
        else:                                    # no lag -> no relaxation to do
            w = np.zeros(e - lo, dtype=np.float64)
        out[lo:e, 0] = tgt_x + (cur[0] - tgt_x) * w
        out[lo:e, 1] = tgt_y + (cur[1] - tgt_y) * w
        cur = (float(out[e - 1, 0]), float(out[e - 1, 1]))
    return out


def _lay_one(p, t, seg_len, dt, v_seg, v_eff, lag_of, zeta, settle_taus,
             fill_per_tau, transient_taus, max_points_per_fiber,
             *, model, cts=0.0, coiling=False, samples_per_period=40,
             is_loop=False, init=None, settle=True, with_state=False,
             placement=None, frame=None, corner_chord=None, chart=None,
             dev=None):
    """Reshape one fibre. Returns (xyz (M,3), times (M,), max_lag, max_coil,
    seed_times), or (..., state, seed_times) with `with_state` -- the final contact
    (c, w) for the next segment. `seed_times` is the grid BEFORE the adaptive corner
    grid refined it (a subset of `times`, and `times` itself when the grid is off);
    the sag solve runs on it -- see `laid_fibers`.

    `init` (c, w) starts the contact tracker from a handed-in state instead of at rest
    under the nozzle -- the continuous-jet seam (`simulate_travel`), where the jet
    arrives already trailing. `settle` False drops the settle tail, which models the
    head STOPPING while the material in flight lands and is wrong mid-program: there
    the NEXT segment lays it. Defaults are byte-identical.

    The lag is sized at the EFFECTIVE (accel-limited) per-interval speed `v_eff` while
    the integrator runs on the naive clock at `v_seg`, so tau = lag/v_seg trails by the
    real lag distance; with kinematics off v_eff == v_seg. When `coiling` the grid is
    densified to the coil clock and a length-conserving excursion added.

    `placement` (a `mew_mandrel.ContactPlacement`, None = flat) is the mandrel seam.
    Per commanded interval it turns the flat lag into the ALONG-track trail for that
    heading -- read from `p`, already the unrolled (u, v, h) under a rotary frame --
    and returns the lateral offset TARGET that `_relax_offsets` runs the tracker's
    INPUT positions towards, never a step. It is applied to `nx`/`ny` only, never to
    `p`, `seg_len`, `v_seg` or `t`: the offset must not perturb the clock, and the
    commanded Z rides through untouched. None takes no branch at all, so the flat
    path is byte-identical.

    `dev` (a `_Development`) is the WORLD-NATIVE lay: `p` is world, the tracker
    runs on `dev.q`, the placement is asked at each segment's own principal
    curvatures and directions, and the laid points are carried back onto the
    collector (`_Development.to_world`). None is the chart path above, unchanged."""
    p_world = p
    if dev is not None:
        p = dev.q
    n = len(p)
    # tau per commanded interval (and the max lag distance seen, for reporting).
    # tau = lag_distance(v_eff) / v_naive: the touchdown trails by the lag distance at
    # the REAL speed even though the integrator's clock implies v_naive. A
    # non-advancing interval (dt<=0 -> v_seg==0) inherits the previous interval's tau.
    tau_seg = np.empty(n - 1, dtype=np.float64)
    # the lateral pre-offset per commanded interval, or None on a flat collector
    off_seg = None if placement is None else np.zeros((n - 1, 2), dtype=np.float64)
    last_off = (0.0, 0.0)
    max_lag = 0.0
    last_tau = 0.0
    for k in range(n - 1):
        if v_seg[k] > 0.0 and v_eff[k] > 0.0:
            lag = lag_of(v_eff[k])
            if placement is not None and dev is not None:
                # WORLD-NATIVE: the placement's heading is the segment's world
                # tangent resolved on the principal directions at its foot, and
                # its cross-track answer is rebuilt in world on those directions
                # before entering the development. `per_layer_registration`
                # off (the default) asks at the bare surface, as the chart does.
                reg = bool(getattr(placement, "per_layer_registration", False))
                k1, k2, du, dv, d1, d2 = dev.principal(k, reg)
                lag, ou, ov = placement.at_curvatures(k1, k2).resolve(
                    du, dv, lag, speed=v_eff[k])
                last_off = dev.offset_to_dev(k, ou * d1 + ov * d2)
            elif placement is not None:
                # Heading taken in the UNROLLED frame, where a helix is straight.
                #
                # ONE PLACEMENT PER SCAFFOLD, AT THE BARE RADIUS. Eq 1-5 is a
                # single-layer model; re-evaluating it on each grown layer is a
                # composition rule nothing measures, and it leans the wall by
                # ~5.5 um/layer (MANDREL_LAG_DERIVATION.md §12.8). So by default
                # every wall height is placed as h = 0. `frame` is still consulted
                # for the fibre's SPEED upstream (`_metric_path`) -- kinematics, not
                # contact geometry. The hook restores the per-layer path, and is
                # all-or-nothing on purpose: the per-layer terms OPPOSE each other,
                # so freezing a subset re-creates a lean of the other sign (§12.1).
                du = p[k + 1, 0] - p[k, 0]
                dv = p[k + 1, 1] - p[k, 1]
                m1, m2, sh = 1.0, 1.0, 0.0
                local_placement = placement
                where = None          # set only when the CHART answers the heading
                if frame is not None and getattr(placement, "per_point", False):
                    # A DOUBLY-CURVED collector (five-axis) has neither a
                    # single radius nor a single curvature pair, so the
                    # placement is re-asked per segment at the frame's own
                    # per-point query rather than held for the scaffold. The
                    # quantum inside `at_curvatures` is what stops that being
                    # one object per segment.
                    #
                    # `metric_v` is deliberately NOT called here: on a
                    # non-developable frame it raises (one scalar cannot
                    # describe a chart whose stretch varies along u), and the
                    # heading's v-stretch is first-fundamental-form work not
                    # done here. At h = 0 -- which is every
                    # segment unless per-layer registration is on -- the
                    # stretch is exactly 1 and nothing is being dropped; with
                    # it on, the wall-height stretch of the HEADING is
                    # unaccounted while the curvature it feeds is not; that seam
                    # is open.
                    h_mid = (0.5 * (p[k, 2] + p[k + 1, 2])
                             if placement.per_layer_registration else 0.0)
                    u_mid = 0.5 * (p[k, 0] + p[k + 1, 0])
                    v_mid = 0.5 * (p[k, 1] + p[k + 1, 1])
                    local_placement = placement.at_curvatures(
                        *chart.curvatures(u_mid, v_mid, h_mid))
                    # THE HEADING, ASKED OF THE CHART. `GlobalChart` answers
                    # None -- it hands its raw step through, exactly as this
                    # branch always did, so a cone's placement cannot move --
                    # while the pole's polar chart answers the step in the
                    # principal (meridian, parallel) frame, which is the
                    # direction `CurvedPlacement.resolve` is asking for. The
                    # paragraph above is why the chart can answer it at all:
                    # this chart has no shear and its two scales are known.
                    hd = chart.heading(u_mid, v_mid, h_mid, du, dv)
                    if hd is not None:
                        du, dv = hd
                        where = (u_mid, v_mid, h_mid)
                elif (frame is not None
                        and getattr(placement, "per_layer_registration", False)):
                    h_mid = 0.5 * (p[k, 2] + p[k + 1, 2])
                    mm = frame.metric(0.5 * (p[k, 0] + p[k + 1, 0]),
                                      0.5 * (p[k, 1] + p[k + 1, 1]), h_mid)
                    m1, m2, sh = float(mm[0]), float(mm[1]), float(mm[2])
                    du, dv = m1 * du, m2 * (dv - sh * du)   # LOCAL metric,
                    #                        both axes and the chart's shear
                    if hasattr(placement, "at_surface_radius"):
                        local_placement = placement.at_surface_radius(
                            frame.surface_radius(h_mid))
                lag, ou, ov = local_placement.resolve(du, dv,
                                                       lag, speed=v_eff[k])
                # the exact inverse of whatever mapped the heading forward, so a
                # chart that shears or scales cannot leak a heading into the
                # stored offset. `where` is set only where the CHART answered the
                # heading; otherwise this is the metric inverse it always was.
                if where is not None:
                    last_off = chart.chart_offset(where[0], where[1], where[2],
                                                  ou, ov)
                else:
                    last_off = (ou / m1, ov / m2 + sh * ou / m1)  # -> bare chart
            last_tau = lag / v_seg[k]
            max_lag = max(max_lag, lag)
        tau_seg[k] = last_tau
        if off_seg is not None:
            off_seg[k] = last_off
    # The fill step is the smallest tau -- among the taus that carry a lag. A
    # feed at EXACTLY CTS (a 480 mm/min rung at CTS = 8 mm/s) leaves a lag of
    # rounding error, a tau near 1e-17 s, and stepping a fast run's window at
    # that asked for ~1e16 samples. A tau a millionth of the fibre's largest is
    # a lag a millionth of its largest (nanometres), which no fill resolves
    # anyway; a fibre whose taus sit within that range is unchanged.
    pos_tau = tau_seg[tau_seg > 0.0]
    tau_min = (float(pos_tau[pos_tau >= 1e-6 * float(pos_tau.max())].min())
               if pos_tau.size else 0.0)
    if tau_min <= 0.0 and not coiling:        # degenerate (no motion); pass through
        # The SEED grid rides out with every other return from this function.
        # It is `t` itself here, and that is not a convention: the branch hands
        # the commanded path straight back, so the grid the sag solve should run
        # on IS the commanded one -- the same answer `laid_fibers` gives on its
        # `cts_invalid` pass-through.  Omitting it returns a 4-tuple into a
        # 5-tuple unpack and raises, and only a print at EXACTLY CTS reaches
        # this branch, where the steady lag is zero while `coiling` is still
        # False -- so it is easy to miss, on a flat 3-axis path as much as a
        # curved one.
        if with_state:
            return (p_world.copy(), t.copy(), max_lag, 0.0,
                    _end_pin_state(p, dt), t.copy())
        return p_world.copy(), t.copy(), max_lag, 0.0, t.copy()
    # Below CTS the steady lag is 0 (theta_cts pinned to the boundary), so tau_min is
    # 0 here NOT because the fibre is motionless but because there is no lag to trail.
    # The trailing centre then reduces to the commanded nozzle path (correct: no lag
    # below CTS) and the sewing-machine coil is added on top, its grid driven by the
    # coil CLOCK (period below), not the lag. Only bail for a truly non-coiling fibre
    # with no lag; a coiling one still lays its length-conserving excursion.

    # Refined time grid: every commanded time (corners stay exact) plus a fill
    # inside the transient window after the start and after each sharp corner /
    # speed change -- where the touchdown actually curves. Straight interiors keep
    # their own (sparse) sampling because the contact there is just a constant
    # offset line, which two endpoints already capture. That holds only while the
    # tracker's INPUT is constant across the interior. Under a mandrel
    # `placement` the lateral offset relaxes for `transient_taus` * tau after the
    # corner -- the SAME window this fill covers, and by then exp(-t/tau) has run
    # out -- so the sparse remainder is constant again. The relaxation itself is
    # grid-independent (`_relax_offsets`), so refining moves it by nothing.
    step = tau_min / fill_per_tau
    extra = []
    corners = _corner_vertices(p if dev is not None else _metric_path(p, frame),
                               v_seg)
    if is_loop and dev is not None:
        # the closure is a WORLD coincidence; a development does not close
        s0 = p_world[0]
        for k in range(2, n - 1):
            if np.all(np.abs(p_world[k] - s0) < 1e-6):
                corners = sorted(set(corners) | {k - 1, k})
                break
    elif is_loop:
        # A linking loop RETURNS to its start vertex heading the SAME way the
        # deposition then leaves (the lead-out keeps the previous fibre's exit
        # heading), so the closure is NOT a sharp corner and `_corner_vertices`
        # misses it. But the touchdown is still catching up out of the loop's curve
        # across the two LONG commanded intervals straddling the closure -- the
        # lead-in chord back to the entry and the first deposition pass -- which then
        # receive NO fill, so the catch-up collapses into a single straight chord
        # (the locked-sim "straight span in the loop" artifact; the live re-lay avoids
        # it by extending every window to the fibre end). Force both straddling
        # intervals into the refine set -- just these two, no global densification.
        sx, sy = p[0, 0], p[0, 1]
        for k in range(2, n - 1):
            if abs(p[k, 0] - sx) < 1e-6 and abs(p[k, 1] - sy) < 1e-6:
                corners = sorted(set(corners) | {k - 1, k})
                break
    # Adaptive per-fibre budget: keep at least as many samples per corner as its
    # refine window can hold (transient_taus * fill_per_tau, floored by
    # _MIN_FILL_PER_CORNER) so the corner-rounding fill is never thinned into a
    # blocky polygon -- at ANY Corner-resolution setting, not just the default (see
    # the note above _MIN_FILL_PER_CORNER). Floored by the flat cap (small fibres
    # unchanged); rises with the corner count so one big single-fibre serpentine or
    # unit-cell tube resolves every corner. Used for BOTH the coil-grid clamp and
    # the final thinning below.
    per_corner = max(_MIN_FILL_PER_CORNER,
                     int(math.ceil(transient_taus * fill_per_tau)))
    cap = max(max_points_per_fiber, n + len(corners) * per_corner)
    for ci, k in enumerate(corners):          # k indexes the interval to fill into
        # The catch-up out of a corner rings for transient_taus*tau, which can OUTLAST
        # the corner's own short interval: a turnaround loop exits SMOOTHLY into the
        # long pass after it, so that junction is not itself a corner and the catch-up
        # spills into a pass that then gets NO fill. Clamping the window to the next
        # vertex starved that spill and collapsed the touchdown into one straight
        # chord across the part -- the locked-sim "diagonal across an angled layer"
        # artifact, invisible on a square because the chord lies along the pass.
        # Extending to the NEXT CORNER instead is byte-identical on a dense arc-run
        # (its next corner IS the next vertex) and fills the catch-up into an isolated
        # corner before a long sparse pass. The general form of the `is_loop` patch.
        nxt = corners[ci + 1] if ci + 1 < len(corners) else (n - 1)
        win_end = min(t[k] + transient_taus * tau_seg[k], t[nxt])
        # A window of exactly N steps must give N points. Without the 1e-9 the
        # floor read N or N-1 on the last ulp of t[k], so the same corner laid a
        # different grid in a chained run and a glued one.
        m = int((win_end - t[k]) / step + 1e-9) if step > 0.0 else 0
        if m > 0:
            extra.append(t[k] + step * np.arange(1, m + 1))
    rt = (np.unique(np.concatenate([t] + extra)) if extra else t.copy())

    # Settle tail: hold the nozzle at the last point for a few tau so the trailing
    # touchdown catches up to the endpoint (the in-flight material that lands after
    # the head moves on), instead of ending short by the lag distance. The tail
    # spans (and is sampled by) the LAST interval's own tau -- not tau_min -- so it
    # fully settles even when the fibre ends on a slower (longer-tau) move.
    tail_tau = tau_seg[-1]
    if tail_tau > 0.0 and settle:
        tail_step = tail_tau / fill_per_tau
        n_tail = int(settle_taus * fill_per_tau)
        tail = rt[-1] + tail_step * np.arange(1, n_tail + 1)
        rt = np.concatenate([rt, tail])

    # Below CTS the coil oscillates over the WHOLE fibre, not just at corners, so
    # lay a dense uniform grid at the fastest coil clock on top of the corner grid
    # (union keeps the commanded vertices, so the trailing centre still rounds
    # corners exactly). `samples_per_period` points per coil period resolve the
    # loops smoothly.
    if coiling and cts > 0.0 and model is not None:
        omega_max = 0.0
        for vk in v_eff:                      # the coil clock follows the REAL speed
            if vk > 0.0:
                omega_max = max(
                    omega_max, model.coil_angular_frequency(cts, vk / cts))
        if omega_max > 0.0:
            period = 2.0 * math.pi / omega_max
            span = float(rt[-1] - rt[0])
            # The coil lays tight loops over the WHOLE span, not just at corners, so
            # the budget must also cover samples_per_period per coil period -- else a
            # long below-CTS stretch (a slow print, which coils MORE) is capped and the
            # loops coarsen. Raise the cap to hold the coil grid (the corner term
            # already covers the rounding); the final thin then only bites true
            # pathology. n grows with both the corner count and the coil-period count.
            n_coil = int(samples_per_period * span / period) + 1
            cap = max(cap, n + n_coil)
            if n_coil > 2:
                coil_grid = rt[0] + span * (np.arange(n_coil + 1) / n_coil)
                rt = np.unique(np.concatenate([rt, coil_grid]))

    # enforce the adaptive per-fibre cap by thinning the fill (vertices always kept)
    if len(rt) > cap:
        rt = _cap_grid(rt, t, cap)

    # One march on a given time grid. Factored out so the adaptive corner grid
    # can re-run it on a refined `rt`; a single call with `corner_chord=None` is
    # the plain code path, statement
    # for statement, which is what keeps the flat export byte-identical.
    def _march(rt):
        # nozzle position at each refined time (exact linear interpolation -- the head
        # moves at constant speed within a commanded interval); z rides the layer.
        nx = np.interp(rt, t, p[:, 0])
        ny = np.interp(rt, t, p[:, 1])
        nz = np.interp(rt, t, p[:, 2])
        nozzle_xy = np.column_stack([nx, ny]) if dev is not None else None

        # tau for each refined sub-interval = the tau of the commanded interval it
        # starts in (clamped); samples in the settle tail use the last interval's tau.
        seg_of = np.clip(np.searchsorted(t, rt[:-1], side="right") - 1,
                         0, n - 2)
        sub_tau = tau_seg[seg_of]

        if off_seg is not None:
            # `seg_of` indexes intervals, not samples, and the settle tail must
            # inherit the last interval's heading, so the target gets its own
            # full-length index. `side="left"`: interval k's TARGET owns
            # (t[k], t[k+1]], closed on the right, so the change to the next heading
            # lands on the first sample AFTER the vertex. That keeps the target's
            # steps ALIGNED to commanded vertices, which every sample grid contains,
            # and that alignment is what makes the relaxation exact and grid-free.
            # THE TARGET IS NOT THE OFFSET: applied raw, Eq 5's stationary
            # equilibrium steps the tracker's input sideways by up to 754 um in a
            # single sample on the default 3 mm tube, and the tracker closes a loop
            # around it at every junction. `_relax_offsets` runs the offset onto its
            # target on that interval's own tau instead. Flat prints skip this.
            off_of = np.clip(np.searchsorted(t, rt, side="left") - 1, 0, n - 2)
            off = _relax_offsets(off_seg, off_of, rt, tau_seg)
            nx = nx + off[:, 0]
            ny = ny + off[:, 1]

        # The PLACEMENT RULE: Hrynevich 2020's ray / V_JCP step, not a linear filter.
        # `lag_ceiling`/`lag_floor` are the lag law's own saturation constants in mm at
        # THIS gap; the per-interval steady lag (and with it the accel limit, the live
        # speed scale and the mandrel's anisotropy) still arrives through `sub_tau`.
        # See `mew_physics.integrate_contact_path_ray`. `zeta` is retired: it damped the
        # second-order filter this replaced and no longer reaches any geometry.
        ceiling, floor, sat_exp = model.lag_ray_constants()
        # With the adaptive corner grid on, the output is sampled far finer than
        # `fill_per_tau` alone would sample it, and the tracker's fixed 16
        # sub-steps PER SAMPLE would multiply an integration that had already
        # converged -- which would be most of what the grid costs. Hand it the
        # calibrated steps-per-tau budget instead, so the inner work follows
        # dt/tau. Passed only when it applies, so a flat call is the same call
        # it has always been, down to the keyword list (a couple of suites stub
        # this function with a fixed-signature lambda).
        ray_kw = ({} if corner_chord is None
                  else {"steps_per_tau": corner_chord[2]})
        res = integrate_contact_path_ray(
            list(zip(nx.tolist(), ny.tolist())), rt.tolist(), sub_tau.tolist(),
            lag_ceiling=ceiling, lag_floor=floor, saturation_exp=sat_exp,
            init=init, with_state=with_state, **ray_kw)
        if with_state:
            contact, end_state = res
        else:
            contact = res
        contact = np.asarray(contact, dtype=np.float64)

        cx, cy = contact[:, 0], contact[:, 1]
        max_coil = 0.0
        if coiling and cts > 0.0 and model is not None:
            # the coil's eps/clock follow the REAL (accel-limited) speed v_eff
            ex, ey, max_coil = _coil_excursion(contact, t, rt, v_eff, cts, model)
            cx = cx + ex
            cy = cy + ey

        if dev is not None:
            xyz = dev.to_world(t, rt, np.column_stack([cx, cy]), nozzle_xy)
        else:
            xyz = np.column_stack([cx, cy, nz])
        return xyz, max_coil, (end_state if with_state else None)

    # THE SEED GRID, kept. Refining is add-only, so this stays a strict subset of
    # whatever `rt` becomes -- which is what lets the SAG solve run on it while the
    # drawn fibre keeps every refined sample. See `laid_fibers`' `sag_times` and
    # `gcode_to_fibers.carry_sag_onto_drawn`. With the corner grid off it IS `rt`,
    # so the plain path hands back the same array.
    seed_rt = rt
    xyz, max_coil, end_state = _march(rt)
    # The ADAPTIVE corner grid. Everything above places samples in TIME, uniformly
    # inside each corner window; this asks the geometry that came out whether that
    # was enough, and re-marches wherever it was not. Off (None) is the plain
    # single march.
    if corner_chord is not None:
        rt, xyz, max_coil, end_state = _refine_corner_grid(
            rt, xyz, max_coil, end_state, _march, corner_chord,
            _refine_cap(cap, seg_len, corner_chord))
    if with_state:
        # NB the state is the trailing CENTRE's, deliberately excluding any coil
        # excursion: the excursion is an oscillation ABOUT the centre, not part of
        # the tracker's state, so the next segment must inherit the centre.
        return xyz, rt, max_lag, max_coil, end_state, seed_rt
    return xyz, rt, max_lag, max_coil, seed_rt


def _assemble_coil_offsets(psi_s, phase, r_s):
    """Closed-form coil offsets (along, perp) from per-sample Psi, phase and coil
    radius R: offset = R * sum_k c_k(Psi) sin(k*phase), with the harmonic
    coefficients c_k from mew_physics.coil_offset_series (each carrying J_k(Psi), so
    the offset -> 0 as Psi -> 0). Grouped by rounded Psi so the Bessel series is
    evaluated once per distinct speed (a single-feed coil hits one group)."""
    nsamp = len(phase)
    along_off = np.zeros(nsamp)
    perp_off = np.zeros(nsamp)
    psi_r = np.round(psi_s, 6)
    for pv in np.unique(psi_r):
        mask = psi_r == pv
        perp_terms, along_terms = coil_offset_series(float(pv))
        ph = phase[mask]
        po = np.zeros(ph.shape)
        ao = np.zeros(ph.shape)
        for k, c in perp_terms:
            po += c * np.sin(k * ph)
        for k, c in along_terms:
            ao += c * np.sin(k * ph)
        perp_off[mask] = r_s[mask] * po
        along_off[mask] = r_s[mask] * ao
    return along_off, perp_off


def _coil_excursion(contact, t, rt, v_seg, cts, model):
    """Length-conserving sewing-machine coil added on top of the trailing centre.

    Below CTS the contact lays at the constant landing speed U=CTS with its tangent
    making angle psi = Psi*cos(phase) to the local travel direction. Psi is fixed by
    arc-length conservation (J0(Psi)=eps=v/CTS), so the along-travel speed averages
    to v -- the coil stays registered with the nozzle -- and the deposited length is
    CTS/v per unit travel. The coil is built as a CLOSED-FORM bounded local-frame
    offset (along/perp) -- the centred meander perp = 2 J1(Psi) R sin(phase)
    (Morris et al. 2008 y=A sin(wt)) plus its 2:1 companion along ~ -J2(Psi) R sin(2
    phase) and higher harmonics (see mew_physics.coil_offset_series) -- and only THEN
    rotated into the world by the trailing-centre tangent. Using the closed form
    (rather than INTEGRATING the coil velocity) is what guarantees the excursion
    VANISHES as Psi -> 0: every harmonic carries a J_k(Psi) factor, so the coil joins
    the straight fibre continuously at CTS AND leaves NO residual offset where it
    stops coiling. (A velocity integral instead FREEZES at a non-zero residual when
    the coil ends, which would shift the rest of the fibre -- visible as alternating
    pass spacing on a serpentine after a partial-fibre coiling stretch.) The
    head-held settle tail deposits no fibre, so the phase is frozen there and the
    coil holds its last position. Returns (ex, ey, max_amp_mm)."""
    # TODO(coil-kink): a coiling fibre shows weird random kinks where it runs through
    # a LINKING LOOP (the serpentine turnaround). The coil offset is rotated into the
    # world by the trailing-centre tangent; at a sharp ~180deg turnaround that tangent
    # sweeps fast over a few samples, so the offset direction whips around and the coil
    # folds into a cusp instead of a smooth loop. Likely fixes: ease/limit the tangent
    # rotation rate through a corner, densify the coil grid across the turnaround, or
    # decouple the coil phase from the corner. SAME bug in the twin
    # mew_live._coil_excursion (real-time sim). NOT yet fixed.
    nsamp = len(rt)
    if nsamp < 2:
        return np.zeros(nsamp), np.zeros(nsamp), 0.0
    nseg = len(v_seg)
    # commanded interval each sample sits in (settle-tail samples clamp to the last)
    samp_seg = np.clip(np.searchsorted(t, rt, side="right") - 1, 0, nseg - 1)
    # per-sample eps, then Psi and omega (cached by rounded eps -- a single-feed
    # print hits 1-2 keys; the cache keeps the J0 inversions off the hot path).
    vs = v_seg[samp_seg]
    eps_s = np.clip(np.where(vs > 0.0, vs / cts, 1.0), 0.0, 1.0)
    cache = {}
    psi_s = np.empty(nsamp, dtype=np.float64)
    om_s = np.empty(nsamp, dtype=np.float64)
    for i in range(nsamp):
        key = round(float(eps_s[i]), 6)
        hit = cache.get(key)
        if hit is None:
            hit = (model.coil_tangent_amplitude(key),
                   model.coil_angular_frequency(cts, key))
            cache[key] = hit
        psi_s[i], om_s[i] = hit
    # coil phase = cumulative integral of omega over time, FROZEN in the head-held
    # settle tail (no fibre deposited there, so the coil holds its last position).
    dts = np.diff(rt)
    moving = rt <= t[-1] + 1e-12
    om_phase = np.where(moving, om_s, 0.0)
    phase = np.concatenate([[0.0], np.cumsum(om_phase[:-1] * dts)])
    # closed-form bounded offset = R * sum_k c_k(Psi) sin(k*phase), R = CTS/omega the
    # coil radius (per-sample, so a calibrated speed-dependent clock still scales it;
    # 0 wherever the coil is inactive). c_k carry J_k(Psi) -> the offset vanishes as
    # Psi -> 0, leaving no residual (see the docstring).
    r_s = np.where(om_s > 0.0, cts / om_s, 0.0)
    along_off, perp_off = _assemble_coil_offsets(psi_s, phase, r_s)
    # unit tangent of the (smooth, rounded) trailing centre; carry the last good
    # direction through any near-stationary sample (start / settle tail) so the
    # frozen coil keeps its orientation instead of collapsing to the spine.
    dC = np.empty_like(contact[:, :2])
    dC[1:-1] = contact[2:, :2] - contact[:-2, :2]
    dC[0] = contact[1, :2] - contact[0, :2]
    dC[-1] = contact[-1, :2] - contact[-2, :2]
    cn = np.hypot(dC[:, 0], dC[:, 1])
    chx = np.empty(nsamp); chy = np.empty(nsamp)
    lx, ly = 1.0, 0.0
    for i in range(nsamp):
        if cn[i] > 1e-12:
            lx, ly = dC[i, 0] / cn[i], dC[i, 1] / cn[i]
        chx[i], chy[i] = lx, ly
    # world excursion = along*t_hat + perp*n_hat, n_hat = (-t_hat_y, t_hat_x)
    ex = along_off * chx - perp_off * chy
    ey = along_off * chy + perp_off * chx
    max_amp = float(np.max(np.hypot(ex, ey)))
    return ex, ey, max_amp


def _corner_vertices(p, v_seg):
    """Interval indices to refine into: the start (0) plus any interval that
    BEGINS at a sharp direction change or a marked speed change. Returns indices k
    such that the window [t[k], t[k+1]] should be filled (the post-event ring)."""
    n = len(p)
    corners = {0}                             # the start transient (lag builds)
    # direction change at interior vertex k (between interval k-1 and k): the
    # touchdown rounds/whips it, and that rings out along interval k.
    cos_thr = math.cos(math.radians(_CORNER_DEG))
    d = np.diff(p[:, :2], axis=0)
    seg_xy = np.linalg.norm(d, axis=1)
    for k in range(1, n - 1):
        a, b = seg_xy[k - 1], seg_xy[k]
        if a <= 1e-12 or b <= 1e-12:
            continue
        cosang = float(np.dot(d[k - 1], d[k]) / (a * b))
        if cosang < cos_thr:                  # turn sharper than _CORNER_DEG
            corners.add(k)
        elif (k < len(v_seg) and v_seg[k - 1] > 0.0
              and abs(v_seg[k] - v_seg[k - 1]) / v_seg[k - 1] > _SPEED_CHANGE_FRAC):
            corners.add(k)
    return sorted(corners)


def _cap_grid(rt, vertex_times, cap):
    """Thin a refined grid down to `cap` points while ALWAYS keeping the commanded
    vertex times (corners must stay exact). Drops fill points on the widest-budget
    uniform stride; a last resort for pathologically long fibres."""
    keep = np.isin(rt, vertex_times)
    fill_idx = np.flatnonzero(~keep)
    budget = cap - int(keep.sum())
    if budget <= 0:                           # vertices alone exceed the cap
        return rt[keep]
    if len(fill_idx) > budget:
        stride = len(fill_idx) / budget
        pick = fill_idx[(np.arange(budget) * stride).astype(np.int64)]
        keep[pick] = True
    else:
        keep[fill_idx] = True
    return rt[keep]


def _collapse_knots(ts, ds):
    """(t_knots, d_knots) float64 strictly increasing in time, or None if empty.

    np.interp wants strictly-increasing x, so exact-duplicate times are dropped
    (keeping the first), which also collapses fibre-boundary coincidences. Then the
    interior of each flat run goes: a knot whose diameter equals BOTH neighbours lies
    exactly on the piecewise-linear interp, so dropping it leaves every queried
    diameter unchanged while shrinking the lookup from one knot per segment to just
    the speed-change boundaries (a single-feed print -> 2 knots). Diameters within a
    run are byte-identical (the same cached `d_at` value), so `==` is exact."""
    if not len(ts):
        return None
    ts = np.asarray(ts, dtype=np.float64)
    ds = np.asarray(ds, dtype=np.float64)
    order = np.argsort(ts, kind="stable")
    ts, ds = ts[order], ds[order]
    keep = np.concatenate(([True], np.diff(ts) > 0.0))
    ts, ds = ts[keep], ds[keep]
    if len(ds) > 2:
        edge = (ds[1:-1] != ds[:-2]) | (ds[1:-1] != ds[2:])   # differs from a neighbour
        keep = np.concatenate(([True], edge, [True]))         # always keep the ends
        ts, ds = ts[keep], ds[keep]
    return ts, ds


def landing_speeds(laid_paths, laid_ptimes, frame=None):
    """(t_mid, landing speed) per LAID segment -- the rate the fibre actually reaches
    the collector, off the touchdown geometry itself.

    The nozzle speed and this differ only where the touchdown is in transient: a
    steady lag is a constant offset, so on a straight run the contact tracks the head
    exactly and the two are equal. At a start, a corner, a speed change and above all
    an immediate 180 deg reversal they are not -- through a reversal the contact
    stalls, reverses and then briefly outruns the head.

    Times are the laid path's own, i.e. the naive print clock `laid_fibers` stamps,
    which is the clock the `.depo`, the tube stage and the player all key on.
    Yields nothing for a path with no advancing segment."""
    for p, t in zip(laid_paths, laid_ptimes):
        p = np.asarray(p, dtype=np.float64)
        t = np.asarray(t, dtype=np.float64)
        if len(p) < 2:
            continue
        seg = np.linalg.norm(np.diff(_metric_path(p, frame), axis=0), axis=1)
        dt = np.diff(t)
        v = np.divide(seg, dt, out=np.zeros_like(seg), where=dt > 0.0)
        tm = 0.5 * (t[1:] + t[:-1])
        for k in range(len(v)):
            if v[k] > 0.0:
                yield float(tm[k]), float(v[k])


def _speed_ratio_curve(paths, ptimes, max_accel, square_corner_velocity,
                       entry_vs, exit_vs, frame=None, rotary_accel=0.0):
    """(t, v_effective / v_commanded) on the naive clock, or None when the accel
    limit is off/unavailable and the ratio is identically 1.

    `laid_fibers` reshapes with the accel-limited speed but stamps the OUTPUT on the
    naive clock, so a landing rate measured off that geometry is a naive-clock rate.
    This is the factor that puts it back on the head's real trajectory, which is what
    keeps the ramp bead the kinematics path exists for."""
    if plan_trajectory is None or max_accel is None or max_accel <= 0.0:
        return None
    ts, rs = [], []
    for idx, (p, t) in enumerate(zip(paths, ptimes)):
        p = np.asarray(p, dtype=np.float64)
        t = np.asarray(t, dtype=np.float64)
        if len(p) < 2:
            continue
        mp = _metric_path(p, frame)
        seg = np.linalg.norm(np.diff(mp, axis=0), axis=1)
        dt = np.diff(t)
        v_naive = np.divide(seg, dt, out=np.zeros_like(seg), where=dt > 0.0)
        v_eff = _effective_seg_speeds(
            mp, seg, v_naive, max_accel,
            square_corner_velocity,
            entry_v=(entry_vs[idx] if entry_vs is not None else 0.0),
            exit_v=(exit_vs[idx] if exit_vs is not None else 0.0),
            seg_accel=_seg_accel(p, mp, frame, max_accel, rotary_accel))
        r = np.divide(v_eff, v_naive, out=np.ones_like(v_naive), where=v_naive > 0.0)
        ts.extend(0.5 * (t[1:] + t[:-1]))
        rs.extend(r)
    if not ts:
        return None
    ts = np.asarray(ts, dtype=np.float64)
    rs = np.asarray(rs, dtype=np.float64)
    order = np.argsort(ts, kind="stable")
    ts, rs = ts[order], rs[order]
    keep = np.concatenate(([True], np.diff(ts) > 0.0))
    return ts[keep], rs[keep]


def diameter_by_time(paths, ptimes, model, *, pressure, voltage, temperature,
                     speed_scale=1.0, max_accel=None, square_corner_velocity=6.0,
                     loop_entry=None, entry_vs=None, exit_vs=None, frame=None,
                     laid_paths=None, laid_ptimes=None, rotary_accel=0.0):
    """Build a lookup of deposited fibre DIAMETER vs print time so the laid fibre
    can THICKEN/THIN along its length -- the deposited twin of the jet's own live
    diameter.

    The diameter is mass conservation d = sqrt(4Q/(pi v)) -- the SAME
    `JetState.fiber_diameter` the animated jet uses. Pressure and temperature enter
    through Q; voltage and elevation do not change d, but every knob goes through
    `model.solve`. `speed_scale` (default 1.0) reads the diameter at the EFFECTIVE
    speed v*speed_scale, so the march's export-relay branch sizes a sped-up layer
    exactly as `mew_live.diameter_schedule` would.

    WHICH SPEED, AND WHY IT IS THE LANDING ONE. Mass conservation is a statement
    about the fibre being LAID, not about the head: the melt arrives at Q whatever
    the head does, so Q = (pi/4) d^2 v_landing at the touchdown point. Pass
    `laid_paths`/`laid_ptimes` (the reshaped centrelines and their times) and the
    speed is read off that geometry -- `landing_speeds` -- instead of off the
    commanded one. A steady lag is a constant offset, so on every straight run the
    two speeds are equal and nothing moves; they part company only at starts,
    corners, speed changes and 180 deg reversals, which is exactly where the
    reshape acts. Keying on the head speed instead silently discards the melt that
    arrives while the touchdown is stalled -- on the sag-ladder repro that was 15.0%
    of the whole print (`test_mass_is_conserved_where_the_clamp_is_inactive`).

    THE SATURATION IS THE CTS ONSET, not a fudge. d -> infinity as v_landing -> 0,
    but the straight-fibre model stops holding below CTS: there the jet coils rather
    than laying an ever-thicker straight fibre. So d saturates at
    d_CTS = sqrt(4Q/(pi*CTS)) -- the identity `mew_calibration.cts_from_diameter`
    already states, no new constant. The melt arriving below CTS beyond what a d_CTS
    fibre carries is genuinely unplaceable by a straight-fibre model (it is the coil
    or buckle at the turn); it is ~1% of the ladder print and is NOT hidden -- it is
    what keeps the whole-path mass ratio just under 1.

    Omitting `laid_paths`/`laid_ptimes` keeps the legacy head-speed branch, which is
    byte-identical to the pre-mass-conservation build and is what the kinematics-bead
    guards below pin; no shipping caller takes it.

    With `max_accel` > 0 the speed is the accel-limited EFFECTIVE speed, so the fibre
    BEADS where the head really slows -- fibre ends, corners, tight turnaround arcs;
    None/<=0 (default) keeps the commanded feed, byte-identical. The speed is BUFFERED
    over the jet's lag distance before it becomes a diameter, because the jet does not
    change thickness instantly (`_buffer_speed`), and the buffer length is the
    calibrated lag, not a fitted constant. The resulting bead is MICRON-scale and
    buffer-smoothed, usually below eye resolution: read it off this lookup's range.

    One knot per moving segment at its MIDPOINT time (solved once per distinct rounded
    speed and cached), with flat runs collapsed to their boundaries. Returns
    (t_knots, d_knots) float64, strictly increasing in time, or None if no moving
    segment exists. Consumed at tube time as
    radius = 0.5 * interp(vertex_time, t_knots, d_knots)."""
    cache = {}

    def d_at(v):
        key = round(v, 6)
        d = cache.get(key)
        if d is None:
            d = model.solve(ProcessParams(
                pressure=pressure, voltage=voltage,
                speed=max(v, 1e-9), temperature=temperature)).fiber_diameter
            cache[key] = d
        return d

    # The jet's lag DISTANCE at a speed -- the arc-length over which the deposited
    # diameter relaxes to a new value (the jet buffers the change; Hrynevich 2020).
    # Used as the diameter buffer length, so the smoothing scale rides on the already-
    # calibrated lag law -- NOT a new fitted constant (no print sweep of its own).
    lag_cache = {}

    def lag_at(v):
        key = round(v, 6)
        lg = lag_cache.get(key)
        if lg is None:
            lg = model.solve(ProcessParams(
                pressure=pressure, voltage=voltage,
                speed=max(v, 1e-9), temperature=temperature)).lag_distance
            lag_cache[key] = lg
        return lg

    # Below CTS the straight-fibre diameter saturates (the jet coils rather than
    # laying an ever-thicker straight fibre, and the coil's own thickness change is
    # out of scope), so the sub-interval ramp toes near a stop are clamped at the CTS
    # speed -- bounding the start/end bead instead of letting d -> inf as v -> 0. Only
    # the kinematics path samples those low speeds, so the clamp is off otherwise (the
    # per-move knots never reach them -> behaviour stays byte-identical).
    landing = laid_paths is not None and laid_ptimes is not None
    clamp_cts = (landing or (plan_trajectory is not None
                             and max_accel is not None and max_accel > 0.0))
    cts = (model.critical_speed(
               voltage, model.working_distance,
               flow_rate=model.flow_rate(pressure, temperature),
               viscosity=model.viscosity(temperature))
           if clamp_cts else 0.0)
    clamp_cts = clamp_cts and cts > 0.0

    if landing:
        # THE LANDING-SPEED BRANCH. One knot per laid segment, at the speed the fibre
        # reaches the collector. The accel limit is folded back in as a ratio because
        # `laid_fibers` reshapes on the real trajectory but stamps the naive clock, so
        # a rate measured off that geometry is a naive-clock rate; with kinematics off
        # the ratio is identically 1 and nothing is applied. No `_buffer_speed` here:
        # the mass balance Q = (pi/4) d^2 v is exact AT the touchdown whatever the jet
        # did upstream, and low-passing it would break the identity it exists to hold.
        ratio = _speed_ratio_curve(paths, ptimes, max_accel,
                                   square_corner_velocity,
                                   entry_vs, exit_vs, frame=frame,
                                   rotary_accel=rotary_accel)
        ts, ds = [], []
        for t_mid, v_land in landing_speeds(laid_paths, laid_ptimes, frame=frame):
            v = v_land * speed_scale
            if ratio is not None:
                v *= float(np.interp(t_mid, ratio[0], ratio[1]))
            ts.append(t_mid)
            ds.append(d_at(max(v, cts) if clamp_cts else v))
        return _collapse_knots(ts, ds)

    # Boundary speeds so a linking-loop junction is planned as continuous motion
    # (no v->0 bead at the loop entry); 0 everywhere else (see `junction_speeds`).
    # A caller relaying ONE layer in isolation passes the whole-set values (see
    # `laid_fibers`); None computes them here, byte-identical to before.
    if entry_vs is None or exit_vs is None:
        entry_vs, exit_vs = junction_speeds(paths, ptimes, loop_entry,
                                            frame=frame)

    ts, ds = [], []
    for idx, (p, t) in enumerate(zip(paths, ptimes)):
        p = np.asarray(p, dtype=np.float64)
        t = np.asarray(t, dtype=np.float64)
        if len(p) < 2:
            continue
        metric_p = _metric_path(p, frame)
        seg_len = np.linalg.norm(np.diff(metric_p, axis=0), axis=1)
        dt = np.diff(t)
        v_seg = np.divide(seg_len, dt, out=np.zeros_like(seg_len), where=dt > 0.0)
        # diameter at the head's REAL speed -- thicker where it slows (corners, tight
        # arcs, fibre ends). With kinematics ON, _diam_speed_samples samples the accel
        # / decel RAMPS sub-interval (not one mean per commanded move) and maps them
        # onto the naive clock, so a long ramping move beads at its slow ENDS instead
        # of thickening uniformly; OFF, one knot per move at the commanded feed
        # (byte-identical). Cruise stays a single sample, so flat runs still collapse.
        # buffer_len: the jet buffers the diameter change over ~its lag distance (at the
        # fibre's CRUISE feed), so a sharp corner dip barely beads while a long feed
        # grade does (Hrynevich 2020). 0 when kinematics off -> byte-identical.
        cruise_v = float(v_seg.max()) if v_seg.size else 0.0
        buf_len = (lag_at(cruise_v * speed_scale)
                   if (clamp_cts and cruise_v > 0.0) else 0.0)
        for t_mid, v in _diam_speed_samples(metric_p, t, seg_len, v_seg, max_accel,
                                            square_corner_velocity,
                                            entry_v=entry_vs[idx],
                                            exit_v=exit_vs[idx],
                                            buffer_len=buf_len,
                                            seg_accel=_seg_accel(
                                                p, metric_p, frame, max_accel,
                                                rotary_accel)):
            # mass conservation reads the EFFECTIVE (scaled) speed -- a faster what-if
            # lays a thinner fibre (d ~ v^-0.5); the buffer above low-passes the raw
            # kinematic speed and the scale is applied after, matching mew_live.
            # diameter_schedule. scale == 1.0 is byte-identical (v*1.0 == v).
            v_eff = v * speed_scale
            ts.append(t_mid)
            ds.append(d_at(max(v_eff, cts) if clamp_cts else v_eff))
    return _collapse_knots(ts, ds)


def make_transform(model, *, pressure, voltage, temperature, zeta=1.0,
                   dynamic_diameter=False, coil=False,
                   coil_wave_amp=0.0, coil_wave_len=0.3, max_accel=None,
                   square_corner_velocity=6.0, simulate_travel=False,
                   fill_per_tau=6.0, corner_chord=None, log=None,
                   reuse_repeats=False, rotary_accel=0.0):
    """Wrap `laid_fibers` into the `(paths, ptimes) -> ...` callable that
    `gcode_to_fibers`'s `_FIBER_TRANSFORM` seam expects.

    simulate_travel installs the CONTINUOUS-JET transform instead (default off): the
    returned callable takes a THIRD argument, `travels` from
    `parse_gcode(with_travels=True)`, so install it with
    `set_fiber_transform(t, with_travels=True)`. It lays the travels as real deposited
    fibre, carries the contact state into the fibre after each one, and MERGES each
    laid travel into the FRONT of that fibre -- which is what keeps the seam's
    fibre-count contract, the converter's grouping asserting sum(layers)==len(paths),
    and is where this differs from `laid_fibers(simulate_travel=True)`, which keeps
    the travels separate for callers that slice fibres positionally.

    `log`, if given, is called once with a one-line summary for the GUI log, and the
    `info` dict is dropped so the converter pipeline sees a plain tuple.

    fill_per_tau is the resolution the TURNAROUND CREST is drawn at: the grid is
    uniform in TIME while the contact runs FASTER than the nozzle through a turn, so
    the same step buys less arc at the tightest bend. Doubling it halves the worst
    laid turn -- 38.4 deg -> 22.6 deg -> 11.5 deg at fill 6 -> 12 -> 24, on a 10x10 /
    stepover 1 / 500 mm-min scaffold -- and doubles the point count and time with it
    (`test_mew_deposition.test_corner_resolution_sharpens_the_turnaround_crest`).

    dynamic_diameter adds a (t_knots, d_knots) diameter-vs-time lookup as a 3rd return
    element (`diameter_by_time`); off (default) it is a plain (paths, ptimes) 2-tuple.

    reuse_repeats is `laid_fibers`' own switch, passed straight through: the live
    preview's, which lays each repeated layer once (`_RepeatLays`)."""
    def transform(paths, ptimes, travels=None, *, frame=None):
        # The converter owns the rotary-frame decision, then hands it through the
        # opt-in transform seam.  Re-attach this run's working distance to the
        # frame: ``gcode_to_fibers`` needs the geometry only, whereas the
        # cylindrical contact model also needs the nozzle-to-surface gap.
        # ``with_nozzle_gap`` copies rather than rebuilding a named subclass, so
        # a frame family this code has not met yet still arrives intact. Flat
        # calls retain ``placement=None`` and therefore the plain
        # byte-identical branch.
        placement = None
        if frame is not None and getattr(frame, "rotary", False):
            import mew_mandrel
            contact_frame = frame.with_nozzle_gap(model.working_distance)
            placement = mew_mandrel.placement_for_frame(
                contact_frame, model.calibration, model=model)
        # A rotary lay runs on the MANDREL's amplitude and CTS when the profile
        # carries them (`mew_lag_fit.mandrel_calibration`); a flat lay keeps
        # `model` untouched, which is what keeps a mandrel calibration from
        # moving any flat print.
        lay_model = model
        if placement is not None:
            import mew_lag_fit
            lay_model = mew_lag_fit.mandrel_model(model)
        # Which fibres begin with a layer-change linking loop (detected on the
        # commanded centrelines, before reshaping rounds the closure). Threaded into
        # both the lag and the diameter so the head flows THROUGH the loop junction
        # at speed instead of stopping there (no spurious v->0 bead at the entry).
        loop_entry = loop_entry_flags(paths)
        # Stale-proofing: cleared BEFORE the lay, so a caller that reads this after
        # a raising call, or after a call that laid nothing, cannot pick up the
        # grid of a previous one.
        transform.sag_grid = None
        out_paths, out_ptimes, info = laid_fibers(
            paths, ptimes, lay_model, pressure=pressure, voltage=voltage,
            temperature=temperature, zeta=zeta, coil=coil,
            coil_wave_amp=coil_wave_amp, coil_wave_len=coil_wave_len,
            max_accel=max_accel,
            square_corner_velocity=square_corner_velocity,
            rotary_accel=rotary_accel, loop_entry=loop_entry,
            fill_per_tau=fill_per_tau, corner_chord=corner_chord,
            travels=(travels if simulate_travel else None),
            simulate_travel=simulate_travel, placement=placement, frame=frame,
            reuse_repeats=reuse_repeats)
        n_travel = 0
        if simulate_travel:
            # Splice each LAID travel onto the front of the fibre it fed. The
            # touchdown is continuous across the junction (the fibre inherited the
            # travel's end state), so the joined path has no seam -- drop the shared
            # vertex. Fibre count and order are untouched.
            tvp, tvt = info["travel_paths"], info["travel_ptimes"]
            tvs, sag_t = info["travel_sag_times"], info["sag_times"]
            for i in range(len(out_paths)):
                if tvp[i] is None:
                    continue
                n_travel += 1
                out_paths[i] = np.concatenate([tvp[i], out_paths[i][1:]], axis=0)
                out_ptimes[i] = np.concatenate([tvt[i], out_ptimes[i][1:]])
                # the seed grids splice the same way. The fibre's own grid always
                # starts at its first vertex time -- the shared junction the concat
                # above drops -- so drop it here too and the subset stays a subset.
                sag_t[i] = np.concatenate([tvs[i], sag_t[i][1:]])
            # the trailing travel (the exit, index len(paths)) leads OUT of the
            # last fibre: onto its END, sharing the fibre's last vertex
            if len(tvp) > len(out_paths) and tvp[-1] is not None and out_paths:
                n_travel += 1
                out_paths[-1] = np.concatenate([out_paths[-1], tvp[-1][1:]],
                                               axis=0)
                out_ptimes[-1] = np.concatenate([out_ptimes[-1], tvt[-1][1:]])
                sag_t[-1] = np.concatenate([sag_t[-1], tvs[-1][1:]])
            # the diameter lookup is keyed on the global print clock, which already
            # spans the travels -- so feed it the COMMANDED merged geometry too, or
            # the travel stretches would carry no diameter knots at all
            paths, ptimes = merge_travels(paths, ptimes, travels)
            loop_entry = loop_entry_flags(paths)
        # The commanded geometry still comes in: it carries the accel-limited speed
        # ratio and the loop-entry junction speeds. The LAID geometry is what sets the
        # diameter, because mass conservation is a statement about the landing speed.
        diam_lookup = (diameter_by_time(
            paths, ptimes, lay_model, pressure=pressure, voltage=voltage,
            temperature=temperature, max_accel=max_accel,
            square_corner_velocity=square_corner_velocity, loop_entry=loop_entry,
            frame=frame, laid_paths=out_paths, laid_ptimes=out_ptimes,
            rotary_accel=rotary_accel)
            if dynamic_diameter else None)
        if log is not None:
            if info.get("cts_invalid"):
                msg = ("  physics deposition: CTS <= 0 (check calibration) -- "
                       "left all fibres as commanded")
            else:
                msg = (f"  physics deposition: reshaped {info['n_fibers']} "
                       f"fibre(s), max lag {info['max_lag_distance'] * 1000:.0f} um")
                if simulate_travel:
                    msg += (f"; continuous jet: {n_travel} travel(s) deposited "
                            f"and their lag state carried into the next fibre")
                if info["n_coiling"] and coil:
                    msg += (f"; {info['n_coiling']} below CTS coiled "
                            f"(sewing machine, max coil "
                            f"{info['max_coil_amplitude'] * 1000:.0f} um)")
                elif info["n_coiling"]:
                    msg += (f"; {info['n_coiling']} below CTS left as commanded "
                            f"(coiling regime -- enable Coiling to simulate)")
                if info.get("n_coiling_partly"):
                    msg += (f" -- {info['n_coiling_partly']} of them only where "
                            f"the feed drops below CTS, lagged elsewhere")
                if diam_lookup is not None:
                    msg += (f"; dynamic diameter "
                            f"{diam_lookup[1].min() * 1000:.1f}-"
                            f"{diam_lookup[1].max() * 1000:.1f} um")
            log(msg)
            sat = info.get("lag_arc_saturated")
            if sat:
                where = ("from %.1f mm/s upward" % sat["min_speed"]
                         if sat["min_speed"] else "at every speed")
                log("  mandrel contact arc SATURATED at the tangency bound "
                    "%.1f deg, %s (%d interval(s)). Past that bound a jet "
                    "cannot reach, so the deviation is an upper bound there, "
                    "not a prediction -- see MANDREL_LAG_DERIVATION.md 12.5."
                    % (sat["arc_deg"], where, sat["n"]))
        # The SAG GRID, published as an attribute rather than a fourth tuple
        # element: `gcode_to_fibers`' seam contract is a 2- or 3-tuple and six
        # other callers unpack it positionally (gui_preview, gui_mandrel_preview,
        # gui_sweeps, sag_bench, harness_deviation, the diagnostics), so widening
        # the tuple would break every one of them for a hint that is optional by
        # design. None unless the adaptive corner grid actually ran: with it off
        # the seed grid IS the output grid, and a consumer that took the decoupled
        # path anyway would do the same work through more code.
        transform.sag_grid = (info["sag_times"] if corner_chord is not None
                              else None)
        if diam_lookup is not None:
            return out_paths, out_ptimes, diam_lookup
        return out_paths, out_ptimes
    transform.sag_grid = None
    return transform
