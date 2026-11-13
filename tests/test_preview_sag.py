#!/usr/bin/env python3
"""test_preview_sag.py -- self-running tests for the preview drape-sag engine
(preview_sag.py, behind the Scaffold-designer preview's "Sagging" toggle).

Run:  python tests/test_preview_sag.py    (prints PASS/FAIL per test, exit 1 on any
failure -- same convention as the other suites). Headless: pure numpy, no Tk.

Guards the engine's mechanism against hand-built geometry (bed rest, the
rest-one-diameter crossing lift, the free-hanging tight-pore droop, deposition
order, the multiplier pass-through, time resampling) and -- the load-bearing
check -- its ENVELOPE against the exact `apply_sag` on real generated
scaffolds at 90 and 60 degrees (mean RMS under ~half a fibre diameter, the
validated bound from the 2026-07-02 prototype study)."""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import os
import sys
import hashlib
import subprocess
import types

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import gcode_to_fibers as g2f  # noqa: E402
import mew_frame               # noqa: E402
import preview_sag as ps       # noqa: E402

FAILURES = []
RAN = []


def check(name, fn):
    RAN.append(name)
    try:
        fn()
        print(f"PASS  {name}")
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(name)
        print(f"FAIL  {name}: {exc!r}")


def _line(p0, p1, z, n=9):
    t = np.linspace(0.0, 1.0, n)
    return np.column_stack([p0[0] + (p1[0] - p0[0]) * t,
                            p0[1] + (p1[1] - p0[1]) * t,
                            np.full(n, z)])


# ---------------------------------------------------------------------------
# mechanism, on hand-built geometry
# ---------------------------------------------------------------------------
def bottom_layer_rests_on_bed():
    """Commanded Z 0 is the PLATE, and the fibre rests its UNDERSIDE on it, so
    the centreline comes back one radius up -- the drape's twin of the exact
    solver's `bed_at`."""
    d = 0.02
    paths = [_line((-5, 0), (5, 0), 0.0)]
    out, _ = ps.drape_sag(paths, None, d)
    assert np.allclose(out[0][:, 2], 0.5 * d, atol=1e-9), \
        f"bottom layer not seated: z in [{out[0][:, 2].min()}, {out[0][:, 2].max()}]"


def crossing_lifts_one_diameter():
    d = 0.02
    paths = [_line((-5, 0), (5, 0), 0.0),        # support along X
             _line((0, -5), (0, 5), d)]          # crosses it along Y
    out, _ = ps.drape_sag(paths, None, d)
    z = out[1][:, 2]
    # one diameter above the support's CENTRELINE, which seats one radius off
    # the plate; the far ends come back down onto that same seat
    assert abs(z.max() - 1.5 * d) < 0.25 * d, f"crossing peak {z.max()} != {1.5 * d}"
    assert z.min() < 0.75 * d, f"far ends did not sag to bed: {z.min()}"
    # the peak is AT the crossing (y=0), not somewhere else
    y = out[1][:, 1]
    assert abs(float(y[np.argmax(z)])) < 0.2, "peak not at the crossing"


def tight_pore_free_hangs():
    """Two supports closer than the droop can reach the floor: the bridging
    fibre must droop sag_depth(span), not slam to the bed."""
    d = 0.05
    L = 0.3                                       # droop 0.018 < diameter
    paths = [_line((-0.5 * L, -5), (-0.5 * L, 5), 0.0),
             _line((+0.5 * L, -5), (+0.5 * L, 5), 0.0),
             _line((-5, 0), (5, 0), d)]           # bridges both
    out, _ = ps.drape_sag(paths, None, d)
    b = out[2]
    # the middle fifth of the span: wider than a sample (0.025 mm here), so
    # the display thinning cannot leave it empty
    mid = b[np.abs(b[:, 0]) < 0.1 * L, 2]
    # the bridge seats one diameter above the supports' centrelines, and they in
    # turn seat one radius off the plate
    want = 1.5 * d - g2f.sag_depth(L, d)
    assert len(mid), "no mid-span samples"
    got = float(mid.min())
    assert abs(got - want) < 0.3 * g2f.sag_depth(L, d) + 1e-6, \
        f"mid-span droop {d - got:.4f}, want {g2f.sag_depth(L, d):.4f}"


def deposition_order_stacks():
    """Same two crossed fibres, opposite order: whoever is laid SECOND rides."""
    d = 0.02
    a = _line((-5, 0), (5, 0), 0.0)
    b = _line((0, -5), (0, 5), d)
    out1, _ = ps.drape_sag([a, b], None, d)
    out2, _ = ps.drape_sag([b, a], None, d)
    assert abs(out1[1][:, 2].max() - 1.5 * d) < 0.25 * d
    assert np.allclose(out2[0][:, 2], 0.5 * d, atol=1e-9), \
        "first-laid must be flat on its seat"
    assert abs(out2[1][:, 2].max() - 1.5 * d) < 0.25 * d, "second-laid must ride"


def seam_head_does_not_ride_its_own_tail():
    """A fibre that opens on the previous fibre's last vertex is the same
    filament carrying on -- a continuous-flow layer change -- and must leave the
    seam at the tail's own height (the exact solver's visit seam, `seam_prev`).
    The drape seated it a diameter ON the tail it had just stamped, which on the
    default 10x10 preset climbed every layer change a diameter and drew a
    stepped break at two corners where nothing crosses. RED on the pre-fix
    engine: head 1.5 d over a 0.5 d tail.

    The second half pins the rule to the HEAD: the same fibre's return pass
    crosses that tail again a whole loop later and must still ride it, because
    this raster does not see a fibre's own earlier strand."""
    d = 0.02
    tail = _line((-1, 0), (0, 0), 0.0)
    cont = np.vstack([_line((0, 0), (0.4, 0), d),
                      _line((0.4, 0), (0.4, 0.4), d)[1:],
                      _line((0.4, 0.4), (-0.02, 0.4), d)[1:],
                      _line((-0.02, 0.4), (-0.02, -0.4), d)[1:]])
    out, _ = ps.drape_sag([tail, cont], None, d)
    gap = abs(out[1][0, 2] - out[0][-1, 2])
    assert gap < 0.05 * d, \
        f"seam head {out[1][0, 2] / d:.2f} d over tail {out[0][-1, 2] / d:.2f} d"
    ret = out[1][(np.abs(out[1][:, 0] + 0.02) < 1e-9)
                 & (np.abs(out[1][:, 1]) < 0.5 * d)]
    assert len(ret), "no return-pass sample over the tail"
    assert abs(float(ret[:, 2].max()) - 1.5 * d) < 0.25 * d, \
        f"return pass over the tail at {ret[:, 2].max() / d:.2f} d, want 1.5 d"


