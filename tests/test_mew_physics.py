"""Tests for mew_physics: the physical SCALING LAWS, not fitted numbers.

COMPARATORS: the literature the laws are anchored to (Hrynevich 2020's own Eq.4
curve, Hrynevich's Eqs 1-5 transcribed independently here, and the paper repo's
TrackerCompare implementation by a different author), plus ratio invariants that
need no reference. Ratio-based wherever possible, so a calibration change cannot
silently break the physics.

Run:  python tests/test_mew_physics.py
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import dataclasses
import math

from mew_physics import (
    Calibration, MEWModel, Material, Nozzle, ProcessParams, Regime,
    integrate_contact_path, integrate_contact_path_2nd,
    integrate_contact_path_ray, RAY_SUBSTEPS,
)


def _model():
    return MEWModel()


def _params(pressure=8.0e4, voltage=4.0, speed=10.0, temperature=90.0):
    return ProcessParams(pressure=pressure, voltage=voltage,
                         speed=speed, temperature=temperature)


def approx(a, b, rel=1e-6):
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


# --- mass conservation: the diameter backbone ------------------------------ #
def test_mass_conservation_roundtrip():
    """d returned must satisfy Qv = (pi/4) d^2 v exactly."""
    m = _model()
    s = m.solve(_params())
    assert approx(s.flow_rate, math.pi / 4.0 * s.fiber_diameter ** 2 * 10.0)


def test_diameter_scales_as_inverse_sqrt_speed():
    """d ~ v^-0.5: 4x the speed -> half the diameter."""
    m = _model()
    d1 = m.solve(_params(speed=10.0)).fiber_diameter
    d4 = m.solve(_params(speed=40.0)).fiber_diameter
    assert approx(d1 / d4, 2.0, rel=1e-6)


def test_diameter_scales_as_sqrt_flow():
    """Qv ~ dP (Hagen-Poiseuille) and d ~ Qv^0.5, so 2x pressure -> sqrt(2)x d."""
    m = _model()
    d1 = m.solve(_params(pressure=8.0e4)).fiber_diameter
    d2 = m.solve(_params(pressure=1.6e5)).fiber_diameter
    assert approx(d2 / d1, math.sqrt(2.0), rel=1e-6)


# --- flow rate: Hagen-Poiseuille + Arrhenius ------------------------------- #
def test_flow_linear_in_pressure():
    m = _model()
    q1 = m.flow_rate(5.0e4, 90.0)
    q2 = m.flow_rate(1.0e5, 90.0)
    assert approx(q2 / q1, 2.0, rel=1e-6)


def test_no_flow_without_pressure():
    m = _model()
    assert m.flow_rate(0.0, 90.0) == 0.0
    assert m.flow_rate(-1.0, 90.0) == 0.0


def test_viscosity_falls_with_temperature():
    m = _model()
    assert m.viscosity(80.0) > m.viscosity(100.0)


def test_hotter_melt_flows_more_and_thickens_fibre():
    """Higher T -> lower viscosity -> more flow -> thicker fibre (monotonic part
    of the temperature effect; the experimental non-monotonic minimum is a
    higher-order effect deferred to calibration)."""
    m = _model()
    cool = m.solve(_params(temperature=80.0))
    hot = m.solve(_params(temperature=100.0))
    assert hot.flow_rate > cool.flow_rate
    assert hot.fiber_diameter > cool.fiber_diameter


# --- the key result: voltage drives lag/CTS, NOT diameter ------------------ #
def test_voltage_does_not_change_diameter():
    """At fixed flow and speed, mass conservation pins the diameter -- so voltage
    must not move it (matches the empirical 'voltage least influential')."""
    m = _model()
    low = m.solve(_params(voltage=3.0))
    high = m.solve(_params(voltage=6.0))
    assert approx(low.fiber_diameter, high.fiber_diameter, rel=1e-9)


# --- CTS: a fittable anchor that assumes nothing by default ---------------- #
def test_cts_is_constant_by_default():
    """Default exponents are 0, so CTS asserts NO voltage or gap dependence: it
    equals cts_ref regardless of voltage, and is the same at different gaps."""
    m = _model()
    assert approx(m.solve(_params(voltage=3.0)).critical_speed,
                  m.solve(_params(voltage=6.0)).critical_speed)
    assert approx(m.solve(_params()).critical_speed, m.calibration.cts_ref)
    # gap-independent too (matches: straight fibres at both 4.0 and 4.5 mm)
    m40 = MEWModel(working_distance=4.0)
    m45 = MEWModel(working_distance=4.5)
    assert approx(m40.solve(_params()).critical_speed,
                  m45.solve(_params()).critical_speed)


def test_cts_voltage_dependence_is_fittable():
    """Setting the voltage exponent (what a fit would do) makes CTS scale as
    (V/V_ref)^exp -- exp=1 -> linear, so doubling V from the ref doubles CTS."""
    cal = Calibration(cts_voltage_exp=1.0, cts_voltage_ref=5.0, cts_ref=7.5)
    m = MEWModel(calibration=cal)
    assert approx(m.solve(_params(voltage=5.0)).critical_speed, 7.5)
    assert approx(m.solve(_params(voltage=10.0)).critical_speed, 15.0)


def test_cts_gap_dependence_is_fittable():
    """Likewise the gap exponent: exp=1 -> CTS ~ gap/gap_ref."""
    cal = Calibration(cts_gap_exp=1.0, cts_gap_ref=4.0, cts_ref=7.5)
    assert approx(MEWModel(working_distance=4.0, calibration=cal)
                  .solve(_params()).critical_speed, 7.5)
    assert approx(MEWModel(working_distance=8.0, calibration=cal)
                  .solve(_params()).critical_speed, 15.0)


def test_cts_default_ignores_pressure_and_temperature():
    """With the flow/viscosity exponents at their default 0, CTS is independent of
    pressure AND temperature -- it stays the measured anchor, asserting no such
    dependence until a fit turns the hooks on (mirrors the voltage/gap default)."""
    m = _model()
    a = m.solve(_params(pressure=5.0e4, temperature=80.0)).critical_speed
    b = m.solve(_params(pressure=1.5e5, temperature=110.0)).critical_speed
    assert approx(a, b)
    assert approx(a, m.calibration.cts_ref)


def test_cts_flow_dependence_is_fittable():
    """PRESSURE enters CTS only through flow rate Q. With the flow exponent fit to
    1.0 (anchored to a reference Q), CTS scales as (Q/Q_ref)^1; since Q ~ pressure
    (Hagen-Poiseuille), doubling the pressure doubles Q and so doubles CTS."""
    ref = MEWModel()
    q_ref = ref.flow_rate(8.0e4, 90.0)                 # Q at the reference run
    cal = Calibration(cts_flow_exp=1.0, cts_flow_ref=q_ref, cts_ref=7.5)
    m = MEWModel(calibration=cal)
    assert approx(m.solve(_params(pressure=8.0e4)).critical_speed, 7.5)
    assert approx(m.solve(_params(pressure=1.6e5)).critical_speed, 15.0)


def test_cts_viscosity_dependence_is_fittable():
    """TEMPERATURE enters CTS through viscosity (and Q). With only the viscosity
    exponent fit (>0), a hotter melt (lower eta) lowers CTS: CTS ~ (eta/eta_ref)^exp."""
    ref = MEWModel()
    eta_ref = ref.viscosity(90.0)
    cal = Calibration(cts_visc_exp=1.0, cts_visc_ref=eta_ref, cts_ref=7.5)
    m = MEWModel(calibration=cal)
    assert approx(m.solve(_params(temperature=90.0)).critical_speed, 7.5)
    assert m.solve(_params(temperature=110.0)).critical_speed < 7.5   # hotter -> lower CTS


def test_cts_flow_exp_without_reference_raises():
    """Turning on a hook without its reference is a calibration error, not a silent
    factor-1: the model refuses rather than quietly ignoring the fit."""
    cal = Calibration(cts_flow_exp=1.0)                # cts_flow_ref left None
    m = MEWModel(calibration=cal)
    try:
        m.solve(_params())
    except ValueError:
        return
    raise AssertionError("expected ValueError when cts_flow_ref is missing")


# --- jet-lag angle behaviour ----------------------------------------------- #
def test_lag_angle_equals_floor_at_cts():
    """At v == CTS the angle equals whatever floor is configured, so a future
    rig-measured CTS lag can reset lag_angle_cts_deg without touching this test."""
    m = _model()
    cts = m.solve(_params()).critical_speed
    s = m.solve(_params(speed=cts))
    assert approx(s.speed_ratio, 1.0, rel=1e-6)
    assert approx(s.lag_angle_deg, m.calibration.lag_angle_cts_deg, rel=1e-6)


def test_lag_angle_grows_with_speed_and_is_bounded():
    m = _model()
    cts = m.solve(_params()).critical_speed
    a = m.solve(_params(speed=cts * 1.2)).lag_angle_deg
    b = m.solve(_params(speed=cts * 2.0)).lag_angle_deg
    assert a < b < 90.0


def test_lag_distance_matches_hrynevich_full_range():
    """COMPARATOR: Hrynevich et al. 2020's own directly-measured laydown-lag curve
    L_stable = 10.02*(1 - SR^-0.304) mm (n=7, adjusted R^2=0.9811, over SR 1.125-17).

    The model IS that curve with the gap divided out, so the agreement is EXACT
    rather than a fit and the 1e-9 tolerance is tight ON PURPOSE -- a loose one
    would be blind to a form swap. It does NOT test whether Eq.4 is right for OUR
    rig; that campaign lives in the paper repo."""
    m = MEWModel(working_distance=3.5)          # Hrynevich's standard gap
    cts = m.solve(_params()).critical_speed
    for sr in (1.125, 1.25, 1.5, 2.0, 2.2, 3.0, 4.0, 6.0, 8.0, 12.0, 17.0):
        lag = m.solve(_params(speed=cts * sr)).lag_distance
        hrynevich = 10.02 * (1.0 - sr ** -0.304)
        assert abs(lag - hrynevich) <= 1e-9 * hrynevich, (sr, lag, hrynevich)


def test_lag_saturates_not_diverges_at_extreme_speed():
    """The lag must stay BOUNDED in deep overstretch -- a 90 deg tangent ceiling
    once sent it to ~625 mm at 17*CTS. The bound is Eq.4's own asymptote A*H --
    10.02 mm at this gap, not the 4.02 mm the retired fitted ceiling imposed -- and
    this must NOT be re-tightened to a plateau the curve reaches: b = 0.304 makes
    the approach slow, so at 20*CTS the lag is still only 60% of A*H."""
    m = MEWModel(working_distance=3.5)
    cts = m.solve(_params()).critical_speed
    asymptote = m.working_distance * m.calibration.lag_amplitude
    for sr in (20.0, 100.0, 1e4, 1e9):
        lag = m.solve(_params(speed=cts * sr)).lag_distance
        assert lag < asymptote, (sr, lag, asymptote)
    lag_20 = m.solve(_params(speed=cts * 20.0)).lag_distance
    assert lag_20 < 6.0           # mm -- bounded, not the old 625 mm divergence
    # and it must still be RISING there, or the ceiling is back
    lag_40 = m.solve(_params(speed=cts * 40.0)).lag_distance
    assert lag_40 > lag_20 * 1.05, (lag_20, lag_40)


def test_lag_floor_does_not_move_the_ceiling():
    """A non-zero lag_angle_cts_deg must shift the lag AT CTS and leave the
    ASYMPTOTE alone. A supported path, not a hypothetical: the Calibration lag_*
    block tells a rig to set the floor once a sweep resolves a lag at CTS. The
    first cut of the power form added the floor on top of the full arctan, and
    since tan(x+y) != tan(x)+tan(y) a 2.9 deg floor inflated the asymptote by 19%
    -- caught by inspection rather than by a test, hence this one."""
    A = Calibration().lag_amplitude
    for floor in (0.0, 0.84, 2.9, 4.0):
        cal = dataclasses.replace(Calibration(), lag_angle_cts_deg=floor)
        m = MEWModel(calibration=cal, working_distance=3.5)
        at_cts = math.tan(math.radians(m.lag_angle(1.0 + 1e-9)))
        asym = math.tan(math.radians(m.lag_angle(1e12)))
        assert abs(at_cts - math.tan(math.radians(floor))) < 1e-6, (floor, at_cts)
        assert abs(asym / A - 1.0) < 1e-3, (floor, asym, A)


def test_lag_amplitude_scales_the_whole_law_exactly():
    """A scales the lag's RISE ABOVE THE FLOOR proportionally, at every speed
    ratio: it is an amplitude, not a reshaping. An A that changed the curve's
    SHAPE shows up here as a ratio drifting with eps, which is what fitting the
    amplitude and the exponent to the same data produces.

    WHAT THE FLOOR DOES, AND WHAT CHANGED. The law is
    `base + (A - base)*(1 - eps^-b)` with `base = tan(theta_CTS)`, so the floor is
    an INTERCEPT and A is the asymptote it climbs to. The RISE above the floor
    therefore scales as `(A - base)`, not as `A`, and the whole content of "A is
    an amplitude" is that this ratio does not depend on eps.

    The retired per-rig multiplier `k` sat outside the whole expression and so
    scaled the floor TOO; collapsing it into A ends that. It reaches no shipped
    print -- `lag_angle_cts_deg` is pinned to 0, where `(A - 0)/(A0 - 0)` is just
    `A/A0` and the two readings coincide -- which is why the loop runs both.
    """
    H = 3.5
    A0 = Calibration().lag_amplitude
    for floor in (0.0, 2.9):
        on_floor = H * math.tan(math.radians(floor))
        base = MEWModel(working_distance=H, calibration=dataclasses.replace(
            Calibration(), lag_angle_cts_deg=floor))
        for A in (0.7 * A0, 1.35 * A0, 2.0 * A0):
            m = MEWModel(working_distance=H, calibration=dataclasses.replace(
                Calibration(), lag_angle_cts_deg=floor, lag_amplitude=A))
            want = ((A - math.tan(math.radians(floor)))
                    / (A0 - math.tan(math.radians(floor))))
            for eps in (1.0, 1.0 + 1e-9, 1.2, 3.0, 12.0, 1e6):
                a = H * math.tan(math.radians(base.lag_angle(eps))) - on_floor
                b = H * math.tan(math.radians(m.lag_angle(eps))) - on_floor
                if abs(a) < 1e-12:
                    assert abs(b) < 1e-9, (floor, A, eps, b)
                    continue
                assert abs(b / a - want) < 1e-6, (floor, A, eps, b / a, want)


def test_lag_amplitude_is_hrynevichs_published_ratio():
    """The default IS Eq.4's amplitude with the gap divided out, 10.02/3.5, and
    is stored as that ratio rather than as an angle whose tangent is it.

    The angle form (`lag_ceiling_angle_deg = 70.745620335`, retired) tangented to
    2.8628571428152698 -- right to 11 digits and no further, with nine decimals of
    angle spent buying that. The ratio is exact, which is what this asserts."""
    assert Calibration().lag_amplitude == 10.02 / 3.5
    m = MEWModel(working_distance=3.5)
    # ... and the asymptote it implies is Hrynevich's own 10.02 mm at her gap
    assert abs(m.lag_ray_constants()[0] - 10.02) < 1e-12


def test_the_ray_tracker_carries_the_same_amplitude_as_the_lag_law():
    """`lag_ray_constants` must compose to `solve`'s own lag: the tracker
    re-derives the lag from (ceiling, floor, exp) to normalise V_JCP, so an
    amplitude that reached `lag_angle` and not these three would mis-size the ray
    everywhere. This is why the helper exists rather than two copies of the
    expression in `mew_deposition` and `mew_live`.

    RED CONTROL, in the test itself: `wrong` is the same expression at the
    SHIPPED amplitude, and it must miss by exactly the ratio of the two.
    """
    H = 3.5
    A0 = Calibration().lag_amplitude
    for floor in (0.0, 2.9):
        for A in (A0, 1.35 * A0):
            cal = dataclasses.replace(Calibration(), lag_angle_cts_deg=floor,
                                      lag_amplitude=A)
            m = MEWModel(working_distance=H, calibration=cal)
            amp, L0, b = m.lag_ray_constants()
            wrong = H * A0
            assert abs(amp / wrong - A / A0) < 1e-12, (floor, A)
            for eps in (1.2, 3.0, 12.0):
                from_ray = L0 + (amp - L0) * (1.0 - eps ** -b)
                from_law = H * math.tan(math.radians(m.lag_angle(eps)))
                assert abs(from_ray - from_law) < 1e-12, (floor, A, eps)


def test_lag_time_consistent_with_geometry():
    """lag_time == H*tan(theta)/v."""
    m = _model()
    s = m.solve(_params(speed=30.0))
    expected = m.working_distance * math.tan(math.radians(s.lag_angle_deg)) / 30.0
    assert approx(s.lag_time, expected)
    assert approx(s.lag_distance, m.working_distance * math.tan(math.radians(s.lag_angle_deg)))


# --- regime classification ------------------------------------------------- #
def test_regimes_across_cts():
    m = _model()
    cts = m.solve(_params()).critical_speed
    assert m.solve(_params(speed=cts * 0.5)).regime is Regime.COILING
    assert m.solve(_params(speed=cts * 1.5)).regime is Regime.STABLE
    assert m.solve(_params(speed=cts * 3.0)).regime is Regime.OVERSTRETCH


def test_straight_fibre_flag_tracks_cts():
    m = _model()
    cts = m.solve(_params()).critical_speed
    assert not m.solve(_params(speed=cts * 0.5)).straight_fibre
    assert m.solve(_params(speed=cts * 1.5)).straight_fibre


def test_stable_window_is_label_only():
    """The STABLE/OVERSTRETCH boundary (`stable_window`) moves the regime LABEL and
    nothing else: every other JetState field is identical when only the window changes.
    The one geometric boundary is eps=1 (recomputed in mew_deposition as
    below_cts = mean_v < cts), never this enum -- so the deposited fibre cannot depend
    on stable_window. Pins the regime dossier's central claim that the window (and the
    Regime label generally) drives no geometry."""
    cts = MEWModel().solve(_params()).critical_speed
    p = _params(speed=cts * 3.0)                                    # eps = 3
    narrow = MEWModel(calibration=Calibration(stable_window=2.2)).solve(p)
    wide = MEWModel(calibration=Calibration(stable_window=4.0)).solve(p)
    assert narrow.regime is Regime.OVERSTRETCH                      # eps=3 > 2.2
    assert wide.regime is Regime.STABLE                            # eps=3 < 4.0
    # The window changed the label only -- everything geometric is unchanged.
    assert narrow.speed_ratio == wide.speed_ratio
    assert narrow.fiber_diameter == wide.fiber_diameter
    assert narrow.lag_angle_deg == wide.lag_angle_deg
    assert narrow.lag_distance == wide.lag_distance
    assert narrow.straight_fibre == wide.straight_fibre


# --- robustness / guards --------------------------------------------------- #
def test_zero_speed_rejected():
    m = _model()
    try:
        m.solve(_params(speed=0.0))
    except ValueError:
        return
    raise AssertionError("expected ValueError for zero speed")


def test_zero_gap_rejected():
    try:
        MEWModel(working_distance=0.0)
    except ValueError:
        return
    raise AssertionError("expected ValueError for zero working distance")


def test_custom_config_threads_through():
    """A narrower nozzle thins the fibre (less throughput at fixed pressure)."""
    wide = MEWModel(nozzle=Nozzle(inner_radius=0.20))
    narrow = MEWModel(nozzle=Nozzle(inner_radius=0.10))
    assert narrow.solve(_params()).fiber_diameter < wide.solve(_params()).fiber_diameter


# --- real-machine anchor --------------------------------------------------- #
def test_matches_mewron_datapoint():
    """Real MEWron run the default calibration is anchored to: 3 psi, 5 kV,
    100 C, 450 mm/min (7.5 mm/s) produced ~10-20 um fibres, STRAIGHT at both
    4.0 and 4.5 mm gaps. Guards the viscosity_ref / cts_ref defaults against
    drift, and that the (default) model stays straight at both gaps."""
    psi = 6894.76
    run = ProcessParams(pressure=3 * psi, voltage=5.0,
                        speed=450 / 60.0, temperature=100.0)
    for gap in (4.0, 4.5):
        s = MEWModel(nozzle=Nozzle(), working_distance=gap).solve(run)
        assert 10.0 <= s.fiber_diameter * 1000.0 <= 20.0   # measured range
        assert s.straight_fibre                            # straight at both gaps
        assert 0.8 <= s.speed_ratio <= 1.2                 # at/above CTS


# --- transient contact dynamics (Phase 3) ---------------------------------- #
def _straight_run(speed, tau, duration, steps):
    """A nozzle moving at constant `speed` along +x for `duration`, sampled into
    `steps` intervals, plus the matching per-interval tau. Returns the integrated
    contact path and the sample times."""
    times = [duration * i / steps for i in range(steps + 1)]
    points = [(speed * t, 0.0) for t in times]
    relax = [tau] * steps
    return integrate_contact_path(points, times, relax), times


def test_contact_reduces_to_steady_lag():
    """The key invariant: on a constant-speed straight run the contact settles to
    trailing the nozzle by exactly v*tau -- i.e. it REPRODUCES the static lag the
    old (t - lag) shift gave, so Phase 3 only adds transients, never moves the
    calibrated steady geometry."""
    speed, tau = 10.0, 0.05            # v*tau = 0.5 mm steady trailing distance
    contact, times = _straight_run(speed, tau, duration=2.0, steps=400)
    nozzle_end = speed * times[-1]
    trail = nozzle_end - contact[-1][0]      # along +x
    assert approx(trail, speed * tau, rel=1e-3)


def test_contact_matches_model_lag_distance():
    """Wiring check: feeding tau = JetState.lag_time makes the steady trailing
    distance equal the model's lag_distance = H*tan(theta) -- the same number the
    jet renderer used as a static offset."""
    m = _model()
    s = m.solve(_params(speed=30.0))
    contact, times = _straight_run(30.0, s.lag_time, duration=2.0, steps=400)
    trail = 30.0 * times[-1] - contact[-1][0]
    assert approx(trail, s.lag_distance, rel=1e-3)


def test_contact_lag_builds_from_zero_at_start():
    """At ignition the contact sits under the nozzle (zero lag) and the trailing
    distance grows MONOTONICALLY toward the steady value -- the start-of-path
    under-deposition transient."""
    speed, tau = 10.0, 0.1
    contact, times = _straight_run(speed, tau, duration=1.0, steps=200)
    trails = [speed * t - c[0] for t, c in zip(times, contact)]
    assert approx(trails[0], 0.0, rel=1e-9)
    assert all(b >= a - 1e-12 for a, b in zip(trails, trails[1:]))   # monotone up
    assert trails[-1] < speed * tau                                  # not yet saturated early-on is fine
    assert approx(trails[-1], speed * tau, rel=1e-2)                 # but converged by 10 tau


def test_contact_catches_up_when_head_stops():
    """When the nozzle stops, the trailing contact keeps relaxing toward it and
    closes the gap -- the end-of-path over-deposition (pooling) transient."""
    # move, then dwell at the end position
    move_t = [0.0, 0.5, 1.0]
    move_p = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)]
    dwell_t = [1.0 + 0.1 * i for i in range(1, 21)]      # stand still for 2 s
    dwell_p = [(10.0, 0.0)] * 20
    times = move_t + dwell_t
    points = move_p + dwell_p
    relax = [0.1] * (len(points) - 1)
    contact = integrate_contact_path(points, times, relax)
    gap_at_stop = 10.0 - contact[2][0]
    gap_at_end = 10.0 - contact[-1][0]
    assert gap_at_stop > 0.0                 # was trailing while moving
    assert gap_at_end < gap_at_stop          # caught up during the dwell
    assert approx(gap_at_end, 0.0, rel=1e-3)


def test_contact_integrator_is_stable_under_coarse_steps():
    """A single huge step (dt >> tau) must not overshoot or blow up -- the contact
    is a convex blend of nozzle and prior contact, so it just lands at/short of the
    nozzle. Guards against the explicit-Euler instability the exact integrator
    avoids."""
    points = [(0.0, 0.0), (100.0, 0.0)]
    times = [0.0, 1000.0]                     # dt = 1000 s, tau = 0.01 s
    contact = integrate_contact_path(points, times, [0.01])
    assert 0.0 <= contact[1][0] <= 100.0      # bounded between the two endpoints
    # the nozzle was MOVING over the step (v = 0.1 mm/s), so the contact lands the
    # steady trail v*tau short of the end -- not overshooting it. That residual is
    # the correct physics; the point is it stays finite and bounded for any dt.
    assert approx(100.0 - contact[1][0], 0.1 * 0.01, rel=1e-3)


def test_integrator_tau_couples_rate_and_steady_offset():
    """A first-order lag's tau sets BOTH how fast it relaxes and the steady trail
    (v*tau): a larger tau lags more at a given instant AND settles further behind.
    This coupling is exactly why the player pins tau to the jet flight time rather
    than exposing an independent relaxation knob -- the two cannot be separated in a
    first-order model."""
    speed = 10.0
    short, _ = _straight_run(speed, 0.05, duration=0.05, steps=1)
    long, _ = _straight_run(speed, 0.15, duration=0.05, steps=1)
    # mid-transient: the larger-tau contact trails more at the same instant
    assert (speed * 0.05 - long[-1][0]) > (speed * 0.05 - short[-1][0])
    # and the steady trail is v*tau
    conv, times = _straight_run(speed, 0.05, duration=2.0, steps=400)
    assert approx(speed * times[-1] - conv[-1][0], speed * 0.05, rel=1e-3)


def test_contact_handles_zero_length_and_zero_tau():
    """Degenerate inputs are absorbed, not raised on: a zero-length (zero-dt) move
    leaves the contact put; a zero tau pins it to the nozzle."""
    pts = [(0.0, 0.0), (0.0, 0.0), (5.0, 0.0)]
    times = [0.0, 0.0, 1.0]                   # first interval is zero-dt
    contact = integrate_contact_path(pts, times, [0.1, 0.0])
    assert contact[1] == (0.0, 0.0)           # didn't move on the zero-dt step
    assert approx(contact[2][0], 5.0, rel=1e-9)   # zero tau -> rides the nozzle


# --- second-order whip (Phase 3.5) ----------------------------------------- #
def _straight_run_2nd(speed, tau, zeta, duration, steps):
    """A nozzle moving at constant `speed` along +x, integrated with the 2nd-order
    contact tracker. Returns (contact, times)."""
    times = [duration * i / steps for i in range(steps + 1)]
    points = [(speed * t, 0.0) for t in times]
    relax = [tau] * steps
    return integrate_contact_path_2nd(points, times, relax, zeta), times


def test_2nd_reduces_to_steady_lag_for_any_zeta():
    """The load-bearing invariant: omega = 2 zeta / tau pins the steady trailing
    distance to v*tau for EVERY zeta, so the damping knob never moves the
    calibrated steady lean -- it only adds transients."""
    speed, tau = 10.0, 0.05                 # v*tau = 0.5 mm steady trail
    for zeta in (0.4, 1.0, 2.5):
        contact, times = _straight_run_2nd(speed, tau, zeta, duration=3.0, steps=600)
        trail = speed * times[-1] - contact[-1][0]
        assert approx(trail, speed * tau, rel=1e-3), zeta


def test_2nd_high_zeta_matches_first_order():
    """As zeta -> infinity the second-order tracker reduces to the first-order
    integrator. A large zeta should track it closely on the same grid."""
    speed, tau = 10.0, 0.05
    times = [0.01 * i for i in range(201)]
    pts = [(speed * t, 0.0) for t in times]
    relax = [tau] * (len(times) - 1)
    first = integrate_contact_path(pts, times, relax)
    high = integrate_contact_path_2nd(pts, times, relax, zeta=50.0)
    assert all(approx(a[0], b[0], rel=2e-2) for a, b in zip(first, high))


def test_2nd_underdamped_overshoots_at_stop():
    """When the nozzle stops, an UNDERDAMPED contact overshoots -- it passes the
    nozzle position before settling back (the corner/stop whip). A critically
    damped contact must NOT overshoot."""
    move_t = [0.0, 0.5, 1.0]
    move_p = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)]
    dwell_t = [1.0 + 0.02 * i for i in range(1, 101)]      # stand still 2 s
    dwell_p = [(10.0, 0.0)] * 100
    times, points = move_t + dwell_t, move_p + dwell_p
    relax = [0.1] * (len(points) - 1)
    under = integrate_contact_path_2nd(points, times, relax, zeta=0.35)
    crit = integrate_contact_path_2nd(points, times, relax, zeta=1.0)
    # overshoot == the contact goes PAST the stopped nozzle (x > 10) at some point
    assert max(p[0] for p in under) > 10.0 + 1e-4         # underdamped overshoots
    assert max(p[0] for p in crit) <= 10.0 + 1e-6         # critical does not
    # both still settle onto the nozzle
    assert approx(under[-1][0], 10.0, rel=1e-3)
    assert approx(crit[-1][0], 10.0, rel=1e-3)


def test_2nd_starts_from_rest_under_the_nozzle():
    contact, times = _straight_run_2nd(10.0, 0.1, 0.5, duration=1.0, steps=200)
    assert approx(contact[0][0], 0.0, rel=1e-9)           # under the nozzle
    assert approx(contact[1][0], 0.0, rel=1e-2)           # ~at rest just after start


def test_2nd_stable_under_coarse_steps():
    """A single huge step (dt >> tau) must stay bounded, like the first-order
    integrator -- the exact transition has spectral radius < 1."""
    points = [(0.0, 0.0), (100.0, 0.0)]
    times = [0.0, 1000.0]                                 # dt = 1000 s, tau = 0.01
    for zeta in (0.3, 1.0, 3.0):
        contact = integrate_contact_path_2nd(points, times, [0.01], zeta)
        assert 0.0 <= contact[1][0] <= 100.0             # bounded between endpoints
        assert approx(100.0 - contact[1][0], 0.1 * 0.01, rel=1e-3)   # steady trail


def test_2nd_handles_zero_dt_and_zero_tau():
    pts = [(0.0, 0.0), (0.0, 0.0), (5.0, 0.0)]
    times = [0.0, 0.0, 1.0]                               # first interval zero-dt
    contact = integrate_contact_path_2nd(pts, times, [0.1, 0.0], zeta=0.5)
    assert contact[1] == (0.0, 0.0)                       # didn't move on zero-dt
    assert approx(contact[2][0], 5.0, rel=1e-9)           # zero tau -> rides nozzle


# --- the ray / V_JCP placement rule (Hrynevich 2020) ------------------------ #
# These are the guards for `integrate_contact_path_ray`, the SHIPPED placement rule.
# The two second-order tests above it guard `integrate_contact_path_2nd`, which is no
# longer a placement rule but IS the comparator this one was measured against, so it
# keeps its own coverage.
_RAY_MODEL = MEWModel()
_RAY_H = _RAY_MODEL.working_distance
_RAY_CTS = _RAY_MODEL.critical_speed(4.0, _RAY_H)
_RAY_AMP = _RAY_H * _RAY_MODEL.calibration.lag_amplitude
_RAY_L0 = _RAY_H * math.tan(math.radians(
    _RAY_MODEL.calibration.lag_angle_cts_deg))
_RAY_B = _RAY_MODEL.calibration.lag_saturation_exp
_RAY_LAW = dict(lag_ceiling=_RAY_AMP, lag_floor=_RAY_L0, saturation_exp=_RAY_B)


def _lag_at(v):
    """The shipped steady lag distance (mm) at nozzle speed v."""
    return _RAY_H * math.tan(math.radians(_RAY_MODEL.lag_angle(v / _RAY_CTS)))


def _ray_l_path(v, leg=14.0, n=401):
    """A 90 deg L walked at constant speed v, with the law's own tau."""
    xs = [leg * i / (n - 1) for i in range(n)]
    pts = [(x, 0.0) for x in xs] + [(leg, y) for y in xs[1:]]
    tms = [0.0]
    for a, b in zip(pts, pts[1:]):
        tms.append(tms[-1] + math.dist(a, b) / v)
    return pts, tms, [_lag_at(v) / v] * (len(pts) - 1)


