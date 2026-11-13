"""Forward-accumulating live re-lay of MEW deposition.

The engine behind the player's REAL-TIME PARAMETER ADJUSTMENT mode. It answers a
different question from `mew_deposition.laid_fibers`: not "lay the whole print at
one fixed parameter set" but "lay it while the operator changes parameters mid-run,
so fibre already laid keeps the look it was deposited with and only fibre laid
AFTER a change reflects the new settings".

WHY THIS IS A SMALL MODULE AND NOT A TIME-MARCHED REWRITE. The ray tracker is
CAUSAL -- contact[k] depends only on the nozzle path and the per-interval tau for
sub-intervals <= k -- so feeding it a tau(t) that records which parameters were
active at each deposition-time (a piecewise-constant `ParamSchedule`) makes the
contact geometry before any breakpoint mathematically identical whatever comes
after. Forward accumulation falls straight out: re-lay the whole fibre from the
schedule on every change, and the past is reproduced bit for bit while the seam
carries the physical relax transient rather than snapping.

What it computes:
* CENTRELINE X/Y -- the trailing touchdown locus with TIME-VARYING tau, exactly as
  `mew_deposition._lay_one` does for a fixed parameter set. MANDREL-AWARE:
  `frame` and `placement` are accepted and applied where that function
  applies them -- metric lengths, the along-track trail from
  `ContactPlacement.resolve`, and the relaxed lateral pre-offset -- so a tube
  re-laid LIVE gets the cylindrical contact the export gets, not the flat one
  (~135 um apart on a 6 mm mandrel at 45 deg). Both default to
  None, which takes no branch at all and keeps the flat relay byte-identical.
  This module still MIRRORS `_lay_one` rather than calling it, so the two must be
  kept in step by hand -- see `_place_interval`, and note that the mirroring has
  already gone wrong once (a `tau > 0` guard that skipped the below-CTS offset
  RESET, caught in review).
* Z IS LEFT AS THE COMMANDED LAYER HEIGHT, the same contract as `laid_fibers`; the
  player overlays the baseline sag droop by time, sag being reused, not re-solved.
* DIAMETER -- `diameter_schedule` builds the same (t_knots, d_knots) lookup shape
  `mew_deposition.diameter_by_time` produces.

Two deliberate differences from `mew_deposition`: below CTS is gated PER
SUB-INTERVAL rather than per fibre, so a live speed sweep coils only the portion it
pushes below CTS; and the transient-window fill step is LOCAL to each interval's
own tau, so a later parameter change cannot re-grid the frozen past.

Pure module in the `mew_physics` spirit: no VTK, no file I/O, no GUI -- and it does
NOT import `mew_deposition`, so it stays robust while that file is under edit; the
two small geometry helpers it needs are copied here. Units follow `mew_physics`.
"""

from __future__ import annotations

import math
from collections import namedtuple

import numpy as np

from mew_physics import (ProcessParams, coil_offset_series,
                         integrate_contact_path_ray)
from mew_coil_wave import below_cts_runs, coil_wave_reshape, overlay_wave_runs

# Accel-limited re-timing (optional; off by default so a plain re-lay is the
# byte-identical pre-kinematics behaviour). Mirrors mew_deposition's use: the lag +
# diameter then track the head's REAL ramped speed (corner / tight-arc slowdown).
try:
    from mew_kinematics import plan_trajectory, chart_seg_accels
except Exception:  # noqa: BLE001
    plan_trajectory = chart_seg_accels = None


def _seg_accel(p, planned_p, frame, max_accel, rotary_accel):
    """Per-segment accelerations under a separate rotary limit, or None (one
    limit, byte-identical). Mirrors mew_deposition._seg_accel; `planned_p` is
    the polyline handed to the planner, which here is sometimes the chart
    itself."""
    if chart_seg_accels is None:
        return None
    return chart_seg_accels(p, planned_p, frame, max_accel, rotary_accel)

__all__ = ["Params", "ParamSchedule", "relay_fibers", "diameter_schedule"]

# A direction change sharper than this (degrees) at an interior vertex is a
# "corner": the touchdown rounds/whips it over a few tau, so the time grid is
# refined there. Mirrors mew_deposition's _CORNER_DEG (copied to stay decoupled).
_CORNER_DEG = 20.0
# A fractional speed change this large between adjacent intervals also opens a
# transient window (e.g. a turnaround printed at a slower loop speed).
_SPEED_CHANGE_FRAC = 0.1
# Points per coil period when densifying the time grid for the below-CTS coil
# (mirrors mew_deposition._lay_one's samples_per_period -- enough to resolve loops).
_COIL_SAMPLES_PER_PERIOD = 40
# Per-corner floor for the ADAPTIVE point budget (mirrors mew_deposition's
# _MIN_FILL_PER_CORNER, copied to stay decoupled). In continuous MEW one layer is a
# single fibre, so a large fine-stepover serpentine has thousands of corners; a FLAT
# point cap thins the corner-rounding fill until the smooth touchdown arc reverts to
# a blocky polygon. The budget instead scales with the transient-event count so every
# corner keeps enough fill, however many there are; `max_points_per_fiber` still
# floors small fibres and bounds true pathology.
_MIN_FILL_PER_CORNER = 12


# --------------------------------------------------------------------------- #
# The parameter schedule: piecewise-constant process knobs vs deposition-time
# --------------------------------------------------------------------------- #
# pressure Pa, voltage kV, temperature degC, speed_scale dimensionless (the
# live print-speed what-if multiplier on the g-code feed). namedtuple so it is
# hashable (used as a solve-cache key) and immutable.
Params = namedtuple("Params", "pressure voltage temperature speed_scale")


class ParamSchedule:
    """Process parameters as a step function of DEPOSITION-time (s).

    Breakpoint i (time t_i, params P_i) applies for deposition-times in
    [t_i, t_{i+1}); t_0 is pinned to 0.0 so every time has a defined value. A
    live knob change appends a breakpoint at the deposition-time currently being
    laid (the jet's contact time), so everything already laid keeps the earlier
    params and the causal integrator freezes it (see module docstring).

    Pure data + lookups; no physics. Held by the player, fed to `relay_fibers` /
    `diameter_schedule`, and trivially serialisable for tests."""

    def __init__(self, base: Params):
        self._t = [0.0]
        self._p = [base]

    def set_at(self, t, params: Params):
        """Record `params` taking effect from deposition-time `t` forward.

        Editing at `t` means "from here on, use these knobs", so any existing
        breakpoint AT OR AFTER `t` is discarded first (the operator is re-deciding
        the future -- this is what makes a scrub-back-then-edit, or a rapid knob
        mash at one instant, collapse cleanly instead of stacking stale steps).
        The segment starting at `t` is then set (replacing an earlier breakpoint /
        the t=0 base if `t` coincides with it)."""
        t = max(0.0, float(t))
        while len(self._t) > 1 and self._t[-1] >= t - 1e-9:
            self._t.pop()
            self._p.pop()
        if abs(self._t[-1] - t) <= 1e-9:
            self._p[-1] = params          # coincides with the base / an earlier step
        else:
            self._t.append(t)
            self._p.append(params)

    def params_at(self, s) -> Params:
        """The params in force at deposition-time `s` (the last breakpoint <= s)."""
        # linear scan: the schedule has at most a handful of breakpoints (one per
        # live edit), so a bisect would be over-engineering.
        p = self._p[0]
        for ti, pi in zip(self._t, self._p):
            if ti <= s + 1e-12:
                p = pi
            else:
                break
        return p

    def reset(self, base: Params):
        """Collapse back to a single segment at `base` (the Reset button)."""
        self._t = [0.0]
        self._p = [base]

    def breakpoint_times_in(self, t0, t1):
        """Breakpoint times strictly inside (t0, t1) -- the live param steps the
        re-lay must refine its time grid around so the relax transient after each
        change is actually sampled (the start t0 is refined separately)."""
        return [ti for ti in self._t if t0 + 1e-12 < ti < t1 - 1e-12]


