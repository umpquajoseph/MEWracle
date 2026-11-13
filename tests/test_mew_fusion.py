"""Tests for fibre fusion: the contact-localised inter-layer compaction.

These pin the behaviour the feature promises, NOT a fitted number, across the law
(`mew_physics`), the seam (`gcode_to_fibers.set_fusion_law` and friends) and the
geometry (`apply_sag`). The test names say what each asserts; three claims are
load-bearing enough to name here.

  * FUSION OFF IS BYTE-IDENTICAL to the geometric stack -- the comparator for
    every "off" gate is the unfused solve itself, not a recorded number.
  * PEAKS DROP, SPANS DO NOT FLATTEN. Stacked crossover peaks come down by ~delta
    while suspended spans keep their sag depth and merely translate with their
    supports. If the spans fused as much, the feature would change nothing.
  * THE WALL / JUNCTION SPLIT is SELECTIVE: a pure wall answers only to phi_wall
    and a pure junction tower only to phi_junction, at any value of the other, so
    the classifier cannot leak. Unset or set EQUAL is byte-identical to the
    unsplit solve. The sink applies to each CANDIDATE before the max rather than
    to the winner after, which is what keeps the geometry continuous when the
    argmax flips, and the crossing angle is read over a ~1-diameter window so a
    resampled loop corner cannot flick the sink mid-fibre.

The incremental==batch keystone under fusion lives in test_mew_incremental.py.

Run:  python tests/test_mew_fusion.py
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math

import numpy as np

from mew_physics import Calibration, MEWModel
import gcode_to_fibers as gtf
from gcode_to_fibers import apply_sag, set_fusion_law, _fusion_law, _sink_fused_contacts

_D = 0.02
_TEMP = 90.0
_GAP = 4.0


def approx(a, b, rel=1e-9):
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def close(a, b, atol):
    return abs(a - b) <= atol


def _model(**cal):
    return MEWModel(working_distance=_GAP,
                    calibration=Calibration(**cal) if cal else Calibration())


def _law(model=None, temperature=_TEMP):
    return (model or _model()).make_fusion_law(temperature=temperature)


def _split(phi_j, phi_w):
    """(law, band) for a wall/junction-split calibration -- what set_fusion_law gets."""
    m = _model(fusion_overlap=phi_j, fusion_wall_overlap=phi_w)
    return _law(m), m.fusion_wall_band()


def _wall_stack(n_layers=8, d=_D, npts=81, span=2.0):
    """A PURE wall: one straight line printed n_layers times (rotation=0). EVERY
    inter-layer contact is collinear, so phi_wall alone may set its height."""
    return [np.column_stack([np.linspace(0.0, span, npts),
                             np.full(npts, 1.0), np.full(npts, L * d)])
            for L in range(n_layers)]


def _junction_tower(n_layers=8, d=_D, npts=81, span=2.0):
    """A PURE junction tower: alternating +x/+y single fibres crossing at 90 deg over
    ONE point. Nothing runs parallel to anything, so phi_junction alone may set it."""
    paths = []
    for L in range(n_layers):
        s = np.linspace(0.0, span, npts)
        z = np.full(npts, L * d)
        paths.append(np.column_stack([s, np.full(npts, 1.0), z]) if L % 2 == 0
                     else np.column_stack([np.full(npts, 1.0), s, z]))
    return paths


def _solve_split(paths, phi_j, phi_w, d=_D):
    law, band = _split(phi_j, phi_w)
    set_fusion_law(law, band)
    try:
        return apply_sag([np.array(p) for p in paths], d, sag_multiplier=1.0,
                         self_crossings=True)
    finally:
        set_fusion_law(None)


#: The bottom layer's commanded Z is the PLATE, and a fibre rests its UNDERSIDE
#: there, so its centreline seats one radius up (`_SagState._solve_layer`'s
#: `bed_at`). Every ABSOLUTE height below is that seat plus a stack of contact
#: steps; the stack heights themselves are unaffected, which is why this
#: constant appears only in the absolute bars.
_SEAT = 0.5 * _D


def _top(out):
    return max(p[:, 2].max() for p in out)


def _grid(n_layers, n_lines, span=2.0, pitch=0.15, d=_D, npts=61, offset0=0.5):
    """Stacked 0/90 cross-hatch: each layer is `n_lines` parallel straight fibres,
    alternating +x / +y by layer so upper fibres cross (and rest on) lower ones.
    Tight default pitch -> the top spans sag only PARTIALLY (supported at the nodes,
    not bottomed out on the bed), the regime where 'spans translate, not flatten' is
    cleanest to see."""
    paths = []
    for L in range(n_layers):
        z = L * d
        for k in range(n_lines):
            off = offset0 + k * pitch
            s = np.linspace(0.0, span, npts)
            p = (np.column_stack([s, np.full(npts, off), np.full(npts, z)])
                 if L % 2 == 0 else
                 np.column_stack([np.full(npts, off), s, np.full(npts, z)]))
            paths.append(p)
    return paths


def _solve(paths, d=_D, law=None):
    set_fusion_law(law)
    try:
        return apply_sag([np.array(p) for p in paths], d,
                         sag_multiplier=1.0, self_crossings=True)
    finally:
        set_fusion_law(None)


def _top_peak(out, top_idx):
    return max(out[i][:, 2].max() for i in top_idx)


# --------------------------------------------------------------------------- #
# THE LAW (mew_physics)
# --------------------------------------------------------------------------- #
def test_junction_sink_zero_overlap_is_zero():
    """phi = 0 -> no sink (the default-off contract); the installed closure agrees."""
    m = _model(fusion_overlap=0.0)
    assert m.junction_sink(_D) == 0.0
    assert _law(m)(_D) == 0.0


def test_junction_sink_is_phi_times_diameter():
    """Default (diam_exp = 0): delta = phi*d -- scales LINEARLY with diameter, and
    delta/d is the calibrated overlap phi at any diameter."""
    m = _model()
    phi = m.calibration.fusion_overlap
    assert approx(m.junction_sink(_D), phi * _D)
    assert approx(m.junction_sink(2 * _D), 2 * m.junction_sink(_D))
    assert approx(m.junction_sink(_D) / _D, m.junction_sink(3 * _D) / (3 * _D))


def test_junction_sink_clamped_below_full_merge():
    """An over-large phi is clamped at fusion_max_overlap*d so the upper centre never
    reaches the lower fibre's centre (tubes overlap, never coincide)."""
    m = _model(fusion_overlap=5.0, fusion_max_overlap=0.9)
    assert approx(m.junction_sink(_D), 0.9 * _D)


