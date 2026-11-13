"""Tests for mew_deposition: the geometric CONSEQUENCES of the contact dynamics on
the laid centreline, not fitted numbers.

COMPARATORS: the `mew_physics` integrators the laid geometry must reproduce (the
retired second-order filter is the measured reference the ray swap was decided
against), a converged fill_per_tau 96 march for the corner grid, and geometric
invariants that need no reference. What is pinned is the promise -- the fibre is
laid at the trailing touchdown, so it lags, rounds corners, reaches its endpoint
and falls back honestly below CTS -- so no refactor can quietly turn the
physics-shaped scaffold back into the commanded path.

Run:  python tests/test_mew_deposition.py
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math

import numpy as np

from mew_physics import MEWModel, ProcessParams
import mew_deposition as md
from mew_deposition import laid_fibers, diameter_by_time, make_transform

# fixed process knobs for the tests; only speed (read from geometry) varies
_P, _V, _T = 8.0e4, 4.0, 90.0
_GAP = 4.0          # standoff / working distance, mm


def _model():
    return MEWModel(working_distance=_GAP)


def _fiber(points2d, z=0.0, speed=15.0):
    """(p (N,3), t (N,)) for a polyline drawn at constant `speed` (mm/s)."""
    p = np.array([[x, y, z] for (x, y) in points2d], dtype=np.float64)
    seg = np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1)
    t = np.concatenate([[0.0], np.cumsum(seg) / speed])
    return p, t


def _straight(length=30.0, n=121, z=0.0, speed=15.0):
    xs = np.linspace(0.0, length, n)
    return _fiber([(x, 0.0) for x in xs], z=z, speed=speed)


def _lag_distance(speed):
    return _model().solve(ProcessParams(_P, _V, speed, _T)).lag_distance


def approx(a, b, rel=1e-6, abs_=1e-9):
    return abs(a - b) <= max(abs_, rel * max(1.0, abs(a), abs(b)))


def _run(p, t, **kw):
    return laid_fibers([p], [t], _model(), pressure=_P, voltage=_V,
                       temperature=_T, **kw)


# --- structure / invariants ------------------------------------------------- #
def test_fiber_count_preserved():
    """One output path per input path, in order -- per-scaffold grouping keys off
    the count, so the transform must never add or drop a fibre."""
    paths = [_straight()[0], _straight(length=10.0, n=41)[0], _straight()[0]]
    ptimes = [_straight()[1], _straight(length=10.0, n=41)[1], _straight()[1]]
    out_p, out_t, _ = laid_fibers(paths, ptimes, _model(), pressure=_P,
                                  voltage=_V, temperature=_T)
    assert len(out_p) == 3 and len(out_t) == 3


def test_output_is_3d_and_time_sorted():
    p, t = _straight()
    out_p, out_t, _ = _run(p, t)
    xyz, times = out_p[0], out_t[0]
    assert xyz.ndim == 2 and xyz.shape[1] == 3
    assert len(xyz) == len(times)
    assert np.all(np.diff(times) >= -1e-12)         # non-decreasing


def test_z_is_preserved():
    """The lag is horizontal; the fibre lands on its commanded layer."""
    p, t = _straight(z=0.37)
    out_p, _, _ = _run(p, t)
    assert np.allclose(out_p[0][:, 2], 0.37, atol=1e-9)


def test_degenerate_fiber_passthrough():
    """A <2-point fibre is returned unchanged (nothing to reshape)."""
    p = np.array([[1.0, 2.0, 0.0]])
    t = np.array([0.0])
    out_p, out_t, _ = laid_fibers([p], [t], _model(), pressure=_P,
                                  voltage=_V, temperature=_T)
    assert np.array_equal(out_p[0], p) and np.array_equal(out_t[0], t)


# --- the steady lag: a constant trailing offset ----------------------------- #
def test_start_anchored_at_commanded():
    """Lag builds from zero: the first laid point is under the nozzle (no trail
    yet at ignition)."""
    p, t = _straight()
    out_p, _, _ = _run(p, t)
    assert np.allclose(out_p[0][0], p[0], atol=1e-9)


def test_interior_trails_by_lag_distance():
    """Far from the ends a straight constant-speed fibre is the commanded line
    shifted backward by exactly the model's steady lag distance."""
    speed = 15.0
    p, t = _straight(length=30.0, n=121, speed=speed)
    out_p, out_t, _ = _run(p, t)
    xyz, times = out_p[0], out_t[0]
    t_mid = 15.0 / speed                              # half-way along, in seconds
    cx = np.interp(t_mid, times, xyz[:, 0])
    cy = np.interp(t_mid, times, xyz[:, 1])
    lag = _lag_distance(speed)
    assert approx(cx, 15.0 - lag, abs_=0.02 * lag)   # trails along travel (+x)
    assert approx(cy, 0.0, abs_=0.02 * lag)


def test_lag_amplitude_scales_the_laid_trail():
    """END TO END: an A 1.35x the shipped one moves the laid touchdown 1.35x back.

    It proves the amplitude reaches the tau path and the reported
    `max_lag_distance`. It does NOT cover the ray tracker's own (ceiling, floor,
    exp): handing the tracker an unscaled ceiling leaves this test green, because
    the ceiling shapes the TRANSIENT while the settled trail is set by tau alone.
    `test_mew_physics.test_the_ray_tracker_carries_the_same_amplitude_as_the_lag_law`
    is the guard that goes red on that; do not delete it on the strength of this one.

    The scale is expressed against the SHIPPED A rather than as an absolute
    number, because `lag_amplitude` is Eq.4's amplitude itself (2.8629) and not
    the multiplier-on-top-of-it it used to be.

    Read at 100 mm of a 120 mm pass, not mid-way through 30 mm: a 1.35x lag needs
    1.35x the distance to settle, and at 15 mm it is still 3.2% short of its own
    steady trail -- reading inside the transient would hide a real error that size.
    """
    import dataclasses
    from mew_physics import Calibration

    speed, at = 15.0, 100.0
    p, t = _straight(length=120.0, n=481, speed=speed)
    lag = _lag_distance(speed)
    A0 = Calibration().lag_amplitude
    for k in (1.0, 1.35):          # k = 1 is the paired control on one fixture
        model_k = MEWModel(working_distance=_GAP,
                           calibration=dataclasses.replace(
                               Calibration(), lag_amplitude=k * A0))
        out_p, out_t, info = laid_fibers([p], [t], model_k, pressure=_P,
                                         voltage=_V, temperature=_T)
        cx = float(np.interp(at / speed, out_t[0], out_p[0][:, 0]))
        assert approx(info["max_lag_distance"], k * lag, rel=1e-12), k
        assert approx(cx, at - k * lag, abs_=1e-6), (k, cx, at - k * lag)


def test_speed_scale_default_is_byte_identical():
    """speed_scale=1.0 (the default) must leave the laid centreline and the
    diameter lookup byte-for-byte the no-argument call -- the live march's
    export-relay branch passes it on every layer, so a constant 1.0 schedule can
    never perturb the locked/export geometry."""
    p, t = _straight(length=30.0, n=121, speed=15.0)
    a_p, a_t, _ = _run(p, t)
    b_p, b_t, _ = _run(p, t, speed_scale=1.0)
    assert np.array_equal(a_p[0], b_p[0]) and np.array_equal(a_t[0], b_t[0])
    da = diameter_by_time([p], [t], _model(), pressure=_P, voltage=_V, temperature=_T)
    db = diameter_by_time([p], [t], _model(), pressure=_P, voltage=_V,
                          temperature=_T, speed_scale=1.0)
    assert np.array_equal(da[0], db[0]) and np.array_equal(da[1], db[1])


def test_speed_scale_lags_at_the_scaled_speed():
    """The live print-speed what-if shapes the fibre 'as if printed at v*scale': the
    interior steady offset trails by the lag distance at the SCALED speed (which
    grows with speed -> more whip), exactly as mew_live._tau_at folds it -- not the
    commanded-speed lag. This is what keeps the export relay matching a live sweep."""
    speed, scale = 15.0, 1.5
    p, t = _straight(length=40.0, n=161, speed=speed)
    out_p, out_t, _ = laid_fibers([p], [t], _model(), pressure=_P, voltage=_V,
                                  temperature=_T, speed_scale=scale)
    t_mid = 20.0 / speed                              # half-way along, in seconds
    cx = np.interp(t_mid, out_t[0], out_p[0][:, 0])
    lag_scaled = _lag_distance(speed * scale)
    assert lag_scaled > _lag_distance(speed)         # faster what-if -> more lag
    assert approx(cx, 20.0 - lag_scaled, abs_=0.03 * lag_scaled)


def test_speed_scale_thins_the_diameter():
    """Mass conservation d ~ v^-0.5: a faster print-speed what-if lays a THINNER
    fibre. diameter_by_time at scale>1 reports a smaller diameter than at 1.0."""
    p, t = _straight(length=30.0, n=121, speed=15.0)
    d1 = diameter_by_time([p], [t], _model(), pressure=_P, voltage=_V, temperature=_T)
    d2 = diameter_by_time([p], [t], _model(), pressure=_P, voltage=_V,
                          temperature=_T, speed_scale=2.0)
    assert float(np.max(d2[1])) < float(np.min(d1[1]))