# --------------------------------------------------------------------------- #
# tau / diameter from the schedule (cached per rounded parameter set)
# --------------------------------------------------------------------------- #
def _solve_cache_key(p: Params, v_eff):
    # speed_scale is already folded into v_eff, so it is NOT part of the key
    return (round(p.pressure, 3), round(p.voltage, 6),
            round(p.temperature, 6), round(v_eff, 6))


def _cts_for(model, gap, p: Params, cts_cache):
    """CTS (mm/s) at params `p` and the working gap, cached by the knobs that move
    it. Mirrors MEWModel.solve / mew_deposition.laid_fibers: the flow (pressure) and
    viscosity (temperature) terms are passed so a CALIBRATED CTS (nonzero
    flow/viscosity exponents) gates the regime exactly as the HUD's solve() does --
    and so a fitted exponent never raises here for a missing flow_rate/viscosity.
    With the default exponents both are a factor of 1, so CTS is voltage&gap only and
    this is identical to the old voltage-keyed lookup."""
    key = (round(p.voltage, 6), round(p.pressure, 3), round(p.temperature, 6))
    hit = cts_cache.get(key)
    if hit is None:
        hit = model.critical_speed(
            p.voltage, gap,
            flow_rate=model.flow_rate(p.pressure, p.temperature),
            viscosity=model.viscosity(p.temperature))
        cts_cache[key] = hit
    return hit


def _lag_distance_at(model, p: Params, v_eff, solve_cache):
    """Steady lag DISTANCE (mm) the model predicts at effective speed `v_eff` and
    params `p` -- H*tan(theta), the spatial trailing offset. Cached by rounded set."""
    key = _solve_cache_key(p, v_eff)
    hit = solve_cache.get(key)
    if hit is None:
        hit = model.solve(ProcessParams(pressure=p.pressure, voltage=p.voltage,
                                        speed=max(v_eff, 1e-9),
                                        temperature=p.temperature)).lag_distance
        solve_cache[key] = hit
    return hit


def _tau_at(model, gap, p: Params, v_base, solve_cache, cts_cache, v_ramped=None):
    """Relaxation time tau (s, on the ANIMATION clock) for the contact tracker on a
    sub-interval whose NAIVE nozzle speed is `v_base`, under params `p`.

    The print-speed scale is a what-if: the g-code timeline (and so the nozzle
    motion) is unchanged, but the deposited geometry should look "as if printed at
    the scaled speed" v_eff = v_real*speed_scale. The tracker's steady spatial offset
    is v_nozzle*tau; to make that offset equal the model's lag distance at v_eff
    (which GROWS with speed -> more whip), tau = lag_distance(v_eff)/v_base. At
    scale = 1 and no kinematics this is lag_distance(v_base)/v_base = the model's
    lag_time, i.e. exactly mew_deposition's tau -- so a constant schedule reduces to
    laid_fibers.

    `v_ramped` (optional) is the accel-limited REAL speed of the interval: when given
    the lag is sized at v_real = v_ramped (so it tracks the corner / tight-arc
    slowdown) while the DIVISOR stays v_base (the naive nozzle speed the integrator's
    clock implies), so the touchdown trails by the real lag distance. None -> v_real =
    v_base, the pre-kinematics behaviour.

    Returns 0.0 (pin-to-nozzle, no lag) for a non-positive speed or when v_eff is
    below CTS -- the honest below-CTS passthrough (coiling is not modelled)."""
    if v_base <= 0.0:
        return 0.0
    v_real = v_base if v_ramped is None else v_ramped
    v_eff = v_real * p.speed_scale
    if v_eff <= 0.0:
        return 0.0
    cts = _cts_for(model, gap, p, cts_cache)
    if not (cts > 0.0) or v_eff < cts * (1.0 - 1e-9):
        return 0.0
    return _lag_distance_at(model, p, v_eff, solve_cache) / v_base


def _diam_at(model, p: Params, v_eff, diam_cache):
    """Deposited diameter (mm) at effective speed `v_eff` under params `p`, or None
    for a non-positive speed. Mass conservation via the model, same value the jet
    uses; computed for every moving segment (matching diameter_by_time), the
    below-CTS value being reported untrusted rather than gated here."""
    if v_eff <= 0.0:
        return None
    key = _solve_cache_key(p, v_eff)
    hit = diam_cache.get(key)
    if hit is None:
        hit = model.solve(ProcessParams(pressure=p.pressure, voltage=p.voltage,
                                        speed=max(v_eff, 1e-9),
                                        temperature=p.temperature)).fiber_diameter
        diam_cache[key] = hit
    return hit


# --------------------------------------------------------------------------- #
# the re-lay
# --------------------------------------------------------------------------- #
def relay_fibers(cmd_paths, cmd_ptimes, model, schedule, *, zeta=1.0, coil=False,
                 coil_wave_amp=0.0, coil_wave_len=0.3,
                 max_accel=None, square_corner_velocity=6.0,
                 settle_taus=6.0, fill_per_tau=6.0, transient_taus=8.0,
                 max_points_per_fiber=20000, entry_vs=None, exit_vs=None,
                 frame=None, placement=None, rotary_accel=0.0):
    """Re-lay commanded fibre centrelines into the touchdown paths produced by a
    TIME-VARYING parameter schedule.

    `cmd_paths`/`cmd_ptimes` are the COMMANDED centrelines and their global
    deposition times (parse_gcode output); `model` is a built mew_physics.MEWModel
    whose working_distance is the gap H; `schedule` is a ParamSchedule mapping
    deposition-time -> Params. `zeta` is RETIRED -- accepted, ignored, reaching no
    geometry, kept because the player, the preview and the sweeps page pass it.
    `coil` is the below-CTS coiling model (default off, matching
    `mew_deposition.make_transform`), gated PER SUB-INTERVAL; off, a below-CTS
    sub-interval pins to the nozzle. `coil_wave_amp`/`_len` (mm) are the SIMPLE
    coiling-WAVE indicator instead, gated PER FIBRE and taken off the COMMANDED
    centreline so it is byte-identical to the locked path the live sim seeds from.
    `max_accel`/`square_corner_velocity` re-time onto the real ramp; None/<=0
    (default) keeps the commanded feed, byte-identically. `rotary_accel` (deg/s^2,
    0 = none) adds the mandrel's rotary limit, as `mew_deposition.laid_fibers`.
    `settle_taus`/`fill_per_tau`/`transient_taus`/`max_points_per_fiber` are the
    grid controls, same meaning as mew_deposition.laid_fibers.

    Returns (out_paths, out_ptimes), one reshaped path + times per input fibre in
    order (a degenerate fibre, or a below-CTS one with coiling off, comes back as
    its commanded centreline). Z is the commanded layer height."""
    gap = float(model.working_distance)
    solve_cache, cts_cache = {}, {}
    out_paths, out_ptimes = [], []
    for idx, (p, t) in enumerate(zip(cmd_paths, cmd_ptimes)):
        p = np.asarray(p, dtype=np.float64)
        t = np.asarray(t, dtype=np.float64)
        if len(p) < 2:
            out_paths.append(p.copy())
            out_ptimes.append(t.copy())
            continue
        ev = entry_vs[idx] if entry_vs is not None else 0.0
        xv = exit_vs[idx] if exit_vs is not None else 0.0
        xyz, times = _relay_one(p, t, model, gap, schedule, zeta, coil, solve_cache,
                                cts_cache, settle_taus, fill_per_tau,
                                transient_taus, max_points_per_fiber,
                                max_accel, square_corner_velocity,
                                entry_v=ev, exit_v=xv,
                                coil_wave_amp=coil_wave_amp,
                                coil_wave_len=coil_wave_len,
                                frame=frame, placement=placement,
                                rotary_accel=rotary_accel)
        out_paths.append(xyz)
        out_ptimes.append(times)
    return out_paths, out_ptimes


