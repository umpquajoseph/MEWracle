"""Slice 2 of `20260911-132550-arbitrary-collector-surfaces` (`docs/specs/` in
the ops repo): `_SegIndex` stops binning a doubly-curved collector's contacts
by the `(u, v)` chart and bins by the point's own WORLD footprint instead
(`gcode_to_fibers._footprint_frame`), and `_SegIndex.rest_heights` measures the
exact WORLD distance to each candidate rather than a chart offset
(`_world_aug_array`).

THE AUTHORITY is an exhaustive world-XYZ segment-against-segment search,
`_exhaustive_rest_height` below -- no spatial hash, no chart, every candidate
segment tested against every query point. It is the SAME closed-form
sphere-vs-line quadratic `_SegIndex.rest_heights`' WORLD-EXACT branch solves,
just without the grid, so a design small enough to run it in a Python loop
(every case here) checks the hashed answer against a comparator that does not
share its machinery.

THE RED CONTROL (`test_the_seam_is_missed_without_a_frame`) mirrors
`test_mandrel_seam.py`'s pattern for a cylinder: a design wrapping most of the
LOCAL period at its own `u` is found across the seam by the frame-enabled
index and missed by the historical raw-chart one, which is the failure
`RevolutionFrame.period_v` being `None` off a cylinder (no scalar to ghost by)
leaves unfixed without this slice.
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math
import sys
import time

import numpy as np

import gcode_to_fibers as gf
import mew_frame
import scaffold_gcode as sg

_PASS, _FAIL = [], []


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       ("   " + detail) if detail else ""))


DIAM = 0.02
CELL = gf._hash_cell(DIAM)
DOME = sg.surface_frame("dome", radius=10.0, span=(0.0, 8.0))


def _fiber_world(u0, u1, v0, v1, h, n=200):
    """A straight-in-chart fibre from (u0, v0, h) to (u1, v1, h), as the
    (u, v, h) polyline `_SagState`/`_SegIndex` are handed today."""
    u = np.linspace(u0, u1, n)
    v = np.linspace(v0, v1, n)
    return np.column_stack([u, v, np.full(n, h)])


def _exhaustive_rest_height(frame, query_uvh, candidates, q_radius, seg_radius,
                            floor=0.0, subdivide=1):
    """THE AUTHORITY: for one query point `(u, v, h)`, the highest height a
    fibre of `q_radius` would rest at over `candidates` (a list of (u, v, h)
    polylines), by testing the query's WORLD ray against every segment of
    every candidate polyline in a plain Python loop -- no hash, no chart.

    The touching condition is the sphere-vs-line quadratic (`to_world` is
    affine in `h`, so the query is a straight ray `P0 + t*N`), evaluated by
    hand so it shares no machinery with the code under test.

    `subdivide` IS PART OF THE AUTHORITY, not a knob. A fibre is a curve ON the
    collector, and a polyline's straight WORLD segment is a chord that cuts
    inside it -- so at `subdivide = 1` this function answers for the chord, not
    for the fibre, and it answers LOW by the chord's sagitta. Refining the
    candidate in the chart (where the fibre IS straight between its vertices)
    and re-mapping makes the answer converge on the real one; every call below
    passes a `subdivide` high enough that the last doubling moves it by less
    than the band, and `test_the_exhaustive_search_needs_its_refinement` is the
    control that the refinement is doing something.
    """
    uq, vq, _hq = query_uvh
    p0 = np.array(frame.to_world(uq, vq, 0.0), dtype=np.float64)
    p1 = np.array(frame.to_world(uq, vq, 1.0), dtype=np.float64)
    n = p1 - p0
    best = float(floor)
    s = q_radius + seg_radius
    for poly in candidates:
        poly = np.asarray(poly, dtype=np.float64)
        if subdivide > 1:
            # linear refinement in the CHART, spelled out here so the authority
            # still borrows nothing from `mew_frame`
            fine = [poly[0]]
            for i in range(len(poly) - 1):
                for j in range(1, subdivide + 1):
                    fine.append(poly[i] + (poly[i + 1] - poly[i])
                                * (float(j) / subdivide))
            poly = np.asarray(fine, dtype=np.float64)
        w = np.column_stack(frame.to_world(poly[:, 0], poly[:, 1], poly[:, 2]))
        for i in range(len(w) - 1):
            a = w[i]
            d = w[i + 1] - a
            aq = a - p0
            aq_par = float(np.dot(aq, n))
            d_par = float(np.dot(d, n))
            aq_perp = aq - aq_par * n
            d_perp = d - d_par * n
            L2 = float(np.dot(d_perp, d_perp))
            t = 0.0 if L2 < 1e-18 else -float(np.dot(aq_perp, d_perp)) / L2
            t = min(1.0, max(0.0, t))
            perp2 = float(np.dot(aq_perp + t * d_perp, aq_perp + t * d_perp))
            if perp2 < s * s:
                par = aq_par + t * d_par
                h = par + math.sqrt(max(0.0, s * s - perp2))
                if h > best:
                    best = h
    return best


# --------------------------------------------------------------------------- #
# off-axis regression: the frame-enabled index against the exhaustive search
# --------------------------------------------------------------------------- #
def test_off_axis_matches_the_exhaustive_search():
    """Two crossing fibres 6 mm off-axis (well outside any pole disc): the
    hashed WORLD-EXACT rest height must agree with a brute-force search over
    the same geometry, to well inside one micron."""
    lower = _fiber_world(2.5, 3.5, -0.3, 0.3, 0.0)
    idx = gf._SegIndex(CELL, frame=DOME)
    idx.add_fiber(lower, seg_radius=0.5 * DIAM)
    q = np.array([3.0]), np.array([0.0])
    z = idx.rest_heights(q[0], q[1], 0.5 * DIAM, 0.0)
    want = _exhaustive_rest_height(DOME, (3.0, 0.0, 0.0), [lower],
                                   0.5 * DIAM, 0.5 * DIAM, subdivide=64)
    check("6mm-off-axis crossing matches the exhaustive world search",
          abs(float(z[0]) - want) < 1e-6,
          "hashed %.8f vs exhaustive %.8f" % (float(z[0]), want))
    check("...and it is a real contact, not the floor",
          float(z[0]) > 0.5 * DIAM * 0.9, "%.6f" % float(z[0]))


# --------------------------------------------------------------------------- #
# the apex: returns contacts at all
# --------------------------------------------------------------------------- #
def test_apex_centred_design_returns_contacts():
    """Two spokes crossing near the very apex (u down to 0.02 mm, inside the
    ~0.16 mm pole-exclusion band `mew_frame.POLE_BAND` refuses a raw chart at)
    at different azimuths. The frame-enabled index must find the contact and
    agree with the exhaustive world search -- a design centred on the apex
    'returns contacts at all' is the spec's own bar."""
    spoke_a = _fiber_world(0.02, 1.0, 0.0, 0.0, 0.0)
    spoke_b = _fiber_world(0.02, 1.0, 0.0, 0.6, 0.0)  # a different azimuth
    idx = gf._SegIndex(CELL, frame=DOME)
    idx.add_fiber(spoke_a, seg_radius=0.5 * DIAM)
    qu, qv = 0.05, 0.3
    z = idx.rest_heights(np.array([qu]), np.array([qv]), 0.5 * DIAM, 0.0)
    want = _exhaustive_rest_height(DOME, (qu, qv, 0.0), [spoke_a],
                                   0.5 * DIAM, 0.5 * DIAM, subdivide=64)
    check("a query near the apex agrees with the exhaustive world search",
          abs(float(z[0]) - want) < 1e-6,
          "hashed %.8f vs exhaustive %.8f" % (float(z[0]), want))
    idx2 = gf._SegIndex(CELL, frame=DOME)
    idx2.add_fiber(spoke_b, seg_radius=0.5 * DIAM)
    z2 = idx2.rest_heights(np.array([0.05]), np.array([0.0]), 0.5 * DIAM, 0.0)
    check("a design centred on the apex returns a contact at all",
          float(z2[0]) > 1e-6, "rest height %.6f mm near u=0" % float(z2[0]))