def test_endpoint_reached_after_settle():
    """The settle tail lets the touchdown catch up to the last commanded point,
    so the fibre does not end short by the lag distance (which would gap turns)."""
    p, t = _straight(length=20.0, n=81)
    out_p, _, _ = _run(p, t)
    end = out_p[0][-1]
    assert np.allclose(end[:2], p[-1][:2], atol=0.02 * _lag_distance(15.0))


def test_smaller_gap_smaller_lag():
    """lag_distance = H*tan(theta): a shorter standoff trails less -- so the
    centreline shift is genuinely tied to the physics, not a fixed nudge."""
    speed = 15.0
    p, t = _straight(speed=speed)
    tm = 15.0 / speed                                # mid-fibre, in seconds

    def mid_offset(gap):
        out_p, out_t, _ = laid_fibers(
            [p], [t], MEWModel(working_distance=gap),
            pressure=_P, voltage=_V, temperature=_T)
        cx = np.interp(tm, out_t[0], out_p[0][:, 0])
        return 15.0 - cx                             # trailing offset along +x

    assert mid_offset(4.0) > mid_offset(2.0) > 0.0


# --- corners: rounding (always) and whip (underdamped only) ----------------- #
def _l_path(leg=10.0, n=61, speed=15.0):
    """An L: +x for `leg`, then +y for `leg`, constant speed."""
    xs = np.linspace(0.0, leg, n)
    ys = np.linspace(0.0, leg, n)[1:]
    pts = [(x, 0.0) for x in xs] + [(leg, y) for y in ys]
    return _fiber(pts, speed=speed)


def test_corner_is_rounded():
    """The touchdown cannot turn instantly, so the laid path does NOT reach the
    commanded apex -- it rounds it by ~the lag distance."""
    speed = 15.0
    p, t = _l_path(speed=speed)
    out_p, _, _ = _run(p, t)
    xyz = out_p[0]
    apex = np.array([10.0, 0.0])
    dmin = np.min(np.linalg.norm(xyz[:, :2] - apex, axis=1))
    assert dmin > 0.25 * _lag_distance(speed)        # clearly rounded


def test_faster_print_rounds_more():
    """Rounding scales with the lag distance, which grows with speed: a faster
    print rounds its corners more. The grounded 'parameters change the scaffold'."""
    slow_p, slow_t = _l_path(speed=10.0)
    fast_p, fast_t = _l_path(speed=20.0)
    slow = _run(slow_p, slow_t)[0][0]
    fast = _run(fast_p, fast_t)[0][0]
    apex = np.array([10.0, 0.0])
    d_slow = np.min(np.linalg.norm(slow[:, :2] - apex, axis=1))
    d_fast = np.min(np.linalg.norm(fast[:, :2] - apex, axis=1))
    assert d_fast > d_slow


def test_zeta_reaches_no_geometry():
    """zeta damped the second-order filter the ray step replaced as the placement
    rule, and the ray step has no damping knob. The argument survives in every
    signature because the GUI, the Lite registry and `mew_export --zeta` all pass
    it, so this is what says it is INERT: the laid centreline must be BYTE-identical
    across the whole range zeta ever took, including the underdamped values that
    used to whip the corner outward. The filter itself is not deleted --
    `integrate_contact_path_2nd` is the comparator the swap was measured against."""
    p, t = _l_path(speed=15.0)
    base = _run(p, t, zeta=1.0)[0][0]
    for zeta in (0.3, 0.6, 1.2, 50.0):
        other = _run(p, t, zeta=zeta)[0][0]
        assert other.shape == base.shape, zeta
        assert np.array_equal(other, base), zeta


def test_corner_rounds_more_than_the_retired_filter():
    """The swap, at the laid-geometry level: the second-order filter under-rounded
    every corner (measured -0.276 mm cutback bias at H = 3.5 mm and -0.712 mm at
    5.5 mm over our 24 lag squares, 8/8 cells one way at both gaps), so the shipped
    ray tracker must cut the commanded apex by MORE than it did, at every speed.

    The same contract `test_mew_physics` pins on the integrator, repeated here
    because between the two sits the whole grid-building path -- the transient fill,
    the corner windows, the settle tail -- where a regression shows as the laid
    corner losing its rounding while the integrator stays correct."""
    import mew_physics as _mp
    apex = np.array([10.0, 0.0])

    def cutback(out_p):
        return float(np.min(np.linalg.norm(out_p[0][:, :2] - apex, axis=1)))

    ray = _mp.integrate_contact_path_ray
    for speed in (10.0, 15.0, 30.0, 60.0):
        p, t = _l_path(speed=speed)
        got = cutback(_run(p, t)[0])
        import mew_deposition as _md
        _md.integrate_contact_path_ray = (
            lambda pts, tms, rx, *, lag_ceiling, lag_floor, saturation_exp,
            substeps=None, init=None, with_state=False:
            _mp.integrate_contact_path_2nd(pts, tms, rx, 1.0, init=init,
                                           with_state=with_state))
        try:
            retired = cutback(_run(p, t)[0])
        finally:
            _md.integrate_contact_path_ray = ray
        assert got > retired * 1.05, (speed, got, retired)


# --- below CTS: the sewing-machine coil ------------------------------------- #
def _cts():
    return _model().critical_speed(_V, _GAP)


def _arclen(xyz):
    return float(np.sum(np.linalg.norm(np.diff(xyz[:, :2], axis=0), axis=1)))


def _straight_at_ratio(eps, length=30.0, n=121, axis="x"):
    """A straight commanded fibre printed at v = eps*CTS (so v/CTS = eps)."""
    speed = eps * _cts()
    xs = np.linspace(0.0, length, n)
    pts = [(x, 0.0) for x in xs] if axis == "x" else [(0.0, x) for x in xs]
    return _fiber(pts, speed=speed)


def test_below_cts_passthrough_is_the_default():
    """Coiling is OPT-IN: with the default coil=False a below-CTS fibre is left as
    its commanded centreline (the honest passthrough) and reported as coiling --
    never faked straight, and no coil added until asked."""
    p, t = _straight_at_ratio(0.3)                   # well below CTS
    out_p, out_t, info = _run(p, t)                  # coil defaults to False
    assert np.array_equal(out_p[0], p) and np.array_equal(out_t[0], t)
    assert info["n_coiling"] == 1 and info["coiling_fibers"] == [0]
    assert info["max_coil_amplitude"] == 0.0


def test_coiling_fiber_is_reshaped_when_enabled():
    """With coil=True a below-CTS fibre is reshaped into the coiling centreline
    (not a passthrough) AND still honestly reported as coiling."""
    p, t = _straight_at_ratio(0.3)                   # well below CTS
    out_p, out_t, info = _run(p, t, coil=True)
    assert info["n_coiling"] == 1 and info["coiling_fibers"] == [0]
    assert not np.array_equal(out_p[0], p)           # geometry actually changed
    assert info["max_coil_amplitude"] > 0.0


def test_above_cts_is_reshaped_not_coiled():
    """At/above CTS the fibre is reshaped (lag + rounding) but NOT coiled or
    flagged -- the coil excursion is exactly zero there, with or without the flag."""
    p, t = _straight_at_ratio(2.0)
    out_p, _, info = _run(p, t, coil=True)
    assert info["n_coiling"] == 0
    assert info["max_coil_amplitude"] == 0.0
    assert not np.array_equal(out_p[0], p)           # geometry actually changed


def test_coiling_conserves_arc_length():
    """The coil absorbs the surplus delivered length: a straight fibre laid at
    v = eps*CTS deposits arc length ~ commanded/eps (= CTS/v per unit travel).
    Grounded in arc-length conservation (Brun et al. 2015; Morris et al. 2008)."""
    length = 30.0
    for eps in (0.7, 0.5, 0.3):
        p, t = _straight_at_ratio(eps, length=length)
        out_p, _, _ = _run(p, t, coil=True)
        assert approx(_arclen(out_p[0]), length / eps, rel=0.05)


def test_coil_amplitude_vanishes_at_cts():
    """Continuity: the coil amplitude -> 0 as eps -> 1, so the coiling fibre joins
    the straight (rounded) fibre smoothly across CTS -- no cliff, no asymmetry."""
    _, _, near = _run(*_straight_at_ratio(0.999), coil=True)
    _, _, mid = _run(*_straight_at_ratio(0.5), coil=True)
    assert near["max_coil_amplitude"] < 0.1 * mid["max_coil_amplitude"]


def test_slower_print_coils_more():
    """Length conservation forces a bigger excursion as the collector falls
    further behind the jet: the coil amplitude grows as the speed drops."""
    _, _, fast = _run(*_straight_at_ratio(0.85), coil=True)
    _, _, slow = _run(*_straight_at_ratio(0.45), coil=True)
    assert slow["max_coil_amplitude"] > fast["max_coil_amplitude"] > 0.0


def test_coiling_is_rotation_invariant():
    """The coil is isotropic: the same sub-CTS fibre laid along +x and along +y
    deposits the same arc length and the same coil amplitude. This is the direct
    guard against the 0 deg / 90 deg asymmetry the feature used to show."""
    px, tx = _straight_at_ratio(0.5, axis="x")
    py, ty = _straight_at_ratio(0.5, axis="y")
    ox, _, ix = _run(px, tx, coil=True)
    oy, _, iy = _run(py, ty, coil=True)
    assert approx(_arclen(ox[0]), _arclen(oy[0]), rel=1e-6)
    assert approx(ix["max_coil_amplitude"], iy["max_coil_amplitude"], rel=1e-6)