def test_make_fusion_law_thermal_off_is_temperature_independent():
    """The thermal hook defaults to 0 -> the sink does not depend on temperature."""
    m = _model()
    assert approx(_law(m, 70.0)(_D), _law(m, 130.0)(_D))


def test_make_fusion_law_thermal_hook_hotter_fuses_more():
    """With a negative viscosity exponent fitted, a hotter (less viscous, longer-
    molten) fibre fuses MORE; at the reference temperature the factor is exactly 1."""
    m = _model(fusion_thermal_visc_exp=-1.0, fusion_temp_ref=90.0)
    ref = _law(m, 90.0)(_D)
    hot = _law(m, 120.0)(_D)
    cold = _law(m, 70.0)(_D)
    assert hot > ref > cold                          # monotone in temperature
    assert approx(ref, m.junction_sink(_D))          # factor 1 at T_ref


def test_junction_sink_diam_exp_hook_superlinear():
    """A positive diameter exponent makes delta grow FASTER than linearly with d
    (delta/d increases with d) -- the optional super-linear diameter hook (off by
    default; kept at 0, which Lamb supports for HEIGHT: 9um == 31um compaction)."""
    m = _model(fusion_diam_exp=1.0, fusion_diam_ref=_D)
    assert m.junction_sink(2 * _D) / (2 * _D) > m.junction_sink(_D) / _D


# --------------------------------------------------------------------------- #
# THE SEAM (gcode_to_fibers)
# --------------------------------------------------------------------------- #
def test_fusion_seam_default_is_zero():
    """No law installed -> _fusion_law returns 0 (geometric stack, stable-safe)."""
    gtf.set_fusion_law(None)
    assert _fusion_law(_D) == 0.0