def _metric_path(points, frame=None):
    """The path with BOTH chart axes rescaled to true arc length at each
    segment's own height.

    Copied from `mew_deposition` to keep this module decoupled from it (see the
    module note; `_assemble_coil_offsets` is here for the same reason). `v` is
    unwrapped arc length at the BARE radius, so a pass laid on top of five layers of
    wall has travelled further than its `v` coordinate says; every speed, and so
    every lag, is read off these lengths. `frame=None` returns the input unchanged,
    which is what keeps the flat relay byte-identical.

    A FIVE-AXIS COLLECTOR (`mew_deposition._world_surface`) takes the export's own
    world development, `mew_deposition._Development.q`, called rather than copied:
    `points` are world there and there is no chart to rescale.

    ALL THREE COLUMNS ARE DEVELOPED, not column 1 alone. `mew_frame.metric` returns
    three factors and
    a chart step at wall height h travels `(a*du, b*(dv - c*du), dh)`; dropping
    `a` costs exactly nothing on a cylinder, where `a` is 1 by construction, and
    that is why nothing saw it for four years of mandrel work. On a dome the
    meridian stretches too: `a = 1 + k1*h`, so a 20 um fibre at a 15-layer wall on
    an R = 5 dome is 1.060, and a pure-meridian pass came back **6.0% short**
    against the export's twin. The live relay and the export must not disagree
    by 6% on the same design, so this is the twin's expression verbatim --
    same order, same casts, same accumulation -- rather than a paraphrase of it.

    The accumulation in column 0 is why the cylinder digests moved by float
    association: `q[k] + (p[k+1,0] - p[k,0])` is not bit-identical to
    `p[k+1,0]`, even with `a` exactly 1. The move was measured before it was
    accepted and the guards re-anchored by running the pre-change code; see the
    report."""
    p = np.asarray(points, dtype=np.float64)
    if frame is None or len(p) < 2:
        return p
    if _world_dev(frame, p) is not None:
        return _world_dev(frame, p).q
    q = np.empty_like(p)
    q[0] = p[0]
    for k in range(len(p) - 1):
        d = p[k + 1] - p[k]
        a, b, c = frame.metric(0.5 * (p[k, 0] + p[k + 1, 0]),
                               0.5 * (p[k, 1] + p[k + 1, 1]),
                               0.5 * (p[k, 2] + p[k + 1, 2]))
        a, b, c = float(a), float(b), float(c)
        q[k + 1] = q[k] + (a * d[0], b * (d[1] - c * d[0]), d[2])
    return q


def _world_dev(frame, p):
    """The export's `_Development` of world fibre `p` when `frame` is a five-axis
    collector laid world-native, else None.

    THE ONE PLACE THIS MODULE REACHES INTO `mew_deposition`, and it calls the
    world relay's pieces instead of copying them: the hand mirror of `_lay_one`
    has drifted twice already (`_metric_path`'s note), and a development is
    exactly the kind of geometry a paraphrase gets subtly wrong. The import is
    local because nothing flat or 4-axis should pay for it."""
    if frame is None or bool(getattr(frame, "chart_currency", False)):
        return None
    import mew_deposition
    if mew_deposition._world_surface(frame) is None:
        return None
    return mew_deposition._development_of(p, frame)


def _relax_offsets(off_seg, off_of, rt, tau_seg):
    """The mandrel cross-track pre-offset, RELAXED onto the refined grid.

    Copied verbatim from `mew_deposition._relax_offsets` -- read the reasoning
    there, which is the authority. In one line: Eq 5's lateral deviation is a
    STATIONARY equilibrium, so handing the ray tracker the raw step makes it pursue
    a one-sample sideways jump and close a small loop at every junction; the offset
    instead relaxes onto each interval's target on that interval's own tau,
    integrated exactly, which leaves the steady state untouched and is
    grid-independent."""
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


def _place_interval(p, k, frame, placement, lag, v_base, speed, dev=None):
    """`(tau, (off_u, off_v))` for commanded interval `k` under a mandrel placement.

    A verbatim port of the `placement is not None` branch of
    `mew_deposition._lay_one` -- constants, symmetries and the per-layer hook all
    come from there, and the two must not drift: a constant-param layer is relaid by
    that function and a layer straddling a live edit by this one, so a scaffold can
    show both and any difference reads as a seam in the fibre.

    `lag` is the FLAT lag distance this interval would have had, which the caller
    recovers as `tau * v_base` (`_tau_at` returns lag/v_base, so the multiply is
    exact). `speed` is the effective speed the placement is read at -- accel-limited
    AND scaled by the live speed what-if, matching `mew_deposition`'s `v_eff`, which
    has `speed_scale` folded in before `lag_of` sees it.

    ONE PLACEMENT PER SCAFFOLD, AT THE BARE RADIUS, by default: Eq 1-5 is a
    single-layer model and re-evaluating it per grown layer leans the wall
    (MANDREL_LAG_DERIVATION.md 12.8), so every wall height is placed as h = 0 unless
    `per_layer_registration` is set. `frame` is still read for the fibre's SPEED
    upstream (`_metric_path`) -- kinematics, not contact geometry.

    `dev` (a world `_Development`) is the five-axis branch of `_lay_one`: the
    placement is asked at segment k's principal curvatures, with the heading
    resolved on the principal directions, and its cross-track answer is rebuilt
    in world on those directions before it enters the development."""
    if dev is not None:
        reg = bool(getattr(placement, "per_layer_registration", False))
        k1, k2, du, dv, d1, d2 = dev.principal(k, reg)
        lag, ou, ov = placement.at_curvatures(k1, k2).resolve(du, dv, lag,
                                                              speed=speed)
        return lag / v_base, dev.offset_to_dev(k, ou * d1 + ov * d2)
    du = p[k + 1, 0] - p[k, 0]
    dv = p[k + 1, 1] - p[k, 1]
    m1, m2, sh = 1.0, 1.0, 0.0
    local_placement = placement
    if frame is not None and getattr(placement, "per_layer_registration", False):
        h_mid = 0.5 * (p[k, 2] + p[k + 1, 2])
        mm = frame.metric(0.5 * (p[k, 0] + p[k + 1, 0]),
                          0.5 * (p[k, 1] + p[k + 1, 1]), h_mid)
        m1, m2, sh = float(mm[0]), float(mm[1]), float(mm[2])
        du, dv = m1 * du, m2 * (dv - sh * du)    # LOCAL metric, both axes
        #                                          and the chart's shear
        if hasattr(placement, "at_surface_radius"):
            local_placement = placement.at_surface_radius(
                frame.surface_radius(h_mid))
    lag, ou, ov = local_placement.resolve(du, dv, lag, speed=speed)
    # the exact inverse of `(m1*du, m2*(dv - sh*du))`; mirrors `mew_deposition`
    return lag / v_base, (ou / m1, ov / m2 + sh * ou / m1)   # -> bare chart


