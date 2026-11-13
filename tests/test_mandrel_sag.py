"""The sag solver on a mandrel.

Three things land in phase 4 and this suite holds all three to their own bar: the
periodic SEAM being wired through (without a period a tube solves as a flat strip
and every layer falls through the mandrel to the bed), TERM C the metric, and
TERM B the normal curvature of a chord below the design surface.

COMPARATORS -- not one of them is this file's own arithmetic:

  * OFF: `apply_sag(frame=None)` against `apply_sag(frame=PlanarFrame())`, byte
    for byte.
  * TERM C: the ratio of the droop's SLOPE in `sag_multiplier`, cylinder over
    plane, against `cos^2(psi) + (metric_v(h)*sin(psi))^2` -- a length, from the
    geometry of the unroll.
  * TERM B: the droop's INTERCEPT in `sag_multiplier` against
    `r*(1 - cos(dphi/2))`, the EXACT depth of a chord below a circle, which the
    solver's leading-order form must approach as the mandrel grows.

The parabola is INVERTED per run (`depth = (chord - z)/(4t(1-t))`) rather than
read off the sample nearest midspan, because at 45 degrees the flat run has no
midspan sample at all and reading the nearest one moved a slope ratio by 12%; a
straight line across multipliers then separates the two terms, since the droop
scales with `sag_multiplier` and the chord drop does not.

RED CONTROLS (`test_the_ladder_can_fail`, run every time): a cylinder frame with
`k2` forced to 0 must FAIL the term-B bar, and the same tube with the period
taken away must fall onto the bed. Both swing.

NOT COVERED: `mew_live` / `mew_incremental` mirror `_lay_one` rather than calling
it, so the live march is still FLAT-ONLY -- a live/locked divergence on a mandrel
is that, not this.
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math
import sys

import numpy as np

import gcode_to_fibers as gf
import mew_frame
import scaffold_gcode
import mew_tube
from mew_frame import CylinderFrame, PlanarFrame

# Probe files (g-code, STLs and the .depo caches the converter writes beside
# them) go to a scratch directory that is removed at exit -- not the repo root,
# where they sat untracked after every run until 2026-09-01.
import atexit
import os
import shutil
import tempfile

_SCRATCH = tempfile.mkdtemp(prefix="mandrel_sag_")
atexit.register(shutil.rmtree, _SCRATCH, ignore_errors=True)


def _tmp(name):
    """Absolute scratch path for a probe file named `name`."""
    return os.path.join(_SCRATCH, name)

_PASS, _FAIL = [], []


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       ("   " + detail) if detail else ""))


DIAM = 0.02
#: Where the ladder's spanning fibre SEATS, as opposed to where it is commanded.
#: The commanded Z of a layer is the surface that layer lands on, so layer 0
#: (commanded h = 0) rests its underside on the bare mandrel and its CENTRELINE
#: stands one radius off it; the spanning fibre, commanded at h = DIAM, rests on
#: layer 0 at the contact distance -- one full diameter above that centreline.
#: Every reference height in this file is the seat, never the commanded Z. The
#: two were the same number until the bed datum was fixed (`bed_at`), which is
#: why one symbol used to serve both.
SEAT = 1.5 * DIAM
SPAN = 0.15                    # short enough that the droop never reaches the bed
MULTS = (0.4, 1.0)


# --------------------------------------------------------------------------- #
# the ladder: the only shape in which a droop law and a chord drop are visible
# --------------------------------------------------------------------------- #
def ladder(span, psi_deg, u0=1.0, voff=0.0):
    """Two support fibres in layer 0, one fibre crossing both in layer 1.

    The two crossings are the spanning fibre's only supports, so the stretch
    between them is genuinely unsupported. `psi_deg` is that fibre's helix angle
    FROM u: 90 = circumferential, 0 = axial. Returned in the surface frame
    (u, v, h), which is what `parse_gcode(frame=...)` produces.
    """
    c, s = math.cos(math.radians(psi_deg)), math.sin(math.radians(psi_deg))
    px, py = -s, c
    out = []
    for k in (0.0, span):
        cx, cy = u0 + c * k, voff + s * k
        out.append(np.array([[cx - px, cy - py, 0.0], [cx + px, cy + py, 0.0]]))
    lead = 0.4 * span
    out.append(np.array([[u0 - c * lead, voff - s * lead, DIAM],
                         [u0 + c * (span + lead), voff + s * (span + lead), DIAM]]))
    return out


def span_depth(span, psi_deg, frame, mult, voff=0.0, lo=0.2, hi=0.8):
    """Midspan droop of the ladder's span, inverted from the parabola.

    Returns (depth, spread) -- the spread over the interior samples is 0 when
    the span really is the single clean parabola the solver draws, and grows
    when the fixture has picked up an extra support. A caller that ignores it
    can fit a straight line through a contaminated measurement and never know.
    """
    p = np.asarray(gf.apply_sag(ladder(span, psi_deg, voff=voff), DIAM,
                                sag_multiplier=mult, frame=frame)[-1],
                   dtype=np.float64)
    c, s = math.cos(math.radians(psi_deg)), math.sin(math.radians(psi_deg))
    t = ((p[:, 0] - 1.0) * c + (p[:, 1] - voff) * s) / span
    inner = (t > lo) & (t < hi)
    if not inner.any():
        return None, 0.0
    d = (SEAT - p[inner, 2]) / (4.0 * t[inner] * (1.0 - t[inner]))
    return float(d.mean()), float(d.max() - d.min())


def split_terms(span, psi_deg, frame, voff=0.0):
    """(slope, intercept, spread) of depth against `sag_multiplier`."""
    got = [span_depth(span, psi_deg, frame, m, voff) for m in MULTS]
    if any(g[0] is None for g in got):
        return None
    (d0, s0), (d1, s1) = got
    slope = (d1 - d0) / (MULTS[1] - MULTS[0])
    return slope, d1 - slope * MULTS[1], max(s0, s1)


def exact_chord(radius, span, psi_deg, h=SEAT):
    """Depth of the straight chord below the deposition surface -- circle
    geometry, no appeal to the solver. Two points on a cylinder of radius `r`
    whose azimuths differ by `dphi` are joined by a chord whose midpoint sits at
    `r*cos(dphi/2)`, and only the CIRCUMFERENTIAL component of the span opens an
    azimuth at all -- which is why this goes to zero on an axial span."""
    r = radius + h
    dphi = span * math.sin(math.radians(psi_deg)) / radius
    return r * (1.0 - math.cos(0.5 * dphi))


def metric_factor(frame, psi_deg, h=SEAT):
    """(true span / unrolled span)^2 at helix angle psi -- term C, squared
    because the droop law goes as span^2."""
    m = frame.metric_v(h)
    c, s = math.cos(math.radians(psi_deg)), math.sin(math.radians(psi_deg))
    return c * c + (m * s) ** 2


# --------------------------------------------------------------------------- #
# the OFF bar
# --------------------------------------------------------------------------- #
def _flat_scaffold(tmp=_tmp("_p4_flat.gcode"), **kw):
    p = dict(width=1.5, height=1.5, stepover=0.15, rotation=90.0, layers=4,
             fiber_diameter=DIAM, speed=600.0)
    p.update(kw)
    open(tmp, "w").write(scaffold_gcode.generate_scaffold_gcode(**p))
    return gf.parse_gcode(tmp, with_times=True)


def test_a_planar_frame_is_the_no_op():
    paths, ptimes = _flat_scaffold()
    a, at = gf.apply_sag(paths, DIAM, point_times=ptimes)
    b, bt = gf.apply_sag(paths, DIAM, point_times=ptimes, frame=PlanarFrame())
    same = (len(a) == len(b)
            and all(x.shape == y.shape and x.tobytes() == y.tobytes()
                    for x, y in zip(a, b))
            and all(x.tobytes() == y.tobytes() for x, y in zip(at, bt)))
    check("a PlanarFrame solve is byte-identical to frame=None", same,
          "%d fibres, %d points" % (len(a), sum(len(p) for p in a)))
    check("...and a PlanarFrame has nothing for the two terms to act on",
          PlanarFrame().k1 == 0.0 and PlanarFrame().k2 == 0.0
          and PlanarFrame().period_v is None and PlanarFrame().metric_v(1.0) == 1.0)


def test_a_flat_scaffold_with_loops_is_unchanged():
    """The self-contact pass reads its candidates through the index's stored
    pieces now, not from `xo`/`yo` -- so the one flat shape that exercises it
    (a linking-loop turnaround crossing its own earlier strand) has to be
    byte-identical too."""
    paths, ptimes = _flat_scaffold(tmp=_tmp("_p4_loops.gcode"), link_loops="auto")
    a = gf.apply_sag(paths, DIAM, point_times=ptimes)[0]
    b = gf.apply_sag(paths, DIAM, point_times=ptimes, frame=PlanarFrame())[0]
    cross = any(gf._self_crosses(np.asarray(p, dtype=np.float64),
                                 gf._hash_cell(DIAM)) for p in paths)
    check("a self-crossing flat fibre is byte-identical under a PlanarFrame",
          all(x.tobytes() == y.tobytes() for x, y in zip(a, b)))
    check("...and that scaffold really does self-cross (else nothing was tested)",
          cross)


# --------------------------------------------------------------------------- #
# the seam, wired
# --------------------------------------------------------------------------- #
def _tube(radius=1.5, layers=4, angle=45.0, length=12.0):
    gc = scaffold_gcode.generate_mandrel_gcode(2.0 * radius, length, angle,
                                               DIAM, layers, 600.0)
    open(_tmp("_p4_tube.gcode"), "w").write(gc)
    frame = CylinderFrame(radius)
    return gf.parse_gcode(_tmp("_p4_tube.gcode"), frame=frame), frame


def test_a_tube_layer_crosses_itself_only_under_a_period():
    """The lattice is a CIRCUIT crossing itself once v is periodic.

    A circuit, not a parsed layer: since 2026-09-03 the nozzle steps out per
    PLY, so a path break falls at the half-circuit boundary and one parsed
    layer is a single fibre family. A single family never crosses itself --
    that is what makes it a family -- so the two are joined back together
    here. The controls below check both halves of that: the pair crosses, one
    ply alone does not."""
    paths, frame = _tube()
    cell = gf._hash_cell(DIAM)
    ply = np.asarray(paths[0], dtype=np.float64)
    # drop ply 1's first point: the parser re-anchors each path at the
    # position the previous one ended, so joining them verbatim leaves a
    # duplicated vertex and a zero-length segment, which reads as a crossing.
    circuit = np.vstack([ply, np.asarray(paths[1], dtype=np.float64)[1:]])
    check("a tube circuit does NOT self-cross in the unwrapped frame",
          not gf._self_crosses(circuit, cell))
    check("...and DOES once v is periodic -- that crossing is the lattice",
          gf._self_crosses(circuit, cell, frame.period_v))
    cum = gf._arc_lengths(circuit)[0]
    n = len(gf._self_intersections(circuit, cum, cell, frame.period_v))
    check("...with more than one crossing found", n > 1, "%d crossings" % n)


def test_a_tube_builds_a_wall_instead_of_collapsing():
    paths, frame = _tube()
    flat = gf.apply_sag(paths, DIAM)
    curved = gf.apply_sag(paths, DIAM, frame=frame)
    top_flat = max(float(p[:, 2].max()) for p in flat)
    tops = [float(p[:, 2].max()) for p in curved]
    check("solved with no frame every layer falls to the bed",
          top_flat <= SEAT + 1e-9, "top h = %.5f mm" % top_flat)
    check("...and with the frame the layers stack",
          tops[-1] > tops[0] >= 0.0 and all(
              b > a for a, b in zip(tops[:-1], tops[1:])),
          "layer tops %s mm" % ["%.4f" % t for t in tops])
    lows = [float(p[:, 2].min()) for p in curved]
    # Ply 1 is the opposite family: it crosses ply 0 only at the lattice
    # points and sags back to the mandrel between them, exactly as a flat
    # layer 2 touches the bed on a coarse enough stepover. From ply 2 on
    # there is a full family underneath everywhere, so nothing reaches it.
    # "reaches the bare mandrel" is the fibre's UNDERSIDE touching it, which
    # puts its centreline one radius up -- 0.5 * DIAM, not 0.
    check("...and only plies 0 and 1 reach the bare mandrel",
          lows[0] <= 0.5 * DIAM + 1e-9
          and (len(lows) < 3 or min(lows[2:]) >= SEAT - 1e-9),
          "layer floors %s mm" % ["%.4f" % t for t in lows])


def test_no_deposited_point_sits_inside_the_mandrel():
    """The radial twin of `shots.json`'s flat `underside_clearance_mm`. On a
    mandrel the flat check reads as passing whatever the geometry does.

    Measured at the fibre's UNDERSIDE -- centre minus one radius -- because that
    is what touches the collector, and it is the half of the question the
    centreline cannot answer. Until the bed datum was fixed this ladder's bottom
    ply rested its CENTRE on the bare surface: the centreline bar below passed
    with `delta` exactly 0 while half of every bottom fibre was inside the
    mandrel, which is the same defect `underside_clearance_mm` read as
    -0.0109 mm on the plate. RED control: reverting `bed_at`'s lift puts
    `worst` at R0 - DIAM/2 and fails the first bar, not the second."""
    paths, frame = _tube()
    worst = None
    worst_centre = None
    for p in gf.apply_sag(paths, DIAM, frame=frame):
        r = frame.radius + np.asarray(p, dtype=np.float64)[:, 2]
        lo = float(r.min())
        worst_centre = lo if worst_centre is None else min(worst_centre, lo)
        lo -= 0.5 * DIAM
        worst = lo if worst is None else min(worst, lo)
    check("no deposited fibre's UNDERSIDE lies inside the bare mandrel radius",
          worst >= frame.radius - 1e-9,
          "min underside radius %.6f mm vs R0 %.6f (delta %+.6f)"
          % (worst, frame.radius, worst - frame.radius))
    check("...and the bottom ply seats ON it, not above it",
          worst <= frame.radius + 0.05 * DIAM,
          "min underside radius %.6f mm vs R0 %.6f" % (worst, frame.radius))
    check("...so its CENTRELINE stands one radius off the surface",
          abs(worst_centre - (frame.radius + 0.5 * DIAM)) <= 0.05 * DIAM,
          "min centre radius %.6f mm vs R0 + r %.6f"
          % (worst_centre, frame.radius + 0.5 * DIAM))


def test_the_wrap_is_a_projection_at_the_seam():
    """`np.mod(-1e-18, P)` returns exactly `P`, one past a half-open range. The
    fold has to be idempotent or `rest_heights` measures at `P` while
    `point_pairs` looks up at 0 -- the two ends of the seam. Found by phase 4's
    own ladder: a support at v = -1e-18 read 0.000 mm where the same contact at
    v = +1e-18 read 0.020, and the span that lost it drooped onto the bed."""
    frame = CylinderFrame(1.5)
    idx = gf._SegIndex(gf._hash_cell(DIAM), period=frame.period_v)
    idx.add_fiber(np.array([[1.866, -0.5, 0.0], [0.134, 0.5, 0.0]]), 0.5 * DIAM)
    got = [float(idx.rest_heights(np.array([1.0]), np.array([v]),
                                  0.5 * DIAM, 0.0)[0])
           for v in (1e-18, 0.0, -1e-18, -1e-15, -1e-9)]
    check("a contact a hair either side of the seam rests at one diameter",
          all(abs(g - DIAM) < 1e-12 for g in got),
          "%s" % ["%.5f" % g for g in got])
    y = idx._wrap_q(np.array([-1e-18, 0.0, frame.period_v]))
    check("...because the fold is idempotent",
          np.array_equal(idx._wrap_q(y), y) and float(y.max()) < frame.period_v)


# --------------------------------------------------------------------------- #
# term C -- the metric
# --------------------------------------------------------------------------- #
#: (R0, psi). psi = 20 is deliberately absent: at that angle the ladder's own
#: support fibres run close enough alongside the span to pick up an extra
#: contact, which shows up as a non-zero interior spread (3e-4 mm) and a flat
#: slope of 0.01162 where the clean parabola gives 0.01125. That is the FIXTURE,
#: not the solver -- but a measurement whose control says it is contaminated
#: does not belong in an assertion.
CASES = [(0.75, 90), (0.75, 60), (0.75, 45),
         (1.5, 90), (1.5, 60), (1.5, 45),
         (11.0, 90), (11.0, 60), (11.0, 45)]


def test_term_c_scales_the_span_by_the_metric():
    worst = 0.0
    clean = True
    for radius, psi in CASES:
        cyl = CylinderFrame(radius)
        fp = split_terms(SPAN, psi, PlanarFrame())
        fc = split_terms(SPAN, psi, cyl)
        want = metric_factor(cyl, psi)
        got = fc[0] / fp[0]
        worst = max(worst, abs(got - want) / want)
        clean = clean and max(fp[2], fc[2]) < 1e-9
    check("the droop's span scales as cos^2 + (metric*sin)^2, every case",
          worst < 1e-4, "worst relative error %.2e over %d cases"
          % (worst, len(CASES)))
    check("...measured on a clean parabola (no stray support in the fixture)",
          clean)


def test_an_axial_span_sees_neither_term():
    cyl = CylinderFrame(1.5)
    fp = split_terms(SPAN, 0, PlanarFrame())
    fc = split_terms(SPAN, 0, cyl)
    check("an axial span droops exactly as it does on a plane",
          abs(fc[0] - fp[0]) < 1e-12 and abs(fc[1] - fp[1]) < 1e-12,
          "slope %.9f vs %.9f, intercept %.2e vs %.2e"
          % (fc[0], fp[0], fc[1], fp[1]))
    check("...because the normal curvature along the axis is zero",
          abs(cyl.normal_curvature(0.0)) < 1e-15
          and abs(cyl.chord_drop(SPAN, 0.0)) < 1e-15)


# --------------------------------------------------------------------------- #
# term B -- the chord
# --------------------------------------------------------------------------- #
def test_term_b_is_the_chord_below_the_circle():
    rows = []
    for radius, psi in CASES:
        cyl = CylinderFrame(radius)
        got = split_terms(SPAN, psi, cyl)[1]
        want = exact_chord(radius, SPAN, psi)
        rows.append((radius, psi, got, want, (got - want) / want))
    worst = max(abs(r[4]) for r in rows)
    check("the chord drop matches the exact circular chord",
          worst < 0.03,
          "worst %.2f%% (%.2f mm mandrel, psi %d)"
          % (100.0 * worst, 2.0 * max(rows, key=lambda r: abs(r[4]))[0],
             max(rows, key=lambda r: abs(r[4]))[1]))
    # the solver's form is `k_n*s^2/8`, the leading term of R(1 - cos(s/2R)), so
    # the agreement has to IMPROVE as the mandrel grows -- that is the signature
    # of a leading-order expansion rather than of a coincidence
    at90 = {r[0]: abs(r[4]) for r in rows if r[1] == 90}
    check("...and converges as the mandrel grows",
          at90[11.0] < at90[1.5] < at90[0.75],
          "err %.3f%% / %.3f%% / %.3f%% at R0 = 0.75 / 1.5 / 11 mm"
          % (100 * at90[0.75], 100 * at90[1.5], 100 * at90[11.0]))


def test_the_span_free_ratio_is_the_plans():
    """Because both terms go as span^2 the ratio is
    span-free, `chord/droop = sin^2(psi)/(8*R*c)` with `c = SAG_COEFF/diameter`.
    Checked at psi = 90 on more than one mandrel, as phase 4's bar (b) asks."""
    c = gf.SAG_COEFF / DIAM
    rows = []
    for radius in (0.75, 1.5, 11.0):
        cyl = CylinderFrame(radius)
        slope, inter, _ = split_terms(SPAN, 90, cyl)
        got = inter / slope                       # chord / droop at multiplier 1
        want = 1.0 / (8.0 * (radius + SEAT) * c)  # r LOCAL: at the fibre's seat
        rows.append((radius, got, want, (got - want) / want))
    worst = max(abs(r[3]) for r in rows)
    check("chord/droop equals sin^2(psi)/(8*r*c) at psi = 90",
          worst < 0.03,
          "  ".join("R0=%.2f %.4f vs %.4f" % (r[0], r[1], r[2]) for r in rows))