def test_sink_fused_contacts_bed_protected_fibre_lowered():
    """Bed contacts (at the bed height) are left alone; fibre-on-fibre contacts drop
    by the sink; nothing is pushed below the bed."""
    bed = 0.0
    h = np.array([bed, bed + 0.02, bed + 0.05, bed + 0.003])
    _sink_fused_contacts(h, bed, 0.007, eps=1e-4)
    assert h[0] == bed                               # bed: untouched
    assert approx(h[1], 0.013)                       # fibre: lowered by 0.007
    assert approx(h[2], 0.043)
    assert h[3] == bed                               # would-go-below-bed: clamped


def test_sink_fused_contacts_protect_mask_and_noop():
    """The protect mask (the already-merged loop entry) is skipped; sink<=0 no-ops."""
    bed = 0.0
    h = np.array([0.05, 0.05, 0.05])
    _sink_fused_contacts(h, bed, 0.007, 1e-4, np.array([False, True, False]))
    assert approx(h[0], 0.043) and approx(h[1], 0.05) and approx(h[2], 0.043)
    h2 = np.array([0.05])
    _sink_fused_contacts(h2, bed, 0.0, 1e-4)         # sink 0 -> untouched
    assert h2[0] == 0.05


# --------------------------------------------------------------------------- #
# THE GEOMETRY (apply_sag)
# --------------------------------------------------------------------------- #
def test_fusion_off_is_byte_identical():
    """phi = 0 installed is byte-for-byte the no-law geometric solve -- the seam is
    truly inert when off (the byte-identical / stable-safe guarantee)."""
    g = _grid(3, 5)
    a = _solve(g, law=None)
    b = _solve(g, law=_law(_model(fusion_overlap=0.0)))
    assert len(a) == len(b)
    assert all(np.array_equal(x, y) for x, y in zip(a, b))


def test_crossover_peaks_drop_by_delta():
    """The stacked crossover peak comes down by ~delta (the whole point)."""
    law = _law()
    delta = law(_D)
    g = _grid(2, 7)
    top = range(7, 14)
    drop = _top_peak(_solve(g), top) - _top_peak(_solve(g, law=law), top)
    assert close(drop, delta, max(1e-5, 0.1 * delta)), f"peak drop {drop} != {delta}"


def test_spans_translate_not_flatten():
    """THE load-bearing claim: a suspended span keeps its SAG DEPTH -- it rides down
    with its (lowered) supports, it is NOT flattened. Measured on a middle top fibre
    at an interior node (peak) and the midpoint to the next node (valley)."""
    law = _law()
    g = _grid(2, 7)
    geo, fus = _solve(g), _solve(g, law=law)

    def pk_vl(out):
        f = out[10]                                  # a middle top (+y) fibre
        return (np.interp(1.10, f[:, 1], f[:, 2]),   # node at y = 0.5 + 4*0.15
                np.interp(1.175, f[:, 1], f[:, 2]))  # midpoint to node y = 1.25
    gpk, gvl = pk_vl(geo)
    fpk, fvl = pk_vl(fus)
    assert (gpk - gvl) > 5e-4, "the test span barely sags -- pick a wider pore"
    assert fpk < gpk, "fused peak did not drop"
    assert close(gpk - gvl, fpk - fvl, 0.05 * (gpk - gvl)), \
        f"span sag flattened: geo {gpk - gvl:.5f} vs fused {fpk - fvl:.5f}"


def test_height_grows_linearly_reduced_slope():
    """The predicted height-vs-layers dependence: column height ~ base + (N-1)*(d -
    delta), i.e. EXACTLY delta of compaction per inter-layer junction, for every N."""
    law = _law()
    delta = law(_D)
    for N in (2, 4, 6, 8):
        g = _grid(N, 7)
        per_junction = (_top_peak(_solve(g), range(len(g))) -
                        _top_peak(_solve(g, law=law), range(len(g)))) / (N - 1)
        assert close(per_junction, delta, 0.05 * delta), \
            f"N={N}: per-junction drop {per_junction:.5f} != delta {delta:.5f}"


def test_fusion_sink_scales_with_fibre_diameter():
    """The peak drop tracks delta = phi*d as the fibre diameter changes."""
    for d in (0.01, 0.02, 0.04):
        m = _model()
        delta = _law(m)(d)
        g = _grid(2, 7, d=d)
        top = range(7, 14)
        drop = _top_peak(_solve(g, d=d), top) - _top_peak(_solve(g, d=d, law=_law(m)), top)
        assert close(drop, delta, max(1e-5, 0.12 * delta)), \
            f"d={d}: drop {drop:.5f} != delta {delta:.5f}"


