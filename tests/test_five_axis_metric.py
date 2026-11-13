"""The metric: the chart's first fundamental form, not one scalar.

`metric_v(h)` returned ONE
number -- the stretch in `v` -- which is the whole answer only where `k1 = 0`
and `rho' = 0`. That is a cylinder, and it is why one scalar survived four
axes. `mew_frame.metric(u, v, h)` returns `(a, b, c)`, the factors of the same
chart's full first fundamental form:

    a chart step (du, dv) lands at   (a*du,  b*(dv - c*du))
    E = a**2 + (b*c)**2      F = -(b**2)*c      G = b**2

THREE COMPARATORS, none of them this file's own arithmetic:

  1. FINITE DIFFERENCES of `mew_frame.to_world`. E, F and G are dot products of
     the parameterisation's own partial derivatives, taken by central
     difference. It never calls `profile.tangent` or `profile.curvature`, so a
     sign error or a missing factor in the closed form fails here; a test that
     re-derived the closed form would not.
  2. THE WORLD CHORD ON A PROJECTED PATH. `mew_project` lays a real serpentine
     on a dome and reports every point in world XYZ. The metric's prediction
     for a segment is compared against the distance between those world points
     -- a number produced by a different module, through a different code path,
     that this file cannot influence.
  3. THE SHIPPED CYLINDER, as a BYTE statement. `metric(u, v, h)` must be
     `(1.0, metric_v(h), 0.0)` exactly, not to a tolerance: `a == 1.0` and
     `c == 0.0` are what make every call site's `a*du` and `dv - c*du`
     bit-exact, and `b` is `metric_v`'s expression verbatim. The digest guard
     in `test_mandrel_sag.py` is the other half of that statement (with
     `test_mandrel_placement.py`, removed 2026-10-30), and both were measured to go RED on a ONE-ULP
     change to `b`.

AND THE CONTROL THAT MAKES THE REST EVIDENCE.
`test_CTL_dropping_the_shear_is_seen` re-runs comparator 2 with `c` forced to
zero and requires it to FAIL. That control is not decoration. With the shear
dropped, `test_mew_incremental`, `test_mew_fusion`, `test_sag_reset`,
`test_sag_approx`, `test_sag_crest`, `test_mandrel_sag`,
`test_mandrel_placement`, `test_frame_contract` and `test_five_axis_frame` ALL
STAY GREEN -- measured 2026-09-08 -- because not one of them solves on a
doubly-curved frame. The first implementation of this slice dropped `c`, was
wrong by 34% of span on a 5 mm dome, and passed all nine.
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math
import sys

import numpy as np

import mew_frame
import mew_project


_PASS = []
_FAIL = []


def ok(name, msg=""):
    _PASS.append(name)
    print("PASS %s   %s" % (name, msg))


def bad(name, msg):
    _FAIL.append(name)
    print("FAIL %s   %s" % (name, msg))


def check(name, cond, msg=""):
    ok(name, msg) if cond else bad(name, msg)


# --------------------------------------------------------------------------- #
# the independent measures
# --------------------------------------------------------------------------- #
def fd_fff(frame, u, v, h, e=1e-6):
    """(E, F, G) by central difference of `to_world`. Comparator 1."""
    def P(uu, vv):
        return np.array([float(x) for x in frame.to_world(uu, vv, h)])
    du = (P(u + e, v) - P(u - e, v)) / (2.0 * e)
    dv = (P(u, v + e) - P(u, v - e)) / (2.0 * e)
    return float(du @ du), float(du @ dv), float(dv @ dv)


def closed_fff(frame, u, v, h):
    a, b, c = (float(x) for x in frame.metric(u, v, h))
    return a * a + (b * c) ** 2, -(b * b) * c, b * b


def dome(R, u1=None):
    return mew_frame.RevolutionFrame(mew_frame.ArcProfile(R), u0=0.0,
                                     u1=0.85 * R if u1 is None else u1)


def _serpentine(passes=6, length=8.0, stepover=0.5, n=33):
    out = []
    for j in range(passes):
        p = np.linspace(0.0, length, n)
        if j % 2:
            p = p[::-1]
        out.append(np.stack([p, np.full_like(p, j * stepover)], axis=1))
    return np.concatenate(out)


def projected_span_error(frame, u0, h, shear=True, nsub=16):
    """Comparator 2: |metric arc / world arc - 1| over a projected pattern.

    BOTH SIDES ARE ARC LENGTHS, and `nsub` is why. The metric predicts the
    LENGTH OF A CURVE; a single world chord between the segment endpoints is
    shorter than that curve by `L**2/(24 R**2)`, which at `mew_project`'s
    0.25 mm step is 1.4e-3 on a 5 mm dome -- bigger than anything the metric
    itself gets wrong, and it is a property of the comparator rather than of
    the code under test. So each chart segment is walked in `nsub` sub-steps
    and both sides are summed over the same sub-steps. What remains falls as
    `1/nsub**2`, which `test_the_comparator_converges_second_order` measures
    rather than assumes.

    `shear=False` is the planted defect the control needs -- the same
    expression with `c` forced to zero, which is what the first implementation
    of this slice computed.
    """
    path = mew_project.project_pattern(mew_project.FrameSurface(frame),
                                       _serpentine(), (u0, 0.0), step=0.25)
    uv = np.asarray(path.uv, dtype=np.float64)
    errs = []
    for k in range(len(uv) - 1):
        du = uv[k + 1, 0] - uv[k, 0]
        dv = uv[k + 1, 1] - uv[k, 1]
        if du == 0.0 and dv == 0.0:
            continue
        pred, true, prev = 0.0, 0.0, None
        for i in range(nsub + 1):
            t = i / float(nsub)
            W = np.array([float(x) for x in
                          frame.to_world(uv[k, 0] + t * du,
                                         uv[k, 1] + t * dv, h)])
            if prev is not None:
                true += float(np.linalg.norm(W - prev))
            prev = W
            if i < nsub:
                tm = (i + 0.5) / float(nsub)
                a, b, c = (float(x) for x in
                           frame.metric(uv[k, 0] + tm * du,
                                        uv[k, 1] + tm * dv, h))
                if not shear:
                    c = 0.0
                pred += math.hypot(a * du / nsub,
                                   b * (dv / nsub - c * du / nsub))
        if true > 0.0:
            errs.append(abs(pred / true - 1.0))
    return np.array(errs)


# --------------------------------------------------------------------------- #
# comparator 3 -- the cylinder is a BYTE statement
# --------------------------------------------------------------------------- #
def test_a_cylinder_is_metric_v_verbatim_and_bit_exact():
    cyl = mew_frame.CylinderFrame(3.0)
    wrong = []
    for h in (0.0, 0.02, 0.4, 1.7, -0.01):
        for v in (0.0, 2.5, -7.25):
            a, b, c = cyl.metric(1.5, v, h)
            if not (a == 1.0 and c == 0.0 and b == cyl.metric_v(h)):
                wrong.append("h=%r v=%r -> %r" % (h, v, (a, b, c)))
    check("a cylinder's metric is (1.0, metric_v(h), 0.0), bit for bit",
          not wrong,
          "5 wall heights x 3 azimuths, exact float equality"
          if not wrong else "; ".join(wrong))


def test_the_flat_frame_has_no_metric_at_all():
    f = mew_frame.PlanarFrame()
    got = [f.metric(u, v, h)
           for u in (0.0, 12.0) for v in (0.0, -3.0) for h in (0.0, 0.6)]
    flat = all(g == (1.0, 1.0, 0.0) for g in got)
    check("the plane is the literal identity: every factor is (1.0, 1.0, 0.0)",
          flat, "8 query points" if flat else repr(got))


def test_a_constant_radius_revolution_reproduces_the_cylinder():
    cyl = mew_frame.CylinderFrame(3.0)
    rev = mew_frame.RevolutionFrame(mew_frame.LineProfile(3.0), u0=0.0, u1=10.0)
    wrong = []
    for h in (0.0, 0.02, 0.4):
        for v in (0.0, 2.5):
            got = tuple(float(x) for x in rev.metric(1.5, v, h))
            if got != cyl.metric(1.5, v, h):
                wrong.append("h=%r v=%r %r vs %r"
                             % (h, v, got, cyl.metric(1.5, v, h)))
    check("a constant-radius profile reproduces CylinderFrame's metric exactly",
          not wrong, "the shear is exactly 0.0 where rho' is"
          if not wrong else "; ".join(wrong))


# --------------------------------------------------------------------------- #
# comparator 1 -- finite differences of to_world
# --------------------------------------------------------------------------- #
_FD_CASES = (
    ("dome R35", lambda: dome(35.0)),
    ("dome R5", lambda: dome(5.0)),
    ("bowl R35", lambda: mew_frame.RevolutionFrame(
        mew_frame.ArcProfile(35.0, convex=False), u0=0.0, u1=10.0)),
    ("torus", lambda: mew_frame.RevolutionFrame(
        mew_frame.ArcProfile(4.0, centre_rho=12.0), u0=0.0, u1=4.0)),
    ("cone 15deg", lambda: mew_frame.RevolutionFrame(
        mew_frame.LineProfile(3.0, half_angle=math.radians(15.0)),
        u0=0.0, u1=9.0)),
)


def test_the_closed_form_matches_finite_differences_of_to_world():
    worst, where, n = 0.0, "", 0
    for name, build in _FD_CASES:
        fr = build()
        for u in (0.7, 2.0, 3.5):
            for v in (0.0, 0.8, 2.5, -1.7):
                for h in (0.0, 0.3):
                    C, D = closed_fff(fr, u, v, h), fd_fff(fr, u, v, h)
                    scale = max(1.0, max(abs(x) for x in D))
                    e = max(abs(c - d) for c, d in zip(C, D)) / scale
                    n += 1
                    if e > worst:
                        worst, where = e, "%s u=%s v=%s h=%s" % (name, u, v, h)
    check("E, F and G match finite differences of to_world on five surfaces",
          worst < 1e-7,
          "worst relative disagreement %.2e (%s), over %d query points"
          % (worst, where, n))


class _BentShear(object):
    """The planted error: the same frame with its shear scaled by 1.5."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, k):
        return getattr(self._inner, k)

    def metric(self, u, v, h=0.0):
        a, b, c = self._inner.metric(u, v, h)
        return a, b, np.multiply(c, 1.5)