def planar_self_crossing_rides_its_own_strand():
    """A flat fibre that crosses ITSELF -- a linking loop over its own lead-in,
    one turnaround loop over the next -- must ride its earlier strand one
    diameter up, as the exact solver does; the drape used to pass it straight
    through (`_crossing_self_pairs`). RED on 47b014f: the upper strand level
    with the lower, 0.00 d apart.

    The second half checks a later fibre laid along the lower strand rides
    the peak. RED with the exact-cap read of the lifted runs (`self_idx`)
    switched off: the raster holds the strand unlifted, so the second fibre
    sat level with the peak instead of a diameter over it. How WIDE that ride
    is, at the shallow angle a turnaround loop crosses at, is
    `later_fibre_rides_a_shallow_self_crossing_as_a_cap`."""
    d = 0.02
    first = np.vstack([_line((0.0, 0.0), (1.0, 0.0), 0.0, n=41),
                       _line((1.0, 0.0), (1.0, -0.5), 0.0, n=21)[1:],
                       _line((1.0, -0.5), (0.2, -0.3), 0.0, n=33)[1:],
                       _line((0.2, -0.3), (0.8, 0.3), 0.0, n=33)[1:]])
    over = _line((0.3, 0.0), (0.7, 0.0), d, n=17)
    out, _ = ps.drape_sag([first, over], None, d)
    f = out[0]
    at = np.hypot(f[:, 0] - 0.5, f[:, 1]) < d
    lower = f[at & (np.arange(len(f)) < len(f) // 2), 2]
    upper = f[at & (np.arange(len(f)) >= len(f) // 2), 2]
    assert len(lower) and len(upper), "no samples at the crossing"
    sep = float(upper.max() - lower.max())
    assert abs(sep - d) < 0.1 * d, f"upper strand {sep / d:.2f} d over the lower"
    o = out[1]
    ride = o[np.abs(o[:, 0] - 0.5) < d, 2]
    assert len(ride), "no second-fibre sample over the crossing"
    assert abs(float(ride.max()) - (float(upper.max()) + d)) < 0.1 * d, \
        (f"second fibre at {ride.max() / d:.2f} d over a peak at "
         f"{upper.max() / d:.2f} d")


def later_fibre_rides_a_shallow_self_crossing_as_a_cap():
    """A turnaround loop crosses its own earlier strand at ~26 degrees, and the
    next layer, laid along that earlier strand, must ride the lifted crossing
    as the CAP the exact solver rests it on -- about a diameter at full height
    -- not a plateau. The drape stamped the lifted samples into its raster with
    their four edge neighbours, a three-cell top-hat, which a 26-degree
    crossing rides for 3 / sin(26) cells: the kinked, stepped rise over every
    loop crossing of the default preset, compounding layer on layer.
    RED on a9ab0e5: full height over
    3.50 d of arc and lifted over 8.5 d, against the exact solver's 1.02 d and
    4.0 d on this fixture. The comparator is `apply_sag` on the same paths."""
    d = 0.02
    th = np.radians(26.0)
    c, s = np.cos(th), np.sin(th)
    a0, a1 = (0.5 - 0.3 * c, -0.3 * s), (0.5 + 0.3 * c, 0.3 * s)
    first = np.vstack([_line((0.0, 0.0), (1.0, 0.0), 0.0, n=101),
                       _line((1.0, 0.0), (1.0, -0.3), 0.0, n=31)[1:],
                       _line((1.0, -0.3), (a0[0], -0.3), 0.0, n=61)[1:],
                       _line((a0[0], -0.3), a0, 0.0, n=21)[1:],
                       _line(a0, a1, 0.0, n=61)[1:]])
    along = _line((0.1, 0.0), (0.9, 0.0), d, n=81)

    def arcs(o):
        x, z = o[:, 0], o[:, 2]
        base = float(np.median(z))
        full, lifted = x[z > base + 0.9 * d], x[z > base + 0.1 * d]
        return (float(np.ptp(full)) / d if len(full) else 0.0,
                float(np.ptp(lifted)) / d if len(lifted) else 0.0)
    got = arcs(ps.drape_sag([first, along], None, d)[0][1])
    want = arcs(g2f.apply_sag([first, along], d)[1])
    assert abs(got[0] - want[0]) < 0.75, \
        f"full height over {got[0]:.2f} d of arc, exact {want[0]:.2f} d"
    assert abs(got[1] - want[1]) < 1.5, \
        f"lifted over {got[1]:.2f} d of arc, exact {want[1]:.2f} d"


def fibre_beside_a_strand_rides_its_cap_not_its_cell():
    """A fibre laid BESIDE an earlier strand, its offset drifting from 0.2 d
    to 1.4 d, must rest on it as the exact solver does: `sqrt(d^2 - o^2)` up,
    tapering to the bed as the offset reaches a diameter. The flat drape read
    its floor as `H + d` off a one-diameter cell -- full rest anywhere in the
    strand's cell, none one cell over -- so this fibre sat a full diameter up
    wherever it shared the strand's cell and fell off a cliff where it left
    it: the wrinkled fibres of a 60-degree preview, whose same-direction
    layers drift apart that much into their loops.
    The comparator is `apply_sag` on
    the same paths. RED on 168a085, each half on its own: 0.90 d off the
    cap, and a 0.12 d step in one sample against the exact 0.05 d."""
    d = 0.02
    first = _line((0.0, 0.0), (1.0, 0.0), 0.0, n=101)
    beside = _line((0.1, 0.2 * d), (0.9, 1.4 * d), d, n=81)
    got = ps.drape_sag([first, beside], None, d)[0][1]
    want = g2f.apply_sag([first, beside], d)[1]
    x = np.linspace(0.15, 0.85, 141)
    zg = np.interp(x, got[:, 0], got[:, 2])
    zw = np.interp(x, want[:, 0], want[:, 2])
    err = float(np.abs(zg - zw).max()) / d
    step = float(np.abs(np.diff(zg)).max()) / d
    step_w = float(np.abs(np.diff(zw)).max()) / d
    assert err < 0.1, f"drape off the exact cap by {err:.2f} d"
    assert step < step_w + 0.03, \
        f"a {step:.2f} d step in one sample, exact {step_w:.2f} d"


def multiplier_zero_passthrough():
    d = 0.02
    paths = [_line((-5, 0), (5, 0), 0.0), _line((0, -5), (0, 5), d)]
    times = [np.linspace(0, 1, len(p)) for p in paths]
    out, ot = ps.drape_sag(paths, times, d, sag_multiplier=0.0)
    assert out is paths and ot is times, "mult<=0 must pass through untouched"


def times_resampled_monotonic():
    d = 0.02
    paths = [_line((-5, 0), (5, 0), 0.0), _line((0, -5), (0, 5), d)]
    times = [np.linspace(3.0, 7.0, len(p)) for p in paths]
    out, ot = ps.drape_sag(paths, times, d)
    for p, t in zip(out, ot):
        assert len(p) == len(t), "points/times length mismatch"
        assert np.all(np.diff(t) >= -1e-12), "times not monotonic"
    assert abs(ot[1][0] - 3.0) < 1e-9 and abs(ot[1][-1] - 7.0) < 1e-9, \
        "endpoint times not preserved"


def simplify_keeps_peaks():
    """The display thinning must keep the crossover peaks (the look), while
    actually thinning the flats."""
    d = 0.02
    paths = [_line((-5, 0), (5, 0), 0.0), _line((0, -5), (0, 5), d)]
    out, _ = ps.drape_sag(paths, None, d)
    z = out[1][:, 2]
    assert abs(z.max() - 1.5 * d) < 0.25 * d, "peak lost in simplify"
    # flats thinned: way fewer points than the raw ~cell/2 sampling
    raw = 10.0 / (0.5 * max(d, 10.0 / ps._MAX_GRID))
    assert len(z) < 0.6 * raw, f"no thinning: {len(z)} of ~{raw:.0f}"


# ---------------------------------------------------------------------------
# the load-bearing check: envelope vs the exact solver on real scaffolds
# ---------------------------------------------------------------------------
def _generated_paths(rotation, layers):
    import scaffold_gcode
    spec = dict(width=10.0, height=10.0, stepover=0.5, rotation=rotation,
                fiber_diameter=0.02, layers=layers, speed=600.0,
                start_x=0.0, start_y=0.0, start_z=0.0)
    kwargs = scaffold_gcode._scaffold_from_spec(spec)
    gcode = scaffold_gcode.generate_multi_scaffold_gcode([kwargs])
    import tempfile
    gpath = os.path.join(tempfile.gettempdir(), "test_preview_sag.gcode")
    with open(gpath, "w") as fh:
        fh.write(gcode)
    return g2f.parse_gcode(gpath, mode="g1", with_times=False, arc_segments=8)


def _rms_vs_exact(rotation, layers):
    d = 0.02
    paths = _generated_paths(rotation, layers)
    exact = g2f.apply_sag(paths, d)
    drape, _ = ps.drape_sag(paths, None, d)

    def s_of(p):
        seg = np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1)
        return np.concatenate([[0.0], np.cumsum(seg)])

    rms = []
    for e, a in zip(exact, drape):
        se, sa = s_of(e), s_of(a)
        grid = np.linspace(0.0, min(se[-1], sa[-1]), 1500)
        ze = np.interp(grid, se, e[:, 2])
        za = np.interp(grid, sa, a[:, 2])
        rms.append(float(np.sqrt(np.mean((ze - za) ** 2))))
    return np.mean(rms), np.max(rms), d


def envelope_matches_exact_90deg():
    mean, worst, d = _rms_vs_exact(90.0, 6)
    assert mean < 0.55 * d, f"mean RMS {mean * 1e3:.1f} um > 0.55 diam"
    assert worst < 1.0 * d, f"worst-layer RMS {worst * 1e3:.1f} um > 1 diam"


def envelope_matches_exact_60deg():
    mean, worst, d = _rms_vs_exact(60.0, 6)
    assert mean < 0.55 * d, f"mean RMS {mean * 1e3:.1f} um > 0.55 diam"
    assert worst < 1.0 * d, f"worst-layer RMS {worst * 1e3:.1f} um > 1 diam"


def curved_nonperiodic_frame_uses_the_metric_dilator():
    """The gap `test_preview_sag.py` never had: a CURVED, NON-PERIODIC frame
    (`RevolutionFrame` -- the five-axis dome/bowl/cone). `drape_sag`'s
    contact-finding dilation only took the metric-aware `_MetricDilator` when
    `frame.period_v is not None`, so every curved-but-not-wrapped design used
    the plain uniform-index dilation against a metric arclength that is NOT
    uniformly spaced on such a frame (measured 0.63x-1.53x of the assumed step
    on the shipped dome). period_v is None for every RevolutionFrame
    (mew_frame.py:1055); `is_curved()` is not.

    RED at the pre-fix line (`period is not None`): a small dome fibre never
    instantiates `_MetricDilator` at all, so the count stays 0 and this fails.
    Proven by literally reverting the one-line gate and re-running this test
    -- not asserted here as prose.
    """
    import mew_frame

    d = 0.02
    profile = mew_frame.ArcProfile(10.0, convex=True)
    frame = mew_frame.RevolutionFrame(profile, u0=0.0, u1=12.0)
    assert frame.period_v is None, "fixture drifted: expected a non-wrapping frame"
    assert frame.is_curved(), "fixture drifted: expected a curved frame"

    n = 40
    t = np.linspace(0.0, 1.0, n)

    # THE FIXTURE IS WORLD XYZ, and it was chart columns until R1-2 of the
    # arbitrary-collector task. `drape_sag` takes world points in the
    # collector's own frame for a non-developable frame and picks its own chart,
    # so the same two fibres are stated by putting the chart points they always
    # meant THROUGH the frame's forward map. That is strictly a better fixture:
    # the old columns were never on the dome at all, they were two numbers and a
    # height that happened to be read as if they were.
    def on_dome(u, v, h):
        x, y, z = frame.to_world(u, v, h)
        return np.column_stack([np.broadcast_to(x, u.shape),
                                np.broadcast_to(y, u.shape),
                                np.broadcast_to(z, u.shape)])

    u = 2.0 + 8.0 * t
    fiber0 = on_dome(u, np.zeros(n), np.zeros(n))
    fiber1 = on_dome(u, -1.5 + 3.0 * t, np.full(n, d))

    calls = {"n": 0}
    real_init = ps._MetricDilator.__init__

    def spy_init(self, *a, **kw):
        calls["n"] += 1
        return real_init(self, *a, **kw)

    ps._MetricDilator.__init__ = spy_init
    try:
        ps.drape_sag([fiber0, fiber1], None, d, frame=frame)
    finally:
        ps._MetricDilator.__init__ = real_init
    assert calls["n"] > 0, (
        "a curved, non-periodic frame never used the metric-aware dilator -- "
        "the gate is back to testing periodicity instead of curvature")


def mandrel_preview_uses_the_periodic_collector():
    """The preview's own fast drape must see turns meeting through the seam.

    This is intentionally compared with the phase-4 exact cylinder solve, not
    with the export approximate solver: the two approximation engines remain
    independently useful only if the preview carries the same collector seam.
    """
    import tempfile
    import mew_frame
    import scaffold_gcode

    d = 0.02
    frame = mew_frame.CylinderFrame(1.5)
    gcode = scaffold_gcode.generate_mandrel_gcode(
        3.0, 12.0, 45.0, d, 8, 600.0)
    with tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(gcode)
        path = fh.name
    try:
        paths = g2f.parse_gcode(path, frame=frame)
    finally:
        os.unlink(path)
    exact = g2f.apply_sag(paths, d, frame=frame)
    preview, _ = ps.drape_sag(paths, None, d, frame=frame)
    naked, _ = ps.drape_sag(paths, None, d)
    top_exact = max(float(p[:, 2].max()) for p in exact)
    top_preview = max(float(p[:, 2].max()) for p in preview)
    top_naked = max(float(p[:, 2].max()) for p in naked)
    assert top_preview > 0.10, "periodic preview did not build the tube wall"
    assert abs(top_preview - top_exact) < 0.04, \
        f"preview top {top_preview:.5f} vs exact {top_exact:.5f} mm"
    assert top_naked < 0.5 * top_preview, \
        f"without periodic frame {top_naked:.5f} vs {top_preview:.5f} mm"


def mandrel_preview_keeps_interlayer_crossing_clearance_after_simplify():
    """Every exact earlier-fibre anchor remains one diameter clear in display output.

    The two controls are the periodic commanded-path index that finds the
    contacts, and a second index over already-solved preview paths that reads
    their actual floor.  This caught nearly-equal duplicate contact arcs: the
    solve floored one point, while simplification kept its unfloored twin.
    """
    import tempfile
    import mew_frame
    import scaffold_gcode
    d = 0.02
    frame = mew_frame.CylinderFrame(1.5)
    gcode = scaffold_gcode.generate_mandrel_gcode(3.0, 12.0, 45.0, d, 16, 600.0)
    with tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False) as fh:
        fh.write(gcode); path = fh.name
    try:
        commanded = g2f.parse_gcode(path, frame=frame)
    finally:
        os.unlink(path)
    preview, _ = ps.drape_sag(commanded, None, d, frame=frame)

    def arc(p):
        return np.r_[0.0, np.cumsum(np.linalg.norm(
            np.diff(p[:, :2], axis=0), axis=1))]

    raw_index = g2f._SegIndex(d, period=frame.period_v)
    solved_index = g2f._SegIndex(d, period=frame.period_v)
    gaps = []
    for cmd, got in zip(commanded, preview):
        cmd_arc = arc(cmd)
        # A near-duplicate periodic anchor is a zero-length tube segment once
        # wrapped into VTK, visible as a black crack at the crossover.
        assert np.all(np.diff(arc(got)) > 1e-11)
        contacts = np.unique(g2f._crossing_contacts(cmd, cmd_arc, raw_index))
        if len(contacts):
            xy = np.column_stack([np.interp(contacts, cmd_arc, cmd[:, 0]),
                                  np.interp(contacts, cmd_arc, cmd[:, 1])])
            floor = solved_index.rest_heights(xy[:, 0], xy[:, 1], d / 2, -np.inf)
            assert np.all(np.isfinite(floor))
            gaps.extend(np.interp(contacts, arc(got), got[:, 2]) - (floor - d))
        raw_index.add_fiber(cmd, d / 2)
        solved_index.add_fiber(got, d / 2)
    assert gaps and min(gaps) >= d - 1e-6, (len(gaps), min(gaps))


class _PeriodicFlat(mew_frame.DepositionFrame):
    """Periodic contact index without cylinder metric/chord terms.

    A real `DepositionFrame` and not a duck-typed stand-in, since the five-axis
    spec's section 5 makes the frame INTERFACE the contract: `is_curved()` is
    inherited and answers True here on the period alone, which is exactly what
    the hand-written `k1 or k2 or period_v is not None` used to answer. The
    curvature-neutral control is unchanged -- k1 = k2 = 0 with the metric and
    chord terms overridden away.
    """
    period_v = 10.0
    k1 = k2 = 0.0

    @staticmethod
    def metric(_u, _v, _h=0.0):
        return (1.0, 1.0, 0.0)

    @staticmethod
    def chord_drop(_span, _psi, _h):
        return 0.0


def _last_vertical_profile(path, centre):
    """Read the final monotone vertical run, excluding its earlier turn."""
    start = int(np.argmin(path[:, 1]))
    run = path[start:]
    assert np.all(np.diff(run[:, 1]) >= -1e-12), "final run is not monotone"
    return lambda offset: float(np.interp(centre + offset, run[:, 1], run[:, 2]))


def mandrel_self_crossing_rebuilds_two_sided_shoulders():
    """A continuous periodic pass must use the flat preview's span mechanism.

    The old periodic implementation passed the clearance clamp but at +/-2d
    its cap had already returned to zero.  The flat split-path fixture is the
    historical 3-axis comparator; `_PeriodicFlat` is the curvature-neutral
    control for the continuous self-crossing path.  Both sides are measured so
    a one-sided turn shoulder cannot hide behind the crest peak.
    """
    d = 0.02
    lower = np.array([[-1.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    upper = np.array([[0.0, -1.0, 0.0], [0.0, 1.0, 0.0]])
    historical, _ = ps.drape_sag([lower, upper], None, d)
    ref = _last_vertical_profile(historical[1], 0.0)
    joined = np.array([[-1.0, 0.0, 0.0], [1.0, 0.0, 0.0],
                       [1.0, -1.0, 0.0], [0.0, -1.0, 0.0],
                       [0.0, 1.0, 0.0]])
    periodic, _ = ps.drape_sag([joined], None, d, frame=_PeriodicFlat())
    got = _last_vertical_profile(periodic[0], 0.0)
    # The committed flat reference profile at 1x multiplier, preserved here
    # as values so a future edit cannot silently move both comparator sides:
    # (-4d, -2d, -d, +d, +2d, +4d) =
    # (.0072, .0128, .0162, .0200, .0162, .0098) mm.
    # Its one-sample (h = 10 um) shoulder steps are <= 4 um, so 4 um is the
    # interpolation/raster tolerance.  A periodic seam may land in either
    # adjacent cell, hence each side is bounded by the two historical sides,
    # rather than choosing a favourable phase.
    for offset in (2.0 * d, 4.0 * d):
        lo = min(ref(-offset), ref(offset)) - 0.004
        hi = max(ref(-offset), ref(offset)) + 0.004
        for side in (-offset, offset):
            assert lo <= got(side) <= hi, (side, got(side), lo, hi)
    assert got(0.0) >= d - 1e-9


def mandrel_self_crossing_shoulder_wraps_the_seam_and_sags():
    """The repaired support survives the periodic seam and multiplier changes."""
    import mew_frame
    d = 0.02
    frame = mew_frame.CylinderFrame(1.5)
    p = frame.period_v
    joined = np.array([[-1.0, 0.0, 0.0], [1.0, 0.0, 0.0],
                       [1.0, p - 1.0, 0.0], [0.0, p - 1.0, 0.0],
                       [0.0, p + 1.0, 0.0]])
    # Curvature stays armed for the seam/control path; this guard asks only
    # whether the physical span changes in the sag-law direction.  Its absolute
    # shoulder envelope is owned by the same-multiplier flat comparator above.
    low, _ = ps.drape_sag([joined], None, d, sag_multiplier=0.5, frame=frame)
    high, _ = ps.drape_sag([joined], None, d, sag_multiplier=2.0, frame=frame)
    lo = _last_vertical_profile(low[0], p)
    hi = _last_vertical_profile(high[0], p)
    # Higher multiplier deepens the rebuilt span. Both are sampled 2d from the
    # seam crossing, where the baseline clamp is exactly zero.
    assert lo(2.0 * d) > hi(2.0 * d), (lo(2.0 * d), hi(2.0 * d))
    assert min(lo(0.0), hi(0.0)) >= d - 1e-9


# `preview_sag.py` as the segment floor left it, pinned by BLOB (`git show
# <blob>` reads it like a commit's file); a commit cannot name itself.
_PLANAR_PIN = "d92f08e548aaebdee540eb1450f72ec29cbf712c"


def planar_preview_is_digest_identical_to_the_segment_floor():
    """Periodic support work must leave the planar engine exact: X, Y and Z
    all a BYTE comparison, by digest, against the pinned engine.

    Re-pinned 2026-09-23. The flat floor became the exact contact cap
    (`_segment_floor`, `fibre_beside_a_strand_rides_its_cap_not_its_cell`
    says why), which moves planar output ON PURPOSE -- on this fixture 82
    samples a fibre under 9a91938 became 81. The
    pin before it was 9a91938 plus the bed datum, one radius in Z, which this
    engine already carries; that history is in git."""
    source = subprocess.check_output(["git", "show", _PLANAR_PIN], text=True)
    old = types.ModuleType("preview_sag_pinned")
    exec(compile(source, "pinned:preview_sag.py", "exec"), old.__dict__)
    d = 0.02
    paths = [_line((-1, 0), (1, 0), 0.0),
             _line((0, -1), (0, 1), d),
             _line((-1, -.35), (1, -.35), 2 * d)]
    before, _ = old.drape_sag(paths, None, d)
    now, _ = ps.drape_sag(paths, None, d)
    assert [p.shape for p in now] == [p.shape for p in before],         ([p.shape for p in now], [p.shape for p in before])

    def digest(out):
        data = b"".join(np.ascontiguousarray(p).view(np.uint8).tobytes()
                        for p in out)
        return hashlib.sha256(data).hexdigest()
    assert digest(now) == digest(before), (digest(now), digest(before))


def mandrel_angled_fixture_keeps_every_self_crossing_clear():
    """A non-90 degree real tube has the same periodic self-rest contract."""
    import tempfile
    import mew_frame
    import scaffold_gcode
    d = 0.02
    frame = mew_frame.CylinderFrame(1.5)
    gcode = scaffold_gcode.generate_mandrel_gcode(3.0, 12.0, 40.0, d, 8, 600.0)
    with tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False) as fh:
        fh.write(gcode); path = fh.name
    try:
        cmd = g2f.parse_gcode(path, frame=frame)
    finally:
        os.unlink(path)
    got, _ = ps.drape_sag(cmd, None, d, frame=frame)
    gaps = []
    for raw, sagged in zip(cmd, got):
        arc = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(raw[:, :2], axis=0), axis=1))]
        for lo, hi, _angle in g2f._self_intersections(raw, arc, d, period=frame.period_v):
            sa = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(sagged[:, :2], axis=0), axis=1))]
            gaps.append(np.interp(hi, sa, sagged[:, 2]) - np.interp(lo, sa, sagged[:, 2]))
    assert gaps and min(gaps) >= .9 * d, (len(gaps), min(gaps))