def test_fusion_independent_of_pore_size():
    """The per-junction sink is delta regardless of pore size -- it is a property of
    the CONTACT, not the span. (Tight to wide pitch; the peak always drops by delta.)"""
    law = _law()
    delta = law(_D)
    for pitch in (0.1, 0.2, 0.35):
        g = _grid(2, 7, pitch=pitch)
        top = range(7, 14)
        drop = _top_peak(_solve(g), top) - _top_peak(_solve(g, law=law), top)
        assert close(drop, delta, max(1e-5, 0.15 * delta)), \
            f"pitch={pitch}: drop {drop:.5f} != delta {delta:.5f}"


def test_oblique_crossings_also_compact():
    """Non-orthogonal (non-square) pores compact too: fusion keys on contact, not on a
    90 deg crossing. Bottom +x lines crossed by 45 deg top lines."""
    law = _law()
    delta = law(_D)
    npts = 61
    bottom = [np.column_stack([np.linspace(0, 3, npts), np.full(npts, y),
                               np.zeros(npts)]) for y in (0.6, 1.2, 1.8, 2.4)]
    top = []
    for c in (0.3, 0.9, 1.5):
        t = np.linspace(0, 3, npts)
        top.append(np.column_stack([t, t + c, np.full(npts, _D)]))   # 45 deg
    paths = bottom + top
    ti = range(len(bottom), len(paths))
    drop = _top_peak(_solve(paths), ti) - _top_peak(_solve(paths, law=law), ti)
    assert drop > 0.5 * delta, f"oblique crossings barely compacted (drop {drop:.5f})"


def test_bed_contacts_are_not_fused():
    """The bottom layer rests on the BED (nothing below to fuse into), so fusion must
    leave it byte-unchanged -- only fibre-on-fibre contacts sink."""
    g = _grid(2, 7)
    geo, fus = _solve(g), _solve(g, law=_law())
    for i in range(7):                               # bottom layer = first 7 fibres
        assert np.array_equal(geo[i], fus[i]), \
            f"bottom-layer fibre {i} changed -- a bed contact was fused"


def test_peaks_monotonic_in_overlap():
    """More overlap -> lower stacked peaks, monotonically (and phi=0 is the tallest,
    the geometric stack)."""
    g = _grid(4, 7)
    peaks = []
    for phi in (0.0, 0.2, 0.4, 0.6):
        out = _solve(g, law=_law(_model(fusion_overlap=phi)))
        peaks.append(max(p[:, 2].max() for p in out))
    assert peaks[0] > peaks[1] > peaks[2] > peaks[3], f"not monotone: {peaks}"


def test_self_crossing_contact_also_fuses():
    """A fibre crossing its OWN earlier strand (a serpentine turnaround) fuses too:
    the later strand sinks delta into the earlier one, exactly like an inter-layer
    crossover. REGRESSION: fusing only the inter-layer path left scaffold turnarounds
    a full diameter up while the grid interior compacted (raised edges). A single
    self-crossing 'X' path -- the later diagonal rests on the earlier at the centre."""
    law = _law()
    delta = law(_D)
    z = 0.05
    corners = np.array([[0, 0, z], [2, 2, z], [2, 0, z], [0, 2, z]], float)
    seg = [np.linspace(a, b, 120, endpoint=False)
           for a, b in zip(corners[:-1], corners[1:])]
    path = np.concatenate(seg + [corners[-1:]])
    geo = _solve([path])[0]
    fus = _solve([path], law=law)[0]
    assert geo[:, 2].max() > z + 0.4 * _D, "no self-stack formed (crossing not found)"
    drop = geo[:, 2].max() - fus[:, 2].max()
    assert close(drop, delta, max(1e-5, 0.25 * delta)), \
        f"self-crossing drop {drop:.5f} != delta {delta:.5f}"