# --------------------------------------------------------------------------- #
# the controls: this fixture must be able to report a defect
# --------------------------------------------------------------------------- #
def test_the_ladder_can_fail():
    """Two injections, each the shape of a real phase-4 mistake."""
    flat_cyl = CylinderFrame(1.5)
    flat_cyl.k2 = 0.0                    # a cylinder that forgot it is curved
    inter = split_terms(SPAN, 90, flat_cyl)[1]
    want = exact_chord(1.5, SPAN, 90)
    check("CONTROL: k2 = 0 fails the term-B bar",
          abs(inter - want) / want > 0.03,
          "intercept %.6f vs exact chord %.6f" % (inter, want))
    metric_only = CylinderFrame(1.5)
    metric_only.radius = 1e9             # a metric that forgot the wall
    check("CONTROL: a flattened metric fails the term-C bar",
          abs(metric_factor(metric_only, 90) - metric_factor(CylinderFrame(1.5), 90))
          > 1e-4)
    paths, frame = _tube()
    curved = gf.apply_sag(paths, DIAM, frame=frame)
    naked = gf.apply_sag(paths, DIAM)    # the period taken away
    check("CONTROL: the same tube with no period falls onto the bed",
          max(float(p[:, 2].max()) for p in naked)
          < 0.5 * max(float(p[:, 2].max()) for p in curved),
          "top h %.5f vs %.5f mm"
          % (max(float(p[:, 2].max()) for p in naked),
             max(float(p[:, 2].max()) for p in curved)))