def _iterating_tube(layers=10, d=0.02):
    """A tube whose support fixed point actually ITERATES several passes.

    The guard below is about the convergence LOOP, so a fixture that settles at
    the seed cannot reach it, and neither can one whose loop lands on the same
    support heights whatever the in-loop sweep returns. Both were measured: the
    plain helical tubes settle in one pass, and the COMMANDED closed-brick tube
    runs two but lands on the same heights either way, so an injected defect
    moved nothing on either. The physics-laid closed-brick tube runs ~7 passes a
    fibre and DOES discriminate, at 0.5 s a drape -- the cheapest fixture that
    exercises the code.

    The relay options are `gui_preview._pv_snapshot_options`' own, at the factory
    model and calibration, so nothing here is a test-only number.
    """
    import tempfile
    import mew_deposition
    import mew_frame
    import mew_unit_cell
    import scaffold_gcode
    from mew_physics import MEWModel, Nozzle
    design = mew_unit_cell.design_tube_cells("brick-closed", 3.0, 12.0, 2.0,
                                             1.5, fit="scale", amplitude=0.0)
    frame = mew_frame.CylinderFrame(design.radius)
    gcode = scaffold_gcode.generate_mandrel_cell_gcode(3.0, design, d, layers,
                                                       600.0)
    with tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False) as fh:
        fh.write(gcode); path = fh.name
    try:
        paths, ptimes = g2f.parse_gcode(path, with_times=True, frame=frame)
    finally:
        os.unlink(path)
    model = MEWModel(nozzle=Nozzle(), working_distance=4.5)
    transform = mew_deposition.make_transform(
        model, pressure=20.0 * 6894.76, voltage=6.0, temperature=75.0,
        zeta=model.calibration.contact_damping_zeta, coil=False,
        coil_wave_amp=0.0, coil_wave_len=0.3, max_accel=None,
        square_corner_velocity=6.0, fill_per_tau=6.0)
    paths, ptimes = transform(paths, ptimes, frame=frame)
    return paths, ptimes, frame, d