def test_CTL_the_finite_difference_probe_sees_a_planted_error():
    """Comparator 1 must FAIL on a wrong metric, or its green means nothing."""
    fr = dome(35.0)
    worst = 0.0
    for u in (0.7, 2.0, 3.5):
        for v in (0.8, 2.5):
            C = closed_fff(_BentShear(fr), u, v, 0.3)
            D = fd_fff(fr, u, v, 0.3)
            scale = max(1.0, max(abs(x) for x in D))
            worst = max(worst, max(abs(c - d) for c, d in zip(C, D)) / scale)
    check("CTL: the finite-difference probe rejects a shear scaled by 1.5",
          worst > 1e-7,
          "the planted error shows as %.2e, far above the 1e-7 gate" % worst)


# --------------------------------------------------------------------------- #
# comparator 2 -- a real projected path, against the world chord
# --------------------------------------------------------------------------- #
def test_a_projected_dome_path_gets_its_span_lengths_right():
    for R, u0 in ((35.0, 6.0), (5.0, 2.0)):
        e = projected_span_error(dome(R), u0, 0.30)
        check("a %g mm dome's projected serpentine spans are right to 1e-5" % R,
              bool(e.size) and e.max() < 1e-5,
              "%d segments at a 15-layer wall: mean %.2e, worst %.2e"
              % (len(e), e.mean(), e.max()))