def test_apex_query_does_not_raise():
    """`to_world`/`rest_heights` must not raise evaluating exactly AT u = 0,
    v != 0 -- the umbilic limit `RevolutionFrame.metric` documents, exercised
    here through the index rather than through the frame directly."""
    fibre = _fiber_world(0.0, 1.0, 0.3, 0.3, 0.0)
    idx = gf._SegIndex(CELL, frame=DOME)
    idx.add_fiber(fibre, seg_radius=0.5 * DIAM)
    try:
        z = idx.rest_heights(np.array([0.0]), np.array([0.3]), 0.5 * DIAM, 0.0)
        ok = np.isfinite(z).all()
    except Exception as exc:                                    # noqa: BLE001
        ok = False
        z = exc
    check("a query exactly at the apex does not raise", ok, repr(z))


# --------------------------------------------------------------------------- #
# the seam: the RED control, mirroring test_mandrel_seam.py
# --------------------------------------------------------------------------- #
def test_the_seam_is_missed_without_a_frame():
    """RED CONTROL. A fibre wrapping to within a hair of a full LOCAL
    revolution at its own `u` (`RevolutionFrame.period_v_at`, which varies
    with `u` -- `period_v` itself is None off a cylinder, so there is no
    single period for the historical index to ghost by even if it tried).

    The historical raw-chart index (`frame=None`, exactly what
    `_SagState` built before this slice) must MISS the wrap-around contact --
    proving the defect existed -- and the frame-enabled index must find it,
    proving the fix. Both run on the IDENTICAL geometry."""
    u_mid = 3.0
    per = abs(DOME.period_v_at(u_mid))
    eps = 0.001
    fibre = _fiber_world(u_mid - 0.05, u_mid + 0.05, per - eps, per - eps, 0.0)
    q_v = eps                      # a hair past the wrap, the seam's far side

    raw = gf._SegIndex(CELL)       # the pre-slice-2 construction: no frame
    raw.add_fiber(fibre, seg_radius=0.5 * DIAM)
    z_raw = raw.rest_heights(np.array([u_mid]), np.array([q_v]),
                             0.5 * DIAM, 0.0)
    check("RED: the raw chart index misses the seam neighbour",
          float(z_raw[0]) == 0.0,
          "rest height %.6f (wanted 0.0 -- the pre-fix defect)"
          % float(z_raw[0]))

    curved = gf._SegIndex(CELL, frame=DOME)
    curved.add_fiber(fibre, seg_radius=0.5 * DIAM)
    z_fixed = curved.rest_heights(np.array([u_mid]), np.array([q_v]),
                                  0.5 * DIAM, 0.0)
    check("GREEN: the frame-enabled index finds it across the seam",
          float(z_fixed[0]) > 0.5 * DIAM * 0.5,
          "rest height %.6f mm" % float(z_fixed[0]))

    want = _exhaustive_rest_height(DOME, (u_mid, q_v, 0.0), [fibre],
                                   0.5 * DIAM, 0.5 * DIAM, subdivide=64)
    check("...and it agrees with the exhaustive world search",
          abs(float(z_fixed[0]) - want) < 1e-6,
          "hashed %.8f vs exhaustive %.8f" % (float(z_fixed[0]), want))