def test_the_approximate_solver_keeps_a_mandrel_wall():
    paths, frame = _tube()
    exact = gf.apply_sag(paths, DIAM, frame=frame)
    approx = gf.apply_sag_approx(paths, DIAM, frame=frame)
    e_top = max(float(p[:, 2].max()) for p in exact)
    a_top = max(float(p[:, 2].max()) for p in approx)
    check("approximate sag keeps the cylindrical wall at the exact solver's height",
          abs(a_top - e_top) < 2e-3,
          "exact %.5f vs approximate %.5f mm" % (e_top, a_top))
    e_mean = [float(p[:, 2].mean()) for p in exact]
    a_mean = [float(p[:, 2].mean()) for p in approx]
    check("...and its per-layer wall profile stays close to the exact solve",
          max(abs(a - e) for a, e in zip(a_mean, e_mean)) < 2e-3,
          "mean |dh| %.5f mm" % max(abs(a - e) for a, e in zip(a_mean, e_mean)))
    naked = gf.apply_sag_approx(paths, DIAM)
    naked_top = max(float(p[:, 2].max()) for p in naked)
    check("CONTROL: taking away the period collapses the approximate wall",
          naked_top < 0.5 * a_top,
          "without %.5f vs periodic %.5f mm" % (naked_top, a_top))
    flatpaths, _pt = _flat_scaffold(tmp=_tmp("_p4_approx.gcode"), layers=3)
    a = gf.apply_sag_approx(flatpaths, DIAM)
    b = gf.apply_sag_approx(flatpaths, DIAM, frame=PlanarFrame())
    check("...and a flat frame passes through it unchanged",
          all(x.tobytes() == y.tobytes() for x, y in zip(a, b)))


