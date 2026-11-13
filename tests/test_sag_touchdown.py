"""Tests for TANGENT TOUCHDOWN (`gcode_to_fibers._tangent_touchdowns`): where a
span's droop reaches the bed, or a fibre lying along it, the landing is a pinned
support and the fibre arrives tangentially -- not at the rest clamp's corner.

The comparator is the droop law itself (paper section 2.3.4, eq. 5:
depth = c * span**p), not anything this file computes from the solver. Pinning a
support at the landing point T makes the stretch from the span's support A to T a
sub-span of its own, which the law hangs as a parabola of depth D(L), L = |T - A|.
Over a flat floor a drop h below A it stays out of the floor only while
D(L) <= h/4, and at D(L) = h/4 it meets the floor with zero slope. So:

  * THE LANDING IS WHERE THE LAW PUTS IT -- a fibre bridging two bed-level walls
    lands L = sqrt(h / 4c) from each (shipped span**2 law), and under a fitted
    exponent at D(L) = h/4, L = (h / 4c)**(1/p). Measured: the first sample
    resting on the bed, within one sample step, and through the law check
    below, exactly.
  * THE HANGING PART IS THE LAW'S PARABOLA -- every sample between the crest
    and T lies on chord - D(L) * 4t(1 - t), to 1e-5 mm (read at the
    touchdown stage, before the crest rounding). T is the landing the solve
    pinned: the relaxation pins on samples, so T is the law's tangent point to
    within one sample step (the check above), and the stretch that hangs to it
    is the law's own sub-span exactly.
  * NO CORNER -- the largest turn at any vertex resting on the bed falls as the
    sampling refines (a sampled tangent curve turns by ~ curvature x step). The
    clamp's corner stayed at 50-77 degrees on the default sag ladder at every
    resolution.
  * A SPAN THAT NEVER LANDS IS UNTOUCHED -- bit-for-bit, against the solve with
    the feature switched off; the touching geometry differs under the same A/B,
    so the comparison is not blind.
  * NOTHING SINKS -- no sample is below the bed rest height.
  * BOTH SOLVERS -- `sag_approx` lands at the same place.

RED proof: `python tests/test_sag_touchdown.py --red` runs every case with
`_TANGENT_TOUCHDOWN = False`, the old clamp corner. landing_span2,
landing_fitted_exponent, hanging_part_is_the_law, no_corner_on_the_ladder and
approx_lands_too all FAIL; untouched_span and nothing_sinks pass, as they must
(they hold for the clamp too).

Re-proved 2026-10-08, after `_tangent_touchdowns` became the converged
relaxation (`_relax_pins`): the same five FAIL under --red and pass without
it. The law checks now read T off the solve's own landing (a sample within
one step of the law's tangent point) instead of the analytic T, because the
relaxation pins on samples; under --red no stage sample rests on the bed, so
those two checks fail on T itself.

Run:  python tests/test_sag_touchdown.py
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math
import os
import sys
import tempfile

import numpy as np

import gcode_to_fibers as gtf

_D = 0.02           # fibre diameter, mm
_RES = 4.0          # sag resolution for the synthetic cases
_RED = "--red" in sys.argv
# How closely the hanging part must follow the law, mm (5e-4 d). The landing's
# fit test sees a sub-span past tangency at a probe 1e-4 of it short of T
# (`_TOUCHDOWN_PROBE`), which bounds the overshoot: measured 1.0e-6 mm under the
# p = 1.5 law. Without the probe it was a whole sample step, 1.2e-4 mm.
_LAW_TOL = 1e-5


def _c(mult=1.0):
    """The shipped law's coefficient: depth = c * span**2 (SAG_DIAM_EXP = 1)."""
    return gtf.SAG_COEFF * mult / _D ** gtf.SAG_DIAM_EXP


def _bridge(mult=1.0, res=_RES, engine="exact", stage=None):
    """Two walls lying on the bed along y at x = 0 and x = 2, and one fibre one
    layer up along y = 0 from x = -1 to x = 3, crossing both. Returns the
    bridging fibre's solved polyline, sorted by x, and the bed rest height.

    `stage` (a list): also record the bridging fibre's free droop as
    `_tangent_touchdowns` returns it -- (arcs, z) -- BEFORE the crest rounding
    and the rest clamp. That is where the law's sub-span lives undisturbed: the
    crest rounding afterwards lifts closely spaced samples on any slope (its
    dilation reaches about R * slope along it), which is its own business."""
    orig = gtf._tangent_touchdowns
    if stage is not None:
        def spy(arcs, *a, **k):
            r = orig(arcs, *a, **k)
            stage[:] = [np.array(arcs, dtype=np.float64), np.array(r)]
            return r
        gtf._tangent_touchdowns = spy
    n = 41
    wall0 = np.column_stack([np.zeros(n), np.linspace(-1, 1, n), np.zeros(n)])
    wall1 = np.column_stack([np.full(n, 2.0), np.linspace(-1, 1, n),
                             np.zeros(n)])
    m = 81
    top = np.column_stack([np.linspace(-1, 3, m), np.zeros(m),
                           np.full(m, _D)])
    paths = [wall0, wall1, top]
    gtf.set_sag_resolution(res)
    try:
        if engine == "exact":
            out = gtf.apply_sag(paths, _D, sag_multiplier=mult)
        else:
            out, _ = gtf.apply_sag_approx(paths, _D, sag_multiplier=mult,
                                          point_times=[np.linspace(0, 1, len(p))
                                                       for p in paths])
    finally:
        gtf.set_sag_resolution(1.0)
        gtf._tangent_touchdowns = orig
    p = np.asarray(out[-1], dtype=np.float64)
    p = p[np.argsort(p[:, 0])]
    bed = float(np.asarray(out[0])[:, 2].min())    # a wall's centre = bed rest
    return p, bed