def test_the_comparator_converges_second_order():
    """The gate above is 1e-5; this says WHY that number is not a tolerance.

    The residual at `nsub = 1` is 2.7e-4 (R = 35) and 1.4e-3 (R = 5), which is
    where this test first ran and failed. Refining the comparator quarters it
    at every doubling -- the signature of chord-versus-arc sampling and of
    nothing else -- so it converges to the metric rather than bounding it. A
    metric that was merely CLOSE would flatten out at its own error instead.
    """
    for R, u0 in ((35.0, 6.0), (5.0, 2.0)):
        got = [projected_span_error(dome(R), u0, 0.30, nsub=n).max()
               for n in (2, 4, 8)]
        ratios = [got[i] / got[i + 1] for i in range(len(got) - 1)]
        check("a %g mm dome's residual quarters at every refinement" % R,
              all(3.6 < r < 4.4 for r in ratios),
              "worst error %s at nsub 2, 4, 8 -- ratios %s"
              % (", ".join("%.2e" % g for g in got),
                 ", ".join("%.2f" % r for r in ratios)))


def test_CTL_dropping_the_shear_is_seen():
    """THE control. Nine shipped suites stay green without `c`; this must not."""
    for R, u0 in ((35.0, 6.0), (5.0, 2.0)):
        good = projected_span_error(dome(R), u0, 0.30)
        flat = projected_span_error(dome(R), u0, 0.30, shear=False)
        check("CTL: a %g mm dome sees the shear dropped" % R,
              flat.max() > 100.0 * good.max(),
              "worst span error %.2e with the shear, %.2e without -- %.0fx"
              % (good.max(), flat.max(), flat.max() / good.max()))


# --------------------------------------------------------------------------- #
# the two regimes the four-axis metric never had to answer
# --------------------------------------------------------------------------- #
def test_concave_shrinks_the_stretch_and_the_fold_is_refused():
    bowl = mew_frame.RevolutionFrame(
        mew_frame.ArcProfile(5.0, convex=False), u0=0.0, u1=4.0)
    a, b, _c = (float(x) for x in bowl.metric(1.0, 0.5, 0.30))
    check("a concave patch SHRINKS both stretches below 1",
          a < 1.0 and b < 1.0,
          "R = 5 mm bowl at a 15-layer wall: a = %.6f, b = %.6f" % (a, b))

    tight = mew_frame.RevolutionFrame(
        mew_frame.ArcProfile(0.5, convex=False), u0=0.0, u1=0.2)
    fine = tight.metric(0.1, 0.0, 0.499)
    raised = False
    try:
        tight.metric(0.1, 0.0, 0.5)
    except mew_frame.OffsetSurfaceFold:
        raised = True
    check("the offset fold is refused AT the fold and not before",
          raised and float(fine[1]) > 0.0,
          "h = 0.499 on R = 0.5 gives b = %.4f; h = 0.5 raises "
          "OffsetSurfaceFold" % float(fine[1]))