# --------------------------------------------------------------------------- #
# the export path: a tube reaches an STL and a .depo, wrapped
# --------------------------------------------------------------------------- #
def test_the_wrap_carries_the_times_with_it():
    """`wrap_path` densifies -- a segment straight in (u, v) is a helix in space
    and its endpoints alone cut a chord through the collector. A caller that
    wrapped the geometry and kept the old times would hand the reveal two arrays
    of different lengths and misalign silently."""
    frame = CylinderFrame(1.5)
    pts = np.array([[0.0, 0.0, 0.0], [10.0, 9.4, 0.0], [20.0, 18.8, 0.0]])
    t = np.array([0.0, 1.0, 3.0])
    w, wt = mew_frame.wrap_path(frame, pts, t)
    check("the wrap densifies across the curvature", len(w) > len(pts),
          "%d -> %d points" % (len(pts), len(w)))
    check("...times stay parallel and monotonic",
          len(wt) == len(w) and bool(np.all(np.diff(wt) >= -1e-12))
          and abs(wt[0] - t[0]) < 1e-12 and abs(wt[-1] - t[-1]) < 1e-12)
    check("...and the world points really are on the cylinder",
          float(np.abs(np.hypot(w[:, 1], w[:, 2]) - 1.5).max()) < 1e-9)
    flat = PlanarFrame()
    fw, ft = mew_frame.wrap_path(flat, pts, t)
    check("flat: the identity, points and times verbatim",
          np.array_equal(fw, pts) and np.array_equal(ft, t))