def test_a_point_far_around_is_still_far():
    """THE CONTROL for the seam fix itself: a point genuinely on the far side
    of the collector at this `u` (half the local period away) must NOT read as
    a contact -- the fix must join the seam, not collapse the parallel."""
    u_mid = 3.0
    per = abs(DOME.period_v_at(u_mid))
    fibre = _fiber_world(u_mid - 0.05, u_mid + 0.05, 0.0, 0.0, 0.0)
    idx = gf._SegIndex(CELL, frame=DOME)
    idx.add_fiber(fibre, seg_radius=0.5 * DIAM)
    z = idx.rest_heights(np.array([u_mid]), np.array([0.5 * per]),
                         0.5 * DIAM, 0.0)
    check("half a local period away is genuinely far, not the seam",
          float(z[0]) == 0.0, "rest height %.6f (wanted 0.0)" % float(z[0]))


# --------------------------------------------------------------------------- #
# the read-back / file -> fibres -> STL chain completes for an apex design
# --------------------------------------------------------------------------- #
def test_apply_sag_completes_for_an_apex_centred_design():
    """`apply_sag` -- the batch driver `_SagState`'s callers use -- must
    complete, with no exception, on a two-layer design that passes through the
    near-apex band, and produce finite output. This is the 'whole file ->
    fibres -> STL chain completes' bar in the spec's Slice 2 read literally at
    the solver level (the file/STL plumbing is `main`'s, untouched here)."""
    lower = _fiber_world(0.02, 1.5, 0.0, 0.0, 0.0, n=80)
    upper = _fiber_world(0.02, 1.5, 0.0, 0.4, DIAM, n=80)
    try:
        # point_times=None -> apply_sag returns the plain polyline LIST, not
        # a (polylines, times) tuple (see its docstring's `point_times`).
        out = gf.apply_sag([lower, upper], DIAM, frame=DOME,
                           self_crossings=False)
        ok = (len(out) == 2 and all(np.isfinite(p).all() for p in out))
    except Exception as exc:                                     # noqa: BLE001
        ok = False
        out = exc
    check("apply_sag completes for an apex-centred two-layer design", ok,
          repr(out) if not ok else "")
    if ok:
        check("...and the upper layer picked up SOME support above its floor",
              float(np.max(out[1][:, 2])) > DIAM * 0.5,
              "max upper-layer h = %.6f" % float(np.max(out[1][:, 2])))