def periodic_support_loop_reduction_is_byte_identical():
    """Sweeping only the pairs that can reach a support must change NOTHING.

    `_support_closure_pairs` drops every pair whose query point cannot influence
    `z[si]`, the one thing the loop carries between passes. An exactness claim,
    not an approximation, so the drape with the reduction must be byte-for-byte
    the drape without it. COMPARATOR: forcing the closure to return None takes
    the engine's own documented "sweep everything" fallback, so the reference is
    not a second implementation.

    RED CONTROL: an EMPTY closure (the reduction taken too far) must move the
    output, or a reduction that dropped every pair would pass the equality above
    by matching a broken reference.
    """
    cmd, ptimes, frame, d = _iterating_tube()
    real = ps._support_closure_pairs
    try:
        ps._support_closure_pairs = lambda *a, **k: None      # sweep everything
        full, _ = ps.drape_sag(cmd, ptimes, d, frame=frame)
        ps._support_closure_pairs = real
        got, _ = ps.drape_sag(cmd, ptimes, d, frame=frame)
        assert len(got) == len(full), (len(got), len(full))
        for a, b in zip(got, full):
            assert a.shape == b.shape and a.tobytes() == b.tobytes(), a.shape

        ps._support_closure_pairs = (
            lambda pairs, *a, **k: None if pairs is None
            else ([], [], [], [], None if pairs[4] is None else []))
        broken, _ = ps.drape_sag(cmd, ptimes, d, frame=frame)
    finally:
        ps._support_closure_pairs = real
    # pointwise, on each fibre's own XY arclength: comparing per-path maxima is
    # far too coarse to see a support that moved mid-fibre
    def prof(p, grid):
        p = np.asarray(p, dtype=np.float64)
        s = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(p[:, :2], axis=0),
                                                axis=1))]
        return np.interp(grid, s, p[:, 2])

    moved = 0.0
    for x, y in zip(broken, full):
        L = min(float(np.sum(np.linalg.norm(np.diff(np.asarray(q)[:, :2],
                                                    axis=0), axis=1)))
                for q in (x, y))
        g = np.linspace(0.0, L, 4000)
        moved = max(moved, float(np.max(np.abs(prof(x, g) - prof(y, g)))))
    assert moved > 0.0, "CONTROL did not swing: an empty closure changed nothing"