def _relay_one(p, t, model, gap, schedule, zeta, coil, solve_cache, cts_cache,
               settle_taus, fill_per_tau, transient_taus, max_points_per_fiber,
               max_accel=None, square_corner_velocity=6.0,
               entry_v=0.0, exit_v=0.0, coil_wave_amp=0.0, coil_wave_len=0.3,
               frame=None, placement=None, rotary_accel=0.0):
    """Re-lay one fibre under the schedule. Returns (xyz (M,3), times (M,)).

    The trailing CENTRE (lag + corner rounding/whip above CTS, pinned to the nozzle
    below it) is always built the same way. With `coil` on, any sub-interval whose
    effective speed is below CTS also gets the length-conserving sewing-machine
    excursion (see `_coil_excursion`) added on top, and the time grid is densified to
    the coil clock so the loops resolve; with `coil` off (or above CTS) no excursion
    is added and the result is byte-for-byte the pre-coiling re-lay.

    `max_accel` (> 0) sizes the lag / regime gate / coil at the accel-limited REAL
    speed `v_eff` (corner / tight-arc slowdown); None/<=0 keeps the commanded feed."""
    n = len(p)
    # A FIVE-AXIS collector is laid on the fibre's world development, exactly as
    # `mew_deposition._lay_one(dev=)` lays it: everything below runs on `dev.q`
    # and the laid points go back onto the collector at the end.
    dev = _world_dev(frame, p)
    p_world = p
    if dev is not None:
        if placement is not None and not hasattr(placement, "at_curvatures"):
            raise ValueError("a world-native relay needs a per-point placement "
                             "(mew_mandrel.CurvedPlacement), got %r"
                             % (placement,))
        p = dev.q
    # Lengths -- and so every speed, lag and tau below -- off the METRIC path: on a
    # mandrel `v` is arc length at the BARE radius, so a pass laid on a grown wall
    # has travelled further than its coordinate says. Flat returns `p` itself.
    seg_len = np.linalg.norm(np.diff(p if dev is not None
                                     else _metric_path(p, frame), axis=0), axis=1)
    dt = np.diff(t)
    v_seg = np.divide(seg_len, dt, out=np.zeros_like(seg_len), where=dt > 0.0)
    # accel-limited REAL speed per interval (== v_seg when kinematics off); sizes the
    # lag (via _tau_at's v_ramped), the regime gate, the coil clock and the diameter.
    v_eff = _effective_seg_speeds(p, seg_len, v_seg, max_accel,
                                  square_corner_velocity,
                                  entry_v=entry_v, exit_v=exit_v,
                                  seg_accel=_seg_accel(p, p, frame, max_accel,
                                                       rotary_accel))

    # Below-CTS coiling-WAVE indicator (mew_coil_wave), the fixed-amplitude sine
    # marker -- distinct from the physical `coil` model handled further down. Gated
    # PER FIBRE and off the COMMANDED centreline, mirroring the locked path, so a
    # below-CTS fibre waves byte-identically to the .depo the live sim seeds from.
    # The speed scale is read PER INTERVAL from the schedule, not once at the fibre
    # start: a live speed edit can drop the collector below CTS PARTWAY through the
    # fibre being laid, and reading only the start scale would classify it by its
    # pre-edit speed and skip the wave. The mean is then taken over the fibre's
    # CURRENT regime only, from its LAST schedule breakpoint onward -- everything
    # before it is already frozen, and a real MEW fibre is one long serpentine, so a
    # mid-lay edit was otherwise outvoted by everything already printed above CTS and
    # the wave never appeared. A constant schedule stays byte-identical.
    #
    # A fibre whose feed (or live scale, or CTS) crosses CTS is judged RUN BY
    # RUN instead -- `mew_coil_wave.below_cts_runs`, the one split the locked
    # path uses: it is relayed end to end like any fibre and the wave is added
    # over its slow runs only (`overlay_wave_runs`, at the return). Chart path
    # only, as in `mew_deposition.laid_fibers`.
    wave_runs = None
    if coil_wave_amp > 0.0 and dev is None:
        pk = [schedule.params_at(float(t[k])) for k in range(len(seg_len))]
        scale_k = np.array([q.speed_scale for q in pk], dtype=np.float64)
        cts_k = np.array([_cts_for(model, gap, q, cts_cache) for q in pk],
                         dtype=np.float64)
        runs = below_cts_runs(v_seg * scale_k, v_eff * scale_k, seg_len, cts_k)
        if len(runs) > 1:
            wave_runs = runs
    if coil_wave_amp > 0.0 and wave_runs is None:
        bp = schedule.breakpoint_times_in(float(t[0]), float(t[-1]))
        regime_t0 = max(bp) if bp else float(t[0])
        mask = np.asarray([float(t[k]) >= regime_t0 - 1e-9
                           for k in range(len(seg_len))])
        if not mask.any():
            mask[:] = True
        p0 = schedule.params_at(float(t[-1]))
        cts0 = _cts_for(model, gap, p0, cts_cache)
        tot = float(seg_len[mask].sum())
        if tot > 0.0 and cts0 > 0.0:
            scale_seg = np.array(
                [schedule.params_at(float(t[k])).speed_scale
                 for k in range(len(seg_len)) if mask[k]], dtype=np.float64)
            mean_v = float((v_eff[mask] * scale_seg * seg_len[mask]).sum() / tot)
            if mean_v < cts0 * (1.0 - 1e-9):
                wx, wt = coil_wave_reshape(p, t, coil_wave_amp, coil_wave_len)
                if dev is not None:
                    # drawn on the development and carried back, as the export
                    noz = np.column_stack([np.interp(wt, t, p[:, 0]),
                                           np.interp(wt, t, p[:, 1])])
                    wx = dev.to_world(t, wt, wx[:, :2], noz)
                return wx, wt

    # tau per COMMANDED interval, from the schedule params at that interval's start
    # time, the naive nozzle speed v_seg (the integrator's clock) and the REAL speed
    # v_eff (sizes the lag). Drives the degeneracy check, corner windows, and tail. A
    # non-advancing interval inherits the previous tau.
    tau_seg = np.zeros(n - 1, dtype=np.float64)
    # THE MANDREL PRE-OFFSET, per commanded interval, or None on a flat collector.
    # Eq 5's lateral deviation for that interval's heading -- a TARGET the relaxation
    # below runs the tracker's input towards, never a step. See
    # `mew_deposition._lay_one`, whose branch this mirrors; the two must agree,
    # because a constant-param layer takes that one and a layer straddling an edit
    # takes this one, and a scaffold can contain both.
    off_seg = None if placement is None else np.zeros((n - 1, 2), dtype=np.float64)
    last_off = (0.0, 0.0)
    last_tau = 0.0
    for k in range(n - 1):
        if v_seg[k] > 0.0:
            last_tau = _tau_at(model, gap, schedule.params_at(float(t[k])),
                               v_seg[k], solve_cache, cts_cache, v_eff[k])
            # NOT guarded on `last_tau > 0`. Below CTS the lag law floors to zero,
            # and `ContactPlacement.resolve` answers a non-positive lag with
            # (0, 0, 0) -- "no deviation without a lag". That zero IS the reset:
            # skipping the call when there is no lag leaves `last_off` frozen at
            # whatever the last above-CTS interval computed, so a live speed edit
            # that drops a mandrel pass below CTS would keep laying it ~0.23 mm
            # sideways (10x the fibre) until the speed came back up.
            # `mew_deposition._lay_one` calls `resolve` unconditionally for exactly
            # this reason; the guard was a porting error, caught in review.
            if placement is not None:
                last_tau, last_off = _place_interval(
                    p, k, frame, placement, last_tau * v_seg[k], v_seg[k],
                    v_eff[k] * schedule.params_at(float(t[k])).speed_scale,
                    dev=dev)
        tau_seg[k] = last_tau
        if off_seg is not None:
            off_seg[k] = last_off

    # Coiling (only when enabled): the fastest coil clock over the BELOW-CTS
    # intervals, by EFFECTIVE speed, read from the schedule per interval. Gated per
    # interval -- the forward-accumulating analog of mew_deposition's per-fibre regime
    # split -- so a live speed sweep coils only the stretch it pushes below CTS. With
    # the default calibration omega = CTS/R is speed-INDEPENDENT, so a print-speed
    # what-if does not change the coil-grid density and the frozen past is not
    # re-gridded (forward accumulation). Stays 0 (no densification) above CTS.
    coil_omega_max = 0.0
    if coil and wave_runs is None:           # the wave takes precedence, as locked
        for k in range(n - 1):
            if v_seg[k] <= 0.0:
                continue
            pk = schedule.params_at(float(t[k]))
            cts_k = _cts_for(model, gap, pk, cts_cache)
            if cts_k <= 0.0:
                continue
            eps = v_eff[k] * pk.speed_scale / cts_k
            if 0.0 < eps < 1.0:                # this interval is below CTS -> coils
                coil_omega_max = max(
                    coil_omega_max, model.coil_angular_frequency(cts_k, eps))

    # Wholly straight passthrough only if there is neither lag (above CTS) NOR a coil
    # to lay -- a degenerate/below-CTS fibre with coiling off keeps its commanded path.
    if not np.any(tau_seg > 0.0) and coil_omega_max <= 0.0:
        return p_world.copy(), t.copy()

    # Refine the time grid inside the transient window after each event where the
    # touchdown curves: geometry corners / base speed changes (incl. the start),
    # AND every live schedule breakpoint (the param steps). The fill STEP is local
    # to each window's own tau, so an event added later never re-grids the frozen
    # past (see module note). Windows extend to t[-1], not the next vertex, so a
    # transient that outlasts a short interval is still resolved.
    bp_times = schedule.breakpoint_times_in(float(t[0]), float(t[-1]))
    events = [(float(t[k]), tau_seg[k]) for k in _corner_vertices(p, v_seg)]
    for tb in bp_times:
        kb = int(np.clip(np.searchsorted(t, tb, side="right") - 1, 0, n - 2))
        events.append((tb, _tau_at(model, gap, schedule.params_at(tb),
                                   v_seg[kb], solve_cache, cts_cache, v_eff[kb])))
    # Adaptive per-fibre budget: keep at least _MIN_FILL_PER_CORNER samples per
    # transient event (geometry corner / speed change / schedule breakpoint) so the
    # corner-rounding fill is never thinned into a blocky polygon. Floored by the flat
    # cap (small fibres unchanged); rises with the event count so one big single-fibre
    # serpentine resolves every loop. Used for the coil-grid clamp and the final thin.
    cap = max(max_points_per_fiber, n + len(events) * _MIN_FILL_PER_CORNER)
    # the breakpoint instants themselves become grid nodes, so the param step lands
    # exactly there (the straddling sub-interval doesn't carry the old tau past it)
    extra = [np.asarray(bp_times, dtype=np.float64)] if bp_times else []
    for te, tau_e in events:
        if tau_e <= 0.0:                   # below-CTS / pinned: straight, no fill
            continue
        step = tau_e / fill_per_tau
        win_end = min(te + transient_taus * tau_e, float(t[-1]))
        # the window is a whole number of steps by construction, so the plain
        # floor landed on either side of it by roundoff (see mew_deposition)
        m = int((win_end - te) / step + 1e-9) if step > 0.0 else 0
        if m > 0:
            extra.append(te + step * np.arange(1, m + 1))
    rt = np.unique(np.concatenate([t] + extra)) if extra else t.copy()

    # Settle tail: hold the nozzle at the last point for a few tau so the trailing
    # touchdown catches up to the endpoint instead of ending short by the lag
    # distance. Sized by the last interval's own tau.
    tail_tau = tau_seg[-1]
    if tail_tau > 0.0:
        tail_step = tail_tau / fill_per_tau
        n_tail = int(settle_taus * fill_per_tau)
        rt = np.concatenate([rt, rt[-1] + tail_step * np.arange(1, n_tail + 1)])

    # Below CTS the coil oscillates over the whole coiling span, not just at the
    # transient events, so densify the grid uniformly to the coil clock on top of the
    # event/tail grid (the union keeps the commanded vertices, so the trailing centre
    # still rounds corners exactly). Mirrors mew_deposition._lay_one.
    if coil_omega_max > 0.0:
        period = 2.0 * math.pi / coil_omega_max
        span = float(rt[-1] - rt[0])
        # the coil lays tight loops over the whole below-CTS span, so the budget must
        # also cover _COIL_SAMPLES_PER_PERIOD per coil period -- a slow print coils
        # more, and capping at the corner budget alone would coarsen the loops.
        n_coil = int(_COIL_SAMPLES_PER_PERIOD * span / period) + 1
        cap = max(cap, n + n_coil)
        if n_coil > 2:
            coil_grid = rt[0] + span * (np.arange(n_coil + 1) / n_coil)
            rt = np.unique(np.concatenate([rt, coil_grid]))

    if len(rt) > cap:
        rt = _cap_grid(rt, t, cap)

    # nozzle position at each refined time (exact linear interp -- constant speed
    # within a commanded interval); z rides the commanded layer.
    nx = np.interp(rt, t, p[:, 0])
    ny = np.interp(rt, t, p[:, 1])
    nz = np.interp(rt, t, p[:, 2])
    nozzle_xy = np.column_stack([nx, ny]) if dev is not None else None

    if off_seg is not None:
        # `side="left"`: interval k's TARGET owns (t[k], t[k+1]], closed on the
        # right, so the change to the next heading lands on the first sample AFTER
        # the vertex -- which keeps the target's steps aligned to commanded
        # vertices, and that alignment is what makes the relaxation grid-free. The
        # target is relaxed, never applied raw: Eq 5's stationary equilibrium
        # stepped in sideways makes the ray tracker close a loop at every junction.
        # Added to the tracker's INPUT only -- never to `p`, `seg_len`, `v_seg` or
        # `t`, so the offset cannot perturb the clock, and `nz` rides through.
        off_of = np.clip(np.searchsorted(t, rt, side="left") - 1, 0, n - 2)
        off = _relax_offsets(off_seg, off_of, rt, tau_seg)
        nx = nx + off[:, 0]
        ny = ny + off[:, 1]

    # per sub-interval tau, looked up from the schedule at the sub-interval's START
    # time and the base nozzle speed of the commanded interval it falls in. This
    # time-varying tau is what makes the re-lay forward-accumulating and causal:
    # tau for sub-intervals before a breakpoint uses the old params, so the contact
    # there is identical no matter what params are set later.
    seg_of = np.clip(np.searchsorted(t, rt[:-1], side="right") - 1, 0, n - 2)
    sub_tau = np.empty(rt.size - 1, dtype=np.float64)
    for i, s in enumerate(rt[:-1]):
        k = seg_of[i]
        sp = schedule.params_at(float(s))
        sub_tau[i] = _tau_at(model, gap, sp, v_seg[k], solve_cache, cts_cache,
                             v_eff[k])
        # The placement is resolved HERE too, not just per commanded interval: the
        # along-track trail is what the tracker actually integrates, and on a
        # mandrel it is a function of the live params as well as the heading. Doing
        # it only in `tau_seg` would leave a knob edit mid-pass moving the flat lag
        # while the tube kept the pre-edit anisotropy.
        if placement is not None and sub_tau[i] > 0.0:
            sub_tau[i], _off = _place_interval(
                p, k, frame, placement, sub_tau[i] * v_seg[k], v_seg[k],
                v_eff[k] * sp.speed_scale, dev=dev)

    # The PLACEMENT RULE -- the same one `mew_deposition._lay_one` uses, so a live
    # re-lay and a locked export agree: Hrynevich's ray / V_JCP step. The steady lag
    # (with the schedule's params, the speed scale and the accel limit already folded
    # in) still arrives per sub-interval through `sub_tau`; the two law constants
    # below are what the ray step needs on top of it. `zeta` is retired -- it damped
    # the second-order filter this replaced and reaches no geometry.
    # The gap inside these constants must be the one the LAG LAW used --
    # `model.working_distance`, not the local `gap`, even though `relay_fibers`
    # currently sets that from the same attribute. A ceiling that disagrees with the
    # tau it normalises against mis-sizes V_JCP everywhere. That, and carrying the
    # per-rig amplitude k, is why this is one helper rather than three expressions
    # repeated per call site.
    ceiling, floor, sat_exp = model.lag_ray_constants()
    contact = np.asarray(integrate_contact_path_ray(
        list(zip(nx.tolist(), ny.tolist())), rt.tolist(), sub_tau.tolist(),
        lag_ceiling=ceiling, lag_floor=floor,
        saturation_exp=sat_exp), dtype=np.float64)

    cx, cy = contact[:, 0], contact[:, 1]
    if coil_omega_max > 0.0:           # add the below-CTS sewing-machine excursion
        ex, ey, _max_coil = _coil_excursion(contact, t, rt, v_eff, model, gap,
                                            schedule, cts_cache)
        cx = cx + ex
        cy = cy + ey
    if dev is not None:
        return dev.to_world(t, rt, np.column_stack([cx, cy]), nozzle_xy), rt
    if wave_runs is not None:
        return overlay_wave_runs(np.column_stack([cx, cy, nz]), rt, p, t,
                                 wave_runs, coil_wave_amp, coil_wave_len)
    return np.column_stack([cx, cy, nz]), rt