def test_every_other_tower_fuses_onto_its_lower_strand():
    """REGRESSION (loop/turnaround tip stacked too tall): a self-crossing whose
    LOWER strand rests on a fibre below must fuse at BOTH junctions -- the inter-layer
    rest under the lower strand AND the self-cross on top of it -- not just the self-
    cross. The bug: the self-crossing read the RAW (full-diameter) all_lower height, so
    the rest UNDER a turnaround/loop tip never compacted; the tower fused at half the
    grid's rate and the tip peak stuck up. Here a single self-crossing 'X' sits one
    diameter above a flat support fibre: geometric top = 2d over the support, so a
    fully-fused tip must drop by 2*delta (both junctions), not 1*delta (self-cross only)."""
    law = _law()
    delta = law(_D)
    npts = 121
    # a flat lower-layer support fibre on the bed, spanning the crossing region
    support = np.column_stack([np.linspace(-1.0, 3.0, npts),
                               np.full(npts, 1.0), np.zeros(npts)])
    # an 'X' one layer up, centred over the support so its LOWER strand rests on it
    z = _D
    corners = np.array([[0, 0, z], [2, 2, z], [2, 0, z], [0, 2, z]], float)
    seg = [np.linspace(a, b, npts, endpoint=False)
           for a, b in zip(corners[:-1], corners[1:])]
    xpath = np.concatenate(seg + [corners[-1:]])
    paths = [support, xpath]
    geo = _solve(paths)
    fus = _solve(paths, law=law)
    # the X self-crosses at (1,1) -- directly over the support -> a 2-diameter stack
    # above the bed (lower strand rests on the support at +d, self-cross adds +d)
    gtop = geo[1][:, 2].max()
    assert gtop > 1.8 * _D, "no 2-diameter tip stack formed (geometry changed)"
    drop = gtop - fus[1][:, 2].max()
    assert close(drop, 2.0 * delta, 0.2 * delta), \
        f"tip dropped {drop:.5f} (~{drop/delta:.2f}*delta); expected 2*delta " \
        f"(inter-layer rest UNDER the self-cross did not fuse -> tip sticks up)"


def test_nothing_sinks_below_bed_even_at_huge_overlap():
    """An extreme overlap never drives any fibre below the bed plane (the max(bed,..)
    clamp holds in the full solve, not just the unit helper)."""
    g = _grid(3, 6)
    base = min(p[:, 2].min() for p in _solve(g))
    fus = _solve(g, law=_law(_model(fusion_overlap=0.9)))
    assert min(p[:, 2].min() for p in fus) >= base - 1e-9


# --------------------------------------------------------------------------- #
# THE WALL / JUNCTION SPLIT
# A contact is not one thing: fibres CROSSING touch at a point, fibres stacked
# COLLINEARLY touch along a line (a wall, or a fibre sagging into a valley onto the
# parallel layer below), and a rig may calibrate the two differently. These pin the
# claims the split makes -- above all SELECTIVITY (the classifier must not leak) and
# the byte-identical default.
# --------------------------------------------------------------------------- #
def test_wall_overlap_unset_means_inherit():
    """The default (None) -> wall contacts reuse the junction phi, and NO band is
    published -- which is what keeps the solver on its scalar path."""
    m = _model()
    assert m.calibration.fusion_wall_overlap is None
    assert m.wall_sink(_D) == m.junction_sink(_D)
    assert m.fusion_wall_band() is None


def test_wall_sink_uses_wall_overlap_when_set():
    """Set -> wall_sink is driven by phi_wall, junction_sink still by phi_junction,
    and both run the SAME hook chain (a shared clamp)."""
    m = _model(fusion_overlap=0.35, fusion_wall_overlap=0.10)
    assert approx(m.junction_sink(_D), 0.35 * _D)
    assert approx(m.wall_sink(_D), 0.10 * _D)
    assert m.fusion_wall_band() is not None
    clamped = _model(fusion_overlap=0.1, fusion_wall_overlap=5.0,
                     fusion_max_overlap=0.9)
    assert approx(clamped.wall_sink(_D), 0.9 * _D)      # same clamp as junction


def test_fusion_law_wall_flag_selects_the_magnitude():
    """The installed closure answers both magnitudes; the default (no flag) is the
    JUNCTION one, so an old one-arg call site keeps its old meaning."""
    law, _band = _split(0.35, 0.10)
    assert approx(law(_D), 0.35 * _D)
    assert approx(law(_D, False), 0.35 * _D)
    assert approx(law(_D, True), 0.10 * _D)