def periodic_metric_dilation_ignores_redundant_samples():
    """Anchor insertion must not change a physical max-plus dilation.

    The peak is present in both representations; only zero-valued collinear
    samples are inserted.  Holding the original `W`/kernel fixed makes the old
    index-distance code move the peak farther away, while metric distances at
    the shared physical positions remain identical.
    """
    d, h, W = .02, .01, 4
    coarse_s = np.arange(5, dtype=float) * h
    coarse_f = np.array([0., 0., d, 0., 0.])
    fine_s = np.arange(9, dtype=float) * (0.5 * h)
    fine_f = np.zeros(9); fine_f[4] = d
    kern = g2f.sag_depth(2.0 * np.arange(-W, W + 1) * h, d, 1.0)
    metric_coarse = ps._MetricDilator(coarse_s, W * h, d, 1.0)(coarse_f)
    metric_fine = ps._MetricDilator(fine_s, W * h, d, 1.0)(fine_f)
    assert np.allclose(metric_coarse, metric_fine[::2], atol=1e-12)
    # `_MetricDilator` streams its workspace on the first call and caches
    # it from the second, so a repeated fibre must answer identically.
    again = ps._MetricDilator(fine_s, W * h, d, 1.0)
    first, cached = again(fine_f), again(fine_f)
    assert np.array_equal(first, cached), (
        "the cached dilation workspace disagreed with the streamed one")
    index_fine = ps._dilate(fine_f, kern, W)
    assert not np.allclose(metric_coarse, index_fine[::2], atol=1e-9), \
        "index-distance red control did not move the peak"