def _assemble_coil_offsets(psi_s, phase, r_s):
    """Closed-form coil offsets (along, perp) from per-sample Psi, phase and coil
    radius R: offset = R * sum_k c_k(Psi) sin(k*phase), the harmonic coefficients
    c_k from mew_physics.coil_offset_series (each carrying J_k(Psi), so the offset
    -> 0 as Psi -> 0). Grouped by rounded Psi so the Bessel series is evaluated once
    per distinct speed. Copied from mew_deposition to keep this module decoupled."""
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


def _coil_excursion(contact, t, rt, v_seg, model, gap, schedule, cts_cache):
    """Length-conserving sewing-machine coil added on top of the trailing centre,
    SCHEDULE-aware -- the live twin of mew_deposition._coil_excursion.

    Below CTS the contact lays at the local landing speed U = CTS with its tangent at
    angle psi = Psi*cos(phase) to travel; Psi is fixed by arc-length conservation
    J0(Psi) = eps = v_eff/CTS, so the deposited length is CTS/v_eff per unit travel
    and the coil stays registered with the nozzle. Every per-sample eps, Psi, coil
    clock omega and landing speed CTS is read from the schedule's params at that
    sample's deposition-time, so the coil FORWARD-ACCUMULATES exactly like the lag: a
    sample laid before a speed drop is still above CTS (eps>=1 -> Psi=0 -> zero
    excursion), one laid after coils, and the offset grows continuously from the
    crossing. The coil is the CLOSED-FORM bounded offset R*sum_k c_k(Psi) sin(k*phase)
    (mew_physics.coil_offset_series), rotated into the world by the trailing-centre
    tangent. Using the closed form (not a velocity integral) is what makes the
    excursion VANISH as Psi -> 0: so when a live speed sweep brings the jet back ABOVE
    CTS the fibre rejoins the commanded line with NO residual offset -- a velocity
    integral would instead freeze at a residual and shift every pass laid afterwards
    (alternating serpentine spacing). The head-held settle tail deposits no fibre, so
    the phase is frozen there and the coil holds its last position.
    Returns (ex, ey, max_amp_mm)."""
    # TODO(coil-kink): a coiling fibre shows weird random kinks where it runs
    # through a LINKING LOOP (the serpentine turnaround). The coil offset is
    # rotated into the world by the trailing-centre tangent (chx/chy below); at a
    # sharp ~180deg turnaround that tangent sweeps fast over a few samples, so the
    # offset direction whips around and the coil folds into a cusp instead of a
    # smooth loop. Likely fixes to explore: ease/limit the tangent rotation rate
    # through a corner, refine the coil grid more densely across the turnaround, or
    # decouple the coil phase from the corner. The SAME bug lives in the twin
    # mew_deposition._coil_excursion (static export). NOT yet fixed.
    nsamp = len(rt)
    if nsamp < 2:
        return np.zeros(nsamp), np.zeros(nsamp), 0.0
    nseg = len(v_seg)
    # commanded interval each sample sits in (settle-tail samples clamp to the last)
    samp_seg = np.clip(np.searchsorted(t, rt, side="right") - 1, 0, nseg - 1)
    # per-sample Psi / omega / landing-speed from the schedule params at each
    # sample's deposition-time (cached by rounded (eps, CTS) -- a single-feed coiling
    # print hits one key, a live speed sweep a handful, keeping the J0 inversions off
    # the hot path). eps sets Psi/omega but is not kept: the closed-form offset is a
    # function of Psi, not eps.
    psi_s = np.empty(nsamp, dtype=np.float64)
    om_s = np.empty(nsamp, dtype=np.float64)
    cts_s = np.empty(nsamp, dtype=np.float64)
    cache = {}
    for i in range(nsamp):
        pp = schedule.params_at(float(rt[i]))
        cts_i = _cts_for(model, gap, pp, cts_cache)
        v_eff = v_seg[samp_seg[i]] * pp.speed_scale
        eps = (v_eff / cts_i) if (v_eff > 0.0 and cts_i > 0.0) else 1.0
        eps = min(max(eps, 0.0), 1.0)
        key = (round(eps, 6), round(cts_i, 6))
        hit = cache.get(key)
        if hit is None:
            hit = (model.coil_tangent_amplitude(eps),
                   model.coil_angular_frequency(cts_i, eps))
            cache[key] = hit
        cts_s[i] = cts_i
        psi_s[i], om_s[i] = hit
    # coil phase = cumulative integral of omega over time, FROZEN in the head-held
    # settle tail (no fibre deposited there, so the coil holds its last position).
    dts = np.diff(rt)
    moving = rt <= t[-1] + 1e-12
    om_phase = np.where(moving, om_s, 0.0)
    phase = np.concatenate([[0.0], np.cumsum(om_phase[:-1] * dts)])
    # closed-form bounded offset = R * sum_k c_k(Psi) sin(k*phase), R = CTS/omega the
    # per-sample coil radius (0 where the coil is inactive). c_k carry J_k(Psi) so the
    # offset vanishes as Psi -> 0 -- no frozen residual when coiling ends.
    r_s = np.where(om_s > 0.0, cts_s / om_s, 0.0)
    along_off, perp_off = _assemble_coil_offsets(psi_s, phase, r_s)
    # unit tangent of the (smooth) trailing centre; carry the last good direction
    # through any near-stationary sample (start / settle tail) so the frozen coil
    # keeps its orientation instead of collapsing to the spine.
    dC = np.empty_like(contact[:, :2])
    dC[1:-1] = contact[2:, :2] - contact[:-2, :2]
    dC[0] = contact[1, :2] - contact[0, :2]
    dC[-1] = contact[-1, :2] - contact[-2, :2]
    cn = np.hypot(dC[:, 0], dC[:, 1])
    chx = np.empty(nsamp, dtype=np.float64)
    chy = np.empty(nsamp, dtype=np.float64)
    lx, ly = 1.0, 0.0
    for i in range(nsamp):
        if cn[i] > 1e-12:
            lx, ly = dC[i, 0] / cn[i], dC[i, 1] / cn[i]
        chx[i], chy[i] = lx, ly
    # world excursion = along*t_hat + perp*n_hat, n_hat = (-t_hat_y, t_hat_x)
    ex = along_off * chx - perp_off * chy
    ey = along_off * chy + perp_off * chx
    return ex, ey, float(np.max(np.hypot(ex, ey)))