def test_wall_weight_blends_and_is_continuous():
    """1 at collinear, 0 at a square crossing, smoothstep between -- and pure
    JUNCTION by 36 deg, the smallest crossing any standard pore shape makes (90/60/
    45/36), so no real scaffold's junctions get blended toward wall."""
    band = _model(fusion_overlap=0.3, fusion_wall_overlap=0.1).fusion_wall_band()
    assert gtf._wall_weight(0.0, band) == 1.0                 # collinear -> wall
    assert gtf._wall_weight(1.0, band) == 0.0                 # square -> junction
    assert gtf._wall_weight(math.sin(math.radians(36)), band) == 0.0
    s = np.linspace(0.0, 1.0, 501)
    w = gtf._wall_weight(s, band)
    assert np.all((w >= 0.0) & (w <= 1.0))
    assert np.all(np.diff(w) <= 1e-12), "weight must be monotone in the angle"
    # continuity: no step anywhere (a hard threshold would jump 1.0 in one sample)
    assert float(np.abs(np.diff(w)).max()) < 0.02


def test_split_off_is_byte_identical_two_ways():
    """The split is inert unless it says something: wall UNSET, and wall SET EQUAL to
    the junction phi, must both reproduce the unsplit solve byte-for-byte. The second
    is the real guard -- it proves the angular path is gated on the MAGNITUDES
    differing, not merely on a band being installed."""
    g = _grid(3, 5)
    base = _solve(g, law=_law())
    unset = _solve_split(g, 0.35, None)
    equal = _solve_split(g, 0.35, 0.35)
    assert all(np.array_equal(x, y) for x, y in zip(base, unset))
    assert all(np.array_equal(x, y) for x, y in zip(base, equal))


def test_pure_wall_responds_only_to_wall_phi():
    """SELECTIVITY, half 1: a rotation=0 stack is every-contact-collinear, so its
    height is (N-1)*(d - phi_wall*d) EXACTLY and the junction phi -- at ANY value,
    including 0.9 -- must not move it by so much as a nanometre. A classifier that
    leaked would show up here immediately."""
    for phi_j in (0.0, 0.35, 0.9):
        h = _top(_solve_split(_wall_stack(), phi_j, 0.0))
        assert close(h, _SEAT + 7 * _D, 1e-9), \
            f"phi_j={phi_j} moved a pure wall (h={h:.6f}, want {_SEAT+7*_D:.6f})"
    for phi_w in (0.0, 0.35, 0.7):
        h = _top(_solve_split(_wall_stack(), 0.35, phi_w))
        assert close(h, _SEAT + 7 * (_D - phi_w * _D), 1e-9), \
            f"wall height at phi_w={phi_w} is {h:.6f}, not the predicted stack"


def test_pure_junction_tower_responds_only_to_junction_phi():
    """SELECTIVITY, half 2: a 0/90 tower crosses at a point and runs parallel to
    nothing, so phi_wall -- at ANY value, including 0.9 -- must not move it."""
    for phi_w in (0.0, 0.35, 0.9):
        h = _top(_solve_split(_junction_tower(), 0.35, phi_w))
        assert close(h, _SEAT + 7 * (_D - 0.35 * _D), 1e-9), \
            f"phi_w={phi_w} moved a pure junction tower (h={h:.6f})"


def test_fit_fusion_rows_inverts_the_engine():
    """The Calibration page's fusion fit, checked against the SOLVER rather than
    against its own formula: build a pure wall and a pure junction tower at known
    phis, read each stack's overall height off `apply_sag` (top centre + one
    radius, which is what a side-on measurement sees), and hand those heights to
    `mew_calibration.fit_fusion_rows` exactly as an operator would. It must give
    the phis back, each from its own feature.

    RED proof: fitting against N*d with x = N instead of N-1 misses by 0.1+ here."""
    import mew_calibration as mc
    phi_j, phi_w = 0.35, 0.2
    wall, junc = [], []
    for n in (3, 5, 8):
        wall.append((n, _top(_solve_split(_wall_stack(n), phi_j, phi_w)) + _D / 2))
        junc.append((n, _top(_solve_split(_junction_tower(n), phi_j, phi_w))
                     + _D / 2))
    assert close(mc.fit_fusion_rows(junc, _D).phi, phi_j, 1e-6), junc
    assert close(mc.fit_fusion_rows(wall, _D).phi, phi_w, 1e-6), wall