# --- junction fusion -------------------------------------------------------- #
# The drape has ONE floor (`f = H + diameter`), which is both of the exact solver's
# twin sink sites at once (support heights + the rest-on-top clamp), so fusion is
# just lowering it where a fibre -- not the bed -- is underneath. These pin that it
# reproduces the exact solver's falsifiable claim rather than merely "moves down".

_FD = 0.02          # fibre diameter for the fusion grid
_FSTEP = 0.4
_FSIZE = 2.4


def _fgrid(layers=6, npts=121):
    """Alternating-direction grid, one Z per layer, layer height = diameter."""
    paths = []
    n = int(round(_FSIZE / _FSTEP)) + 1
    for li in range(layers):
        z = li * _FD
        for k in range(n):
            c = k * _FSTEP
            t = np.linspace(0.0, _FSIZE, npts)
            f = np.full(npts, c)
            zz = np.full(npts, z)
            paths.append(np.column_stack([t, f, zz]) if li % 2 == 0
                         else np.column_stack([f, t, zz]))
    return paths, n


def _fpeaks(out, per, layers):
    return [max(p[:, 2].max() for p in out[li * per:(li + 1) * per])
            for li in range(layers)]


def fusion_off_is_byte_identical():
    """fuse_sink=0 (the default) must not move a single point."""
    paths, _ = _fgrid(4)
    a, _ = ps.drape_sag(paths, None, _FD)
    b, _ = ps.drape_sag(paths, None, _FD, fuse_sink=0.0)
    for i, (x, y) in enumerate(zip(a, b)):
        assert np.array_equal(x, y), f"fibre {i} moved with fusion off"