def test_self_pairs_across_the_seam_are_folded():
    """`_self_pairs_xy` (the same-fibre self-crossing candidate finder) must
    re-branch a candidate found across the un-ghosted seam onto the query's
    own local `v` (`_fold_seam_branch`) before the exact 2D test runs -- else
    a genuine self-crossing near a full local revolution reads as two segments
    a whole parallel apart instead of a hair apart.

    ONE smooth, monotone-in-`v` polyline, mirroring a real helical pass a
    revolution wide (`_self_pairs_xy`'s own docstring) -- not two legs joined
    by an artificial jump, which would itself be an un-physical segment
    spanning the whole period and prove nothing about the fold."""
    u_mid = 3.0
    per = abs(DOME.period_v_at(u_mid))
    v_end = per + 6.0 * DIAM
    base = _fiber_world(u_mid, u_mid, 0.0, v_end, 0.0, n=400)
    got = gf._self_pairs_xy(base, CELL, frame=DOME)
    check("a same-fibre candidate across the seam is found",
          got is not None, "no candidate pairs at all")
    if got is not None:
        q, s, qseg, sseg, _qt, _st = got
        # identify wrap-straddling pairs by ORIGINAL VERTEX INDEX (one end of
        # the pair near the polyline's start, the other near its end) rather
        # than by post-fold `v`: a correct fold moves the candidate's `v` OFF
        # 0 and onto the query's own branch, which is the point of it, so a
        # v-based "still near 0" filter would fail on exactly the pairs the
        # fix repairs.
        near0 = np.minimum(q, s) < 5
        nearN = np.maximum(q, s) > (len(base) - 7)
        wrap = near0 & nearN
        check("...and at least one candidate pair straddles the wrap",
              bool(wrap.any()), "%d of %d pairs" % (int(wrap.sum()), len(q)))
        if wrap.any():
            dv = np.abs(sseg[wrap, 1] - qseg[wrap, 1])
            check("...folded onto the query's local branch (small dv, not ~period)",
                  bool(np.all(dv < 0.5 * per)),
                  "max |dv| = %.6f, half-period = %.6f"
                  % (float(dv.max()), 0.5 * per))


def test_self_contact_does_not_widen_k_by_the_chart_overstatement():
    """RED/GREEN, the fix of 2026-09-12: a design that
    self-crosses close to the pole used to blow `_self_contact_pairs`'s
    candidate-search radius `kq` up by the RAW CHART's overstatement even
    though candidates there are already found by WORLD footprint -- and that
    overstatement is UNBOUNDED approaching the pole, not merely large. Measured
    on the merged `collector-surfaces`: k reached ~5462 and a 9.49 TiB
    allocation (~1241 and 114 GiB on the geometry reproduced here).

    An X-shaped self-crossing entirely inside the ~0.16 mm pole-exclusion band
    -- two segments of one fibre that cross in the chart at u down to 0.1 um,
    well within where a raw `(u, v)` distance overstates a world one without
    limit (`_footprint_frame`'s docstring)."""
    u = np.array([0.0001, 0.6, 0.6, 0.0001])
    v = np.array([-0.3, 0.3, -0.3, 0.3])
    base = np.column_stack([u, v, np.zeros(4)])
    local_of = gf._local_chart(DOME)
    local = local_of(0.0)

    # RED: the historical formula (`_self_contact_pairs` before this fix),
    # reconstructed by hand off the SAME overstatement this design actually
    # produces -- not re-run, since the buggy branch no longer exists to call.
    abc = tuple(np.broadcast_to(
        np.atleast_1d(np.asarray(m, dtype=np.float64)), base[:, 0].shape)
        for m in local(base[:, 0], base[:, 1]))
    hist_inflate = 1.0 + float(np.max(mew_frame.overstatement_from_metric(*abc)))
    hist_kq = max(1, int(math.ceil(DIAM * hist_inflate / CELL + 0.25)))
    check("RED: the historical chart-overstatement formula sizes k in the "
          "thousands on this design",
          hist_kq > 100, "historical kq would have been %d" % hist_kq)

    arcs, _total = gf._arc_lengths(base, local=local_of)
    t0 = time.time()
    got = gf._self_contact_pairs(arcs, base[:, 0], base[:, 1], DIAM, CELL,
                                 local=local, frame=DOME)
    dt = time.time() - t0
    check("GREEN: _self_contact_pairs completes in bounded time",
          got is not None and dt < 5.0,
          "None" if got is None else "%.3fs" % dt)
    if got is None:
        return
    q, k, t, lift, _sink = got
    check("...and it finds the X's self-crossing",
          len(q) > 0, "%d pair(s)" % len(q))
    if len(q):
        check("...with a lift close to one diameter (near-zero planar offset "
              "at the crossing)",
              bool(np.max(lift) > 0.9 * DIAM),
              "max lift %.6f mm vs diameter %.6f mm"
              % (float(np.max(lift)), DIAM))