def test_wall_phi_monotone_and_never_below_bed():
    """More wall overlap -> a shorter wall, monotonically, and an extreme overlap
    never drives anything through the plate."""
    heights = [_top(_solve_split(_wall_stack(), 0.35, p))
               for p in (0.0, 0.2, 0.4, 0.6, 0.9)]
    assert all(a > b for a, b in zip(heights, heights[1:])), heights
    out = _solve_split(_wall_stack(), 0.35, 0.9)
    assert min(p[:, 2].min() for p in out) >= -1e-9


def test_candidate_is_fused_before_the_max_not_after():
    """The ordering claim, and the reason the split does not crack the geometry.

    A query with TWO supports at the same height -- one crossing (90 deg), one
    collinear -- must rest on the higher POST-fusion contact: the collinear one, at
    -phi_wall*d. Sinking the winner of a GEOMETRIC max instead would let the argmax
    pick either, and the floor would jump by (phi_j - phi_w)*d the moment it flipped
    (measured at 1.04d on a rot=45 scaffold before this was fixed).

    Built directly on the index so it pins the mechanism, not a downstream symptom:
    a +x segment and a +y segment at identical Z, queried by a +x-running fibre."""
    d, r = _D, _D / 2
    idx = gtf._SegIndex(gtf._hash_cell(d))
    idx.add_fiber(np.array([[0.0, 1.0, 0.0], [2.0, 1.0, 0.0]]), r)   # collinear
    idx.add_fiber(np.array([[1.0, 0.0, 0.0], [1.0, 2.0, 0.0]]), r)   # crossing
    phi_j, phi_w = 0.9, 0.05
    _law_, band = _split(phi_j, phi_w)
    tx = np.array([1.0])          # the query fibre runs +x, like the first segment
    ty = np.array([0.0])

    def pair_sink(q, sdx, sdy):
        L = np.hypot(sdx, sdy)
        ok = L > 0.0
        sin_t = np.where(ok, np.abs(tx[q] * sdy - ty[q] * sdx)
                         / np.where(ok, L, 1.0), 1.0)
        w = gtf._wall_weight(np.minimum(sin_t, 1.0), band)
        return phi_w * d * w + phi_j * d * (1.0 - w)

    h = idx.rest_heights(np.array([1.0]), np.array([1.0]), r, -1.0,
                         pair_sink=pair_sink)[0]
    assert close(h, d - phi_w * d, 1e-12), \
        (f"rested at {h:.6f}; expected the COLLINEAR contact's post-fusion height "
         f"{d - phi_w * d:.6f} (fusing after the max would give "
         f"{d - phi_j * d:.6f})")


def test_contact_angle_is_read_over_a_fibre_width():
    """A contact's melt is smeared over ~1 fibre width, so the crossing angle is read
    over a ~1-DIAMETER arc window. Read off a raw segment instead, a resampled loop
    corner swings the tangent (and with it the sink) the full wall->junction range in
    a sub-micron step and drops a cliff into a continuous centreline. Pin that the
    window actually smooths a sharp corner: a right-angle turn's windowed tangent is
    the 45 deg chord, not either leg."""
    corner = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]])
    cum, _t = gtf._arc_lengths(corner)
    at = np.array([1.0])                       # exactly the corner vertex
    raw_x, raw_y = gtf._tangent_at(corner, cum, at)
    assert close(raw_x[0], 0.0, 1e-12) and close(raw_y[0], 1.0, 1e-12)
    win_x, win_y = gtf._tangent_at(corner, cum, at, window=0.4)
    assert close(win_x[0], math.cos(math.radians(45)), 1e-9)
    assert close(win_y[0], math.sin(math.radians(45)), 1e-9)


def test_split_geometry_is_finite_and_above_bed_on_a_real_layer_stack():
    """Whole-solve sanity at wildly mismatched magnitudes: no NaN, nothing under the
    plate, on a stack that has crossings, parallel neighbours AND turnarounds."""
    g = _grid(4, 7)
    for phi_j, phi_w in [(0.35, 0.0), (0.0, 0.7), (0.9, 0.05)]:
        out = _solve_split(g, phi_j, phi_w)
        assert all(np.isfinite(p[:, 2]).all() for p in out), (phi_j, phi_w)
        assert min(p[:, 2].min() for p in out) >= -1e-9, (phi_j, phi_w)


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