def test_coil_excursion_stays_bounded_on_a_reversing_wave():
    """Robustness: even a commanded wave that REVERSES direction faster than the
    coil oscillates (where a rotating-frame velocity integral would drift without
    bound) keeps the excursion bounded to a few coil radii."""
    R = _model().calibration.coil_radius_ref
    # a fine zig-zag that doubles back on itself every 0.3 mm
    pts = []
    for k in range(40):
        pts.append((0.3 * k, 0.0))
        pts.append((0.3 * k + 0.15, 0.4))
    p, t = _fiber(pts, speed=0.5 * _cts())
    _, _, info = _run(p, t, coil=True)
    assert info["max_coil_amplitude"] < 3.0 * R      # bounded, not a runaway drift


def test_coil_vanishes_on_an_above_cts_segment_within_a_coiling_fiber():
    """Regression (the live alternating-spacing bug's static twin): inside a fibre
    that coils by MEAN speed, a stretch that is locally ABOVE CTS gets NO coil --
    the closed-form amplitude carries J_k(Psi) = 0 there, so the path rejoins the
    commanded line on that stretch with no residual. A velocity integral instead
    froze the coil offset across the fast stretch and shifted everything after it."""
    cts = _cts()
    xs = np.concatenate([np.linspace(0, 12, 49),            # slow: coiling
                         np.linspace(12, 16, 17)[1:],       # fast: above CTS
                         np.linspace(16, 28, 49)[1:]])      # slow: coiling
    p = np.array([[x, 0.0, 0.0] for x in xs], dtype=np.float64)
    seg = np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1)
    fast = (p[:-1, 0] >= 12.0) & (p[:-1, 0] < 16.0)         # the above-CTS segments
    v = np.where(fast, 2.0 * cts, 0.4 * cts)
    t = np.concatenate([[0.0], np.cumsum(seg / v)])
    out_p, out_t, info = _run(p, t, coil=True)
    assert info["n_coiling"] == 1                           # mean below CTS -> coils
    op = out_p[0]
    nx = np.interp(out_t[0], t, p[:, 0])                    # nozzle x per laid sample
    mid = np.abs(op[(nx > 12.5) & (nx < 15.5), 1])          # laid in the fast stretch
    ends = np.abs(op[(nx > 2.0) & (nx < 10.0), 1])          # laid in the slow stretches
    R = _model().calibration.coil_radius_ref               # amplitude scales with R
    assert mid.max() < 0.03                                 # fast stretch on the line
    assert ends.max() > 0.25 * R                            # slow stretches coil (~R-scaled)


# --- accel-limited re-timing: ramps bead, cruise is preserved --------------- #
def test_kinematics_off_by_default_is_identity():
    """max_accel defaults to None (off): the laid geometry is byte-identical to the
    pre-kinematics build (and max_accel <= 0 disables it too), so existing prints
    and the calibration never shift unasked."""
    p, t = _straight(length=20.0, n=81)
    base = _run(p, t)[0][0]                            # default: off
    off0 = _run(p, t, max_accel=0.0)[0][0]            # <= 0 also off
    assert np.array_equal(base, off0)


def test_kinematics_preserves_cruise_lag():
    """Steady-state guarantee: with kinematics ON the cruise interior STILL trails by
    exactly the commanded-speed lag distance -- only the ramped ends differ -- so the
    calibration the build rests on does not move."""
    speed = 15.0
    p, t = _straight(length=30.0, n=121, speed=speed)
    out_p, out_t, _ = _run(p, t, max_accel=2000.0)
    cx = np.interp(15.0 / speed, out_t[0], out_p[0][:, 0])    # mid-fibre (cruise)
    lag = _lag_distance(speed)
    assert approx(cx, 15.0 - lag, abs_=0.03 * lag)


def test_kinematics_beads_the_diameter_at_fibre_ends():
    """With max_accel set the rest-to-rest ramp at the fibre ends lays a THICKER
    fibre there (mass conservation d ~ v^-0.5 at the slower speed), while the cruise
    middle keeps the commanded-feed diameter -- the transient-diameter beading. Off,
    the diameter is uniform."""
    speed = 15.0
    p, t = _straight(length=30.0, n=121, speed=speed)
    tk, dk = diameter_by_time([p], [t], _model(), pressure=_P, voltage=_V,
                              temperature=_T, max_accel=2000.0)
    d_off = diameter_by_time([p], [t], _model(), pressure=_P, voltage=_V,
                             temperature=_T)[1]
    d_mid = float(np.interp(15.0 / speed, tk, dk))           # cruise
    assert float(dk[0]) > d_mid + 1e-4                       # beads thicker at the start
    assert approx(d_mid, float(d_off[0]), rel=1e-3)          # cruise == commanded-feed d
    assert dk.max() > d_off.max() + 1e-4                     # off uniform, on beads


def test_tight_arc_beads_more_than_a_gentle_one():
    """A tight turnaround arc slows the head (centripetal limit), so its MIDDLE lays a
    thicker fibre than a gentle (large-radius) arc commanded at the same feed -- the
    grounded 'the geometry changes with the path', through kinematics."""
    def arc_mid_diam(R, n=40):
        pts = [(R * math.cos(math.pi * i / (n - 1)),
                R * math.sin(math.pi * i / (n - 1))) for i in range(n)]
        p, t = _fiber(pts, speed=30.0)
        tk, dk = diameter_by_time([p], [t], _model(), pressure=_P, voltage=_V,
                                  temperature=_T, max_accel=2000.0)
        return float(np.interp(0.5 * (t[0] + t[-1]), tk, dk))

    assert arc_mid_diam(0.3) > arc_mid_diam(3.0) + 1e-4      # tighter -> slower -> thicker


def test_long_move_beads_sub_interval():
    """#4: on a LONG, coarsely-sampled move the diameter is sampled ALONG the accel /
    decel ramps (Trajectory.resample), not collapsed to one mean per commanded move,
    so the cruise middle keeps the thin commanded-feed diameter while the slow ENDS
    bead thicker -- bounded at the CTS diameter. Off, the single move is one uniform
    knot (the pre-kinematics behaviour). This is the long-move case the dense-fibre
    bead test above cannot see (there each short segment already resolves the ramp)."""
    speed = 40.0                                            # well above CTS (7.5)
    p, t = _fiber([(0.0, 0.0), (60.0, 0.0)], speed=speed)   # ONE 60 mm segment
    m = _model()
    cts = m.critical_speed(_V, _GAP, flow_rate=m.flow_rate(_P, _T),
                           viscosity=m.viscosity(_T))
    d_cts = m.solve(ProcessParams(_P, _V, cts, _T)).fiber_diameter
    d_cruise = m.solve(ProcessParams(_P, _V, speed, _T)).fiber_diameter

    # kinematics off: one uniform knot at the commanded feed (no ramp sampled)
    dk_off = diameter_by_time([p], [t], m, pressure=_P, voltage=_V,
                              temperature=_T)[1]
    assert dk_off.size == 1 and approx(float(dk_off[0]), d_cruise, rel=1e-6)

    # low accel -> a long start/stop ramp; sub-interval sampling beads the ends
    tk, dk = diameter_by_time([p], [t], m, pressure=_P, voltage=_V,
                              temperature=_T, max_accel=200.0)
    d_mid = float(dk.min())
    assert dk.size > 2                                      # ramp sampled, not one mean
    assert approx(d_mid, d_cruise, rel=1e-3)                # cruise middle stays thin
    assert float(dk[0]) > d_mid + 1e-4                      # beads at the start
    assert float(dk[-1]) > d_mid + 1e-4                     # and at the end
    assert dk.max() <= d_cts + 1e-9                         # bead bounded at the CTS diameter


# --- the transform wrapper the converter seam consumes ---------------------- #
def test_make_transform_returns_two_tuple():
    """make_transform must hand the converter the plain (paths, ptimes) pair it
    expects -- the info dict is dropped (echoed via the log callback instead)."""
    p, t = _straight()
    logged = []
    tf = make_transform(_model(), pressure=_P, voltage=_V, temperature=_T,
                        log=logged.append)
    out = tf([p], [t])
    assert isinstance(out, tuple) and len(out) == 2
    assert len(out[0]) == 1 and len(out[1]) == 1
    assert logged and "physics deposition" in logged[0]