def _landings(p, bed):
    """Distance from the wall at x = 0 to the first bed sample on its right, and
    from the wall at x = 2 to the last bed sample on its left."""
    on = np.abs(p[:, 2] - bed) < 1e-9
    mid = on & (p[:, 0] > 0.0) & (p[:, 0] < 2.0)
    xs = p[mid, 0]
    return float(xs.min()), float(2.0 - xs.max())


def _step(res=_RES, mult=1.0):
    """The solve's span sampling step (`_solve_layer`'s `sag_step`)."""
    return gtf.SAG_STEP_D * _D / math.sqrt(max(1.0, mult)) / res


def _check_landing(p, bed, L_law, tag):
    l0, l1 = _landings(p, bed)
    tol = _step() + 1e-9
    ok = abs(l0 - L_law) <= tol and abs(l1 - L_law) <= tol
    print("%s %s: landings %.4f / %.4f mm from the walls, law %.4f (tol %.4f)"
          % ("PASS" if ok else "FAIL", tag, l0, l1, L_law, tol))
    return ok


def landing_span2():
    p, bed = _bridge()
    h = float(p[np.argmin(np.abs(p[:, 0])), 2]) - bed     # support above bed
    return _check_landing(p, bed, math.sqrt(h / (4.0 * _c())), "landing_span2")


def _stage_landing(stage, bed):
    """The landing the solve pinned, as arc from the wall at x = 0 (arc 1.0
    on the bridging fibre): the first sample past it resting on the bed."""
    arcs, z = stage
    s = arcs - 1.0
    on = (s > 0.0) & (np.abs(z - bed) < 1e-12)
    return float(s[on].min()) if on.any() else float("inf")


def _law_err(stage, hA, bed, L, depth):
    """Largest |z - law| over the samples strictly between the wall at x = 0
    (arc 1.0 on the bridging fibre) and the landing at L: the law's sub-span is
    chord(A -> T) - D(L) * 4t(1 - t)."""
    arcs, z = stage
    s = arcs - 1.0
    m = (s > 0.0) & (s < L - 1e-6)
    t = s[m] / L
    law = hA + (bed - hA) * t - depth * 4.0 * t * (1.0 - t)
    return int(m.sum()), (float(np.abs(z[m] - law).max()) if m.any()
                          else float("inf"))


def landing_fitted_exponent():
    """A law the user re-fitted to p = 1.5 (the paper invites it): the landing
    moves to D(L) = h/4 under THAT law, which no single curvature reproduces,
    and the hanging part is that law's parabola."""
    c, pw = 0.2, 1.5
    gtf.set_sag_law(lambda span, d, m=1.0: c * m * np.power(span, pw))
    stage = []
    try:
        p, bed = _bridge(stage=stage)
    finally:
        gtf.set_sag_law(None)
    hA = float(p[np.argmin(np.abs(p[:, 0])), 2])
    L = ((hA - bed) / (4.0 * c)) ** (1.0 / pw)
    ok = _check_landing(p, bed, L, "landing_fitted_exponent")
    T = _stage_landing(stage, bed)
    n, err = _law_err(stage, hA, bed, T, c * T ** pw)
    ok2 = n >= 4 and err < _LAW_TOL
    print("%s landing_fitted_exponent: %d samples on the p = 1.5 sub-span, "
          "max |z - law| %.2e mm" % ("PASS" if ok2 else "FAIL", n, err))
    return ok and ok2


def hanging_part_is_the_law():
    """Between the wall and the landing, the free droop is the law's own
    sub-span, chord(A -> T) - D(L) * 4t(1 - t), with T where D(L) = h/4."""
    stage = []
    p, bed = _bridge(stage=stage)
    hA = float(p[np.argmin(np.abs(p[:, 0])), 2])
    T = _stage_landing(stage, bed)
    n, err = _law_err(stage, hA, bed, T, _c() * T * T)
    ok = n >= 4 and err < _LAW_TOL
    print("%s hanging_part_is_the_law: %d samples, max |z - law| %.2e mm"
          % ("PASS" if ok else "FAIL", n, err))
    return ok


