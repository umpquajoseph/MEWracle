"""Tests for mew_incremental: the real-time INCREMENTAL (time-marched) deposition.

COMPARATOR: a batch `apply_sag` on the same relaid geometry and per-fibre
diameters (`_batch_like`), which the march must match BYTE-FOR-BYTE -- the
keystone, and what says the march only spreads the verified solve over playback.
The rest are invariants needing no reference:
  * FROZEN PAST / lazy future -- an edit at the playhead drops no laid layer and
    leaves every committed fibre byte-unchanged, while later fibre responds;
  * SCRUB-BACK -- truncating then re-advancing on the same schedule reproduces the
    un-rewound result exactly; stepwise == one-shot;
  * PER-POINT DIAMETER forward-accumulates, and PER-POINT SUPPORT rests a later
    layer on an earlier fibre's LOCAL diameter.

The per-point support used to be a refinement that only a mid-fibre diameter edit
could make visible, because the physics diameter was uniform WITHIN a fibre. It is
not any more: the deposited diameter is mass conservation at the LANDING speed
(`mew_deposition.diameter_by_time`), so it varies along every fibre and the
per-point and per-fibre-scalar seedings differ by ~0.5 d (10.9 um by layer 3 of
`_grid`). `_batch_like` therefore seeds the way the march does. KNOWN GAP: the
locked export still commits one scalar radius per layer through
`_SagState.add_layer`, so export and march disagree by that amount until
`apply_sag` carries per-point radii too.

Run:  python tests/test_mew_incremental.py
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math

import numpy as np

from mew_physics import MEWModel, ProcessParams
from mew_live import Params, ParamSchedule, relay_fibers, diameter_schedule
from gcode_to_fibers import apply_sag, _layer_ids, _SagState, _hash_cell
from mew_incremental import IncrementalDeposition, _INDEX_PRESIZE_MULT

# the march's own per-fibre support-radius rule, so `_batch_like` seeds the lower
# index exactly as `_lay_one_layer` does rather than re-deriving it
_support_radii = IncrementalDeposition._support_radii

_P, _V, _T = 8.0e4, 4.0, 90.0
_GAP = 4.0
_D = 0.02


def _model():
    return MEWModel(working_distance=_GAP)


def _cts():
    return _model().critical_speed(_V, _GAP)


def _grid(n_layers=4, n_lines=4, span=6.0, pitch=1.5, speed=None, d=_D,
          npts=31):
    """A stacked cross-hatch: each layer is `n_lines` parallel straight fibres,
    alternating +x / +y by layer so upper layers cross (and rest on) lower ones.
    Fibres are deposited bottom-up (global times increase layer by layer), so
    deposition order == Z order -- the bit-identical-to-batch case. Returns
    (paths, ptimes) in the parse_gcode shape."""
    speed = speed or 2.0 * _cts()
    paths, ptimes = [], []
    clk = 0.0
    for L in range(n_layers):
        z = L * d
        for k in range(n_lines):
            off = 0.5 + k * pitch
            s = np.linspace(0.0, span, npts)
            if L % 2 == 0:
                p = np.column_stack([s, np.full(npts, off), np.full(npts, z)])
            else:
                p = np.column_stack([np.full(npts, off), s, np.full(npts, z)])
            seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
            t = clk + np.concatenate([[0.0], np.cumsum(seg) / speed])
            clk = float(t[-1]) + 0.02            # tiny travel gap to next fibre
            paths.append(p)
            ptimes.append(t)
    return paths, ptimes


def _stab_then_scaffold(d_base=0.05, d_main=0.02, span=6.0, npts=31):
    """A stabilization-base-then-scaffold layout: a 3-layer base at Z = 0, d_base,
    2*d_base is laid FIRST, then a 3-layer main scaffold at Z = 0, d_main, 2*d_main is
    laid SECOND -- so deposition order does NOT match Z order, and the base's bottom
    layer (Z0, laid first) shares a HEIGHT with the main's bottom layer (Z0, laid much
    later). This is the exact overlapping-Z stabilization case (the base inherits the
    scaffold's start_z, so both start at Z0) that scrambled the march's completion-time
    ordering and stuck the jet at the end of the first layer. Returns (paths, ptimes)
    in deposition order, one fibre per layer."""
    speed = 2.0 * _cts()
    paths, ptimes, clk = [], [], 0.0
    plan = [(L * d_base, L) for L in range(3)] + [(L * d_main, L) for L in range(3)]
    for z, L in plan:
        s = np.linspace(0.0, span, npts)
        if L % 2 == 0:                                   # alternate +x / +y by layer
            p = np.column_stack([s, np.full(npts, 0.5), np.full(npts, z)])
        else:
            p = np.column_stack([np.full(npts, 0.5), s, np.full(npts, z)])
        seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
        t = clk + np.concatenate([[0.0], np.cumsum(seg) / speed])
        clk = float(t[-1]) + 0.02
        paths.append(p)
        ptimes.append(t)
    return paths, ptimes


def _orch(paths, ptimes, *, max_accel=None, coil=False, base_path_diam=None):
    return IncrementalDeposition(
        paths, ptimes, _model(), launch_pressure=_P, launch_voltage=_V,
        launch_temperature=_T, diameter=_D, sag_multiplier=1.0,
        self_crossings=True, base_path_diam=base_path_diam, zeta=1.0, coil=coil,
        max_accel=max_accel, square_corner_velocity=6.0)


def _tmax(ptimes):
    return max(float(t[-1]) for t in ptimes)


def _batch_like(paths, ptimes, inc_diam, *, max_accel=None):
    """A batch reference solve at the EXACT per-fibre diameters the orchestrator
    used, on geometry relaid the SAME way (per layer). This isolates the invariant
    Phase 4 actually changes -- the SAG ACCUMULATION (incremental layer-by-layer ==
    one straight batch solve) -- from orthogonal artefacts:

      * `relay_fibers`' solve cache rounds speeds to 6 dp, so an all-at-once relay
        hands a later fibre a neighbour's cached lag and drifts ~1e-13 from a
        per-layer relay -- hence the reference relays per layer too; and
      * the bed-underside lift is applied by the march from each layer's OWN
        diameter schedule, and feeding one big apply_sag a single global schedule
        rounds a few ULP differently, which can flip a span across the sampling
        threshold. So the reference drives `_SagState` layer by layer on the SAME
        per-layer schedule -- straight-through commit against the march's
        tip/freeze/commit -- which keeps the comparison bit-identical while still
        isolating exactly the accumulation machinery."""
    model = _model()
    sched = ParamSchedule(Params(_P, _V, _T, 1.0))
    npaths = [np.asarray(p, dtype=np.float64) for p in paths]
    layer_of, n_layers = _layer_ids(npaths, layer_tol=0.5 * _D)
    pd = [d if d is not None else _D for d in inc_diam]
    # The index is PRESIZED off the NOMINAL diameter, exactly as the march does
    # (`mew_incremental._INDEX_PRESIZE_MULT * _max_nominal()`), not off the physics
    # diameters that come back. Sizing it from `max(pd)` instead used to be
    # indistinguishable -- every fibre carried the same commanded diameter -- but the
    # deposited diameter now varies with the landing speed, so the two sizings pick
    # different hash cells and find different contacts. This reference mirrors the
    # march; it does not get to choose its own cell.
    max_diam = _INDEX_PRESIZE_MULT * _D
    bed_center = min(float(np.median(p[:, 2])) for p in npaths)
    state = _SagState(shared_cell=_hash_cell(max_diam), bed_center=bed_center,
                      max_diam=max_diam, sag_multiplier=1.0,
                      self_crossings=True, with_times=True)
    sagged = [None] * len(paths)
    sagged_t = [None] * len(paths)
    for li in range(n_layers):
        members = [i for i in range(len(paths)) if layer_of[i] == li]
        rp, rt = relay_fibers([paths[i] for i in members],
                              [ptimes[i] for i in members], model, sched,
                              zeta=1.0, coil=False, max_accel=max_accel,
                              square_corner_velocity=6.0)
        # per-layer bed-underside schedule, exactly as the march's _relay_layer --
        # including the RELAYED geometry, off which the landing speed (and so the
        # deposited diameter) is read. Drop that and this reference silently falls
        # back to the legacy head-speed branch and stops mirroring the march.
        state.bed_diam_by_time = diameter_schedule(
            [paths[i] for i in members], [ptimes[i] for i in members], model, sched,
            max_accel=max_accel, square_corner_velocity=6.0,
            laid_paths=rp, laid_ptimes=rt)
        # Solve, then seed the lower index the way the march does: at the PER-POINT
        # radius where the deposited diameter varies along a fibre, at the scalar
        # where it does not (`mew_incremental._support_radii`). That used to be a
        # distinction without a difference -- the physics diameter was uniform within
        # a fibre unless a mid-fibre edit made it vary -- but the deposited diameter
        # is now mass conservation at the LANDING speed, so it varies along every
        # fibre and the two seedings part company (10.9 um, ~0.5 d, by layer 3 of
        # this fixture).
        #
        # KNOWN GAP, not a test convenience: the LOCKED EXPORT still seeds through
        # `_SagState.add_layer`, which commits one scalar radius per layer, so the
        # export and the march now disagree by that amount. The fix belongs in
        # `apply_sag`'s commit and is owned elsewhere; this reference mirrors the
        # march, which is the physically correct one of the two.
        out_p, out_t, layer_polys, radius = state._solve_layer(
            rp, [pd[i] for i in members], rt)
        lut = state.bed_diam_by_time
        ppd = [np.interp(np.asarray(t, dtype=np.float64), lut[0], lut[1])
               for t in out_t]
        state.commit_solved(out_p, radius,
                            layer_radii=_support_radii(ppd, radius))
        for k, i in enumerate(members):
            sagged[i] = out_p[k]
            sagged_t[i] = out_t[k]
    return sagged, sagged_t


# The march applies the bed-underside lift PER LAYER (each layer's own diameter
# schedule); the batch reference applies it from ONE global schedule spanning all
# layers. np.interp over the per-layer vs the concatenated-global knots rounds a few
# ULP differently -- an artefact orthogonal to the sag ACCUMULATION this keystone
# guards (the same class as the relay-cache ~1e-13 drift `_batch_like` already works
# around). Geometry is therefore compared to a tight tolerance (~1e-16 observed, far
# below any physical scale); TIMES are unaffected by the lift and stay byte-exact.
_BED_ULP = 1e-12


def _assert_march_equals_batch(inc_p, inc_t, bsg, bst):
    assert len(inc_p) == len(bsg)
    for i in range(len(bsg)):
        assert inc_p[i].shape == bsg[i].shape, f"geom shape fibre {i}"
        assert np.allclose(inc_p[i], bsg[i], rtol=0.0, atol=_BED_ULP), \
            f"geom fibre {i} (max |d|={np.abs(inc_p[i] - bsg[i]).max():.2e})"
        assert np.array_equal(inc_t[i], bst[i]), f"time fibre {i}"


# --- the keystone: incremental == batch ------------------------------------- #
def test_incremental_equals_batch():
    """March a constant schedule to the end; the accumulated sagged scaffold is
    byte-for-byte the batch apply_sag on the same geometry + diameters."""
    paths, ptimes = _grid()
    orch = _orch(paths, ptimes)
    assert orch.applicable
    orch.advance_to(_tmax(ptimes) + 1.0)
    inc_p, inc_t, inc_d = orch.committed_fibers()
    assert all(p is not None for p in inc_p)            # everything laid
    bsg, bst = _batch_like(paths, ptimes, inc_d)
    _assert_march_equals_batch(inc_p, inc_t, bsg, bst)


def test_incremental_equals_batch_under_physical_sag():
    """The incremental==batch keystone STILL holds with the grounded physical sag
    law installed in the shared sag-law seam: the march and the batch solve read
    the seam identically, so swapping the droop law never breaks deposition-order
    causality. Tight pores so the law is in its partial-sag regime; a guard checks
    it actually changed the geometry vs the geometric default (not a no-op)."""
    import gcode_to_fibers as gtf
    # tight-pitch stack -> partial (unclamped) sag, where the droop law shows
    paths, ptimes = _grid(n_layers=3, n_lines=5, span=2.0, pitch=0.3, npts=41)
    geo_p = _orch(paths, ptimes)
    geo_p.advance_to(_tmax(ptimes) + 1.0)
    geo_fibres = geo_p.committed_fibers()[0]               # geometric reference
    gtf.set_sag_law(_model().make_sag_law(temperature=_T))
    try:
        orch = _orch(paths, ptimes)
        assert orch.applicable
        orch.advance_to(_tmax(ptimes) + 1.0)
        inc_p, inc_t, inc_d = orch.committed_fibers()
        assert all(p is not None for p in inc_p)
        bsg, bst = _batch_like(paths, ptimes, inc_d)       # also reads the seam
        _assert_march_equals_batch(inc_p, inc_t, bsg, bst)
        assert any(inc_p[i].shape != geo_fibres[i].shape
                   or not np.array_equal(inc_p[i], geo_fibres[i])
                   for i in range(len(inc_p))), "physical law had no effect"
    finally:
        gtf.set_sag_law(None)                              # never leak into other tests


def test_incremental_equals_batch_under_fusion():
    """The incremental==batch keystone STILL holds with junction fusion installed in
    the shared fusion seam: the live march and the batch solve sink crossovers
    IDENTICALLY, so the real-time sim agrees with the locked/exported scaffold. Tight
    pores so the peaks stack and the sink bites; a guard checks fusion actually
    changed the geometry vs the unfused default (not a no-op)."""
    import gcode_to_fibers as gtf
    paths, ptimes = _grid(n_layers=4, n_lines=5, span=2.0, pitch=0.3, npts=41)
    geo_p = _orch(paths, ptimes)
    geo_p.advance_to(_tmax(ptimes) + 1.0)
    geo_fibres = geo_p.committed_fibers()[0]               # unfused reference
    gtf.set_fusion_law(_model().make_fusion_law(temperature=_T))
    try:
        orch = _orch(paths, ptimes)
        assert orch.applicable
        orch.advance_to(_tmax(ptimes) + 1.0)
        inc_p, inc_t, inc_d = orch.committed_fibers()
        assert all(p is not None for p in inc_p)
        bsg, bst = _batch_like(paths, ptimes, inc_d)       # also reads the seam
        _assert_march_equals_batch(inc_p, inc_t, bsg, bst)
        assert any(inc_p[i].shape != geo_fibres[i].shape
                   or not np.array_equal(inc_p[i], geo_fibres[i])
                   for i in range(len(inc_p))), "fusion had no effect"
    finally:
        gtf.set_fusion_law(None)                           # never leak into other tests


def test_overlapping_z_stabilization_marches_in_deposition_order():
    """REGRESSION (the 'jet stuck in the top-right corner with a stabilization base'
    bug): when the stabilization base and the main scaffold both start at Z0, a pure
    Z-cluster merges the base's bottom fibre (laid first) with the scaffold's bottom
    fibre (laid much later) into ONE layer. Its completion time (the max over members)
    then lands late, so commit-time ordering buried it under the base's own top layers
    and the in-progress tip pointed at the wrong (higher-Z) layer -- the jet anchored
    there and stuck while the nozzle traced the real first layer.

    The march now splits each Z-cluster into DEPOSITION-CONTIGUOUS layers, so every
    layer has a single completion time, commit order == deposition order, the tip is
    always the fibre being laid right now, and the march never stalls."""
    paths, ptimes = _stab_then_scaffold()
    orch = _orch(paths, ptimes)

    # every march layer is a CONTIGUOUS run of commanded fibre indices (the property
    # the deposition-causal split restores -- one completion time per layer)
    for mem in orch._members:
        assert mem == list(range(mem[0], mem[-1] + 1)), f"layer not contiguous: {mem}"
    # the same height (Z0) appears in TWO separate layers (base bottom + main bottom),
    # not merged into one -- the heart of the fix
    z0_layers = [li for li in range(orch._n_layers)
                 if abs(orch._layer_z[li]) < 1e-9]
    assert len(z0_layers) == 2, f"Z0 not split by deposition phase: {z0_layers}"
    # commit order is deposition order (completion non-decreasing, layers ordered by
    # their first commanded fibre) -- NOT scrambled by Z height
    comp = [orch._completion[li] for li in orch._order]
    assert comp == sorted(comp), "completion times not monotonic in commit order"
    assert orch._order == sorted(range(orch._n_layers),
                                 key=lambda li: orch._members[li][0]), \
        "commit order is not deposition order (the stabilization scramble)"

    # march the clock: the tip is ALWAYS a fibre actually being laid now (never a
    # future-Z layer), the committed set is exactly the deposition prefix, and a
    # bounded advance loop terminates (no stall -> no stuck jet)
    tmax = _tmax(ptimes)
    march = _orch(paths, ptimes)
    for t in np.linspace(0.0, tmax * 1.02, 25):
        more, guard = True, 0
        while more:
            more = march.advance_to(float(t), max_layers=2)
            guard += 1
            assert guard < 50, "march stalled (the stabilization hang)"
        members, _tp, _tt, _td = march.tip_fibers()
        for i in members:
            assert float(ptimes[i][0]) <= t + 1e-9, \
                f"tip fibre {i} starts in the future at t={t:.3f} (the stuck jet)"
        laid = [i for i in range(len(paths))
                if march.committed_fibers()[0][i] is not None]
        assert laid == list(range(len(laid))), \
            f"committed set is not a deposition prefix: {laid}"
    assert march.n_committed == orch._n_layers              # everything laid, no stall


def test_kinematics_march_self_consistent():
    """With accel-limited kinematics ON the diameter varies strongly ALONG each fibre
    (thicker at the ramped ends, thinner through the fast middle). Two things must
    hold: the per-point diameter really does vary (kinematics is exercised), and
    marching the clock in many small steps lays a scaffold byte-for-byte identical to
    one big advance (the march mechanics -- tip, commit, windowing -- are correct
    under kinematics).

    NB: the accumulated sag is deliberately NOT compared to the scalar-diameter batch
    here. The live support is now PER-POINT -- a later layer rests on the locally-
    thicker fibre ends, matching the diameter the tube is actually drawn at -- which a
    per-fibre-scalar `apply_sag` cannot represent (the divergence is ~half a fibre
    diameter, not numerical). The bottom layer, which rests on the bed rather than on
    any fibre, is still byte-identical to the batch even under kinematics (no support
    radius enters); test_support_radius_tracks_local_diameter pins the refinement."""
    paths, ptimes = _grid()
    tmax = _tmax(ptimes)
    big = _orch(paths, ptimes, max_accel=2000.0)
    big.advance_to(tmax + 1.0)
    assert max(np.ptp(d) for d in big.committed_ppd() if d is not None) > 0.1 * _D, \
        "kinematics did not vary the diameter along the fibre"
    # bottom layer (rests on the bed) == batch: its UNDERSIDE is lifted onto the bed
    # by the per-point radius (bed_diam_by_time), matching the batch export to the
    # per-layer-vs-global np.interp ULP (see _assert_march_equals_batch); times exact.
    inc_p, inc_t, inc_d = big.committed_fibers()
    bsg, bst = _batch_like(paths, ptimes, inc_d, max_accel=2000.0)
    bottom = _layer_ids([np.asarray(p) for p in paths], layer_tol=0.5 * _D)[0]
    for i in range(len(bsg)):
        if bottom[i] == 0:
            assert np.allclose(inc_p[i], bsg[i], rtol=0.0, atol=_BED_ULP), \
                f"bottom geom fibre {i}"
            assert np.array_equal(inc_t[i], bst[i]), f"bottom time fibre {i}"
    # stepwise march == one big advance (march mechanics correct under kinematics)
    step = _orch(paths, ptimes, max_accel=2000.0)
    for f in np.linspace(0.0, 1.05, 40):
        step.advance_to(f * tmax)
    a, b = big.committed_fibers()[0], step.committed_fibers()[0]
    assert all(np.array_equal(a[i], b[i]) for i in range(len(a))), \
        "stepwise != one-shot under kinematics"


def test_bottom_layer_underside_rests_on_bed_across_diameter_edit():
    """The reported bug: adjusting the fibre diameter part-way through the FIRST layer
    made the thinner, already-laid stretch float off the build plate while the thicker
    future stretch sat on it. Root cause: the bottom layer rested its CENTRELINE on the
    bed, so a point's underside (centre - radius) depended on its own diameter -- thin
    and thick stretches of one fibre no longer shared an underside plane.

    Fix: the bed rest lifts each point's centreline by its own radius, so every
    bottom-layer point's UNDERSIDE (centre - drawn radius) lands on the bed plane,
    however the diameter varies. Edit thin->thick mid-fibre and check both the
    already-laid (thin) and the post-edit (thick) undersides sit on the bed -- and that
    the diameter really did jump (so the test exercises the varying-diameter case)."""
    paths, ptimes = _grid()
    bed = min(float(np.median(np.asarray(p)[:, 2])) for p in paths)
    t0, t1 = float(ptimes[0][0]), float(ptimes[0][-1])
    orch = _orch(paths, ptimes)
    t_edit = 0.5 * (t0 + t1)
    orch.set_params(t_edit, Params(_P * 2.0, _V, _T, 1.0))   # thicken the later half
    orch.advance_to(_tmax(ptimes) + 1.0)
    cp = orch.committed_fibers()[0]
    ct = orch.committed_fibers()[1]
    ppd = orch.committed_ppd()
    bottom = _layer_ids([np.asarray(p) for p in paths], layer_tol=0.5 * _D)[0]
    checked_thin = checked_thick = False
    for i in range(len(cp)):
        if bottom[i] != 0 or cp[i] is None:
            continue
        z = np.asarray(cp[i])[:, 2]
        r = 0.5 * np.asarray(ppd[i])
        underside = z - r                      # bottom of the drawn tube
        assert np.allclose(underside, bed, atol=1e-6), \
            f"bottom fibre {i} underside not on the bed: " \
            f"[{underside.min():.6f}, {underside.max():.6f}] vs bed {bed:.6f}"
        tt = np.asarray(ct[i])
        pre, post = tt <= t_edit, tt > t_edit
        checked_thin = checked_thin or bool(pre.any())
        checked_thick = checked_thick or bool(post.any())
        if pre.any() and post.any():
            # a fibre spanning the edit really is thicker after it (varying-diameter
            # case exercised) -- yet both stretches' undersides still rest on the bed
            assert np.max(r[post]) > np.min(r[pre]) + 1e-4, \
                f"edit did not thicken the later stretch of fibre {i}"
    assert checked_thin and checked_thick, "edit did not span a bottom-layer fibre"


def test_support_radius_tracks_local_diameter():
    """The user-facing fix: a later layer rests on an earlier fibre at that fibre's
    LOCAL diameter, not one diameter for the whole fibre. Edit the diameter (pressure)
    part-way through a bottom-layer fibre so it is thin on its first half and thick on
    its second; the committed support then holds a crossing fibre HIGHER over the thick
    half than over the thin half. (Before this fix the sag used one scalar diameter per
    fibre, so the whole fibre lifted together and the second layer floated over the
    thin half despite touching the thick half.)"""
    paths, ptimes = _grid()
    t0, t1 = float(ptimes[0][0]), float(ptimes[0][-1])
    orch = _orch(paths, ptimes)
    orch.set_params(0.5 * (t0 + t1), Params(_P * 1.5, _V, _T, 1.0))
    orch.advance_to(orch._completion[orch._order[0]] + 1e-6)     # commit layer 0
    # fibre 0 runs along +x at a fixed y: thin where laid first (low x), thick later
    ppd0 = orch.committed_ppd()[0]
    assert ppd0[-1] > ppd0[0] + 1e-3, "edit did not thicken the later half"
    idx = orch._state.all_lower
    y = float(np.median(np.asarray(paths[0])[:, 1]))
    qr = 0.5 * _D
    h_thin = idx.rest_heights(np.array([1.0]), np.array([y]), qr, -10.0)[0]
    h_thick = idx.rest_heights(np.array([5.0]), np.array([y]), qr, -10.0)[0]
    assert h_thick > h_thin + 1e-4, \
        f"support not per-point: thick {h_thick:.6f} <= thin {h_thin:.6f}"


def test_incremental_equals_batch_tall_stack():
    """The keystone, but TALL: a stack deep enough that the lower-index per-cell cap
    drops most of the buried interior (a cell holds only the structure within
    `_LOWER_WINDOW_LAYERS` layer heights of its top). The accumulated sag must STILL
    be byte-for-byte the batch solve, which keeps the full history -- proving the cap
    only ever drops segments no draping fibre could reach. If a real scaffold rested
    deeper than the cap depth, this would diverge."""
    from mew_incremental import _LOWER_WINDOW_LAYERS
    paths, ptimes = _grid(n_layers=10)
    orch = _orch(paths, ptimes)
    assert orch.applicable
    assert orch._prune_lower                            # bottom-up -> cap on
    orch.advance_to(_tmax(ptimes) + 1.0)
    assert orch.n_committed > _LOWER_WINDOW_LAYERS + 2
    # the cap really did bound the index: it holds far less than the full stack
    assert orch._state.all_lower.n < 10 * 5000          # not the whole stack held
    inc_p, inc_t, inc_d = orch.committed_fibers()
    assert all(p is not None for p in inc_p)
    bsg, bst = _batch_like(paths, ptimes, inc_d)
    _assert_march_equals_batch(inc_p, inc_t, bsg, bst)


def test_stepwise_equals_oneshot():
    """Advancing the clock in many small steps lays the same scaffold as one big
    advance (the march is idempotent and order-independent in t)."""
    paths, ptimes = _grid()
    big = _orch(paths, ptimes)
    big.advance_to(_tmax(ptimes) + 1.0)
    step = _orch(paths, ptimes)
    for f in np.linspace(0.0, 1.05, 40):
        step.advance_to(f * _tmax(ptimes))
    a = big.committed_fibers()[0]
    b = step.committed_fibers()[0]
    assert all(np.array_equal(a[i], b[i]) for i in range(len(a)))


# --- frozen past / lazy future ---------------------------------------------- #
def test_edit_at_playhead_freezes_past_and_is_o1():
    """A knob edit at the laid frontier drops NO committed layer (O(1), frozen
    past) and leaves every already-committed fibre byte-unchanged; fibre laid after
    the edit differs from a no-edit march."""
    paths, ptimes = _grid()
    tmax = _tmax(ptimes)
    orch = _orch(paths, ptimes)
    orch.advance_to(0.5 * tmax)
    n_before = orch.n_committed
    before = [None if p is None else p.copy()
              for p in orch.committed_fibers()[0]]
    # edit just after the frontier -> no truncation
    orch.set_params(orch.frontier_time() + 1e-6, Params(_P * 1.4, _V, _T, 1.0))
    assert orch.n_committed == n_before                 # nothing dropped
    orch.advance_to(tmax + 1.0)
    after = orch.committed_fibers()[0]
    for i in range(len(before)):
        if before[i] is not None:                       # frozen past byte-identical
            assert np.array_equal(before[i], after[i]), f"frozen fibre {i} moved"
    # a no-edit march, for the "future responded" check
    base = _orch(paths, ptimes)
    base.advance_to(tmax + 1.0)
    base_after = base.committed_fibers()[0]
    changed = any(not np.array_equal(after[i], base_after[i])
                  for i in range(len(after)) if before[i] is None)
    assert changed, "edit did not propagate to fibre laid after it"


def test_scrub_back_reproduces():
    """Rewinding (truncate) then re-advancing with the same schedule reproduces the
    un-rewound result byte-for-byte (the kept prefix is reused, the rest re-laid)."""
    paths, ptimes = _grid()
    tmax = _tmax(ptimes)
    orch = _orch(paths, ptimes)
    orch.advance_to(tmax + 1.0)
    full = [p.copy() for p in orch.committed_fibers()[0]]
    orch.advance_to(0.35 * tmax)                        # scrub back -> truncate
    assert orch.n_committed < len(full)
    orch.advance_to(tmax + 1.0)                         # forward again
    again = orch.committed_fibers()[0]
    assert all(np.array_equal(full[i], again[i]) for i in range(len(full)))


def test_reset_collapses():
    """Reset clears the accumulator; re-marching reproduces the original."""
    paths, ptimes = _grid()
    tmax = _tmax(ptimes)
    orch = _orch(paths, ptimes)
    orch.advance_to(tmax + 1.0)
    full = [p.copy() for p in orch.committed_fibers()[0]]
    orch.set_params(0.5 * tmax, Params(_P * 2.0, _V, _T, 1.0))   # mess it up
    orch.advance_to(tmax + 1.0)
    orch.reset(Params(_P, _V, _T, 1.0))
    assert orch.n_committed == 0
    orch.advance_to(tmax + 1.0)
    again = orch.committed_fibers()[0]
    assert all(np.array_equal(full[i], again[i]) for i in range(len(full)))


# --- the in-progress tip ---------------------------------------------------- #
def test_tip_is_the_in_progress_layer_and_sagged():
    """Mid-print the tip is the layer currently being laid, SAGGED against the
    frozen structure below -- shown at its resting Z (it DIPS into the pores rather
    than floating flat at the commanded layer height: the bug this fixes) -- and
    byte-identical to the geometry that layer commits with, so it settles with no
    snap when complete. At the end there is no tip.

    NB: with the bed-underside rest the whole stack sits ~one radius above the
    commanded Z (the commanded plane is the fibre UNDERSIDE / the plate, not the
    centreline), so the tip's crossover peaks legitimately rise a little above the
    commanded height; the sag signature that guards the original floating bug is the
    DIP into the pores below the commanded plane, which a flat floating tip never has."""
    paths, ptimes = _grid()
    tmax = _tmax(ptimes)
    orch = _orch(paths, ptimes)
    orch.advance_to(0.5 * tmax)
    members, tp, tt, td = orch.tip_fibers()
    assert members and len(tp) == len(members)
    # SAGGED, not the floating relaid centreline: the fibre dips into the pores,
    # below the commanded layer plane (a flat floating tip would sit at/above it).
    dipped = False
    for k, i in enumerate(members):
        zcmd = float(np.median(np.asarray(paths[i])[:, 2]))
        dipped = dipped or bool(np.any(tp[k][:, 2] < zcmd - 1e-6))
    assert dipped, "tip never dips below commanded Z -- not sagged (the bug)"
    # no snap: the tip is byte-for-byte the geometry this layer commits with
    orch.advance_to(tmax + 1.0)
    cp, ct, _cd = orch.committed_fibers()
    for k, i in enumerate(members):
        assert np.array_equal(tp[k], cp[i]), f"tip != committed (snap) fibre {i}"
        assert np.array_equal(tt[k], ct[i]), f"tip time != committed fibre {i}"
    assert orch.tip_fibers() == ([], [], [], [])        # nothing left to lay


# --- degenerate ------------------------------------------------------------- #
def test_single_flat_layer_passthrough():
    """A single flat layer with no self-crossings: sag is N/A, so each fibre passes
    through as its relaid centreline (no crash, no sag state)."""
    npts = 41
    s = np.linspace(0.0, 6.0, npts)
    p = np.column_stack([s, np.zeros(npts), np.zeros(npts)])
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    t = np.concatenate([[0.0], np.cumsum(seg) / (2.0 * _cts())])
    orch = _orch([p], [t])
    assert not orch.applicable
    orch.advance_to(float(t[-1]) + 1.0)
    cp = orch.committed_fibers()[0]
    rp, _ = relay_fibers([p], [t], _model(), ParamSchedule(Params(_P, _V, _T, 1.0)),
                         zeta=1.0, max_accel=None, square_corner_velocity=6.0)
    assert np.array_equal(cp[0], rp[0])


def test_diameter_what_if_moves_geometry():
    """A base_path_diam threads through and the per-fibre diameter the sag uses is the
    true physics value (positive, finite -- no clamp band). Sanity that the diameter
    path is wired, not a bit-identity claim."""
    paths, ptimes = _grid()
    base = [_D] * len(paths)
    orch = _orch(paths, ptimes, base_path_diam=base)
    orch.advance_to(_tmax(ptimes) + 1.0)
    _, _, d = orch.committed_fibers()
    assert all(dd > 0.0 and np.isfinite(dd) for dd in d if dd is not None)


def test_per_point_diameter_forward_accumulates():
    """The drawn (per-point) diameter forward-accumulates WITHIN a fibre: a diameter
    edit (pressure) part-way through a fibre's deposition leaves the already-laid
    stretch at its original diameter and only thickens/thins the stretch laid after
    the edit. This is the live-sim bug the per-point pass fixes -- the old per-fibre
    AVERAGE repainted the whole fibre (including already-laid fibre) on every edit."""
    paths, ptimes = _grid()
    end = _tmax(ptimes) + 1.0
    fi = len(paths) - 1                       # a top-layer fibre (laid last)
    t0, t1 = float(ptimes[fi][0]), float(ptimes[fi][-1])
    tedit = 0.5 * (t0 + t1)                    # edit mid-way through fibre `fi`

    # no-edit reference: the same fibre under a constant schedule. Its diameter is
    # NOT constant along the fibre -- the deposited diameter is mass conservation at
    # the LANDING speed, which varies through starts and turnarounds even at a fixed
    # feed -- so the baseline is the whole per-point curve, read at the edited run's
    # own times. (A diameter edit changes the post-edit sag geometry and so the point
    # count, which is why the comparison goes through the times and not the index.)
    base = _orch(paths, ptimes)
    base.advance_to(end)
    base_ppd = base.committed_ppd()[fi]
    base_t = base.committed_fibers()[1][fi]
    assert base_ppd is not None and len(base_ppd) == len(base_t)

    orch = _orch(paths, ptimes)               # step the diameter up at `tedit`
    orch.set_params(tedit, Params(_P * 1.4, _V, _T, 1.0))
    orch.advance_to(end)
    times = orch.committed_fibers()[1][fi]
    ppd = orch.committed_ppd()[fi]
    assert ppd is not None and len(ppd) == len(times)

    margin = 0.08 * (t1 - t0)
    before = times < tedit - margin
    after = times > tedit + margin
    assert before.any() and after.any(), "fibre does not straddle the edit"
    # The two runs do not sample the fibre at the same TIMES (the edit reshapes the
    # layer's grid) and the diameter curve has steep steps in it, so comparing
    # pointwise against an interpolated baseline measures the sampling offset as
    # well as any repaint. The scale of that offset is not a free parameter: a
    # piecewise-linear interpolant read off-grid cannot err by more than the
    # baseline's own VARIATION over the samples bracketing the query. So use that
    # variation as the yardstick and let the two effects separate themselves --
    # measured here, the already-laid stretch sits at 0.135 of it while the edit's
    # response is 4.34 times it, because the edit multiplies the diameter by
    # sqrt(1.4) = 1.183 and nothing about sampling can imitate an 18% step.
    d_base = np.interp(times, base_t, base_ppd)

    def _local_variation(t, k=2):
        j = int(np.searchsorted(base_t, t))
        w = base_ppd[max(0, j - k):min(len(base_ppd), j + k + 1)]
        return float(w.max() - w.min())

    bound = np.array([_local_variation(float(t)) for t in times])
    err = np.abs(ppd - d_base)
    # already-laid stretch keeps its ORIGINAL diameter (not repainted by the edit)
    assert np.all(err[before] <= bound[before] + 1e-12), (
        "already-laid stretch was repainted by a later edit (the bug): worst "
        "%.4e mm against an interpolation bound of %.4e mm"
        % (err[before].max(), bound[before].max()))
    # the post-edit stretch actually responded (thicker), so the edit DID take effect
    assert np.all(ppd[after] - d_base[after] > 2.0 * bound[after]), \
        "post-edit stretch did not thicken -- edit had no effect"


def test_fat_fibre_support_uncapped_no_clip():
    """The robust fix for the live-sim fat-crossover bug. Drive the deposited fibre
    well past the index pre-size (the old clamp ceiling) and lay a 2-layer cross:

      * BOTH the drawn diameter (committed_ppd) AND the sag-support diameter
        (committed_fibers' diam) grow past the pre-size -- neither is capped (before,
        only the drawn one grew while the support froze ~1.9x nominal); and
      * a top-layer crossover RESTS one TRUE diameter above the fibre below instead of
        being pinned at the pre-size height -- so the fat tube sits cleanly on top
        rather than its underside sinking into the support (the clipping the user saw
        past ~30um). The bottom layer now rests its UNDERSIDE on the bed (centreline at
        ~r = d/2, not z~0), so the top crossover centre ~ d/2 + d = 1.5*d (its own
        underside one true diameter above the bottom fibre's, both above the plate)."""
    paths, ptimes = _grid(n_layers=2, n_lines=3)
    model = _model()
    cap = _INDEX_PRESIZE_MULT * _D
    # a pressure whose physics diameter clearly exceeds the pre-size (d ~ sqrt(Q) ~
    # sqrt(pressure), monotone, so doubling reaches it; bounded so a fit can't hang)
    hi_p, d = _P, 0.0
    for _ in range(40):
        d = model.solve(ProcessParams(pressure=hi_p, voltage=_V, speed=2.0 * _cts(),
                                      temperature=_T)).fiber_diameter
        if d > cap * 1.2:
            break
        hi_p *= 2.0
    assert d > cap * 1.2, "could not drive the physics diameter past the pre-size"

    orch = _orch(paths, ptimes)
    orch.set_params(0.0, Params(hi_p, _V, _T, 1.0))      # high pressure from the start
    orch.advance_to(_tmax(ptimes) + 1.0)
    cpaths, _, diams = orch.committed_fibers()
    ppd = [p for p in orch.committed_ppd() if p is not None]
    assert ppd, "nothing laid"

    # neither the DRAWN nor the SAG-support diameter is capped at the pre-size
    assert max(float(np.max(p)) for p in ppd) > cap + 1e-6, \
        "drawn diameter was capped -- fibre can't grow"
    assert max(dd for dd in diams if dd is not None) > cap + 1e-6, \
        "sag-support diameter was capped at the pre-size (the clipping bug)"

    # a top-layer fibre (laid last) crosses the bottom layer; its highest point is a
    # crossover, which must rest ~one TRUE diameter up, NOT at the old pinned ~1.9x.
    top = cpaths[len(paths) - 1]
    zmax = float(np.max(top[:, 2]))
    assert zmax > cap + 1e-4, \
        f"top crossover pinned at the pre-size (z={zmax:.4f}); fat tube clips in"
    assert abs(zmax - 1.5 * d) < 0.3 * d, \
        f"top crossover not resting at the TRUE diameter (z={zmax:.4f} vs 1.5*d="\
        f"{1.5 * d:.4f}); bottom underside on bed => crossover ~1.5*d"


def test_mid_layer_edit_freezes_deposited_geometry():
    """Already-deposited geometry must NOT move when a parameter is edited mid-print.
    Lay partway into a layer, snapshot the in-progress (tip) layer's already-laid
    portion, then edit the diameter thin->thick AT the playhead and re-advance to the
    same playhead: every point laid before the edit -- committed AND the in-progress
    layer's revealed prefix -- is byte-identical, while a fibre laid after the edit
    still thickens. (Before the freeze-front the in-progress layer re-sagged as a
    whole, so the already-laid thin fibre floated ~30um the instant the thick fibre
    began -- the bug the user hit.)"""
    paths, ptimes = _grid(n_layers=4, n_lines=4)
    model = _model()
    hi_p, d = _P, 0.0
    for _ in range(40):
        d = model.solve(ProcessParams(pressure=hi_p, voltage=_V, speed=2.0 * _cts(),
                                      temperature=_T)).fiber_diameter
        if d > 2.5 * _D:
            break
        hi_p *= 2.0
    assert d > 2.5 * _D, "could not drive a clear thin->thick jump"
    tmax = _tmax(ptimes)
    t_play = 0.6 * tmax                          # partway through an in-progress layer

    orch = _orch(paths, ptimes)
    orch.advance_to(t_play)
    committed_before = [None if p is None else p.copy()
                        for p in orch.committed_fibers()[0]]
    tm, tp, tt, _td = orch.tip_fibers()
    tip_before = {i: np.array(tp[k])[np.asarray(tt[k]) <= t_play]
                  for k, i in enumerate(tm)}

    orch.set_params(t_play, Params(hi_p, _V, _T, 1.0))     # thin->thick mid-layer
    orch.advance_to(t_play)                                # same playhead

    # committed (fully-laid) fibre is byte-unchanged
    ca = orch.committed_fibers()[0]
    for i, p in enumerate(committed_before):
        if p is not None:
            assert np.array_equal(p, ca[i]), f"committed fibre {i} moved on edit"
    # the in-progress layer's already-laid (revealed) portion is byte-unchanged
    tm2, tp2, tt2, _td2 = orch.tip_fibers()
    saw = False
    for k, i in enumerate(tm2):
        if i not in tip_before:
            continue
        za = np.array(tp2[k])[np.asarray(tt2[k]) <= t_play]
        zb = tip_before[i]
        assert len(za) == len(zb) and np.array_equal(za, zb), \
            f"in-progress fibre {i} already-laid portion moved on edit (the float bug)"
        saw = True
    assert saw, "test laid no in-progress tip to check"

    # ...and the edit DID take effect on fibre laid after it (not a frozen no-op)
    orch.advance_to(tmax + 1.0)
    assert any(p is not None and float(np.max(p)) > 1.5 * _D
               for p in orch.committed_ppd()), "edit never thickened later fibre"


def test_rest_heights_widens_for_fat_fibre():
    """`_SegIndex.rest_heights` finds a contact whose reach exceeds the fixed 3x3 cell
    block -- the index correctness that lets the sag support track a fibre fatter than
    the cell was pre-sized for, so the deposited-diameter clamp could be removed. A
    thin cell, one fat-radius support segment, and a query offset the contact spans but
    a 3x3 block does NOT: the widened query must still find it, at the exact geometric
    rest height z = sqrt(s^2 - h^2)."""
    from gcode_to_fibers import _SegIndex, _hash_cell
    dthin = 0.02
    cell = _hash_cell(dthin)
    idx = _SegIndex(cell)
    seg = np.column_stack([np.linspace(0.0, 10.0, 200), np.zeros(200), np.zeros(200)])
    r_big = 2.0 * dthin                                   # a fat support fibre
    idx.add_fiber(seg, seg_radius=r_big)
    qr = 2.0 * dthin                                      # a fat query fibre
    h = 3.5 * dthin                                       # XY offset: in reach, > 3x3
    # a fixed 3x3 block (k=1) does NOT reach this offset -> would miss the contact
    qid, _ = idx.point_pairs(np.array([5.0]), np.array([h]), k=1)
    assert qid.size == 0, "test no longer exercises widening (3x3 already hits)"
    # the real query widens k from the contact distance and finds it, exactly placed
    z = idx.rest_heights(np.array([5.0]), np.array([h]), qr, -1e9)[0]
    s = qr + r_big
    assert abs(z - math.sqrt(s * s - h * h)) < 1e-9, \
        f"fat contact missed or mis-placed (z={z:.6f})"


def test_static_diameter_mode_keeps_nominal():
    """With dynamic_diameter OFF (the GUI's manual fibre-diameter override), the live
    fibre is drawn at the COMMANDED diameter no matter the physics knobs -- so the
    override value shows in the live sim, matching the locked sim / STL viewer. The
    centreline still re-lays for jet-lag; only the diameter is pinned."""
    paths, ptimes = _grid()
    orch = IncrementalDeposition(
        paths, ptimes, _model(), launch_pressure=_P, launch_voltage=_V,
        launch_temperature=_T, diameter=_D, sag_multiplier=1.0,
        self_crossings=True, zeta=1.0, dynamic_diameter=False)
    orch.set_params(0.0, Params(_P * 8.0, _V, _T, 1.0))  # a big edit must NOT re-size
    orch.advance_to(_tmax(ptimes) + 1.0)
    ppd = [d for d in orch.committed_ppd() if d is not None]
    diams = [d for d in orch.committed_fibers()[2] if d is not None]
    assert ppd, "nothing laid"
    assert all(np.allclose(d, _D) for d in ppd), \
        "drawn diameter not pinned to the commanded nominal under override"
    assert all(abs(dd - _D) < 1e-9 for dd in diams), \
        "sag diameter not pinned to the commanded nominal under override"


def test_lower_cap_keeps_exposed_deep_support():
    """The rest-height index's per-cell height cap keeps a deep fibre that is the
    ONLY structure at its XY -- a perimeter turnaround loop the upper layers
    ballooned past (different cells) -- where the old global layer-count window
    evicted it once a few newer layers were committed, so a later fibre dropped
    straight through it. Regression for the trapezoid-perimeter intersections.

    Mechanism check on `_SagState` directly (no march): one isolated deep fibre at
    y=0, then six higher layers all at OTHER y (sharing no cell with it). A 3-layer
    count window would evict the deep fibre; the per-cell cap keeps it because it is
    the TOP of its own (otherwise empty) cells, so a query over it still rests on
    it."""
    from gcode_to_fibers import _SagState as _SS, _hash_cell as _hc
    Dd = 0.02
    st = _SS(shared_cell=_hc(Dd), bed_center=0.0, max_diam=Dd, sag_multiplier=1.0,
             self_crossings=False, with_times=False, lower_cap_depth=3.0 * Dd)
    deep = np.column_stack([np.linspace(0, 10, 50), np.zeros(50), np.zeros(50)])
    st.reuse_layer([deep], [Dd])                          # the exposed deep loop
    for L in range(1, 7):                                 # six higher layers, far in y
        f = np.column_stack([np.linspace(0, 10, 50),
                             np.full(50, 2.0 + L), np.full(50, L * Dd)])
        st.reuse_layer([f], [Dd])
    h = st.all_lower.rest_heights(np.array([5.0]), np.array([0.0]), Dd / 2, -1e9)[0]
    assert h > 0.5 * Dd, \
        f"deep exposed support was evicted (rest h={h:.4f}); a fibre would drop through"


# --- locked-`.depo` seeding: the unedited live sim IS the locked sim --------- #
def _marked_locked(paths, ptimes, inc_diam):
    """A stand-in 'locked .depo' geometry for the seeding tests: the batch apply_sag
    output with every Z lifted by a constant the mew_live re-derive would NEVER
    produce, so a test can prove the march committed the CACHE verbatim (not a
    re-derive that happens to match). Returns the (paths, times, diam, ppd) tuple the
    orchestrator's `locked=` expects."""
    bsg, bst = _batch_like(paths, ptimes, inc_diam)
    marked = [p.copy() for p in bsg]
    for p in marked:
        p[:, 2] += 0.5                       # an unmistakable, un-re-derivable offset
    diam = [d if d is not None else _D for d in inc_diam]
    return (marked, bst, diam, None)


def _seeded(paths, ptimes, locked):
    return IncrementalDeposition(
        paths, ptimes, _model(), launch_pressure=_P, launch_voltage=_V,
        launch_temperature=_T, diameter=_D, sag_multiplier=1.0, self_crossings=True,
        zeta=1.0, max_accel=None, square_corner_velocity=6.0, locked=locked)


def test_unedited_march_rides_locked_verbatim():
    """The headline fix: with a locked `.depo` seed and NO edit, the march commits the
    cached fibres BYTE-FOR-BYTE -- it never re-derives via mew_live -- so the live sim
    is the locked sim. (mew_live and mew_deposition reshape the centreline slightly
    differently, so re-deriving drifts the live crossovers from the locked ones; riding
    the cache removes that entirely.) The seed is marked distinct from any re-derive to
    prove the cache is what was committed."""
    paths, ptimes = _grid()
    base = _orch(paths, ptimes)
    base.advance_to(_tmax(ptimes) + 1.0)
    inc_d = base.committed_fibers()[2]
    locked = _marked_locked(paths, ptimes, inc_d)
    orch = _seeded(paths, ptimes, locked)
    assert orch.has_locked
    orch.advance_to(_tmax(ptimes) + 1.0)
    cp, ct, _ = orch.committed_fibers()
    for i in range(len(cp)):
        assert cp[i] is not None, f"fibre {i} not laid"
        assert np.array_equal(cp[i], locked[0][i]), \
            f"fibre {i} is NOT the locked cache (the march re-derived instead of riding it)"
        assert np.array_equal(ct[i], locked[1][i]), f"fibre {i} times not the cache"


def test_edit_freezes_locked_past_rederives_future():
    """An edit re-derives ONLY fibre laid after it; everything laid before stays the
    locked `.depo` geometry, frozen. So increasing the diameter mid-print never moves
    (e.g. lifts/fuses) the already-laid layers -- the 'first thin layer pulled up to
    fuse with the thick layer above' report. Pre-edit fibres stay byte-equal to the
    cache; post-edit fibres re-derive (and so differ from the marked cache)."""
    paths, ptimes = _grid()
    base = _orch(paths, ptimes)
    base.advance_to(_tmax(ptimes) + 1.0)
    inc_d = base.committed_fibers()[2]
    locked = _marked_locked(paths, ptimes, inc_d)
    orch = _seeded(paths, ptimes, locked)
    te = orch._completion[orch._order[1]]            # edit just after the 2nd-laid layer
    orch.advance_to(te)
    orch.set_params(te, Params(_P * 1.5, _V, _T, 1.0))
    orch.advance_to(_tmax(ptimes) + 1.0)
    cp = orch.committed_fibers()[0]
    pre = post = 0
    for i in range(len(cp)):
        if cp[i] is None:
            continue
        comp_i = next(orch._completion[li] for li, m in enumerate(orch._members)
                      if i in m)
        if comp_i <= te + 1e-9:
            assert np.array_equal(cp[i], locked[0][i]), \
                f"pre-edit fibre {i} MOVED (it must stay the frozen locked cache)"
            pre += 1
        else:
            assert not np.array_equal(cp[i], locked[0][i]), \
                f"post-edit fibre {i} did not re-derive (still the cache)"
            post += 1
    assert pre > 0 and post > 0, f"need both sides of the edit (pre={pre} post={post})"


def test_locked_seed_optional_and_count_guarded():
    """`locked=None` (no cache) -> the march re-derives everything, the pre-`.depo`
    behaviour (the incremental==batch keystone path is untouched). A cache whose fibre
    count differs from the commanded set is rejected at construction (has_locked False)
    rather than mis-aligning."""
    paths, ptimes = _grid()
    assert not _orch(paths, ptimes).has_locked            # no seed -> re-derive
    inc_d = [None] * len(paths)
    good = _marked_locked(paths, ptimes, inc_d)
    short = (good[0][:-1], good[1][:-1], good[2][:-1], None)   # one fibre too few
    assert not _seeded(paths, ptimes, short).has_locked


# --- the crossover-crest-symmetry fix: constant-param re-derives via the EXPORT
#     relay (mew_deposition, symmetric crests) not mew_live (asymmetric) ---------- #
def _real_locked(paths, ptimes):
    """A REAL (un-marked) locked seed: the batch sag geometry + nominal diameter, so a
    re-derived layer can be COMPARED to it (the marked seed's +0.5 Z offset would only
    prove non-equality). The gate only checks the seed's presence, so the relay used to
    build it is immaterial here."""
    bsg, bst = _batch_like(paths, ptimes, [None] * len(paths))
    return (bsg, bst, [_D] * len(paths), None)


def test_constant_param_edit_routes_through_export_relay():
    """With a locked seed, a layer laid AFTER a constant-param edit (no breakpoint
    strictly inside it) re-derives via the EXPORT relay (mew_deposition) -- the fix for
    the live-sim crossover-crest asymmetry. Without a seed the SAME edit keeps mew_live
    (the gate is off), so the pre-`.depo` behaviour is untouched."""
    paths, ptimes = _grid()
    orch = _seeded(paths, ptimes, _real_locked(paths, ptimes))
    assert orch.has_locked
    te = orch._completion[orch._order[0]]            # edit just after the 1st-laid layer
    orch.advance_to(te)
    orch.set_params(te, Params(_P, _V, _T, 1.0))
    post = [li for li in range(orch._n_layers) if orch._completion[li] > te + 1e-9]
    assert post and all(orch._use_export_relay(li) for li in post)
    bare = _orch(paths, ptimes)                      # no seed -> gate off
    bare.advance_to(te)
    bare.set_params(te, Params(_P, _V, _T, 1.0))
    assert not any(bare._use_export_relay(li) for li in range(bare._n_layers))


def test_export_relay_branch_lays_the_mew_deposition_centreline():
    """The export-relay branch's centreline IS `mew_deposition.laid_fibers` (with the
    same boundary speeds / loop flags / speed_scale the orchestrator threads), so a
    re-derived constant-param layer reproduces the locked/export geometry rather than
    mew_live's denser, crest-distorting touchdown."""
    import mew_deposition as md
    paths, ptimes = _grid()
    orch = _seeded(paths, ptimes, _real_locked(paths, ptimes))
    li = orch._order[1]
    members = orch._members[li]
    mp = [orch._cmd_paths[i] for i in members]
    mt = [orch._cmd_ptimes[i] for i in members]
    mev = [orch._entry_vs[i] for i in members]
    mxv = [orch._exit_vs[i] for i in members]
    _m, paths_e, _t, _d, _dlk = orch._relay_layer_export(li, members, mp, mt, mev, mxv)
    le = [orch._loop_entry[i] for i in members]
    ep, _et, _info = md.laid_fibers(
        mp, mt, orch.model, pressure=_P, voltage=_V, temperature=_T, speed_scale=1.0,
        zeta=orch.zeta, coil=orch.coil, max_accel=orch.max_accel,
        square_corner_velocity=orch.scv, loop_entry=le, entry_vs=mev, exit_vs=mxv)
    assert len(paths_e) == len(ep)
    for k in range(len(members)):
        assert np.array_equal(paths_e[k], ep[k]), f"member {k} != mew_deposition relay"


def test_scrub_back_edit_in_tip_matches_no_scrub_edit():
    """A scrub-back edit inside the IN-PROGRESS tip layer must not leave stale-param
    fibre behind. Regression for the frozen-front clamp bug: `_freeze_tip_up_to` only
    ever GREW the freeze front (`max`), so after (a) editing at t1 inside the tip, then
    (b) scrubbing the playhead back to t2 < t1 and editing there, the [t2, t1] span
    stayed frozen with the pre-t2 params even though `ParamSchedule.set_at` had already
    discarded the superseded t1 breakpoint -- so committed geometry disagreed with the
    schedule and the error propagated into the layers resting on top.

    The invariant: the scrub HISTORY must not affect committed geometry. Editing at t2
    after a t1-edit-then-scrub-back must give byte-identical fibre to editing at t2 once
    (both reach the same final schedule with the tip advanced to the same point)."""
    paths, ptimes = _grid(n_layers=4, n_lines=4)
    o0 = _orch(paths, ptimes)
    comp = [o0._completion[o0._order[c]] for c in range(o0._n_layers)]
    lo, hi = comp[1], comp[2]                         # layer 2's deposition window
    t2, t1, tmid = lo + 0.25 * (hi - lo), lo + 0.55 * (hi - lo), lo + 0.80 * (hi - lo)
    tend = _tmax(ptimes) + 1.0
    B = Params(_P * 2.0, _V, _T, 1.0)
    A = Params(_P * 1.5, _V, _T, 1.0)                 # a DIFFERENT, superseded edit

    # equivalent single edit at t2 (tip advanced past t2), NO scrub, NO superseded edit
    ref = _orch(paths, ptimes)
    ref.advance_to(tmid); ref.set_params(t2, B); ref.advance_to(tend)
    pref, _tr, dref = ref.committed_fibers()

    # the scrub-back sequence: edit at t1, scrub the playhead back to t2, edit at t2
    scr = _orch(paths, ptimes)
    scr.advance_to(tmid)
    scr.set_params(t1, A)                             # freezes the tip up to t1
    scr.advance_to(t2)                               # scrub the playhead back
    scr.set_params(t2, B)                            # supersedes A; same final schedule
    scr.advance_to(tend)
    pscr, _ts, dscr = scr.committed_fibers()

    for i, (a, b) in enumerate(zip(pref, pscr)):
        if a is None and b is None:
            continue
        assert a is not None and b is not None, f"fibre {i} committed in only one run"
        assert a.shape == b.shape, f"fibre {i} shape {a.shape} != {b.shape} (scrub left stale fibre)"
        assert np.allclose(a, b, atol=1e-9), (
            f"fibre {i} differs by {np.abs(a - b).max():.2e} mm -- the scrub-back edit "
            f"left stale-param geometry behind")
    for i, (a, b) in enumerate(zip(dref, dscr)):
        if a is None or b is None:
            continue
        assert abs(a - b) < 1e-12, f"fibre {i} rep-diameter {a} != {b}"


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
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    raise SystemExit(1 if failed else 0)