def test_the_realistic_wall_is_nowhere_near_the_fold():
    """Why there is no invented fraction-of-the-pole band in front of it."""
    rows = []
    for R in (2.0, 5.0, 35.0):
        bowl = mew_frame.RevolutionFrame(
            mew_frame.ArcProfile(R, convex=False), u0=0.0, u1=0.4 * R)
        b = float(bowl.metric(0.2 * R, 0.0, 0.30)[1])
        rows.append((R, b, R / 0.30))
    check("a 15-layer wall sits >= 6x from the fold on every bowl measured",
          all(r[2] >= 6.0 and r[1] > 0.8 for r in rows),
          "; ".join("R=%g b=%.3f %.0fx" % r for r in rows))


def test_a_torus_is_anisotropic_where_a_dome_is_not():
    d = dome(35.0)
    t = mew_frame.RevolutionFrame(mew_frame.ArcProfile(4.0, centre_rho=12.0),
                                  u0=0.0, u1=4.0)
    da, db, _ = (float(x) for x in d.metric(3.0, 0.0, 0.30))
    ta, tb, _ = (float(x) for x in t.metric(2.0, 0.0, 0.30))
    check("a dome's two stretches are equal and a torus's are not",
          da == db and abs(ta - tb) > 1e-3,
          "dome a = b = %.9f; torus a = %.6f, b = %.6f (%.1f%% apart)"
          % (da, ta, tb, 100.0 * abs(ta - tb) / tb))


# --------------------------------------------------------------------------- #
# the pole, and the apex
# --------------------------------------------------------------------------- #
def test_the_pole_is_answered_as_the_umbilic_limit():
    d = dome(35.0)
    a, b, c = (float(x) for x in d.metric(0.0, 0.0, 0.30))
    near = [float(x) for x in d.metric(1e-6, 0.0, 0.30)]
    check("a dome's pole answers, isotropic and finite, and matches its limit",
          a == b and c == 0.0 and abs(a - near[0]) < 1e-9,
          "metric(0, 0, 0.3) = (%.9f, %.9f, %.1f); at u = 1e-6, a = %.9f"
          % (a, b, c, near[0]))


def test_a_cone_apex_is_still_refused():
    cone = mew_frame.RevolutionFrame(
        mew_frame.LineProfile(0.0, half_angle=0.3), u0=0.0, u1=9.0)
    raised = False
    try:
        cone.metric(0.0, 0.0, 0.1)
    except mew_frame.AxisSingularity:
        raised = True
    check("a cone apex raises AxisSingularity rather than answering",
          raised, "no tangent plane, so no metric either")


def test_arrays_go_in_and_arrays_come_out():
    d = dome(35.0)
    u = np.array([0.0, 1.0, 5.0])
    v = np.array([0.0, 0.4, -2.0])
    h = np.array([0.0, 0.1, 0.3])
    got = [np.asarray(x) for x in d.metric(u, v, h)]
    good = (all(x.shape == (3,) for x in got)
            and bool(np.all(np.isfinite(got[2]))))
    check("the metric is vectorised in u, v and h",
          good, "a 3-element query returns three 3-element factors, all finite")


def main():
    test_a_cylinder_is_metric_v_verbatim_and_bit_exact()
    test_the_flat_frame_has_no_metric_at_all()
    test_a_constant_radius_revolution_reproduces_the_cylinder()
    test_the_closed_form_matches_finite_differences_of_to_world()
    test_CTL_the_finite_difference_probe_sees_a_planted_error()
    test_a_projected_dome_path_gets_its_span_lengths_right()
    test_the_comparator_converges_second_order()
    test_CTL_dropping_the_shear_is_seen()
    test_concave_shrinks_the_stretch_and_the_fold_is_refused()
    test_the_realistic_wall_is_nowhere_near_the_fold()
    test_a_torus_is_anisotropic_where_a_dome_is_not()
    test_the_pole_is_answered_as_the_umbilic_limit()
    test_a_cone_apex_is_still_refused()
    test_arrays_go_in_and_arrays_come_out()
    print("\n%d passed, %d failed, %d total"
          % (len(_PASS), len(_FAIL), len(_PASS) + len(_FAIL)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