# --------------------------------------------------------------------------- #
# the authority's own control: its refinement swings, and then it converges
# --------------------------------------------------------------------------- #
def test_the_exhaustive_search_needs_its_refinement():
    """BOTH CONTROLS for `_exhaustive_rest_height`'s `subdivide`, AT THE STEP
    THE GENERATOR ACTUALLY WRITES.

    A fibre is a curve on the collector and a polyline's straight WORLD segment
    is a chord inside it, so an unrefined brute-force search answers LOW by the
    chord's sagitta -- a property of the SAMPLE STEP, not of the geometry. The
    fixture here is sampled at 0.5 mm of chart, which is
    `--surface-sample-step`'s shipped default; `_fiber_world`'s own default
    n = 200 is ~5 um a step, 100x finer than any file, which is why this term
    was invisible to every dome suite while the shipped design lost 5 um of it.

    The controls: the authority answers DIFFERENTLY at `subdivide = 1` (so the
    refinement is not inert), stops moving once refined (so the number is the
    curve's, not the discretisation's), and the hashed index -- which evaluates
    its candidate ON the collector at the closest-approach parameter rather
    than on the chord -- lands on the converged one."""
    lower = _fiber_world(2.5, 3.5, -0.3, 0.3, 0.0, n=3)      # 0.5 mm steps
    # HALFWAY ALONG A SEGMENT, where the chord is furthest inside the
    # collector. A vertex is where the chord and the fibre coincide, so a query
    # placed on one cannot see this term at all.
    qu, qv = 3.25, 0.15
    idx = gf._SegIndex(CELL, frame=DOME)
    idx.add_fiber(lower, seg_radius=0.5 * DIAM)
    z = float(idx.rest_heights(np.array([qu]), np.array([qv]),
                               0.5 * DIAM, 0.0)[0])
    ex = lambda k: _exhaustive_rest_height(DOME, (qu, qv, 0.0), [lower],
                                           0.5 * DIAM, 0.5 * DIAM, subdivide=k)
    coarse, fine, finer = ex(1), ex(256), ex(512)
    check("RED: the unrefined exhaustive search answers LOW at the "
          "generator's own 0.5 mm sample step",
          (fine - coarse) > 1e-4,
          "coarse %.8f vs refined %.8f -- %.4f um low"
          % (coarse, fine, (fine - coarse) * 1e3))
    check("GREEN: the refinement has converged (256 -> 512 moves under the "
          "band)",
          abs(finer - fine) < 1e-6,
          "256 %.8f vs 512 %.8f -- %.5f um" % (fine, finer,
                                               abs(finer - fine) * 1e3))
    # 0.2 um, and the number is NAMED rather than fitted: what is left after
    # the candidate is evaluated on the collector is the closest-approach
    # parameter `t` itself, which is still optimised against the chord. It is
    # second order and it shrinks with the segment, so the solver never sees
    # this much -- `_SagState._commit_layer` densifies `all_lower`, and the
    # end-to-end residual on the shipped 6 mm design is 0.004 um.
    # Here the fibre is left
    # at the generator's raw 0.5 mm step deliberately, so this is the worst the
    # index alone can be.
    check("...and the hashed index answers the CONVERGED number, not the "
          "chord's",
          abs(z - fine) < 2e-4 and abs(z - coarse) > 20.0 * abs(z - fine),
          "hashed %.8f: %.5f um from refined, %.5f um from coarse"
          % (z, abs(z - fine) * 1e3, abs(z - coarse) * 1e3))