def diameter_schedule(cmd_paths, cmd_ptimes, model, schedule, *, max_accel=None,
                      square_corner_velocity=6.0, entry_vs=None, exit_vs=None,
                      frame=None, laid_paths=None, laid_ptimes=None,
                      rotary_accel=0.0):
    """Deposited DIAMETER vs deposition-time under the schedule, as a (t_knots,
    d_knots) lookup (the shape gcode_to_fibers._tube_fibers' `diam_by_time` wants),
    or None if no moving segment exists.

    Diameter knots = mass conservation at the LANDING speed and the schedule params
    there, with flat runs collapsed to their boundaries (a constant single-feed
    stretch keeps just its end knots). Mirrors mew_deposition.diameter_by_time --
    including which speed and why (see that docstring) -- with the parameters read
    per-time from the schedule instead of fixed.

    `laid_paths`/`laid_ptimes` are the reshaped centrelines `relay_fibers` returned.
    Given, the speed comes off THAT geometry, because Q = (pi/4) d^2 v holds at the
    touchdown and not at the nozzle; the difference is confined to starts, corners,
    speed changes and reversals, where it is the whole of the deposited mass. The
    diameter then saturates at the per-knot CTS diameter, the coiling onset being
    where the straight-fibre model stops holding.

    `max_accel` (> 0) samples the accel-limited velocity profile sub-interval through
    the ramps (so the fibre beads at its slow ENDS / corners / tight arcs, not one
    mean per move -- see _diam_speed_samples), with the below-CTS ramp toes clamped at
    CTS so the bead stays bounded; None/<=0 keeps one knot per move at the commanded
    feed. Omitting the laid geometry keeps the legacy head-speed branch, byte-identical
    to the pre-mass-conservation build; no shipping caller takes it."""
    diam_cache = {}
    cts_cache = {}
    lag_cache = {}
    gap = model.working_distance
    # Below CTS the straight-fibre diameter saturates (coiling regime; the coil's own
    # thickness change is out of scope), so the speed is clamped at the per-knot CTS --
    # bounding the bead instead of letting d -> inf as v -> 0. On the landing-speed
    # path that is ALWAYS needed (the touchdown stalls at every reversal and every
    # fibre start); on the legacy head-speed path only the kinematics branch samples
    # those low speeds, so the clamp is off otherwise (byte-identical).
    landing = laid_paths is not None and laid_ptimes is not None
    clamp_cts = landing or (plan_trajectory is not None and max_accel is not None
                            and max_accel > 0.0)
    ts, ds = [], []
    if landing:
        # The accel limit folded back in as a ratio: relay_fibers reshapes on the real
        # trajectory but stamps the naive clock, so a rate measured off that geometry
        # is a naive-clock rate. Kinematics off -> the ratio is identically 1. No jet
        # buffer here: the mass balance is exact AT the touchdown, and low-passing it
        # would break the identity it exists to hold.
        ratio = _speed_ratio_curve(cmd_paths, cmd_ptimes, max_accel,
                                   square_corner_velocity, entry_vs, exit_vs,
                                   frame=frame, rotary_accel=rotary_accel)
        for t_mid, v_land in _landing_speeds(laid_paths, laid_ptimes, frame=frame):
            pk = schedule.params_at(t_mid)
            v_eff = v_land * pk.speed_scale
            if ratio is not None:
                v_eff *= float(np.interp(t_mid, ratio[0], ratio[1]))
            cts_k = _cts_for(model, gap, pk, cts_cache)
            if cts_k > 0.0:
                v_eff = max(v_eff, cts_k)
            d = _diam_at(model, pk, v_eff, diam_cache)
            if d is not None:
                ts.append(t_mid)
                ds.append(d)
        return _collapse_knots(ts, ds)
    for idx, (p, t) in enumerate(zip(cmd_paths, cmd_ptimes)):
        p = np.asarray(p, dtype=np.float64)
        t = np.asarray(t, dtype=np.float64)
        if len(p) < 2:
            continue
        # metric lengths, so a mandrel's diameter is read at the speed the head
        # really runs at on the grown wall (see `_metric_path`); flat is unchanged
        metric_p = _metric_path(p, frame)
        seg_len = np.linalg.norm(np.diff(metric_p, axis=0), axis=1)
        dt = np.diff(t)
        v_seg = np.divide(seg_len, dt, out=np.zeros_like(seg_len), where=dt > 0.0)
        # a world fibre's corners are read on its development, as the export does
        if _world_dev(frame, p) is not None:
            p = metric_p
        ev = entry_vs[idx] if entry_vs is not None else 0.0
        xv = exit_vs[idx] if exit_vs is not None else 0.0
        # the jet buffers the diameter change over ~its lag distance (at the fibre's
        # CRUISE effective speed under the fibre's params), so a sharp corner dip barely
        # beads while a long feed grade does (Hrynevich 2020). 0 when kinematics off ->
        # byte-identical. The lag is the calibrated lag law's output -- no new constant.
        cruise_v = float(v_seg.max()) if v_seg.size else 0.0
        buf_len = 0.0
        if clamp_cts and cruise_v > 0.0:
            pmid = schedule.params_at(0.5 * (float(t[0]) + float(t[-1])))
            buf_len = _lag_distance_at(model, pmid,
                                       cruise_v * pmid.speed_scale, lag_cache)
        # sub-interval on the ramps when kinematics is on (beads at slow ENDS), one
        # knot per move when off (byte-identical); see _diam_speed_samples. ev/xv
        # carry head speed through a linking-loop junction (no bead at the entry).
        for t_mid, v in _diam_speed_samples(p, t, seg_len, v_seg, max_accel,
                                            square_corner_velocity,
                                            entry_v=ev, exit_v=xv,
                                            buffer_len=buf_len,
                                            seg_accel=_seg_accel(
                                                p, p, frame, max_accel,
                                                rotary_accel)):
            pk = schedule.params_at(t_mid)
            v_eff = v * pk.speed_scale
            if clamp_cts:
                cts_k = _cts_for(model, gap, pk, cts_cache)
                if cts_k > 0.0:
                    v_eff = max(v_eff, cts_k)
            d = _diam_at(model, pk, v_eff, diam_cache)
            if d is not None:
                ts.append(t_mid)
                ds.append(d)
    return _collapse_knots(ts, ds)