def test_the_converter_writes_a_tube_not_a_sheet():
    """End to end through the CLI, because that is the path the GUI drives and
    the only one that exercises the wrap seam. The bar is RADIAL, replacing the
    flat `underside_clearance_mm` which reads as passing whatever a tube does:
    no fibre may sit inside the bare mandrel."""
    import subprocess
    R0, d, faces = 1.5, 0.02, 12
    gc = scaffold_gcode.generate_mandrel_gcode(2.0*R0, 12.0, 45.0, d, 4, 600.0)
    open(_tmp("_p6_tube.gcode"), "w").write(gc)
    r = subprocess.run([sys.executable, "engine/gcode_to_fibers.py", _tmp("_p6_tube.gcode"),
                        "-o", _tmp("_p6_tube.stl"), "-d", str(d), "-f", str(faces),
                        "--mandrel-diameter", str(2.0*R0)],
                       capture_output=True, text=True)
    check("the converter accepts a 4-axis file", r.returncode == 0,
          (r.stderr or r.stdout)[-300:])
    if r.returncode != 0:
        return
    import struct
    with open(_tmp("_p6_tube.stl"), "rb") as fh:
        fh.read(80)
        n = struct.unpack("<I", fh.read(4))[0]
        raw = np.frombuffer(fh.read(n*50), dtype=np.uint8).reshape(n, 50)
    v = raw[:, 12:48].copy().view(np.float32).reshape(n*3, 3).astype(np.float64)
    rad = np.hypot(v[:, 1], v[:, 2])
    check("the mesh is a TUBE around the mandrel axis",
          rad.min() > 0.9*(R0 - 0.5*d) and rad.max() > R0,
          "radius %.5f .. %.5f mm" % (rad.min(), rad.max()))
    check("no mesh vertex sits inside the bare mandrel",
          rad.min() >= R0 - 0.5*d - 1e-6,
          "min radius %.6f vs R0 - fibre radius %.6f"
          % (rad.min(), R0 - 0.5*d))
    flat = subprocess.run([sys.executable, "engine/gcode_to_fibers.py", _tmp("_p6_tube.gcode"),
                           "-o", _tmp("_p6_flat.stl"), "-d", str(d), "-f", str(faces)],
                          capture_output=True, text=True)
    if flat.returncode == 0:
        with open(_tmp("_p6_flat.stl"), "rb") as fh:
            fh.read(80)
            m = struct.unpack("<I", fh.read(4))[0]
            fraw = np.frombuffer(fh.read(m*50), dtype=np.uint8).reshape(m, 50)
        fv = fraw[:, 12:48].copy().view(np.float32).reshape(m*3, 3).astype(np.float64)
        # a flat sheet: every CENTRELINE at y = 0, so the mesh spans only the
        # tube's own radius either side of it -- against 1.6 mm of tube here
        check("CONTROL: without --mandrel-diameter the same file is a flat sheet",
              float(np.abs(fv[:, 1]).max()) <= 0.5*d + 1e-6,
              "|y| max %.4f mm (a fibre radius is %.4f) -- the rotary word "
              "vanished, as it must without the flag"
              % (float(np.abs(fv[:, 1]).max()), 0.5*d))

    # The desktop Mandrel page drives scaffold_gcode, so this is its end-to-end
    # solver selector: the generator must pass --sag-approx to the cylindrical
    # converter rather than silently falling back to Exact.
    import gcode_to_fibers
    seen = []
    old_main = gcode_to_fibers.main
    gcode_to_fibers.main = lambda argv: (seen.append(list(argv)) or 0)
    try:
        approx_code = scaffold_gcode.main([
            "-o", _tmp("_p6_tube.gcode"), "--mandrel", str(2.0 * R0),
            "--tube-length", "12", "--winding-angle", "45", "-d", str(d),
            "--layers", "4", "--speed", "600", "--stl", _tmp("_p6_tube.stl"),
            "--sag-approx"])
    finally:
        gcode_to_fibers.main = old_main
    check("the mandrel generator forwards approximate sag to its converter",
          approx_code == 0 and len(seen) == 1 and "--sag-approx" in seen[0],
          repr(seen))