# --- end-to-end: the gcode_to_fibers _FIBER_TRANSFORM seam ------------------ #
def test_seam_reshapes_exported_sidecar():
    """Installing the transform via gcode_to_fibers' _FIBER_TRANSFORM seam makes
    main() write a PHYSICS-shaped .depo sidecar (the fibre as laid), not the
    commanded path -- the integration the in-isolation tests above can't see.
    Guards the seam threading the player + STL both depend on."""
    import os
    import shutil
    import tempfile
    import gcode_to_fibers as gtf

    d = tempfile.mkdtemp()
    gpath = os.path.join(d, "seam.gcode")
    spath = os.path.join(d, "seam.stl")
    with open(gpath, "w") as fh:               # 10 mm/s (F600) > CTS -> reshaped
        fh.write("G0 X0 Y0 F600\nG1 X12 Y0\nG1 X12 Y12\nG1 X0 Y12\n")
    tf = make_transform(MEWModel(working_distance=_GAP),
                        pressure=_P, voltage=_V, temperature=_T)
    gtf.set_fiber_transform(tf)
    try:
        rc = gtf.main([gpath, "-o", spath, "-d", "0.05", "-f", "6", "-m", "g1"])
        assert rc == 0
        depo = os.path.splitext(spath)[0] + gtf.DEPO_CACHE_EXT
        # main() stamps its own --sag-reset-layers default into the cache key, so a
        # consumer must ask for the same period (exactly like `transformed` below).
        reset = gtf._DEFAULT_SAG_RESET_LAYERS
        cached = gtf.load_depo_cache(
            depo, diameter=0.05, no_sag=False, sag_multiplier=1.0,
            self_crossings=True, gcode_path=gpath, transformed=True,
            sag_reset_layers=reset)
        # #5: the cache is stamped physics-deposited, so a consumer wanting the PLAIN
        # mode must be refused it (never reuse geometry from the wrong mode).
        wrong_mode = gtf.load_depo_cache(
            depo, diameter=0.05, no_sag=False, sag_multiplier=1.0,
            self_crossings=True, gcode_path=gpath, transformed=False,
            sag_reset_layers=reset)
        cmd, _ = gtf.parse_gcode(gpath, mode="g1", with_times=True)
    finally:
        gtf.set_fiber_transform(None)          # never leak into other tests
        shutil.rmtree(d, ignore_errors=True)

    assert cached is not None
    assert wrong_mode is None                  # #5: wrong-mode load is refused
    paths, _ = cached
    assert len(paths) == len(cmd)              # fibre count preserved
    # the seam reshaped the geometry: more (refined) points, and the laid path
    # rounds the commanded apex (12,0) instead of passing through it
    assert sum(len(p) for p in paths) > sum(len(c) for c in cmd)
    apex = np.array([12.0, 0.0])
    dmin = min(float(np.min(np.linalg.norm(np.asarray(p)[:, :2] - apex, axis=1)))
               for p in paths)
    assert dmin > 0.1


def test_travel_aware_transform_stamps_render_travel_meta():
    """A travel-aware physics transform lays + merges the travels ITSELF, so the
    .depo main() writes DOES contain deposited travel geometry -- even though
    args.render_travel stays False (mew_export consumes --render-travel for the
    transform and never forwards it to this converter's argparse). The cache meta
    must stamp render_travel from the travel-aware seam so a consumer that asked for
    render-travel ACCEPTS the cache instead of rejecting it and falling back to the
    commanded path. Regression for the 'viewer shows commanded not physics' bug: only
    x_square runs with render-travel on, so it was the only preset affected."""
    import os
    import shutil
    import tempfile
    import gcode_to_fibers as gtf

    d = tempfile.mkdtemp()
    gpath = os.path.join(d, "tv.gcode")
    spath = os.path.join(d, "tv.stl")
    with open(gpath, "w") as fh:               # two fibres, a travel (G0) between
        fh.write("G0 X0 Y0 F600\nG1 X12 Y0\nG0 X12 Y6\nG1 X0 Y6\n")
    tf = make_transform(MEWModel(working_distance=_GAP),
                        pressure=_P, voltage=_V, temperature=_T,
                        simulate_travel=True)
    gtf.set_fiber_transform(tf, with_travels=True)     # the travel-aware seam
    try:
        # NOTE: no --render-travel in the argv -- exactly as mew_export leaves it
        # for the downstream converter (it kept known.render_travel for the seam).
        rc = gtf.main([gpath, "-o", spath, "-d", "0.05", "-f", "6", "-m", "g1"])
        assert rc == 0
        depo = os.path.splitext(spath)[0] + gtf.DEPO_CACHE_EXT
        reset = gtf._DEFAULT_SAG_RESET_LAYERS
        # the player's render-travel toggle is on -> it asks render_travel=True; the
        # travel-aware cache must match (before the fix it stamped 0 -> rejected).
        cached = gtf.load_depo_cache(
            depo, diameter=0.05, no_sag=False, sag_multiplier=1.0,
            self_crossings=True, gcode_path=gpath, transformed=True,
            sag_reset_layers=reset, render_travel=True)
    finally:
        gtf.set_fiber_transform(None)          # never leak into other tests
        shutil.rmtree(d, ignore_errors=True)

    assert cached is not None, (
        "travel-aware physics cache must be accepted by a render_travel=True "
        "consumer; a None here is the viewer silently falling back to commanded")