# --------------------------------------------------------------------------- #
# small geometry helpers (copied from mew_deposition to keep this module
# decoupled from a file that is currently under active edit)
# --------------------------------------------------------------------------- #
def _collapse_knots(ts, ds):
    """(t_knots, d_knots) float64 strictly increasing in time, or None if empty.
    Duplicate times go (np.interp wants strictly-increasing x), then the interior of
    each flat run: a knot equal to BOTH neighbours lies on the piecewise-linear
    interp, so dropping it changes no queried diameter. Mirrors
    mew_deposition._collapse_knots."""
    if not len(ts):
        return None
    ts = np.asarray(ts, dtype=np.float64)
    ds = np.asarray(ds, dtype=np.float64)
    order = np.argsort(ts, kind="stable")
    ts, ds = ts[order], ds[order]
    keep = np.concatenate(([True], np.diff(ts) > 0.0))
    ts, ds = ts[keep], ds[keep]
    if len(ds) > 2:
        edge = (ds[1:-1] != ds[:-2]) | (ds[1:-1] != ds[2:])
        keep = np.concatenate(([True], edge, [True]))
        ts, ds = ts[keep], ds[keep]
    return ts, ds


def _landing_speeds(laid_paths, laid_ptimes, frame=None):
    """(t_mid, landing speed) per LAID segment -- the rate the fibre actually reaches
    the collector. Equal to the head speed wherever the lag is a steady offset; they
    part only at starts, corners, speed changes and 180 deg reversals. Times are the
    laid path's own, i.e. the naive clock. Mirrors mew_deposition.landing_speeds."""
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
    """(t, v_effective / v_commanded) on the naive clock, or None when the accel limit
    is off and the ratio is identically 1. The relay reshapes on the real trajectory
    but stamps the naive clock, so this is what puts a landing rate measured off that
    geometry back onto the head's real one. Mirrors
    mew_deposition._speed_ratio_curve."""
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
            mp, seg, v_naive, max_accel, square_corner_velocity,
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