def test_the_footprint_flattener_splits_where_the_surface_bends():
    """`_flatten_anchors` measures the real sag; it does not rubber-stamp.

    It is what replaced a fixed sixteenth-of-a-cell chart step in
    `_cells_of3`, and on the shipped 0.25 mm segments it hands every one back
    as a single chord -- which, taken alone, is indistinguishable from a
    function that accepts everything. The evidence that it is not is that it
    splits when it should, and the threshold is where the closed form says it
    should be.

    On an R10 dome a chart segment of world length L bows off its own chord by
    about L^2/(8R), so the tolerance `_CHORD_TOL_CELLS * cell` = 0.00375 mm is
    reached at L = sqrt(8 * 10 * 0.00375) = 0.55 mm. A quarter-millimetre
    segment must come back whole and a two-millimetre one must not, and the
    piece count on the long one must be enough to bring each piece under that
    length.
    """
    idx = gf._SegIndex(CELL, frame=DOME)
    one = np.array([1.0]), np.array([0.0]), np.array([1.25]), np.array([0.0])
    long_ = np.array([1.0]), np.array([0.0]), np.array([3.0]), np.array([0.0])
    n_short = len(idx._flatten_anchors(*one)[0])
    sid, wa, wb = idx._flatten_anchors(*long_)
    n_long = len(sid)
    worst = float(np.max(np.linalg.norm(wb - wa, axis=1)))
    check("the footprint flattener splits where the surface bends",
          n_short == 1 and n_long > 1 and worst < 0.56,
          "0.25 mm segment -> %d piece; 2.0 mm segment -> %d pieces, longest "
          "%.4f mm, under the 0.55 mm the closed form puts the tolerance at"
          % (n_short, n_long, worst))


def test_CTL_the_flattened_chords_really_track_the_surface():
    """Every accepted chord stays within the tolerance of the true surface.

    The case above checks the SPLITTING RULE fires; this checks what the rule
    is for. Each piece's own chart midpoint is evaluated on the frame and
    compared to the midpoint of the world chord the index will walk -- the
    same quantity `_flatten_anchors` tested, re-measured from outside it, on
    the pieces it actually returned. A flattener that split too little would
    show here as a piece whose middle is nowhere near its chord.

    Run on a segment long enough to force several levels, so the check is
    against pieces the recursion produced rather than against one it was
    handed.

    EACH PIECE'S CHART SPAN COMES BACK FROM ITS OWN WORLD ENDS, via
    `from_world`. The pieces are returned level by level, not in path order,
    and they are not all the same length -- an earlier version of this case
    assumed they tiled the segment evenly and reported 6.4 mm of error that was
    entirely its own bookkeeping.
    """
    idx = gf._SegIndex(CELL, frame=DOME)
    u0, v0, u1, v1 = (np.array([0.5]), np.array([-1.0]),
                      np.array([6.5]), np.array([2.0]))
    sid, wa, wb = idx._flatten_anchors(u0, v0, u1, v1)
    worst = 0.0
    for i in range(len(sid)):
        ua = DOME.from_world(*wa[i])
        ub = DOME.from_world(*wb[i])
        um = 0.5 * (float(ua[0]) + float(ub[0]))
        vm = 0.5 * (float(ua[1]) + float(ub[1]))
        pm = np.asarray(DOME.to_world(um, vm, 0.0), dtype=np.float64)
        worst = max(worst, float(np.linalg.norm(pm - 0.5 * (wa[i] + wb[i]))))
    tol = gf._CHORD_TOL_CELLS * CELL
    check("CTL the flattened chords really track the surface",
          len(sid) > 1 and worst <= tol,
          "%d pieces; worst surface-to-chord %.6f mm against the %.6f mm "
          "tolerance" % (len(sid), worst, tol))


def main():
    test_off_axis_matches_the_exhaustive_search()
    test_the_exhaustive_search_needs_its_refinement()
    test_apex_centred_design_returns_contacts()
    test_apex_query_does_not_raise()
    test_the_seam_is_missed_without_a_frame()
    test_a_point_far_around_is_still_far()
    test_apply_sag_completes_for_an_apex_centred_design()
    test_self_pairs_across_the_seam_are_folded()
    test_self_contact_does_not_widen_k_by_the_chart_overstatement()
    test_the_footprint_flattener_splits_where_the_surface_bends()
    test_CTL_the_flattened_chords_really_track_the_surface()
    print("\n%d passed, %d failed, %d total"
          % (len(_PASS), len(_FAIL), len(_PASS) + len(_FAIL)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