def test_a_build_plate_is_refused_on_a_mandrel():
    """A plate is a flat-collector datum. Drawn under a tube it would put a plane
    through the mandrel and read as the surface the print rests on."""
    import subprocess
    r = subprocess.run([sys.executable, "engine/gcode_to_fibers.py", _tmp("_p6_tube.gcode"),
                        "-o", _tmp("_p6_x.stl"), "-d", "0.02",
                        "--mandrel-diameter", "3.0", "--plate"],
                       capture_output=True, text=True)
    check("--plate with --mandrel-diameter is refused, not ignored",
          r.returncode != 0 and "mandrel" in (r.stderr + r.stdout).lower())


def main():
    test_a_planar_frame_is_the_no_op()
    test_a_flat_scaffold_with_loops_is_unchanged()
    test_a_tube_layer_crosses_itself_only_under_a_period()
    test_a_tube_builds_a_wall_instead_of_collapsing()
    test_no_deposited_point_sits_inside_the_mandrel()
    test_the_wrap_is_a_projection_at_the_seam()
    test_term_c_scales_the_span_by_the_metric()
    test_an_axial_span_sees_neither_term()
    test_term_b_is_the_chord_below_the_circle()
    test_the_span_free_ratio_is_the_plans()
    test_the_ladder_can_fail()
    test_the_approximate_solver_keeps_a_mandrel_wall()
    test_the_wrap_carries_the_times_with_it()
    test_the_converter_writes_a_tube_not_a_sheet()
    test_a_build_plate_is_refused_on_a_mandrel()
    print("\n%d passed, %d failed, %d total"
          % (len(_PASS), len(_FAIL), len(_PASS) + len(_FAIL)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