def _cutback(contact, apex):
    """The closest the laid path comes to the commanded apex -- how much the turn
    was cut. TrackerCompare's M3-frozen metric, and the one the 24 lag squares
    scored the three trackers on."""
    return min(math.dist(p, apex) for p in contact)


def test_ray_is_hrynevich_equations_1_to_5():
    """The shipped tracker normalises V_JCP through each interval's own steady lag
    so that CTS cancels (which is what lets the accel limit / speed scale / mandrel
    seams keep reaching it). That algebra has to be checked against the equations as
    Hrynevich WRITE them -- with an explicit CTS -- not against itself. This is that
    transcription, independent code, same rule:

        V_MA = (RL - L)/dt              Eq. (1)
        SR   = V_MA/CTS                 Eq. (2)
        L'   = RL - V_JCP(L)*dt         Eq. (3)
        L_stable(SR) = amp*(1 - SR^-b)  Eq. (4), gap divided out
        V_JCP(L) = CTS*(1 - L/amp)^-1/b Eq. (5) = Eq. (4) inverted
    """
    def hry(points, times, relax, sub):
        c = [float(points[0][0]), float(points[0][1])]
        out = [tuple(c)]
        for k in range(len(points) - 1):
            dt = times[k + 1] - times[k]
            p0, p1 = points[k], points[k + 1]
            if dt <= 0.0 or relax[k] <= 1e-12:
                if dt > 0.0:
                    c = [float(p1[0]), float(p1[1])]
                out.append(tuple(c))
                continue
            h = dt / sub
            for j in range(sub):
                a0, a1 = j / sub, (j + 1) / sub
                n0 = [p0[d] + a0 * (p1[d] - p0[d]) for d in (0, 1)]
                n1 = [p0[d] + a1 * (p1[d] - p0[d]) for d in (0, 1)]
                L = math.dist(n0, c)
                ray = [n1[d] - c[d] for d in (0, 1)]
                RL = math.hypot(*ray)
                if RL <= 1e-15:
                    continue
                sr = ((RL - L) / h) / _RAY_CTS                        # Eq. (1),(2)
                Ls = _RAY_AMP * (1.0 - sr ** -_RAY_B) if sr > 1.0 else 0.0   # Eq.(4)
                vj = _RAY_CTS * (1.0 - L / _RAY_AMP) ** (-1.0 / _RAY_B)      # Eq.(5)
                Ln = RL - vj * h                                             # Eq.(3)
                Ln = min(Ln, Ls) if L < Ls else max(Ln, Ls)
                Ln = min(max(Ln, 0.0), RL)
                u = Ln / RL
                c = [n1[d] - u * ray[d] for d in (0, 1)]
            out.append(tuple(c))
        return out

    assert _RAY_L0 == 0.0, "this transcription assumes the pinned theta_cts = 0"
    for eps in (1.5, 3.0, 6.0):
        for sub in (1, 4, 16):
            pts, tms, relax = _ray_l_path(eps * _RAY_CTS, n=61)
            mine = integrate_contact_path_ray(pts, tms, relax, substeps=sub,
                                              **_RAY_LAW)
            theirs = hry(pts, tms, relax, sub)
            worst = max(math.dist(a, b) for a, b in zip(mine, theirs))
            assert worst < 1e-12, (eps, sub, worst)