def untouched_span():
    """A droop too shallow to reach the bed (multiplier 0.005: 0.01 mm free on
    the 2 mm span, against a 0.02 mm support height) is bit-for-bit what the
    clamp alone gives. The same A/B on the landing geometry must DIFFER, or the
    switch is not reaching the solve."""
    def ab(mult):
        gtf._TANGENT_TOUCHDOWN = False
        try:
            off, _ = _bridge(mult=mult)
        finally:
            gtf._TANGENT_TOUCHDOWN = not _RED
        on, _ = _bridge(mult=mult)
        return off, on
    off, on = ab(0.005)
    same = off.shape == on.shape and np.array_equal(off, on)
    off1, on1 = ab(1.0)
    control = not (off1.shape == on1.shape and np.array_equal(off1, on1))
    ok = same and (control or _RED)
    print("%s untouched_span: shallow span identical %s; landing span differs %s"
          % ("PASS" if ok else "FAIL", same, control))
    return ok


def nothing_sinks():
    worst = 0.0
    for mult in (0.3, 1.0, 3.0):
        p, bed = _bridge(mult=mult)
        worst = min(worst, float(p[:, 2].min()) - bed)
    ok = worst > -1e-12
    print("%s nothing_sinks: lowest sample %.2e mm relative to the bed rest"
          % ("PASS" if ok else "FAIL", worst))
    return ok


def approx_lands_too():
    p, bed = _bridge(engine="approx")
    h = float(p[np.argmin(np.abs(p[:, 0])), 2]) - bed
    return _check_landing(p, bed, math.sqrt(h / (4.0 * _c())),
                          "approx_lands_too")


def _ladder_max_turn(res):
    """Largest turn (deg) at any vertex of the two bridging fibres that rests on
    the bed, default sag ladder at sag resolution `res`."""
    import mew_sweeps
    text = mew_sweeps.generate_sag(mew_sweeps.SagParams()).gcode_texts[0]
    fd, gc = tempfile.mkstemp(suffix=".gcode")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    try:
        paths, ptimes, travels = gtf.parse_gcode(gc, with_times=True,
                                                 with_travels=True)
    finally:
        os.remove(gc)
    paths, ptimes = gtf.merge_travels(paths, ptimes, travels)
    gtf.set_sag_resolution(res)
    try:
        sag, _ = gtf.apply_sag([np.array(p, float) for p in paths], 0.015,
                               point_times=[np.array(t, float) for t in ptimes],
                               layer_by_deposition=True)
    finally:
        gtf.set_sag_resolution(1.0)
    zmin = min(float(np.asarray(q)[:, 2].min()) for q in sag if len(q))
    worst = 0.0
    for q in sag[-2:]:
        q = np.asarray(q, dtype=np.float64)
        d = np.diff(q, axis=0)
        h = np.hypot(d[:, 0], d[:, 1])
        ang = np.degrees(np.arctan2(d[:, 2], np.maximum(h, 1e-12)))
        turn = np.abs(ang[1:] - ang[:-1])
        ok = (h[1:] > 1e-6) & (h[:-1] > 1e-6) & (q[1:-1, 2] < zmin + 0.002)
        if ok.any():
            worst = max(worst, float(turn[ok].max()))
    return worst


def no_corner_on_the_ladder():
    """The reported geometry. The clamp's corner measured 69 / 78 degrees at 1x
    / 3x (and ~75 at 10x: it does not converge). A tangent landing turns by
    ~ curvature x step per sample: 13.8 degrees at 3x, the worst on the long
    lead-in span, whose sample count is capped (SAG_MAX_SPAN_SAMPLES) and so
    whose chords are the coarsest. It falls with the factor: 17.9 / 9.7 / 5.8
    at 3x / 6x / 10x, measured before the near-T probe tightened the landing."""
    t3 = _ladder_max_turn(3.0)
    ok = t3 < 25.0
    print("%s no_corner_on_the_ladder: max on-bed turn %.1f deg at 3x (< 25)"
          % ("PASS" if ok else "FAIL", t3))
    return ok


if __name__ == "__main__":
    if _RED:
        gtf._TANGENT_TOUCHDOWN = False
        print("RED run: tangent touchdown switched OFF")
    cases = [landing_span2, landing_fitted_exponent, hanging_part_is_the_law,
             untouched_span, nothing_sinks, approx_lands_too,
             no_corner_on_the_ladder]
    failed = [c.__name__ for c in cases if not c()]
    print("\n%d/%d passed" % (len(cases) - len(failed), len(cases)))
    if failed:
        print("FAILED:", ", ".join(failed))
        sys.exit(1)