def test_seam_off_is_identity():
    """With the seam unset (the default), main() writes the plain
    commanded centrelines -- byte-for-byte the pre-feature behaviour."""
    import os
    import shutil
    import tempfile
    import gcode_to_fibers as gtf

    d = tempfile.mkdtemp()
    gpath = os.path.join(d, "plain.gcode")
    spath = os.path.join(d, "plain.stl")
    with open(gpath, "w") as fh:
        fh.write("G0 X0 Y0 F600\nG1 X12 Y0\nG1 X12 Y12\nG1 X0 Y12\n")
    gtf.set_fiber_transform(None)              # explicit: feature off
    try:
        rc = gtf.main([gpath, "-o", spath, "-d", "0.05", "-f", "6", "-m", "g1"])
        assert rc == 0
        cached = gtf.load_depo_cache(
            os.path.splitext(spath)[0] + gtf.DEPO_CACHE_EXT,
            diameter=0.05, no_sag=False, sag_multiplier=1.0,
            self_crossings=True, gcode_path=gpath,
            sag_reset_layers=gtf._DEFAULT_SAG_RESET_LAYERS)
        cmd, _ = gtf.parse_gcode(gpath, mode="g1", with_times=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    assert cached is not None
    paths, _ = cached
    # No transform -> the cached centrelines are the commanded fibres and
    # nothing else. They are not the commanded NUMBERS, because a commanded Z is
    # the surface a fibre lands on and the cache holds centrelines: this one
    # layer has nothing to sag onto, so the converter seats it on the plate
    # itself (`lift_to_underside`, the solver-free twin of `_SagState`'s
    # `bed_at`). What this test is about is the SEAM, and the seam contributes
    # nothing.
    assert len(paths) == len(cmd)
    seated = gtf.lift_to_underside(cmd, None, 0.05)
    assert all(np.allclose(p, c) for p, c in zip(paths, seated))


# --- adaptive point budget: many-corner fibres stay rounded, not blocky ----- #
def _zigzag(n_corners, pitch=1.5, amp=0.7, speed=12.0):
    """A saw-tooth with one sharp corner per tooth -- the stand-in for a big
    serpentine / looping scaffold, which in continuous MEW is ONE many-cornered
    fibre. More teeth => more corners packed into a single path."""
    pts = [(0.0, 0.0)]
    for i in range(n_corners):
        pts.append(((i + 1) * pitch, amp if i % 2 == 0 else -amp))
    return _fiber(pts, speed=speed)


def test_point_budget_scales_with_corner_count():
    """The per-fibre point budget grows with the corner count, so a flat cap that
    suffices for a short fibre cannot thin a long one into a blocky polygon.
    Regression for the blocky-loops bug: a big fine-stepover looping scaffold is one
    fibre with thousands of corners. Quadrupling the corners keeps far more than a
    fixed cap would -- on the old flat cap both would clip to the same value."""
    flat = 400                                    # below the long fibre's natural grid
    n_small = len(_run(*_zigzag(60), max_points_per_fiber=flat)[0][0])
    n_big = len(_run(*_zigzag(240), max_points_per_fiber=flat)[0][0])
    assert n_big > flat                           # the cap scaled past the flat floor
    assert n_big > 2.5 * n_small                  # ~ tracks the 4x corner count


def test_dense_serpentine_keeps_full_rounding_resolution():
    """A fibre whose corner-rounding grid exceeds the flat default cap (a big
    serpentine is ONE fibre) is laid at FULL resolution under the adaptive cap --
    identical to uncapped -- instead of thinned blocky. The old flat 20000 cap
    clipped it; the adaptive floor does not."""
    p, t = _zigzag(2500)
    default = _run(p, t)[0][0]                     # adaptive cap (flat floor 20000)
    uncapped = _run(p, t, max_points_per_fiber=10**9)[0][0]
    assert len(uncapped) > 20000                   # genuinely past the old flat cap
    assert len(default) == len(uncapped)           # adaptive cap did not thin it


def test_long_coil_budget_scales_with_coil_length():
    """Below CTS the fibre coils into tight loops over its WHOLE length (a slow print
    coils more), so the budget must scale with the coil-period count -- not just the
    corner count -- or a long coil is thinned coarse. A near-straight fibre (almost no
    geometry corners) run well below CTS has thousands of coil periods; its grid
    exceeds the old flat 20000 cap and must be kept (near) full so the loops stay
    smooth. Pins the mid-print-slowdown case."""
    p, t = _straight(length=900.0, n=400, speed=3.0)   # 3 mm/s << CTS -> coils
    default = _run(p, t, coil=True)[0][0]
    uncapped = _run(p, t, coil=True, max_points_per_fiber=10**9)[0][0]
    assert len(uncapped) > 20000                   # genuinely past the old flat cap
    assert len(default) >= 0.9 * len(uncapped)     # budget scaled to hold the coil grid


def test_linking_loop_exit_has_no_straight_span():
    """The reshaped touchdown must curve smoothly out of a linking loop -- no random
    straight chord across the loop exit (the locked-sim artifact).

    A linking loop closes on its start vertex heading the SAME way the first pass
    then leaves, so the closure is not a sharp corner and the two long intervals
    straddling it used to get no refinement -- the catch-up collapsed into one
    straight chord. The refine window now extends to the NEXT CORNER, so the span
    stays a normal step WITH OR WITHOUT the loop flag; the same fix kills the
    equivalent collapse at a turnaround exiting into an angled-layer pass, and
    `is_loop` is now the redundant special case of it."""
    from gcode_to_fibers import loop_entry_flags

    model = _model()
    # a lasso: long lead-out (+x), arc, long lead-in (-y), then a long deposition (-y)
    pts = [(0.0, 0.0), (5.0, 0.0), (5.0, 3.0), (4.0, 4.0), (2.0, 4.0), (0.0, 3.0),
           (0.0, 0.0), (0.0, -20.0)]
    p, t = _fiber(pts, z=0.0, speed=20.0)
    paths, ptimes = [p], [t]
    le = loop_entry_flags(paths)
    assert le == [True]
    knobs = dict(pressure=_P, voltage=_V, temperature=_T,
                 max_accel=2000.0, square_corner_velocity=6.0)

    def _max_chord_near_closure(loop_entry):
        op, _ot, _info = laid_fibers(paths, ptimes, model, loop_entry=loop_entry,
                                     **knobs)
        o = op[0]
        s = np.linalg.norm(np.diff(o[:, :2], axis=0), axis=1)
        near = np.hypot(o[:-1, 0], o[:-1, 1]) < 6.0      # the loop / its exit
        return float(s[near].max())

    # The catch-up out of the loop is SAMPLED (not collapsed into one chord) -- the
    # general next-corner window resolves it WITH the loop flag AND without it (so a
    # revert of that window clamp re-breaks the unflagged case and fails here).
    assert _max_chord_near_closure(le) < 1.0       # flagged -> sampled smoothly
    assert _max_chord_near_closure(None) < 1.0     # general fix covers it unflagged too


def test_linking_loop_junction_has_no_diameter_bead():
    """A linking-loop junction must not bead the dynamic diameter. The head flows
    STRAIGHT through it (the loop's lead-out keeps the previous fibre's heading).

    The diameter is BUFFERED over the jet's lag distance (`_buffer_speed`), so a
    sharp junction does not bead even WITHOUT loop_entry -- it was a ~CTS-clamped
    +40% spike before the buffer. loop_entry still carries cruise THROUGH the
    junction, which the centreline LAG needs because that is not buffered. So the
    junction sits at cruise either way and loop_entry never makes it worse."""
    from gcode_to_fibers import loop_entry_flags
    from mew_deposition import junction_speeds

    model = _model()
    f0p, f0t = _fiber([(0.0, 0.0), (10.0, 0.0)], z=0.0, speed=15.0)
    f1p, f1t = _fiber([(10.0, 0.0), (13.0, 0.0), (13.0, 3.0), (10.0, 3.0),
                       (10.0, 0.0), (10.0, -10.0)], z=0.02, speed=15.0)
    f1t = f1t + f0t[-1] + 0.5                      # fibre 1 follows fibre 0 in time
    paths, ptimes = [f0p, f1p], [f0t, f1t]

    le = loop_entry_flags(paths)
    assert le == [False, True]
    evs, xvs = junction_speeds(paths, ptimes, le)
    assert xvs[0] > 0.0 and evs[1] > 0.0          # speed carried across the junction
    assert evs[0] == 0.0 and xvs[1] == 0.0        # true fibre ends still at rest

    knobs = dict(pressure=_P, voltage=_V, temperature=_T,
                 max_accel=2000.0, square_corner_velocity=6.0)
    tk0, dk0 = diameter_by_time(paths, ptimes, model, loop_entry=None, **knobs)
    tk1, dk1 = diameter_by_time(paths, ptimes, model, loop_entry=le, **knobs)
    cruise = model.solve(ProcessParams(pressure=_P, voltage=_V, speed=15.0,
                                       temperature=_T)).fiber_diameter
    tj = float(f0t[-1])                            # the shared corner / loop entry
    d_pinned = float(np.interp(tj, tk0, dk0))      # no loop_entry: buffer still suppresses
    d_through = float(np.interp(tj, tk1, dk1))     # loop_entry: flows through
    # the buffer keeps even the no-loop_entry junction near cruise (a sharp stop barely
    # transfers to the diameter) -- it was a ~CTS-clamped +40% spike before the buffer
    assert d_pinned < cruise * 1.05
    assert abs(d_through - cruise) < 0.02 * cruise  # the through case is at cruise
    assert d_through <= d_pinned + 1e-9             # loop_entry never makes it worse


# --- dynamic-diameter jet BUFFER (the diameter relaxes over the lag distance) ----- #
def _cts_of(model):
    return model.solve(ProcessParams(pressure=_P, voltage=_V, speed=20.0,
                                     temperature=_T)).critical_speed


def test_buffer_suppresses_sharp_corner_bead():
    """The deposited diameter is buffered over the jet's lag distance, so a SHARP 90 deg
    corner -- where the head dips to scv over only tens of um -- barely beads: the jet
    cannot follow a sub-lag speed change (Hrynevich 2020's diameter 'buffer'). Without
    the buffer the corner would read the CTS-clamped diameter (tens of percent thick);
    with it the bead is a few percent. The buffer LENGTH is the calibrated lag distance
    -- no fitted constant of its own."""
    model = _model()
    feed = 2.0 * _cts_of(model)
    p, t = _fiber([(0.0, 0.0), (8.0, 0.0), (8.0, 8.0)], speed=feed)   # one 90 deg corner
    tk, dk = diameter_by_time([p], [t], model, pressure=_P, voltage=_V,
                              temperature=_T, max_accel=2000.0,
                              square_corner_velocity=6.0)
    cruise = model.fiber_diameter(model.flow_rate(_P, _T), feed)
    d_cts = model.fiber_diameter(model.flow_rate(_P, _T), _cts_of(model))  # clamp ceiling
    d_corner = float(np.interp(float(t[1]), tk, dk))
    assert d_corner < cruise * 1.10                  # buffer suppresses the sharp bead
    assert d_corner < 0.5 * (cruise + d_cts)         # well below the CTS-clamped spike


def test_buffer_preserves_long_feed_grade():
    """A genuine, SLOW speed change (over many mm, >> the lag distance) still beads --
    the buffer suppresses only sub-lag transients, not real grades. Two 20 mm legs at
    3x and 1.2x CTS: the slow leg must read clearly thicker."""
    model = _model()
    cts = _cts_of(model)
    vf, vs = 3.0 * cts, 1.2 * cts
    p = np.array([[0.0, 0.0, 0.0], [20.0, 0.0, 0.0], [40.0, 0.0, 0.0]], dtype=np.float64)
    t = np.array([0.0, 20.0 / vf, 20.0 / vf + 20.0 / vs], dtype=np.float64)
    tk, dk = diameter_by_time([p], [t], model, pressure=_P, voltage=_V,
                              temperature=_T, max_accel=2000.0,
                              square_corner_velocity=6.0)
    d_fast = float(np.interp(0.5 * (t[0] + t[1]), tk, dk))
    d_slow = float(np.interp(0.5 * (t[1] + t[2]), tk, dk))
    assert d_slow > d_fast * 1.2                      # the grade survives the buffer


def test_buffer_keeps_cruise_diameter_unbiased():
    """The buffer must not bias the steady cruise diameter: a long constant-feed run
    relaxes to exactly the mass-conservation value. (The IIR uses the exact
    zero-order-hold pole a = 1 - exp(-ds/L), so even one long cruise step relaxes
    fully -- no DC offset, which a naive ds/(ds+L) pole would leave.)"""
    model = _model()
    feed = 2.0 * _cts_of(model)
    p, t = _fiber([(0.0, 0.0), (60.0, 0.0)], speed=feed)   # long run; middle is post-ramp
    tk, dk = diameter_by_time([p], [t], model, pressure=_P, voltage=_V,
                              temperature=_T, max_accel=2000.0,
                              square_corner_velocity=6.0)
    cruise = model.fiber_diameter(model.flow_rate(_P, _T), feed)
    d_mid = float(np.interp(0.5 * float(t[-1]), tk, dk))
    assert abs(d_mid - cruise) < 0.02 * cruise


# --- mass conservation through an immediate 180 deg reversal ---------------- #
# WHY: a sag-ladder wall stroke runs out, stops and comes straight back over its own
# line. The touchdown cannot follow that: it stalls short of the commanded end, turns
# and briefly outruns the head, so the deposited ARC is much shorter than the
# commanded one -- on the reported run, 12.000 mm of wall commanded laid 10.169 mm,
# ratio 0.8474 with sd 0.000000 across all 150 wall visits, 100% of it at the two
# tips. That shortfall is the lag and it is correct (it vanishes as the lag -> 0).
# What was NOT correct is where the polymer went: the melt keeps arriving at Q while
# the touchdown is stalled, and keying the diameter to the HEAD speed threw it away.
# The whole repro print deposited 0.8500 of the volume the extruder delivered.
#
# COMPARATOR: none needed. Q = (pi/4) d^2 v at the touchdown is an identity -- the
# flow law and the clock, nothing of ours on the right-hand side.
# RED PROOF: on the head-speed build these read 0.8657 (clamp-free mass ratio) and
# exactly 1.0000 (tip thickness vs cruise -- the lookup was dead flat).
def _reversal_fixture(model, leg=10.0, nlegs=4, feed_mult=1.5):
    """A wall stroke in miniature: `nlegs` legs out and back along one line, above
    CTS, so every interior vertex is a full 180 deg reversal."""
    Q = model.flow_rate(_P, _T)
    cts = model.critical_speed(_V, model.working_distance, flow_rate=Q,
                               viscosity=model.viscosity(_T))
    feed = feed_mult * cts
    ys = [0.0 if i % 2 == 0 else leg for i in range(nlegs + 1)]
    p = np.array([[0.0, y, 0.0] for y in ys], dtype=np.float64)
    t = np.cumsum([0.0] + [leg / feed] * nlegs)
    tr = make_transform(model, pressure=_P, voltage=_V, temperature=_T,
                        dynamic_diameter=True)
    op, ot, (tk, dk) = tr([p], [t])
    d = np.asarray(op[0], dtype=np.float64)
    tv = np.asarray(ot[0], dtype=np.float64)
    seg = np.linalg.norm(np.diff(d[:, :2], axis=0), axis=1)
    dts = np.diff(tv)
    v = np.divide(seg, dts, out=np.zeros_like(seg), where=dts > 0.0)
    dd = np.interp(0.5 * (tv[1:] + tv[:-1]), tk, dk)
    return dict(Q=Q, cts=cts, feed=feed, seg=seg, dts=dts, v=v, dd=dd,
                tk=tk, dk=dk, cmd_arc=leg * nlegs, y=d[:, 1])


def test_mass_is_conserved_where_the_clamp_is_inactive():
    """Over the intervals the fibre lands AT OR ABOVE CTS no saturation is active, so
    mass conservation there is an exact identity with no allowance of any kind:

        sum (pi/4) d^2 ds  ==  Q * sum dt

    Below CTS the straight-fibre model stops holding (the jet coils) and d saturates
    at d_CTS, so those intervals are excluded -- that is the ONLY place the model is
    allowed to not place the melt, and it is stated rather than hidden."""
    model = _model()
    f = _reversal_fixture(model)
    hi = f["v"] >= f["cts"]
    assert hi.any(), "the fixture must spend most of its time above CTS"
    laid = float((np.pi / 4.0 * f["dd"] ** 2 * f["seg"])[hi].sum())
    delivered = float(f["Q"] * f["dts"][hi].sum())
    ratio = laid / delivered
    # the deposited arc really is much shorter than commanded -- that part is the lag
    assert f["seg"].sum() < 0.95 * f["cmd_arc"], "the fixture must actually reverse"
    assert abs(ratio - 1.0) < 0.01, (
        "mass must be conserved where nothing is clamped: laid %.6f mm^3 vs "
        "delivered %.6f mm^3, ratio %.4f" % (laid, delivered, ratio))


def test_reversal_tip_is_thicker_than_cruise():
    """The melt that arrives while the touchdown is stalled has to be IN the fibre.
    So the deposited diameter must rise through a reversal, up to the CTS saturation
    -- what the user sees at a sag-ladder wall tip is a bulb, not a needle laid at
    the cruise diameter. On the head-speed build this lookup was dead flat."""
    model = _model()
    f = _reversal_fixture(model)
    cruise = model.fiber_diameter(f["Q"], f["feed"])
    d_cts = model.fiber_diameter(f["Q"], f["cts"])
    assert f["dk"].max() > 1.05 * cruise, (
        "the tip must thicken: lookup spans %.4f..%.4f um, cruise %.4f um"
        % (f["dk"].min() * 1000, f["dk"].max() * 1000, cruise * 1000))
    assert f["dk"].max() <= d_cts * (1.0 + 1e-9), (
        "and must saturate at the CTS diameter %.4f um, not run away" % (d_cts * 1000))


def test_straight_run_diameter_is_the_commanded_one():
    """The landing speed equals the head speed wherever the lag is a constant offset,
    so a straight constant-feed fibre must still read the plain cruise diameter --
    the change is LOCAL to transients, and a scaffold's long runs do not move."""
    model = _model()
    Q = model.flow_rate(_P, _T)
    cts = model.critical_speed(_V, model.working_distance, flow_rate=Q,
                               viscosity=model.viscosity(_T))
    feed = 2.0 * cts
    p, t = _straight(length=60.0, n=241, speed=feed)
    tr = make_transform(model, pressure=_P, voltage=_V, temperature=_T,
                        dynamic_diameter=True)
    _op, ot, (tk, dk) = tr([p], [t])
    cruise = model.fiber_diameter(Q, feed)
    d_mid = float(np.interp(0.5 * float(np.asarray(ot[0])[-1]), tk, dk))
    assert abs(d_mid - cruise) < 0.01 * cruise, (
        "cruise diameter moved: %.5f um vs %.5f um" % (d_mid * 1000, cruise * 1000))


# --------------------------------------------------------------------------- #
# Corner resolution (`fill_per_tau`) -- the Export panel's refine-grid knob.
#
# It exists because the refine grid is uniform in TIME while the tracker's contact
# runs FASTER than the nozzle through a turn, so the same time step buys less arc
# exactly at the turnaround crest, the tightest bend on the path.
#
# RED PROOF, so these are not self-confirming: with `fill_per_tau` dropped from
# `make_transform`'s forwarding -- the knob accepted and ignored, which is how this
# wiring actually breaks -- `test_corner_resolution_reaches_the_transform` fails on
# its first assert (14 850 points at both 6 and 24) and
# `test_corner_resolution_sharpens_the_turnaround_crest` fails with 38.4 deg at
# both. Passing 6 explicitly stays byte-identical either way, so the byte-identity
# test CANNOT see the defect; it pins the default, it does not catch the wiring.
# --------------------------------------------------------------------------- #

def _turnaround(leg=8.0, n=41, gap=1.0, speed=15.0):
    """A serpentine hairpin: up one leg, across `gap`, back down. The 180 deg
    turn is a bare corner (no arc), which is the case the knob is for."""
    xs = np.linspace(0.0, leg, n)
    pts = ([(0.0, y) for y in xs]
           + [(gap, y) for y in xs[::-1]])
    return _fiber(pts, speed=speed)


def _max_turn_deg(xyz):
    """Sharpest direction change on a laid path, zero-length steps dropped (a
    repeated point has no direction and would read as a spurious corner)."""
    d = np.diff(np.asarray(xyz)[:, :2], axis=0)
    n = np.linalg.norm(d, axis=1)
    keep = n > 1e-12
    u = d[keep] / n[keep][:, None]
    if len(u) < 2:
        return 0.0
    dot = np.clip((u[:-1] * u[1:]).sum(1), -1.0, 1.0)
    return float(np.degrees(np.arccos(dot)).max())


def test_corner_resolution_default_is_byte_identical():
    """Passing the shipped 6 must reproduce omitting it exactly -- that is what
    lets the knob ship without moving anybody's existing export."""
    model = _model()
    p, t = _turnaround()
    kw = dict(pressure=_P, voltage=_V, temperature=_T)
    a = make_transform(model, **kw)([p], [t])[0][0]
    b = make_transform(model, fill_per_tau=6.0, **kw)([p], [t])[0][0]
    assert a.shape == b.shape, (a.shape, b.shape)
    assert np.array_equal(a, b)


def test_corner_resolution_reaches_the_transform():
    """A raised grid must reach `laid_fibers` through `make_transform` -- the seam
    mew_export, the player's live march and the Scaffold preview all install."""
    model = _model()
    p, t = _turnaround()
    kw = dict(pressure=_P, voltage=_V, temperature=_T)
    n6 = len(make_transform(model, fill_per_tau=6.0, **kw)([p], [t])[0][0])
    n24 = len(make_transform(model, fill_per_tau=24.0, **kw)([p], [t])[0][0])
    assert n24 > n6, (n6, n24)


def test_corner_resolution_sharpens_the_turnaround_crest():
    """The point of the knob: the worst laid turn falls as the grid rises, and
    keeps falling. Asserted as a DIRECTION plus a floor, never as fitted angles --
    the degrees move with the calibration, the claim does not.

    Deliberately NOT asserted: halving-per-doubling. The underlying scheme is
    first-order and does halve on a real scaffold (38.4 -> 22.6 -> 11.5 deg,
    measured), but on this small synthetic hairpin the per-step ratio runs
    1.41, 1.10, 1.96 across 6 -> 12 -> 24 -> 48. The stall between 12 and 24 is
    unexplained and unjudged; it is recorded here rather than banded away,
    because a ratio assert tuned until it passes would be pinning this fixture's
    accident, not the scheme."""
    model = _model()
    p, t = _turnaround()
    kw = dict(pressure=_P, voltage=_V, temperature=_T)
    worst = [_max_turn_deg(make_transform(model, fill_per_tau=f, **kw)([p], [t])[0][0])
             for f in (6.0, 12.0, 24.0, 48.0)]
    assert worst[0] > worst[1] > worst[2] > worst[3], worst
    assert worst[3] < 0.5 * worst[0], worst


def test_the_corner_refiner_flags_a_starved_curve_and_passes_a_dense_one():
    """The instrument's two controls, in one test, because a detector that never
    fires and one that always fires both read as evidence and are neither.

    A circular arc of known radius sampled coarsely MUST be flagged and the same
    arc sampled finely MUST NOT be, with the crossover where the arithmetic says
    it is: the sagitta over a step of `theta` is R*(1 - cos(theta/2)), and the
    trigger is `_REFINE_SPAN_RATIO * tol` measured across TWO steps.
    """
    R, tol, radius = 1.5, 0.002, 0.01          # 2 um on a 20 um fibre
    trigger = md._REFINE_SPAN_RATIO * tol

    def arc(step_deg):
        a = np.radians(np.arange(0.0, 90.0, step_deg))
        return np.column_stack([R * np.cos(a), R * np.sin(a), np.zeros(len(a))])

    # the bend penalty is r*theta, so solve for the step where dev + r*theta bites
    def dev_of(step_deg):
        th = np.radians(step_deg)
        return R * (1.0 - np.cos(th)) + radius * th

    coarse = next(s for s in np.arange(0.1, 30.0, 0.1) if dev_of(s) > 2.0 * trigger)
    fine = next(s for s in np.arange(0.01, 30.0, 0.01) if dev_of(s) > 0.5 * trigger)
    flagged = md._oversize_vertices(arc(coarse), tol, radius)
    passed = md._oversize_vertices(arc(fine * 0.5), tol, radius)
    assert len(flagged) > 0, (coarse, dev_of(coarse), trigger)
    assert len(passed) == 0, (fine * 0.5, dev_of(fine * 0.5), trigger,
                              len(passed))


def test_the_auto_corner_grid_is_off_unless_asked_for():
    """Omitting it, and passing None, must both reproduce the fixed-grid march
    exactly -- that is what lets the 4-axis default ship without moving a single
    flat export."""
    model = _model()
    p, t = _turnaround()
    kw = dict(pressure=_P, voltage=_V, temperature=_T)
    a = make_transform(model, **kw)([p], [t])[0][0]
    b = make_transform(model, corner_chord=None, **kw)([p], [t])[0][0]
    assert a.shape == b.shape, (a.shape, b.shape)
    assert np.array_equal(a, b)


def test_the_auto_corner_grid_refines_a_flat_crest_when_it_is_asked_to():
    """The engine itself is collector-agnostic: the 4-axis-only part is which
    CALLER turns it on. Handed the grid, a flat turnaround must come out with a
    finer crest than the fixed grid gave -- otherwise the parameter is inert and
    every tube measurement downstream is measuring nothing."""
    model = _model()
    p, t = _turnaround()
    kw = dict(pressure=_P, voltage=_V, temperature=_T)
    off = make_transform(model, **kw)([p], [t])[0][0]
    on = make_transform(model, corner_chord=md.auto_corner_grid(0.02),
                        **kw)([p], [t])[0][0]
    assert len(on) > len(off), (len(off), len(on))
    assert _max_turn_deg(on) < _max_turn_deg(off), (_max_turn_deg(off),
                                                    _max_turn_deg(on))


def test_the_auto_corner_grid_stays_under_its_own_ceiling():
    """The refined grid is bounded by path LENGTH, not by the corner count -- the
    whole reason the per-fibre point cap could not serve here. Forced to run its
    passes out, it must still stop at `_REFINE_POINTS_PER_D` samples per fibre
    diameter of path."""
    model = _model()
    p, t = _turnaround()
    kw = dict(pressure=_P, voltage=_V, temperature=_T)
    d = 0.02
    path = float(np.sum(np.linalg.norm(np.diff(np.asarray(p), axis=0), axis=1)))
    ceiling = md._REFINE_POINTS_PER_D * path / d
    saved = md._REFINE_MAX_PASSES
    md._REFINE_MAX_PASSES = 40                 # far past convergence
    try:
        on = make_transform(model, corner_chord=(1e-9, 0.5 * d, 192.0),
                            **kw)([p], [t])[0][0]
    finally:
        md._REFINE_MAX_PASSES = saved
    # +the cap's other arm (the per-fibre point cap) and the settle tail
    assert len(on) <= max(ceiling, 20000) + 1000, (len(on), ceiling)


# --------------------------------------------------------------------------- #
# the FIVE-AXIS clock -- `surface_ptimes`
#
# The only tests in this file that build a `RevolutionFrame`. Every other test
# here runs flat, and the four sag/deposition suites beside it run flat or on a
# cylinder, so nothing else in the repo can see this class at all: a metric
# wrong by 145% of a step once passed nine of them.
# --------------------------------------------------------------------------- #

def _dome(radius=10.0, span=(0.0, 12.0)):
    import scaffold_gcode
    return scaffold_gcode.surface_frame("dome", radius=radius, ring=20.0,
                                        half_angle_deg=20.0, span=span)


def _dome_paths(frame, layers=1, length=6.0, width=4.0, spacing=0.5,
                centre=6.0, sample_step=0.5, fd=0.02):
    """The WORLD XYZ paths one dome design projects to, in the collector frame.

    It was the unrolled `(u, v, 0)` chart until R1-2 of the
    arbitrary-collector task, and that is the currency that changed: a chart
    position is not a position at a dome's pole, where `v = rho*phi` collapses.
    `mew_deposition` takes world now and converts once, internally, through
    `sag_approx.chart_for(frame)`.
    """
    import mew_project
    import scaffold_gcode
    pattern = mew_project.serpentine_pattern(length, width, spacing,
                                             sample_step=sample_step)
    proj = scaffold_gcode.surface_layers(frame, pattern,
                                         np.array([centre, 0.0]), layers, fd,
                                         layer_rotation=0.0,
                                         per_layer_reprojection=True)
    return [np.asarray(pp.world, dtype=np.float64) for pp in proj]


def test_surface_ptimes_delivers_the_commanded_surface_speed():
    """The whole point of the clock: the speed `laid_fibers` RECOVERS is the
    speed the user typed.

    `laid_fibers` reads `v_seg = |diff(_metric_path(p, frame))| / dt` on the
    fibre's world development (`mew_deposition._Development`, a five-axis
    collector takes no chart), so this asserts against that same expression --
    if either grew a second definition of surface length this is what would go
    red.

    RED PROOF (world port, 2026-09-16): the chart-arc clock this replaced
    (`git show cd3c2c0:mew_deposition.py`'s `surface_ptimes`) recovers
    4.999335-4.999507 mm/s against a commanded 5.000 at 300 mm/min -- red. The
    raw 3D chord is NOT a red injection on this dome: on a sphere a chord is
    perpendicular to the normal at its midpoint foot, so its tangential length
    is the chord itself (miss 9.6e-15).
    """
    frame = _dome()
    paths = _dome_paths(frame)
    for mm_min in (300.0, 500.0, 1200.0):
        ptimes = md.surface_ptimes(paths, frame, mm_min)
        for p, t in zip(paths, ptimes):
            seg = np.linalg.norm(np.diff(md._metric_path(p, frame), axis=0),
                                 axis=1)
            v = seg / np.diff(t)
            assert np.allclose(v, mm_min / 60.0, rtol=1e-12, atol=1e-12), (
                mm_min, float(v.min()), float(v.max()))


def test_surface_ptimes_is_invariant_to_the_sample_step():
    """The reason uniform time sampling was rejected.

    The projector's point spacing is a PROJECTION artefact, so a clock that
    read it would make the drawn lag a function of Sample step. Halving the
    step must leave the total print TIME alone (to the projection's own
    resampling error), because the path length is unchanged.

    RED PROOF: under a uniform-time clock the same two designs take 5.80 s and
    11.20 s -- halving the sample step doubles the print, and would halve the
    drawn lag with nothing physical changed.
    """
    frame = _dome()
    coarse = _dome_paths(frame, sample_step=0.5)
    fine = _dome_paths(frame, sample_step=0.25)
    assert len(fine[0]) > len(coarse[0])          # the grids really do differ
    tc = md.surface_ptimes(coarse, frame, 600.0)[0][-1]
    tf = md.surface_ptimes(fine, frame, 600.0)[0][-1]
    assert abs(tf - tc) / tc < 0.02, (tc, tf)
    # ... whereas a uniform-time clock at one sample per step would not be:
    assert abs(len(fine[0]) - len(coarse[0])) / len(coarse[0]) > 0.5


def test_surface_ptimes_runs_one_clock_across_layers():
    """One modal clock, continued -- and the layer hop carries no time, which
    is a decision and not an oversight (see the helper's docstring)."""
    frame = _dome()
    paths = _dome_paths(frame, layers=3)
    assert len(paths) == 3
    ptimes = md.surface_ptimes(paths, frame, 600.0, t0=0.0)
    assert ptimes[0][0] == 0.0
    for a, b in zip(ptimes[:-1], ptimes[1:]):
        assert b[0] == a[-1], (float(a[-1]), float(b[0]))
        assert np.all(np.diff(b) > 0.0)


def test_surface_ptimes_refuses_a_non_positive_speed():
    frame = _dome()
    paths = _dome_paths(frame)
    for bad in (0.0, -1.0):
        try:
            md.surface_ptimes(paths, frame, bad)
        except ValueError:
            continue
        raise AssertionError("accepted speed %r" % (bad,))


def test_a_dome_reaches_the_curved_placement_and_the_lag_grows_with_speed():
    """The five-axis relay, end to end, at the level this file owns.

    Three things at once, because they are one claim: a `RevolutionFrame`
    routes to a `CurvedPlacement` rather than to a cylinder's
    `ContactPlacement`; below CTS the laid path IS the commanded path (there is
    no lag to draw, which is correct physics, not a passthrough bug); and above
    it the departure grows with speed.

    The deviation is measured in WORLD mm from each laid vertex to the
    commanded polyline, so a refined grid stays comparable to a coarse one.

    RED PROOF, and the FIRST version of this test did not have one -- dropping
    `frame=` from the transform call left it green, because a flat relay on a
    chart path still lags and still grows with speed. It only produces the WRONG
    lag. So the last assert compares the two calls directly: at 900 mm/min (2x
    this calibration's 7.500 mm/s CTS) the framed relay departs the commanded
    path by 0.0191 mm mean and the unframed one by 0.0278 mm -- a 46%
    overstatement that no other assert here can see. Re-run under the injection,
    it is red.
    """
    import mew_mandrel

    frame = _dome()
    paths = _dome_paths(frame)
    model = _model()
    placement = mew_mandrel.placement_for_frame(
        frame.with_nozzle_gap(model.working_distance), model.calibration)
    assert isinstance(placement, mew_mandrel.CurvedPlacement), placement
    assert getattr(placement, "per_point", False)

    cts = model.critical_speed(
        _V, model.working_distance, flow_rate=model.flow_rate(_P, _T),
        viscosity=model.viscosity(_T))
    assert cts > 0.0

    # `_dome_paths` and the relay both speak world now, so the conversion this
    # test used to do at every comparison is gone. Kept as a named identity
    # rather than deleted, because every assert below reads "in WORLD mm" and
    # that claim should still be visible at the call sites.
    def world(p):
        return np.asarray(p, dtype=np.float64)

    def deviation(A, B):
        seg = B[1:] - B[:-1]
        L2 = (seg * seg).sum(1)
        L2[L2 == 0.0] = 1e-30
        d = A[:, None, :] - B[None, :-1, :]
        u = np.clip((d * seg[None]).sum(2) / L2[None], 0.0, 1.0)
        foot = B[None, :-1, :] + u[:, :, None] * seg[None]
        return np.linalg.norm(A[:, None, :] - foot, axis=2).min(1)

    commanded = world(paths[0])
    means = {}
    for mm_min in (0.25 * cts * 60.0, 1.2 * cts * 60.0, 2.0 * cts * 60.0):
        ptimes = md.surface_ptimes(paths, frame, mm_min)
        out, _t = make_transform(model, pressure=_P, voltage=_V,
                                 temperature=_T)(paths, ptimes, frame=frame)
        means[mm_min] = float(deviation(
            world(np.asarray(out[0], dtype=np.float64)), commanded).mean())

    below, mid, fast = sorted(means)
    # BELOW CTS: NO LAG TO DRAW. It was `== 0.0` while the relay spoke the chart
    # and this test converted both sides with the same two calls; since R1-2 the
    # pass-through returns the world path through the chart's round trip, so the
    # residual is that round trip and nothing else -- 2.56e-16 mm on this design,
    # against a 0.02 mm fibre. The band is 1e-12 because that is where a real lag
    # would have to hide to pass, and the mid-CTS assert below wants 1e-4.
    assert means[below] <= 1e-12, means[below]
    assert means[mid] > 1e-4, means[mid]
    assert means[fast] > means[mid], (means[mid], means[fast])

    # `frame=` is LOAD-BEARING and this is the only assert that says so. Without
    # it `make_transform` takes its flat branch on a chart path: a plausible,
    # growing, wrong lag. Nothing above can tell the two apart.
    ptimes = md.surface_ptimes(paths, frame, fast)
    tf = make_transform(model, pressure=_P, voltage=_V, temperature=_T)
    framed = deviation(world(np.asarray(tf(paths, ptimes, frame=frame)[0][0],
                                        dtype=np.float64)), commanded).mean()
    tu = make_transform(model, pressure=_P, voltage=_V, temperature=_T)
    unframed = deviation(world(np.asarray(tu(paths, ptimes)[0][0],
                                          dtype=np.float64)), commanded).mean()
    assert unframed > 1.25 * framed, (framed, unframed)


def test_an_apex_centred_design_is_laid_on_the_collector():
    """The relay at the POLE, which is the case R1-2 exists for.

    `_dome_paths(centre=0.0)` puts the pattern over the axis of revolution,
    where the frame's own `v = rho*phi` divides by a vanishing radius. The claim
    is the physical one and needs no reference: every LAID point must still sit
    on the collector, i.e. its signed normal distance `h` -- the frame's own
    inverse, asked of the world point -- must stay inside the wall the design
    implies, and its meridian position must stay inside the pattern's own reach.

    Measured against the 6 mm-off-axis design in the same run, because "the apex
    behaves like the placement that already worked" is the actual bar and a
    band written down here would not be one.

    RED PROOF, run rather than argued. Feeding the relay the CHART path this
    fixture used to return (`pp.uv` with h = 0, the pre-R1-2 currency) puts laid
    points 2.44 mm off the collector -- 122 fibre diameters, against the 0.45 mm
    of real jet lag the working 6 mm design shows -- and the approximate drape
    refused the same design outright. Both numbers were
    measured through `gui_surface.build_surface_preview` on `bc7c085`.
    """
    frame = _dome()
    model = _model()
    cts = model.critical_speed(
        _V, model.working_distance, flow_rate=model.flow_rate(_P, _T),
        viscosity=model.viscosity(_T))
    fast = 2.0 * cts * 60.0

    out = {}
    for tag, centre in (("apex", 0.0), ("6mm", 6.0)):
        paths = _dome_paths(frame, centre=centre)
        ptimes = md.surface_ptimes(paths, frame, fast)
        laid, _t = make_transform(model, pressure=_P, voltage=_V,
                                  temperature=_T)(paths, ptimes, frame=frame)
        w = np.concatenate([np.asarray(p, dtype=np.float64) for p in laid])
        u, _v, h = frame.from_world(w[:, 0], w[:, 1], w[:, 2])
        out[tag] = (np.asarray(h, dtype=np.float64),
                    np.asarray(u, dtype=np.float64))

    h_apex, u_apex = out["apex"]
    h_6mm, u_6mm = out["6mm"]
    # one layer at 0.02 mm, so the wall is one diameter; allow one more for the
    # lag riding a curved surface, which is the same allowance the 6 mm design
    # is held to below
    band = 2.0 * 0.02
    assert np.abs(h_apex).max() <= band, float(np.abs(h_apex).max())
    # and the apex is not merely inside the band -- it is no further off the
    # collector than the placement that already worked
    assert np.abs(h_apex).max() <= 2.0 * max(np.abs(h_6mm).max(), 1e-6), (
        float(np.abs(h_apex).max()), float(np.abs(h_6mm).max()))
    # the pattern reaches the pole and does not fold past it
    assert u_apex.min() >= -1e-9, float(u_apex.min())
    # within 0.1 mm (5 fibre diameters) of the pole: 0.058 since the dome's
    # lag became the exact arc, which rounds the laid crossing
    # differently from the retired small-angle ratio; the fold-past check
    # above is the one that must hold exactly
    assert u_apex.min() < 0.1, float(u_apex.min())
    assert u_apex.max() < 6.0, float(u_apex.max())
    assert u_6mm.min() > 1.0, float(u_6mm.min())


def test_the_flat_relay_is_untouched_by_the_five_axis_clock():
    """`frame=None` is still the historical branch, byte for byte.

    The five-axis work must not have reached the plate. This is the local
    statement of it (the file-level digest in `test_mandrel_placement.py` was
    removed 2026-10-30).
    """
    model = _model()
    p, t = _straight()
    kw = dict(pressure=_P, voltage=_V, temperature=_T)
    a = make_transform(model, **kw)([p], [t])
    b = make_transform(model, **kw)([p], [t], frame=None)
    assert np.array_equal(np.asarray(a[0][0]), np.asarray(b[0][0]))
    assert np.array_equal(np.asarray(a[1][0]), np.asarray(b[1][0]))


def test_a_print_at_exactly_cts_does_not_raise():
    """The degenerate pass-through must return the SEED grid like every other
    return from `_lay_one`.

    At exactly CTS the steady lag is zero while `coiling` is still False, so
    `tau_min` is 0 and `_lay_one` takes its "no motion" pass-through -- which
    returned a 4-tuple into a 5-tuple unpack and raised `ValueError: not enough
    values to unpack`. The branch dates from `8d7c21b` and predates the sag /
    drawn-curve split that added `seed_times`.

    RED PROOF, run before the fix: this raised
    `ValueError: not enough values to unpack (expected 5, got 4)` at
    `mew_deposition.py:789`. It is NOT five-axis-specific -- the path below is
    flat, three-axis, and passes no frame at all. It was found from a curved
    preview and reproduced here with the collector taken away.

    Both arities are checked, because the `simulate_travel` branch had the same
    omission one element further along.
    """
    model = _model()
    pressure, voltage, temperature = _P, _V, _T
    cts = model.critical_speed(
        voltage, model.working_distance,
        flow_rate=model.flow_rate(pressure, temperature),
        viscosity=model.viscosity(temperature))
    assert cts > 0.0
    paths = [np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 2.0, 0.0]])]
    # EXACTLY CTS on every interval -- that is the whole trigger.
    ptimes = [np.array([0.0, 10.0 / cts, 12.0 / cts])]

    tr = make_transform(
        model, pressure=pressure, voltage=voltage, temperature=temperature)
    out, _t = tr(paths, ptimes)
    assert np.array_equal(np.asarray(out[0]), paths[0]),         "with no lag to trail, the pass-through must return the commanded path"

    tr_tv = make_transform(
        model, pressure=pressure, voltage=voltage, temperature=temperature,
        simulate_travel=True)
    out_tv, _t2 = tr_tv(paths, ptimes, [None])
    assert len(out_tv[0]) == len(paths[0])



if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
            print(f"PASS  {fn.__name__}")
        except Exception:  # noqa: BLE001 -- report all failures
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    raise SystemExit(1 if failed else 0)