def test_ray_matches_the_paper_repo_reference():
    """The same check against the OTHER author's implementation -- the paper's
    tracker comparison script, the code that produced the corner-cutback numbers
    this swap was decided on. It is not published with this repository, so the
    check skips wherever it is absent, which is why the transcription test above
    exists as well."""
    import os
    import sys
    # The folder holding the paper's trackers.py, named by the environment.
    ref_dir = os.environ.get("MEWRACLE_TRACKER_REF", "")
    if not ref_dir or not os.path.isfile(os.path.join(ref_dir, "trackers.py")):
        return                                    # reference not present: skip
    sys.path.insert(0, ref_dir)
    try:
        import trackers as TR
    finally:
        sys.path.remove(ref_dir)
    TR.hry_config(_RAY_MODEL, _RAY_CTS)
    try:
        for eps in (1.5, 3.0, 6.0):
            for sub in (1, 6, 16):
                TR.SUBSTEPS = sub
                pts, tms, relax = _ray_l_path(eps * _RAY_CTS, n=61)
                theirs = TR.integrate_contact_path_hry(pts, tms, relax)
                mine = integrate_contact_path_ray(pts, tms, relax, substeps=sub,
                                                  **_RAY_LAW)
                worst = max(math.dist(a, b) for a, b in zip(mine, theirs))
                assert worst < 1e-12, (eps, sub, worst)
    finally:
        TR.SUBSTEPS = 1