def _effective_seg_speeds(p, seg_len, v_naive, max_accel, square_corner_velocity,
                          entry_v=0.0, exit_v=0.0, seg_accel=None):
    """Accel-limited mean speed per commanded interval (mm/s) from the trapezoidal
    plan (mew_kinematics) -- the head's REAL speed (ramped fibre ends, corner /
    tight-arc slowdown) -- or `v_naive` UNCHANGED when kinematics is off (max_accel
    None/<=0) or unavailable. Cruise interiors are unchanged either way (the plan
    cruises them entry==exit -> v_eff == feed). Copied from mew_deposition to keep
    this module decoupled. `entry_v`/`exit_v` (mm/s, default 0 = at rest) carry head
    speed THROUGH a linking-loop junction so the plan inserts no stop/bead there."""
    if plan_trajectory is None or max_accel is None or max_accel <= 0.0:
        return v_naive
    traj = plan_trajectory(p, v_naive * 60.0, max_accel=max_accel,
                           square_corner_velocity=square_corner_velocity,
                           entry_v=entry_v, exit_v=exit_v, seg_accel=seg_accel)
    kt = np.asarray(traj.times, dtype=np.float64)
    if kt.size != len(p):
        return v_naive
    dur = np.diff(kt)
    return np.divide(seg_len, dur, out=v_naive.astype(np.float64, copy=True),
                     where=dur > 0.0)


def _buffer_speed(arcs, speeds, buffer_len):
    """Low-pass the accel-limited speed profile along ARC LENGTH with length constant
    `buffer_len` (the jet's lag distance) -- the jet BUFFERS the deposited diameter, so
    a sharp sub-buffer dip (a corner) barely beads while a long feed grade does
    (Hrynevich 2020: transition "distributed over … up to 10 mm"). `buffer_len` is the
    calibrated lag DISTANCE, not a new fitted constant (no sweep). Copy of
    mew_deposition._buffer_speed (kept here so this module stays decoupled): densify the
    grid so the relaxation has points to land on (the resample is sparse on cruise; the
    relaxed interior collapses out downstream), then an exact zero-order-hold pole
    a = 1 - exp(-ds/buffer_len). Returns (arcs2, speeds2). buffer_len <= 0 -> identity."""
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
            k = min(int(gap / step), 64)
            grid.extend(arcs[j - 1] + gap * i / (k + 1) for i in range(1, k + 1))
        grid.append(float(arcs[j]))
    grid = np.asarray(grid, dtype=np.float64)
    v = np.interp(grid, arcs, speeds)
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
    """Yield (naive_time, speed) diameter samples for one fibre. Copy of
    mew_deposition._diam_speed_samples (kept here so this module stays decoupled).

    Kinematics OFF (`max_accel` None/<=0) or unavailable -> one sample per MOVING
    commanded segment at its midpoint time, at the commanded feed (byte-identical to
    the historical one-knot-per-segment diameter). ON -> sample the trapezoidal
    velocity profile (`Trajectory.resample`: dense through the accel/decel RAMPS, a
    single point per cruise) and map each sample's ARC LENGTH back onto the naive
    print clock, so a long ramping move beads at its slow ENDS rather than carrying
    one mean diameter. Non-advancing samples (v<=0) are skipped. `entry_v`/`exit_v`
    (mm/s, default 0 = at rest) carry head speed through a linking-loop junction so
    no decel/accel ramp -- and so no diameter bead -- forms at the loop entry.

    `buffer_len` (mm, default 0 = off) low-passes the kinematic speed along arc length
    before it becomes a diameter, modelling the jet's diameter buffering (the lag
    distance; see `_buffer_speed`). Applied only on the kinematics-on path, so
    kinematics-off stays byte-identical."""
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


def _corner_vertices(p, v_seg):
    """Interval indices to refine into: the start (0) plus any interval that BEGINS
    at a sharp direction change or a marked speed change."""
    n = len(p)
    corners = {0}
    cos_thr = math.cos(math.radians(_CORNER_DEG))
    d = np.diff(p[:, :2], axis=0)
    seg_xy = np.linalg.norm(d, axis=1)
    for k in range(1, n - 1):
        a, b = seg_xy[k - 1], seg_xy[k]
        if a <= 1e-12 or b <= 1e-12:
            continue
        cosang = float(np.dot(d[k - 1], d[k]) / (a * b))
        if cosang < cos_thr:
            corners.add(k)
        elif (k < len(v_seg) and v_seg[k - 1] > 0.0
              and abs(v_seg[k] - v_seg[k - 1]) / v_seg[k - 1] > _SPEED_CHANGE_FRAC):
            corners.add(k)
    return sorted(corners)


def _cap_grid(rt, vertex_times, cap):
    """Thin a refined grid down to `cap` points while ALWAYS keeping the commanded
    vertex times. Drops fill points on a uniform stride; a last resort for
    pathologically long fibres."""
    keep = np.isin(rt, vertex_times)
    fill_idx = np.flatnonzero(~keep)
    budget = cap - int(keep.sum())
    if budget <= 0:
        return rt[keep]
    if len(fill_idx) > budget:
        stride = len(fill_idx) / budget
        pick = fill_idx[(np.arange(budget) * stride).astype(np.int64)]
        keep[pick] = True
    else:
        keep[fill_idx] = True
    return rt[keep]