def fusion_bottom_layer_does_not_fuse():
    """The bottom layer rests on the BED, which never fuses -- only fibre-on-fibre
    contacts sink (the `_sink_fused_contacts` rule)."""
    paths, per = _fgrid(4)
    off, _ = ps.drape_sag(paths, None, _FD)
    on, _ = ps.drape_sag(paths, None, _FD, fuse_sink=0.4 * _FD)
    for i in range(per):                      # bottom layer = first `per` fibres
        assert np.array_equal(off[i], on[i]), \
            f"bottom-layer fibre {i} moved -- a bed contact was fused"


def fusion_reduces_column_slope_like_exact():
    """The load-bearing claim, shared with test_mew_fusion: the column rises by
    (d - delta) per layer instead of d, and the drape's peaks match the EXACT
    solver's to numerical precision. Guards the drape against drifting from the
    solve it previews."""
    layers = 6
    paths, per = _fgrid(layers)
    delta = 0.4 * _FD

    dr_on = _fpeaks(ps.drape_sag(paths, None, _FD, fuse_sink=delta)[0], per, layers)
    for a, b in zip(dr_on, dr_on[1:]):        # linear reduced slope
        assert abs((b - a) - (_FD - delta)) < 1e-9, \
            f"rise {b - a:.6f} != d - delta {_FD - delta:.6f}"

    law = _fusion_law_or_none(delta)
    if law is None:                           # physics modules absent -> slope only
        return
    g2f.set_fusion_law(law)
    try:
        ex_on = _fpeaks(g2f.apply_sag(paths, _FD), per, layers)
    finally:
        g2f.set_fusion_law(None)              # never leak into other tests
    worst = max(abs(a - b) for a, b in zip(dr_on, ex_on))
    assert worst < 1e-9, \
        f"drape peaks differ from exact by {worst:.6f} mm with fusion on"


# --------------------------------------------------------------------------- #
# The wall/junction split in the drape. The heightmap alone knows how HIGH the
# structure below is but not which fibre put it there, so the engine stamps an `A`
# raster beside `H` carrying the direction on top of each cell. These pin that it
# works AND that it agrees with the exact solver -- a preview that showed the split
# but got it backwards would be worse than one that ignored it.
# --------------------------------------------------------------------------- #
def _split_law(phi_j, phi_w):
    """(law, band) straight from the real model -- the pair gui_preview hands over."""
    from mew_physics import Calibration, MEWModel
    m = MEWModel(working_distance=4.0,
                 calibration=Calibration(fusion_overlap=phi_j,
                                         fusion_wall_overlap=phi_w))
    return m.make_fusion_law(temperature=90.0), m.fusion_wall_band()


def _drape_split(paths, phi_j, phi_w, d=_FD):
    law, band = _split_law(phi_j, phi_w)
    out, _ = ps.drape_sag([np.array(p) for p in paths], None, d,
                          sag_multiplier=1.0, fuse_sink=float(law(d)),
                          fuse_wall=float(law(d, True)), band=band)
    return max(p[:, 2].max() for p in out)


def _exact_split(paths, phi_j, phi_w, d=_FD):
    law, band = _split_law(phi_j, phi_w)
    g2f.set_fusion_law(law, band)
    try:
        out = g2f.apply_sag([np.array(p) for p in paths], d, sag_multiplier=1.0,
                            self_crossings=True)
    finally:
        g2f.set_fusion_law(None)
    return max(p[:, 2].max() for p in out)


def _wall_stack(n=8, d=_FD, npts=81, span=2.0):
    """A PURE wall: one straight line printed n times. Every contact collinear."""
    return [np.column_stack([np.linspace(0.0, span, npts),
                             np.full(npts, 1.0), np.full(npts, L * d)])
            for L in range(n)]


def _junction_tower(n=8, d=_FD, npts=81, span=2.0):
    """A PURE junction tower: +x / +y fibres crossing at 90 deg over one point."""
    out = []
    for L in range(n):
        s = np.linspace(0.0, span, npts)
        z = np.full(npts, L * d)
        out.append(np.column_stack([s, np.full(npts, 1.0), z]) if L % 2 == 0
                   else np.column_stack([np.full(npts, 1.0), s, z]))
    return out


def split_off_is_byte_identical_two_ways():
    """The split must be inert unless it says something: wall UNSET, and wall SET
    EQUAL to the junction phi, both reproduce the pre-split drape byte-for-byte. The
    second is the real guard -- it proves the `A` raster arms on the MAGNITUDES
    differing, not merely on a band being passed."""
    paths, _ = _fgrid(4)
    law, band = _split_law(0.35, None)
    base, _ = ps.drape_sag(paths, None, _FD, fuse_sink=float(law(_FD)))
    unset, _ = ps.drape_sag(paths, None, _FD, fuse_sink=float(law(_FD)),
                            fuse_wall=float(law(_FD, True)), band=band)
    law_e, band_e = _split_law(0.35, 0.35)
    equal, _ = ps.drape_sag(paths, None, _FD, fuse_sink=float(law_e(_FD)),
                            fuse_wall=float(law_e(_FD, True)), band=band_e)
    for i, (x, y) in enumerate(zip(base, unset)):
        assert np.array_equal(x, y), f"fibre {i} moved with the wall phi UNSET"
    for i, (x, y) in enumerate(zip(base, equal)):
        assert np.array_equal(x, y), f"fibre {i} moved with wall phi == junction phi"