def test_ray_reduces_to_the_steady_lag_on_a_straight_run():
    """The load-bearing invariant, shared with both filters: on a straight constant-
    speed run the contact settles trailing by exactly the lag the law asks for. This
    is what makes the swap a change of TRANSIENT only -- the calibrated steady
    geometry must not move."""
    for eps in (1.5, 3.0, 6.0):
        v = eps * _RAY_CTS
        n = 3000
        tms = [6.0 * i / (n - 1) for i in range(n)]
        pts = [(v * t, 0.0) for t in tms]
        relax = [_lag_at(v) / v] * (n - 1)
        c = integrate_contact_path_ray(pts, tms, relax, **_RAY_LAW)
        assert approx(v * tms[-1] - c[-1][0], _lag_at(v), rel=1e-4), eps


def test_ray_rounds_corners_more_than_the_retired_filter():
    """THE reason for the swap, as a direction rather than a fitted number: the
    second-order filter under-rounds. Measured on our 24 lag squares its corner-
    cutback bias was -0.276 mm at H = 3.5 mm and -0.712 mm at 5.5 mm, 8/8 cells the
    same way at both gaps, while the ray tracker was +0.076 / -0.187 mm. So at every
    speed the ray tracker must cut the corner MORE than zeta = 1 does.

    The squares are a WEAK instrument, though -- the three candidates separate by
    only 1.5-7.6% of a side -- so they REJECT zeta = 1 without ranking the survivors.
    The tie-break was that the ray step is a published MEW method with no free knob.

    RED CONTROL, in the assertion itself: the same comparison against the retired
    filter run as the candidate is asserted to FAIL, so a harness that had lost hold
    of one of the two rules could not report this as a pass."""
    apex = (14.0, 0.0)
    for eps in (1.25, 2.0, 3.0, 6.0):
        pts, tms, relax = _ray_l_path(eps * _RAY_CTS)
        ray = _cutback(integrate_contact_path_ray(pts, tms, relax, **_RAY_LAW), apex)
        z1 = _cutback(integrate_contact_path_2nd(pts, tms, relax, 1.0), apex)
        assert ray > z1 * 1.05, (eps, ray, z1)      # the swap, at every speed
        assert not (z1 > z1 * 1.05)                 # the control: filter vs itself