def split_pure_wall_tracks_exact():
    """SELECTIVITY, half 1, against the exact solver: a collinear stack answers to
    phi_wall alone -- phi_junction at ANY value must not move it, in the PREVIEW as
    in the STL. The drape is the thing the user judges the calibration on, so it has
    to agree, not merely respond."""
    for phi_j, phi_w in [(0.35, 0.0), (0.9, 0.0), (0.35, 0.7), (0.0, 0.7)]:
        d = _drape_split(_wall_stack(), phi_j, phi_w)
        e = _exact_split(_wall_stack(), phi_j, phi_w)
        assert abs(d - e) < 1e-9, \
            f"phi_j={phi_j} phi_w={phi_w}: drape {d:.6f} != exact {e:.6f}"


def split_pure_junction_tracks_exact():
    """SELECTIVITY, half 2: a 90 deg tower answers to phi_junction alone -- phi_wall
    at ANY value must not move it, in the preview as in the STL."""
    for phi_j, phi_w in [(0.35, 0.0), (0.35, 0.9), (0.0, 0.9)]:
        d = _drape_split(_junction_tower(), phi_j, phi_w)
        e = _exact_split(_junction_tower(), phi_j, phi_w)
        assert abs(d - e) < 1e-9, \
            f"phi_j={phi_j} phi_w={phi_w}: drape {d:.6f} != exact {e:.6f}"


def split_wall_phi_is_monotone_in_the_preview():
    """More wall overlap -> a shorter wall in the preview, monotonically, and nothing
    is driven below the plate."""
    hs = [_drape_split(_wall_stack(), 0.35, p) for p in (0.0, 0.2, 0.4, 0.6, 0.9)]
    assert all(a > b for a, b in zip(hs, hs[1:])), hs
    law, band = _split_law(0.35, 0.9)
    out, _ = ps.drape_sag(_wall_stack(), None, _FD, fuse_sink=float(law(_FD)),
                          fuse_wall=float(law(_FD, True)), band=band)
    assert min(p[:, 2].min() for p in out) >= -1e-9


def _fusion_law_or_none(delta):
    """A law returning `delta` for our diameter, via the REAL physics seam when it is
    importable (so the test exercises the same closure the GUI installs)."""
    try:
        from mew_physics import Calibration, MEWModel
    except Exception:  # noqa: BLE001
        return None
    model = MEWModel(working_distance=4.0,
                     calibration=Calibration(fusion_overlap=delta / _FD))
    return model.make_fusion_law()


def main():
    check("bottom layer rests on bed", bottom_layer_rests_on_bed)
    check("crossing lifts one diameter", crossing_lifts_one_diameter)
    check("tight pore free-hangs sag_depth(span)", tight_pore_free_hangs)
    check("deposition order decides who rides", deposition_order_stacks)
    check("seam head does not ride its own tail", seam_head_does_not_ride_its_own_tail)
    check("planar self-crossing rides its own strand",
          planar_self_crossing_rides_its_own_strand)
    check("later fibre rides a shallow self-crossing as a cap",
          later_fibre_rides_a_shallow_self_crossing_as_a_cap)
    check("fibre beside a strand rides its cap, not its cell",
          fibre_beside_a_strand_rides_its_cap_not_its_cell)
    check("multiplier<=0 passes through", multiplier_zero_passthrough)
    check("times resampled, monotonic, endpoints kept",
          times_resampled_monotonic)
    check("simplify keeps peaks, thins flats", simplify_keeps_peaks)
    check("envelope vs exact apply_sag, 90 deg grid",
          envelope_matches_exact_90deg)
    check("envelope vs exact apply_sag, 60 deg triangle",
          envelope_matches_exact_60deg)
    check("curved non-periodic frame uses the metric dilator",
          curved_nonperiodic_frame_uses_the_metric_dilator)
    check("mandrel preview sees the periodic collector",
          mandrel_preview_uses_the_periodic_collector)
    check("mandrel preview keeps interlayer crossing clearance after simplify",
          mandrel_preview_keeps_interlayer_crossing_clearance_after_simplify)
    check("mandrel continuous self-crossing rebuilds two-sided shoulders",
          mandrel_self_crossing_rebuilds_two_sided_shoulders)
    check("mandrel self-crossing shoulder wraps the seam and sags",
          mandrel_self_crossing_shoulder_wraps_the_seam_and_sags)
    check("planar preview digest matches the segment-floor pin",
          planar_preview_is_digest_identical_to_the_segment_floor)
    check("mandrel 40-degree fixture keeps self-crossing clearance",
          mandrel_angled_fixture_keeps_every_self_crossing_clear)
    check("periodic support-loop pair reduction is byte-identical (+RED ctl)",
          periodic_support_loop_reduction_is_byte_identical)
    check("periodic metric dilation ignores redundant samples",
          periodic_metric_dilation_ignores_redundant_samples)
    check("fusion off is byte-identical", fusion_off_is_byte_identical)
    check("fusion leaves the bed layer alone", fusion_bottom_layer_does_not_fuse)
    check("wall/junction split off is byte-identical (unset AND equal)",
          split_off_is_byte_identical_two_ways)
    check("split: pure wall tracks exact (phi_wall only)",
          split_pure_wall_tracks_exact)
    check("split: pure junction tower tracks exact (phi_junction only)",
          split_pure_junction_tracks_exact)
    check("split: wall phi monotone in the preview, never below bed",
          split_wall_phi_is_monotone_in_the_preview)
    check("fusion reduces column slope, matching exact",
          fusion_reduces_column_slope_like_exact)
    total = len(RAN)          # counted, not hand-kept: a literal here silently
    n_fail = len(FAILURES)    # under-reported for every test added since (12 vs 16)
    print(f"\n{'FAIL' if n_fail else 'PASS'}: {total - n_fail}/{total} tests passed")
    if n_fail:
        print("failed:", ", ".join(FAILURES))
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