def test_ray_lag_is_not_a_component_filter_sagging():
    """The trap this docstring fell into once. The obvious story for why a component
    filter under-rounds is that it lets the trail |C - N| SAG through a bend. It does
    not: across a right angle a PERFECT path-following trail of length L reads a
    straight-line chord of L/sqrt(2) at its closest, and that is what the filter
    reads. Pinning it here so the sag story cannot come back as an explanation."""
    v = 3.0 * _RAY_CTS
    pts, tms, relax = _ray_l_path(v)
    L = _lag_at(v)
    z1 = integrate_contact_path_2nd(pts, tms, relax, 1.0)
    settled = list(zip(pts, z1))[len(pts) // 3:]
    lo = min(math.dist(p, c) for p, c in settled) / L
    assert approx(lo, 1.0 / math.sqrt(2.0), rel=0.05), lo


def test_ray_survives_a_rescaled_integrator_clock():
    """The seam that made this tracker take no CTS argument. Callers size tau as
    lag(v_effective)/v_naive -- the accel-limited speed, the live speed-scale what-if
    and the mandrel's along-track lag all arrive that way -- so the integrator's clock
    and the physics speed differ. The steady trail must then be lag(v_effective), not
    lag(v_naive): the tracker has to read the steady lag back out of tau rather than
    recomputing it from a CTS it holds itself."""
    for v_naive, scale in ((10.0, 2.0), (30.0, 0.5), (20.0, 1.0)):
        v_eff = v_naive * scale
        n = 3000
        tms = [6.0 * i / (n - 1) for i in range(n)]
        pts = [(v_naive * t, 0.0) for t in tms]
        relax = [_lag_at(v_eff) / v_naive] * (n - 1)
        c = integrate_contact_path_ray(pts, tms, relax, **_RAY_LAW)
        trail = v_naive * tms[-1] - c[-1][0]
        assert approx(trail, _lag_at(v_eff), rel=1e-6), (v_naive, scale, trail)


def test_ray_settles_onto_a_stopped_nozzle():
    """The settle tail: the head stops and the in-flight material still lands, so the
    contact must run in to the nozzle. A stationary interval has v_nozzle = 0, which
    is exactly where the normalisation would divide by nothing -- the speed factor is
    carried forward from the last moving interval instead. Hrynevich's own note that
    at a full stop the measured V_JCP exceeds the steady value is the same effect."""
    v = 3.0 * _RAY_CTS
    tau = _lag_at(v) / v
    n = 400
    tms = [4.0 * tau * i / (n - 1) for i in range(n)]
    pts = [(v * t, 0.0) for t in tms]
    dwell_t = [tms[-1] + 8.0 * tau * i / 200 for i in range(1, 201)]
    tms, pts = tms + dwell_t, pts + [pts[-1]] * 200
    relax = [tau] * (len(pts) - 1)
    c = integrate_contact_path_ray(pts, tms, relax, **_RAY_LAW)
    assert math.dist(c[-1], pts[-1]) < 0.02 * _lag_at(v)     # ran in
    assert max(p[0] for p in c) <= pts[-1][0] + 1e-9         # never overshot


def test_ray_converges_first_order_in_the_substep_count():
    """The whole reason `RAY_SUBSTEPS` exists. The update is first-order accurate in
    dt, so halving the step must halve the error -- and at sub = 1 the error must be
    LARGE, else there would be nothing to sub-step for and the default would be
    unjustified."""
    pts, tms, relax = _ray_l_path(3.0 * _RAY_CTS)
    ref = integrate_contact_path_ray(pts, tms, relax, substeps=1024, **_RAY_LAW)

    def err(sub):
        got = integrate_contact_path_ray(pts, tms, relax, substeps=sub, **_RAY_LAW)
        return max(math.dist(a, b) for a, b in zip(got, ref))

    e1 = err(1)
    assert e1 > 5.0e-3, e1                        # control: sub = 1 is really coarse
    prev = e1
    for sub in (2, 4, 8, 16, 32):
        e = err(sub)
        assert 1.8 < prev / e < 2.2, (sub, prev, e)     # first order, per doubling
        prev = e
    assert err(RAY_SUBSTEPS) < 1.0e-3, err(RAY_SUBSTEPS)   # the shipped default


def test_ray_2d_fast_path_matches_the_general_one():
    """`integrate_contact_path_ray` carries a scalar 2-D inner loop because both
    production callers are 2-D and the loop runs `substeps` times per sample. Two
    branches are only allowed to exist if they are the same function: run the same
    geometry through the general N-D branch by padding a constant z, and require
    BIT-identical x and y."""
    for eps in (1.25, 3.0, 6.0):
        pts, tms, relax = _ray_l_path(eps * _RAY_CTS, n=121)
        flat = integrate_contact_path_ray(pts, tms, relax, **_RAY_LAW)
        pad = integrate_contact_path_ray([(x, y, 0.0) for x, y in pts], tms,
                                         relax, **_RAY_LAW)
        for a, b in zip(flat, pad):
            assert a[0] == b[0] and a[1] == b[1], (eps, a, b)


def _ray_split_matches_whole(pts, tms, relax, k, **kw):
    """Lay a path whole, then in two halves with the state threaded across the seam,
    and return the worst disagreement."""
    whole = integrate_contact_path_ray(pts, tms, relax, **kw, **_RAY_LAW)
    a, mid = integrate_contact_path_ray(pts[:k], tms[:k], relax[:k - 1],
                                        with_state=True, **kw, **_RAY_LAW)
    b = integrate_contact_path_ray(pts[k - 1:], tms[k - 1:], relax[k - 2:],
                                   init=mid, **kw, **_RAY_LAW)
    joined = list(a) + list(b)[1:]
    assert len(joined) == len(whole)
    return max(math.dist(p, q) for p, q in zip(joined, whole))


def test_ray_carries_and_reports_contact_state():
    """The continuous-jet seam: a jet arriving from a travel is already trailing, so
    the tracker must start from a handed-in contact and be able to report its own
    final state. Splitting a path in two and threading the state must reproduce the
    single-shot answer exactly -- mid-motion, and ALSO on a stationary sample.

    The stationary case is the one worth having. The handed-over state has to include
    the speed factor K, because a stopped interval carries no speed to recompute it
    from; without it the resumed run reads "nothing has moved yet" and the contact
    freezes where it stood instead of running in. Splitting mid-straight cannot see
    that -- an earlier version of this test did exactly that and passed while the gap
    was open, which is why the dwell split below is here."""
    v = 3.0 * _RAY_CTS
    pts, tms, relax = _ray_l_path(v, n=201)
    assert _ray_split_matches_whole(pts, tms, relax, 150) < 1e-12   # mid-motion

    # ... and across a settle tail, where K cannot be recovered from the geometry
    tau = _lag_at(v) / v
    dwell_t = [tms[-1] + 6.0 * tau * i / 120 for i in range(1, 121)]
    pts2, tms2 = pts + [pts[-1]] * 120, tms + dwell_t
    relax2 = relax + [tau] * 120
    k = len(pts) + 4                       # a few samples INTO the dwell
    assert pts2[k - 1] == pts2[k - 2], "split point is not stationary"
    assert _ray_split_matches_whole(pts2, tms2, relax2, k) < 1e-12

    _, state = integrate_contact_path_ray(pts, tms, relax, with_state=True,
                                          **_RAY_LAW)
    assert len(state) == 3 and len(state[0]) == 2 and state[2] > 0.0


def test_ray_accepts_a_two_tuple_state_unchanged():
    """`mew_deposition._end_pin_state` hands over a (c, w) pair with no K -- a contact
    PINNED to the nozzle, i.e. below CTS with no lag to run in. That short form must
    keep working and mean exactly what it did: no inherited speed factor, the next
    moving interval computes one."""
    v = 3.0 * _RAY_CTS
    pts, tms, relax = _ray_l_path(v, n=81)
    two = integrate_contact_path_ray(pts, tms, relax,
                                     init=((pts[0][0], pts[0][1]), (0.0, 0.0)),
                                     **_RAY_LAW)
    three = integrate_contact_path_ray(pts, tms, relax,
                                       init=((pts[0][0], pts[0][1]), (0.0, 0.0),
                                             0.0), **_RAY_LAW)
    assert two == three


def test_ray_handles_zero_dt_and_zero_tau():
    """Degenerate inputs are absorbed, not raised on -- same contract as the two
    filters, because the same grids reach all three."""
    pts = [(0.0, 0.0), (0.0, 0.0), (5.0, 0.0)]
    times = [0.0, 0.0, 1.0]                              # first interval zero-dt
    c = integrate_contact_path_ray(pts, times, [0.1, 0.0], **_RAY_LAW)
    assert c[1] == (0.0, 0.0)                            # didn't move on zero-dt
    assert approx(c[2][0], 5.0, rel=1e-9)                # zero tau -> rides nozzle
    assert integrate_contact_path_ray([], [], [], **_RAY_LAW) == []


def test_ray_rejects_a_law_it_cannot_invert():
    """V_JCP is the lag law INVERTED, so a ceiling at or below the floor, or a zero
    exponent, has no inverse. Refuse rather than emit runaway geometry."""
    pts, tms, relax = _ray_l_path(3.0 * _RAY_CTS, n=21)
    for bad in (dict(lag_ceiling=0.0, lag_floor=0.0, saturation_exp=0.3),
                dict(lag_ceiling=1.0, lag_floor=2.0, saturation_exp=0.3),
                dict(lag_ceiling=1.0, lag_floor=0.0, saturation_exp=0.0)):
        try:
            integrate_contact_path_ray(pts, tms, relax, **bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted an uninvertible law: {bad}")


# --- cone-jet profile: the cone is the thick top of the mass-conservation jet --- #
def test_jet_radius_is_mass_conservation():
    """jet_radius is the diameter law in radius form: r = sqrt(Q/(pi v)) = half the
    fibre diameter at the same speed -- the single relation the cone, neck and fibre
    all obey."""
    m = _model()
    s = m.solve(_params(speed=10.0))
    r = m.jet_radius(s.flow_rate, 10.0)
    assert approx(r, math.sqrt(s.flow_rate / (math.pi * 10.0)), rel=1e-12)
    assert approx(2.0 * r, m.fiber_diameter(s.flow_rate, 10.0), rel=1e-12)


def test_cone_base_pinned_to_bore_for_any_flow():
    """THE fix: the cone base = jet_radius at the bore-exit speed = the bore radius
    EXACTLY, for any flow rate -- so it can never widen past the nozzle. v_exit scales
    with Q precisely so this holds."""
    m = _model()
    for Q in (1e-4, 5e-4, 2e-3, 1e-2):
        r_base = m.jet_radius(Q, m.nozzle_exit_speed(Q))
        assert approx(r_base, m.nozzle.inner_radius, rel=1e-12), Q


def test_cone_base_does_not_widen_with_pressure():
    """The reported bug, guarded: raising pressure must NOT widen the cone base -- it
    stays pinned at the bore (and equals JetState.cone_base_radius)."""
    m = _model()
    lo = m.solve(_params(pressure=2.0e4))
    hi = m.solve(_params(pressure=8.0e4))
    assert approx(lo.cone_base_radius, m.nozzle.inner_radius, rel=1e-12)
    assert approx(hi.cone_base_radius, lo.cone_base_radius, rel=1e-12)   # unchanged
    # ... while the FIBRE (the apex) does thicken with pressure -- the honest response
    assert hi.fiber_diameter > lo.fiber_diameter


def test_jet_speed_endpoints_pin_base_and_fibre():
    """The velocity profile spans exactly v_exit -> v_land, so the radius profile spans
    exactly r_nozzle -> the fibre radius: a base pinned at the bore, an apex at the
    fibre, with a smooth taper between."""
    m = _model()
    s = m.solve(_params(speed=8.0))
    Q, v_land = s.flow_rate, 8.0
    assert approx(m.jet_speed_at(Q, v_land, 0.0), m.nozzle_exit_speed(Q), rel=1e-12)
    assert approx(m.jet_speed_at(Q, v_land, 1.0), v_land, rel=1e-12)
    assert approx(m.jet_radius(Q, m.jet_speed_at(Q, v_land, 0.0)),
                  m.nozzle.inner_radius, rel=1e-12)                       # base = bore
    assert approx(m.jet_radius(Q, m.jet_speed_at(Q, v_land, 1.0)),
                  0.5 * s.fiber_diameter, rel=1e-12)                      # apex = fibre


def test_cone_profile_tapers_monotonically():
    """Down the jet (increasing drop fraction) the melt accelerates, so the radius
    DECREASES smoothly from the bore to the fibre -- the gradual cone->fibre transition,
    never a step or a bulge."""
    m = _model()
    s = m.solve(_params(speed=8.0))
    Q = s.flow_rate
    radii = [m.jet_radius(Q, m.jet_speed_at(Q, 8.0, k / 20.0)) for k in range(21)]
    assert all(b <= a + 1e-15 for a, b in zip(radii, radii[1:]))         # monotone down
    assert radii[0] > radii[-1] * 5.0                                    # a real cone


# --- CONE-KNEE-ASHOUR: cone HEIGHT (z-extent), the thing the user sees ------ #
# Ashour & Xu (2022), Polym. Adv. Technol. 33(9):3013 -- Fig. 2A (jet diameter vs
# spinline distance) + Fig. 1a (jet photograph), 1st layer, PCL at 3.5 mm / +4 kV /
# 23 G / 75 C, CTS 1.82 mm/s, final fibre 72 um. Their Eq.3 is v[mm/s] = 9057.5/d^2 --
# the SAME mass conservation this model uses -- so the measured diameter curve IS a
# measured v(s). The (s, d) pairs are DIGITISED BY EYE (raw data on request from the
# authors), so tolerances are deliberately loose: these guard the ANCHOR, not a fit.
_ASHOUR_K = 9057.5            # their Eq.3 constant
_ASHOUR_S_LAND = 3550.0       # spinline arc length at landing, um
_ASHOUR_D_BASE = 390.0        # measured cone-base diameter, um (NB > the 337 um 23G bore)
_ASHOUR_PROFILE = [(500, 250), (1000, 148), (1500, 115),
                   (2000, 100), (2500, 88), (3000, 80)]


def _ashour_model():
    """The model at Ashour's operating point, nozzle radius set from their MEASURED cone
    base (390 um) so the base/fibre RATIO (~5.4) matches theirs -- which is what the
    derived cone exponent depends on. cone_knee_frac is pinned to Ashour's OWN observed
    ~0.20 (their Fig.1a/2A) here, decoupled from the global default (0.12, a cross-rig
    visual compromise): this test guards that the MECHANISM reproduces Ashour's measured
    d(s) at Ashour's cone height, not that the shipped default matches their low-ratio rig."""
    return MEWModel(nozzle=Nozzle(inner_radius=_ASHOUR_D_BASE / 2000.0),
                    calibration=Calibration(cone_knee_frac=0.20))


def _knee_height_fraction(m, Q, v_land):
    """The height fraction at which the drawn jet has necked to HALF the base width --
    the cone's knee, i.e. how tall the cone reads. Found by scanning the profile."""
    r_base = m.jet_radius(Q, m.nozzle_exit_speed(Q))
    for i in range(1, 2001):
        frac = i / 2000.0
        if m.jet_radius(Q, m.jet_speed_at(Q, v_land, frac)) <= 0.5 * r_base:
            return frac
    return 1.0


def test_cone_knee_height_is_ratio_independent():
    """THE fix for the 'cone tiny in z' report, pinned. The cone knee (half-base width)
    must sit at ~cone_knee_frac of the gap WHATEVER the base/fibre ratio -- so a thin
    fibre from a fat bore (a 20:1 rig) gets the same tall cone as Ashour's 5.4:1.

    A FIXED exponent fails this: at 20:1 the half-base knee collapses to ~2% of the
    gap (the stubby nub the user saw). This test would fail hard on that."""
    knee_target = Calibration().cone_knee_frac
    # (fibre RADIUS um, bore RADIUS um) -- all genuine-cone ratios (bore >> fibre).
    for r_fibre_um, r_bore_um in ((36.0, 195.0),      # Ashour: d72/d390, ratio 5.4
                                  (12.5, 257.0),       # 21G bore, thin fibre: ratio 21
                                  (6.0, 257.0)):       # extreme thin fibre: ratio 43
        m = MEWModel(nozzle=Nozzle(inner_radius=r_bore_um / 1000.0))
        # pick Q, v_land to realise this fibre radius: r_fibre = sqrt(Q/(pi v_land))
        v_land = 8.0
        Q = math.pi * v_land * (r_fibre_um / 1000.0) ** 2
        knee = _knee_height_fraction(m, Q, v_land)
        assert abs(knee - knee_target) < 0.05, (
            f"fibre r={r_fibre_um} um / bore r={r_bore_um} um: knee at {knee:.0%} "
            f"of the gap, not ~{knee_target:.0%}")


def test_cone_profile_matches_ashour_spinline():
    """With cone_knee_frac anchored to Ashour, the DERIVED exponent at their ratio
    still reproduces their measured jet-diameter profile along the spinline (this is
    the same data the old fixed exponent was fit to -- the knee anchor preserves it)."""
    m = _ashour_model()
    Q = _ASHOUR_K * math.pi / 4.0 * 1e-6                 # invert Eq.3 -> mm^3/s
    v_land = _ASHOUR_K / 72.0 ** 2                       # from their final 72 um fibre
    errs = []
    for s_um, d_meas in _ASHOUR_PROFILE:
        v = m.jet_speed_at(Q, v_land, s_um / _ASHOUR_S_LAND)
        d_model = 2000.0 * m.jet_radius(Q, v)            # mm -> um diameter
        errs.append((d_model - d_meas) / d_meas)
    rms = math.sqrt(sum(e * e for e in errs) / len(errs))
    assert rms < 0.12, f"cone profile drifted off Ashour: RMS {rms:.1%}"


def test_ashour_landing_speed_corroborates_cts():
    """Independent corroboration of the whole mass-conservation chain: the landing speed
    implied by Ashour's measured 72 um fibre must land on the CTS they report separately
    (1.82 mm/s). Agreement to a few percent is what licenses using their diameter curve
    as a velocity curve at all."""
    v_land = _ASHOUR_K / 72.0 ** 2
    assert abs(v_land - 1.82) / 1.82 < 0.05


def test_cone_knee_is_invariant_at_both_endpoints():
    """cone_knee_frac reshapes ONLY the jet's interior: at drop 0 and 1 the profile is
    pinned to v_exit and v_land regardless, so the deposited diameter, CTS, lag and the
    .depo are invariant to it BY CONSTRUCTION -- the cone stays a zero-risk render knob."""
    Q, v_land = 0.007, 8.0
    lo = MEWModel(calibration=Calibration(cone_knee_frac=0.1))
    hi = MEWModel(calibration=Calibration(cone_knee_frac=0.4))
    for frac in (0.0, 1.0):
        assert approx(lo.jet_speed_at(Q, v_land, frac),
                      hi.jet_speed_at(Q, v_land, frac), rel=1e-12)
    # ... but the interior genuinely differs, or the knob would be inert
    assert not approx(lo.jet_speed_at(Q, v_land, 0.5),
                      hi.jet_speed_at(Q, v_land, 0.5), rel=1e-3)


def test_taller_knee_frac_makes_a_taller_cone():
    """A larger cone_knee_frac keeps the melt fat further down -- a taller cone, so at
    mid-jet the radius is larger. Pins the direction of the knob."""
    m_short = MEWModel(calibration=Calibration(cone_knee_frac=0.1))
    m_tall = MEWModel(calibration=Calibration(cone_knee_frac=0.35))
    Q = m_short.solve(_params()).flow_rate
    r_short = m_short.jet_radius(Q, m_short.jet_speed_at(Q, 7.5, 0.5))
    r_tall = m_tall.jet_radius(Q, m_tall.jet_speed_at(Q, 7.5, 0.5))
    assert r_tall > r_short


def test_nozzle_exit_speed_scales_with_flow():
    """v_exit = Q/(pi r^2): linear in flow, and the speed that pins the base."""
    m = _model()
    assert approx(m.nozzle_exit_speed(2e-3) / m.nozzle_exit_speed(1e-3), 2.0, rel=1e-12)
    r = m.nozzle.inner_radius
    assert approx(m.nozzle_exit_speed(1e-3), 1e-3 / (math.pi * r * r), rel=1e-12)


# --- bridge sag: the grounded tensioned-thread droop (opt-in) -------------- #
def test_bridge_sag_calibrated_defaults():
    """Pin the CALIBRATED magnitude anchor so it can't silently drift: the shipped
    default is c = sag_coeff (mm^-1) in Eq (7) depth = c*span^2 -- = the old 0.004 /
    0.01171 folded when d_ref was dropped from the default form, the same
    single-condition calibration re-expressed. (Tune later if the print data moves.)"""
    cal = Calibration()
    assert approx(cal.sag_coeff, 0.341588, rel=1e-9)            # c (mm^-1), rounded to round-trip the profile store
    assert approx(cal.sag_coeff, 0.004 / 0.01171, rel=1e-5)     # provenance: = old 0.004 / d_ref, unchanged calibration
    assert approx(cal.sag_diam_ref, 0.01171, rel=1e-12)         # kept for the opt-in diameter hook
    assert cal.sag_span_exp == 2.0 and cal.sag_diam_exp == 0.0   # diam exp -> 0: no unmeasured diameter dependence
    assert cal.sag_thermal_visc_exp == 0.0          # temperature hook off by default


def test_bridge_sag_default_is_c_span_squared_and_dref_inert():
    """The shipped default (diam exp 0) is depth = c * span^2 with c = sag_coeff:
    d_ref has DROPPED OUT of the default form, so it is genuinely inert -- changing
    sag_diam_ref leaves the rendered droop unchanged, and the depth equals
    sag_coeff * span^2 for any diameter. (Regression guard for the 2026-07-11 form:
    fails against the old sag_coeff*span^2/d_ref law.)"""
    span = 0.3
    base = MEWModel(calibration=Calibration())
    moved = MEWModel(calibration=Calibration(sag_diam_ref=0.05))   # move the anchor
    expected = base.calibration.sag_coeff * span ** 2
    assert approx(base.bridge_sag_depth(span, 0.02), expected, rel=1e-12)
    assert approx(base.bridge_sag_depth(span, 0.02),
                  moved.bridge_sag_depth(span, 0.02), rel=1e-12)   # d_ref inert at q=0


def test_bridge_sag_span_squared():
    """Tensioned-thread span law: 3x span -> 9x depth (sag_span_exp = 2)."""
    m = _model()
    d = m.calibration.sag_diam_ref
    ratio = m.bridge_sag_depth(0.9, d) / m.bridge_sag_depth(0.3, d)
    assert approx(ratio, 9.0, rel=1e-9)


def test_bridge_sag_diameter_squared_is_opt_in():
    """The +2 (constant-tension) choice is still SELECTABLE, just no longer the
    default: with sag_diam_exp=2 a THICKER fibre sags MORE (2x diameter -> 4x depth),
    the opposite of the geometric heuristic's unphysical 1/d. Pins the +2 option."""
    cal = Calibration(sag_diam_exp=2.0)
    m = MEWModel(calibration=cal)
    span, d = 0.3, cal.sag_diam_ref
    thin = m.bridge_sag_depth(span, d)
    thick = m.bridge_sag_depth(span, 2.0 * d)
    assert thick > thin                                   # thicker sags MORE
    assert approx(thick / thin, 4.0, rel=1e-9)
    assert approx(thin, cal.sag_coeff * span ** 2, rel=1e-9)   # anchored: (d/d_ref)^q = 1 at d = d_ref


def test_bridge_sag_diam_exp_zero_is_default_and_diameter_independent():
    """The DEFAULT: sag_diam_exp = 0 makes the droop diameter-INDEPENDENT
    (assert no unmeasured diameter dependence; unknown -> 0). Verified on the default
    Calibration() -- no explicit override needed."""
    m = _model()                                          # default calibration
    assert m.calibration.sag_diam_exp == 0.0
    span = 0.3
    a = m.bridge_sag_depth(span, 0.01)
    b = m.bridge_sag_depth(span, 0.05)
    assert approx(a, b, rel=1e-12)


def test_bridge_sag_multiplier_linear():
    m = _model()
    d = m.calibration.sag_diam_ref
    assert approx(m.bridge_sag_depth(0.3, d, 2.0)
                  / m.bridge_sag_depth(0.3, d, 1.0), 2.0, rel=1e-12)


def test_bridge_sag_accepts_array_span():
    """span may be a numpy array (the solver's vectorised `parab` path)."""
    import numpy as np
    m = _model()
    d = m.calibration.sag_diam_ref
    out = m.bridge_sag_depth(np.array([0.3, 0.6, 0.9]), d)
    assert isinstance(out, np.ndarray) and out.shape == (3,)
    assert approx(float(out[2]) / float(out[0]), 9.0, rel=1e-9)   # span^2


def test_make_sag_law_matches_method_thermal_off():
    """The installed closure equals bridge_sag_depth when the thermal hook is off
    (default), regardless of the bound temperature."""
    m = _model()
    d = m.calibration.sag_diam_ref
    law = m.make_sag_law(temperature=130.0)
    assert approx(law(0.4, d), m.bridge_sag_depth(0.4, d), rel=1e-12)


def test_make_sag_law_thermal_hook_hotter_sags_more():
    """With a negative viscosity exponent fitted, a hotter (less viscous, longer-
    molten) fibre sags MORE; at the reference temperature the factor is exactly 1."""
    cal = Calibration(sag_thermal_visc_exp=-1.0, sag_temp_ref=90.0)
    m = MEWModel(calibration=cal)
    d = cal.sag_diam_ref
    at_ref = m.make_sag_law(temperature=90.0)(0.3, d)
    hot = m.make_sag_law(temperature=120.0)(0.3, d)
    cold = m.make_sag_law(temperature=70.0)(0.3, d)
    assert hot > at_ref > cold                            # monotone in temperature
    assert approx(at_ref, m.bridge_sag_depth(0.3, d), rel=1e-12)   # factor 1 at T_ref


# -- viscous-creep sag mode (OPT-IN; the Nguyen 2019 reconciliation) ------------------ #

def test_viscous_creep_defaults_off():
    """The viscous-creep mode is OPT-IN: off by default, with the viscous-BENDING
    exponents (s^4 / d^-2) and a continuity magnitude anchor."""
    cal = Calibration()
    assert cal.sag_viscous_creep is False
    assert cal.sag_creep_span_exp == 4.0 and cal.sag_creep_diam_exp == -2.0
    assert approx(cal.sag_creep_span_ref, 0.5, rel=1e-12)


def test_viscous_creep_off_is_byte_identical_static():
    """Flag off (default): make_sag_law returns the STATIC law unchanged, even though
    the creep params exist -- so the default output stays byte-identical."""
    m = _model()
    d = m.calibration.sag_diam_ref
    law = m.make_sag_law(temperature=120.0)
    for span in (0.1, 0.3, 0.7):
        assert approx(law(span, d), m.bridge_sag_depth(span, d), rel=1e-12)


def test_viscous_creep_anchor_equals_coeff():
    """ON: at (span=sag_creep_span_ref, d=sag_diam_ref, T=T_ref) the depth is exactly
    sag_creep_coeff -- the anchored magnitude that encodes the creep time tau."""
    cal = Calibration(sag_viscous_creep=True)
    m = MEWModel(calibration=cal)
    tref = m.material.temperature_ref
    law = m.make_sag_law(temperature=tref)
    assert approx(law(cal.sag_creep_span_ref, cal.sag_diam_ref),
                  cal.sag_creep_coeff, rel=1e-9)
    assert approx(m.bridge_sag_depth_viscous(cal.sag_creep_span_ref, cal.sag_diam_ref),
                  cal.sag_creep_coeff, rel=1e-12)          # direct, viscosity_ratio=1


def test_viscous_creep_temperature_predicted_via_eta():
    """Temperature is PREDICTED, not a fitted hook: depth ~ 1/eta(T), so the depth
    ratio between two temperatures equals eta(T_ref)/eta(T) exactly, and hotter sags
    MORE (matching Nguyen 2019's dominant trend)."""
    cal = Calibration(sag_viscous_creep=True)
    m = MEWModel(calibration=cal)
    tref = m.material.temperature_ref
    s, d = cal.sag_creep_span_ref, cal.sag_diam_ref
    depth_ref = m.make_sag_law(temperature=tref)(s, d)
    depth_hot = m.make_sag_law(temperature=tref + 20.0)(s, d)
    assert depth_hot > depth_ref                            # hotter -> more sag
    assert approx(depth_hot / depth_ref,
                  m.viscosity(tref) / m.viscosity(tref + 20.0), rel=1e-9)


def test_viscous_creep_bending_exponents():
    """Default (bending) scalings at fixed T: span^4 (2x span -> 16x) and d^-2
    (2x diameter -> 0.25x, i.e. thicker sags LESS at fixed temperature; eta(T) is what
    makes hotter+thicker sag more across conditions)."""
    cal = Calibration(sag_viscous_creep=True)
    m = MEWModel(calibration=cal)
    s, d = cal.sag_creep_span_ref, cal.sag_diam_ref
    base = m.bridge_sag_depth_viscous(s, d)
    assert approx(m.bridge_sag_depth_viscous(2.0 * s, d) / base, 16.0, rel=1e-9)
    assert approx(m.bridge_sag_depth_viscous(s, 2.0 * d) / base, 0.25, rel=1e-9)


def _refused(fn, *args, **kw):
    """True if `fn` refuses with a NAMED error (ValueError), not a crash deep
    inside a law (ZeroDivisionError) and not a silent NaN."""
    try:
        fn(*args, **kw)
    except ValueError:
        return True
    return False


def test_impossible_process_values_are_refused_by_name():
    """The model refuses what cannot be a process value instead of computing
    through it. Found by a release-audit campaign: a gap of NaN
    passed the `<= 0` guard (lag distance NaN), a temperature of -273.15 degC
    raised ZeroDivisionError and -300 degC printed a 5.7e30 mm fibre, a NaN
    pressure / speed / temperature solved to NaN, and a zero viscosity anchor
    or nozzle bore raised ZeroDivisionError / math domain error from inside
    `flow_rate`. RED on cc329c7: every item below."""
    from mew_physics import MEWModel, Material, Nozzle, ProcessParams
    nan, inf = float("nan"), float("inf")
    bad = []
    for wd in (nan, inf):
        if not _refused(MEWModel, working_distance=wd):
            bad.append("MEWModel(working_distance=%r)" % wd)
    for kw in ({"material": Material(viscosity_ref=0.0)},
               {"material": Material(viscosity_ref=-1.0)},
               {"material": Material(viscosity_ref=nan)},
               {"nozzle": Nozzle(inner_radius=0.0)},
               {"nozzle": Nozzle(length=0.0)},
               {"nozzle": Nozzle(inner_radius=nan)}):
        if not _refused(MEWModel, **kw):
            bad.append("MEWModel(%r)" % kw)
    m = MEWModel(working_distance=4.0)
    for t in (-273.15, -300.0, nan):
        if not _refused(m.viscosity, t):
            bad.append("viscosity(%r)" % t)
    ok = dict(pressure=20684.0, voltage=6.0, speed=10.0, temperature=90.0)
    for key in ok:
        for val in (nan, inf):
            if not _refused(m.solve, ProcessParams(**dict(ok, **{key: val}))):
                bad.append("solve(%s=%r)" % (key, val))
    assert not bad, "accepted: " + "; ".join(bad)
    # and the in-range answer is untouched
    js = m.solve(ProcessParams(**ok))
    assert all(math.isfinite(v) for v in (js.fiber_diameter, js.lag_angle_deg,
                                          js.critical_speed))


def test_physics_export_refuses_impossible_process_values():
    """`mew_export` (what the GUI launches for a physics-deposited export)
    refuses a process value it cannot model, with its own message and exit 2,
    as it already did for a zero standoff. Before 2026-09-24 a NaN standoff,
    temperature or pressure exported rc 0 with a NaN or 0 um "dynamic
    diameter" in the log, and -273.15 degC died in a ZeroDivisionError
    traceback (`export_extremes.py`). RED on cc329c7: rc was 0 or 1."""
    import contextlib
    import io
    import mew_export
    bad = []
    for extra in (["--standoff", "nan"], ["--temperature", "nan"],
                  ["--temperature", "-273.15"], ["--temperature", "-400"],
                  ["--pressure", "nan"], ["--voltage", "inf"],
                  ["--pressure", "0", "--dynamic-diameter"]):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                rc = mew_export.main(["--target", "converter", *extra,
                                      "no_such_input.gcode", "-o", "x.stl"])
            except Exception as exc:  # noqa: BLE001 -- the defect being tested
                rc = repr(exc)
        if rc != 2 or "mew_export:" not in err.getvalue():
            bad.append("%s -> %r" % (" ".join(extra), rc))
    assert not bad, "not refused: " + "; ".join(bad)


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"PASS  {t.__name__}")
        except Exception:  # noqa: BLE001 -- test runner reports all failures
            failed += 1
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    raise SystemExit(1 if failed else 0)
