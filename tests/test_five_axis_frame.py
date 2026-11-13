"""The five-axis frame: a surface of revolution, and the developability limit.

One class -- a profile curve spun about machine X -- is dome, cone, bowl,
sphere, curved tube and torus, so a dome is a new subclass and not a new solver. `normal_curvature` was
already Euler's theorem over principal curvatures, which is what makes that
true.

THE COMPARATOR IS NOT THE CLOSED FORM'S OWN ARITHMETIC. Every curvature,
tangent and normal below is measured against FINITE DIFFERENCES of the
parameterisation -- a second, independent computation of the same geometry --
and separately against the two frames that already shipped:

  1. FINITE DIFFERENCES. `x'`, `rho'`, `x''`, `rho''` by central difference on
     `Profile.point`, then k1 as `x'' rho' - x' rho''` and k2 as `x'/rho`
     assembled from those. A sign error, a factor, or a wrong traversal
     direction all fail here; none of them would fail a test that re-derived
     the closed form.
  2. THE SHIPPED FRAMES. A `RevolutionFrame` over a constant-radius profile
     must reproduce `CylinderFrame` -- point, normal, curvatures, metric --
     and the base class must still be the literal flat no-op. This is what
     stops "one class covers everything" from quietly becoming a second
     convention.
  3. GEOMETRY THAT NEEDS NO REFERENCE. A sphere has k1 == k2 exactly. A cone
     has k1 == 0 exactly and is developable. A bowl is the same circle with
     the sign of both curvatures flipped. A torus is anisotropic, and its
     inner and outer equators carry opposite k2 sign. The normal is a unit
     vector, is perpendicular to both surface tangents, and is the direction
     `to_world` offsets a point along. Outwardness is anchored on the SPHERE,
     where it is unambiguous -- on the inner half of a torus the outward normal
     correctly points toward the axis, and an assertion that it does not is a
     defect in the test, which is what the first run of this file found.

CONTROLS, so that a green run is evidence:

  `test_CTL_the_finite_difference_probe_sees_a_planted_error` re-runs the
  curvature comparison against a profile whose closed-form curvature is
  deliberately scaled by 1.5 and requires the comparison to FAIL. Without it
  every curvature case could be passing because the probe cannot see a
  curvature at all.
  `test_CTL_a_developable_frame_is_not_refused` is the other direction on the
  developability gate: a cone must pass the same check a dome is stopped by,
  so "it raises" is not just "it always raises". Since five-axis slice 19 the
  SAG solvers no longer use that gate --
  `test_a_dome_is_solved_off_the_pole_and_refused_on_it` is theirs, and it
  carries both directions inside itself on one frame, because a dome is now
  solvable off its pole and refused only on it.
  `test_CTL_the_pole_station_probes_the_division_that_failed` re-runs the
  pre-fix `x'/rho` at the pole and requires it to be nan, so the pole cases
  cannot be passing on a frame that never divides by zero.

Self-running, no pytest: prints PASS/FAIL per case, exits non-zero on any fail.
"""

import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import math
import sys

import numpy as np

import gcode_to_fibers
import mew_frame
import preview_sag
import sag_approx

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
# the independent measure
# --------------------------------------------------------------------------- #
def fd_curvatures(profile, h1=1e-6, h2=1e-3, s=0.0):
    """(k1, k2) at meridian s from FINITE DIFFERENCES of `profile.point`.

    Deliberately assembled from the parameterisation alone -- it never calls
    `tangent` or `curvature` -- so it is a second computation of the geometry
    and not a restatement of the first.

    TWO STEP SIZES, and the reason is the whole reliability of this probe. A
    central second difference divides by `h*h`, so its round-off grows as
    `|x| * 2.2e-16 / h^2` while its truncation shrinks as O(h^2). At a single
    h = 1e-5 on a coordinate of order 35 mm that round-off is 7.5e-5, which is
    0.26 percent of a 1/35 curvature -- measured on the first run of this file,
    where it read as a real disagreement with the closed form and was not one.
    First derivatives are taken at h1 = 1e-6, second derivatives at h2 = 1e-3.
    What is left is ~1e-8, and the planted-error control below shows 1.4e-2, so
    the probe still discriminates by six orders of magnitude.
    """
    def pt(t):
        x, rho = profile.point(t)
        return float(x), float(rho)

    xm1, rm1 = pt(s - h1)
    xp1, rp1 = pt(s + h1)
    dx = (xp1 - xm1) / (2.0 * h1)
    drho = (rp1 - rm1) / (2.0 * h1)

    xm2, rm2 = pt(s - h2)
    x0, r0 = pt(s)
    xp2, rp2 = pt(s + h2)
    ddx = (xp2 - 2.0 * x0 + xm2) / (h2 * h2)
    ddrho = (rp2 - 2.0 * r0 + rm2) / (h2 * h2)
    return ddx * drho - dx * ddrho, dx / r0


def fd_tangent(profile, s, eps=1e-5):
    x0, r0 = profile.point(s - eps)
    x1, r1 = profile.point(s + eps)
    return ((float(x1) - float(x0)) / (2.0 * eps),
            (float(r1) - float(r0)) / (2.0 * eps))


#: The four shapes the slice names, plus the two that must reproduce what
#: already ships. `us` are meridian arc lengths well inside each patch.
def shapes():
    return [
        ("dome  R35", mew_frame.ArcProfile(35.0), [1.0, 5.0, 12.0]),
        ("bowl  R35 (concave)",
         mew_frame.ArcProfile(35.0, convex=False), [1.0, 5.0, 12.0]),
        ("torus R4 about rho=12",
         mew_frame.ArcProfile(4.0, centre_rho=12.0), [0.5, 2.0, 5.0]),
        ("cone  15 deg from rho 3",
         mew_frame.LineProfile(3.0, half_angle=math.radians(15.0)),
         [0.0, 4.0, 9.0]),
        ("cylinder R3 (as a profile)",
         mew_frame.LineProfile(3.0), [0.0, 4.0, 9.0]),
        ("curved tube R20 about rho=2.5",
         mew_frame.ArcProfile(20.0, centre_rho=2.5, t0=math.pi / 2.0),
         [0.5, 3.0, 8.0]),
    ]


def test_closed_form_curvatures_match_finite_differences():
    worst = 0.0
    where = ""
    for label, profile, us in shapes():
        for u in us:
            got = profile.curvature(u), float(np.divide(
                profile.tangent(u)[0], profile.point(u)[1]))
            want = fd_curvatures(profile, s=u)
            for a, b, which in ((got[0], want[0], "k1"), (got[1], want[1], "k2")):
                d = abs(float(a) - b)
                if d > worst:
                    worst, where = d, "%s u=%g %s: %.12g vs %.12g" % (
                        label, u, which, float(a), b)
    check("closed-form k1/k2 match finite differences of the parameterisation",
          worst < 1e-6,
          "6 shapes x 3 stations x 2 curvatures, worst |d| %.3g (%s)"
          % (worst, where))


def test_CTL_the_finite_difference_probe_sees_a_planted_error():
    """Scale one closed form by 1.5 and the comparison must fail."""
    class _Wrong(mew_frame.ArcProfile):
        def curvature(self, s):
            return 1.5 * mew_frame.ArcProfile.curvature(self, s)

    p = _Wrong(35.0)
    d = abs(float(p.curvature(5.0)) - fd_curvatures(p, s=5.0)[0])
    check("CTL: the finite-difference probe sees a planted 1.5x curvature",
          d > 1e-6, "planted error shows as |d| = %.4g (1/35 * 0.5 = %.4g)"
          % (d, 0.5 / 35.0))


def test_the_tangent_is_unit_and_matches_finite_differences():
    worst_unit = 0.0
    worst_fd = 0.0
    for _label, profile, us in shapes():
        for u in us:
            dx, drho = (float(a) for a in profile.tangent(u))
            worst_unit = max(worst_unit, abs(math.hypot(dx, drho) - 1.0))
            fx, fr = fd_tangent(profile, u)
            worst_fd = max(worst_fd, abs(dx - fx), abs(drho - fr))
    check("the meridian tangent is unit-speed and matches finite differences",
          worst_unit < 1e-12 and worst_fd < 1e-8,
          "worst |T|-1 = %.3g, worst |dT| vs FD = %.3g" % (worst_unit, worst_fd))


# --------------------------------------------------------------------------- #
# the shipped frames must not move
# --------------------------------------------------------------------------- #
def test_a_constant_radius_profile_reproduces_CylinderFrame():
    R0 = 3.0
    cyl = mew_frame.CylinderFrame(R0)
    rev = mew_frame.RevolutionFrame(mew_frame.LineProfile(R0), u0=0.0, u1=10.0)
    fails = []
    for u, v, h in ((0.0, 0.0, 0.0), (4.0, 1.7, 0.0), (9.0, 5.5, 0.06),
                    (2.0, -3.1, 0.24)):
        a = np.asarray([float(c) for c in cyl.to_world(u, v, h)])
        b = np.asarray([float(c) for c in rev.to_world(u, v, h)])
        if np.max(np.abs(a - b)) > 1e-12:
            fails.append("to_world%r %r vs %r" % ((u, v, h), a, b))
        na = np.asarray([float(c) for c in cyl.normal(u, v, h)])
        nb = np.asarray([float(c) for c in rev.normal(u, v, h)])
        if np.max(np.abs(na - nb)) > 1e-12:
            fails.append("normal%r %r vs %r" % ((u, v, h), na, nb))
    for h in (0.0, 0.05, 0.3):
        if max(abs(float(x) - float(y)) for x, y in
               zip(cyl.principal_curvatures(h), rev.principal_curvatures(h))) > 1e-15:
            fails.append("principal_curvatures(%r)" % h)
        if abs(cyl.metric_v(h) - rev.metric_v(h)) > 1e-15:
            fails.append("metric_v(%r)" % h)
        if abs(cyl.surface_radius(h) - rev.surface_radius(h)) > 1e-12:
            fails.append("surface_radius(%r)" % h)
    if not rev.developable:
        fails.append("a cylinder profile is not developable")
    check("a constant-radius RevolutionFrame reproduces CylinderFrame",
          not fails, "point, normal, curvatures, metric, radius"
          if not fails else str(fails[:3]))


def test_the_flat_frame_is_still_the_literal_no_op():
    f = mew_frame.PlanarFrame()
    fails = []
    if f.to_world(1.0, 2.0, 3.0) != (1.0, 2.0, 3.0):
        fails.append("to_world")
    if f.unroll(1.0, 2.0, 3.0) != (1.0, 2.0, 3.0):
        fails.append("unroll")
    if f.metric_v(0.5) != 1.0 or f.principal_curvatures(0.5) != (0.0, 0.0):
        fails.append("metric/curvature")
    if f.chord_drop(1.0, 0.3, 0.1) != 0.0:
        fails.append("chord_drop")
    if [float(c) for c in f.normal(1.0, 2.0, 3.0)] != [0.0, 0.0, 1.0]:
        fails.append("normal")
    if not f.developable:
        fails.append("developable")
    check("PlanarFrame is still the literal flat no-op", not fails,
          "6 methods" if not fails else str(fails))


# --------------------------------------------------------------------------- #
# geometry that needs no reference
# --------------------------------------------------------------------------- #
def test_a_sphere_is_isotropic_and_a_torus_is_not():
    dome = mew_frame.RevolutionFrame(mew_frame.ArcProfile(35.0), u0=0.0, u1=10.0)
    torus = mew_frame.RevolutionFrame(
        mew_frame.ArcProfile(4.0, centre_rho=12.0), u0=0.0, u1=4.0)
    k1d, k2d = dome.principal_curvatures(0.0)
    k1t, k2t = torus.principal_curvatures(0.0)
    check("a sphere is k1 == k2 and a torus is not",
          abs(k1d - k2d) < 1e-15 and abs(k1t - k2t) > 1e-3
          and abs(k1d - 1.0 / 35.0) < 1e-15,
          "dome k1 = k2 = %.6f (1/35 = %.6f); torus k1 %.6f k2 %.6f"
          % (k1d, 1.0 / 35.0, k1t, k2t))


def test_concave_is_representable_and_flips_both_signs():
    """The bowl is the spec's reason concave is in v1, not a deferred case."""
    conv = mew_frame.RevolutionFrame(mew_frame.ArcProfile(35.0), u0=0.0, u1=10.0)
    conc = mew_frame.RevolutionFrame(
        mew_frame.ArcProfile(35.0, convex=False), u0=0.0, u1=10.0)
    a = conv.principal_curvatures(0.0)
    b = conc.principal_curvatures(0.0)
    check("a bowl is the same circle with both curvatures negative",
          a[0] > 0 and a[1] > 0 and b[0] < 0 and b[1] < 0
          and abs(a[0] + b[0]) < 1e-15 and abs(a[1] + b[1]) < 1e-15,
          "convex (%.6f, %.6f) vs concave (%.6f, %.6f)" % (a + b))


def test_a_cone_is_developable_and_a_dome_is_not():
    cone = mew_frame.RevolutionFrame(
        mew_frame.LineProfile(3.0, half_angle=math.radians(15.0)), u0=0.0, u1=9.0)
    dome = mew_frame.RevolutionFrame(mew_frame.ArcProfile(35.0), u0=0.0, u1=10.0)
    k1c, k2c = cone.principal_curvatures(0.0)
    check("a cone is developable (k1 == 0) and a dome is not",
          cone.developable and abs(k1c) < 1e-15 and k2c > 0
          and not dome.developable,
          "cone k1 %.3g k2 %.6f developable %s; dome developable %s"
          % (k1c, k2c, cone.developable, dome.developable))


def test_the_normal_is_unit_perpendicular_and_agrees_with_to_world():
    """Unit, perpendicular to BOTH surface tangents, and the SAME direction
    `to_world` offsets a point along.

    The tangents come from differencing `to_world`, so this never consults the
    profile: if `normal` and `to_world` disagreed about which way the meridian
    runs, no amount of internal consistency inside either one would hide it.

    NOTE the invariant this deliberately does NOT assert. The first draft
    required a convex surface to move a point AWAY FROM THE AXIS as h grows,
    and the torus failed it -- correctly. On the inner half of a torus the
    outward normal points toward the axis, and the frame reports k2 < 0 there,
    which is the anisotropy the spec wants a torus for in the first place
    (section 2.1, consequence 2). The defect was the assertion, not the code.
    Outwardness is anchored below on the sphere instead, where it is
    unambiguous.
    """
    worst_unit = worst_dot = worst_off = 0.0
    eps = 1e-6
    for _label, profile, us in shapes():
        fr = mew_frame.RevolutionFrame(profile, u0=min(us), u1=max(us))
        for u in us:
            for v in (0.0, 1.3):
                n = np.asarray([float(c) for c in fr.normal(u, v)])
                worst_unit = max(worst_unit, abs(np.linalg.norm(n) - 1.0))
                w = lambda a, b, c: np.asarray(
                    [float(q) for q in fr.to_world(a, b, c)])
                pu = w(u + eps, v, 0.0) - w(u - eps, v, 0.0)
                pv = w(u, v + eps, 0.0) - w(u, v - eps, 0.0)
                for t in (pu, pv):
                    worst_dot = max(worst_dot,
                                    abs(float(n.dot(t)) / np.linalg.norm(t)))
                # to_world must offset along exactly this normal.
                d = 0.01
                worst_off = max(worst_off, float(np.max(np.abs(
                    (w(u, v, d) - w(u, v, 0.0)) - d * n))))
    check("the normal is unit, perpendicular to both tangents, and is the "
          "direction to_world offsets along",
          worst_unit < 1e-12 and worst_dot < 1e-7 and worst_off < 1e-12,
          "worst |n|-1 %.3g, worst |n.t|/|t| %.3g, worst offset mismatch %.3g mm"
          % (worst_unit, worst_dot, worst_off))


def test_outward_is_anchored_on_the_sphere_where_it_is_unambiguous():
    """A dome normal points away from the sphere centre; a bowl toward it.

    The one shape where outwardness has a reference-free meaning, and the two
    that matter: a dome is the simplest v1 collector and a bowl is the concave
    case put back in v1 so the model can be compared against Terranova.
    """
    got = {}
    for label, convex in (("dome", True), ("bowl", False)):
        fr = mew_frame.RevolutionFrame(
            mew_frame.ArcProfile(35.0, convex=convex), u0=0.0, u1=10.0)
        dots = []
        for u in (1.0, 5.0, 12.0):
            for v in (0.0, 2.0):
                pt = np.asarray([float(c) for c in fr.to_world(u, v, 0.0)])
                n = np.asarray([float(c) for c in fr.normal(u, v)])
                # the meridian circle is centred on the axis, so the world
                # centre of the sphere is the origin.
                dots.append(float(n.dot(pt)))
        got[label] = (min(dots), max(dots))
    check("a dome normal points away from the sphere centre, a bowl toward it",
          got["dome"][0] > 34.9 and got["bowl"][1] < -34.9,
          "n.p over 6 stations: dome %.3f..%.3f, bowl %.3f..%.3f (radius 35)"
          % (got["dome"] + got["bowl"]))

def test_the_offset_surface_flattens_as_the_wall_grows():
    """`k/(1+k*h)`: a wall built on a dome curves less than the dome does."""
    dome = mew_frame.RevolutionFrame(mew_frame.ArcProfile(35.0), u0=0.0, u1=10.0)
    k0 = dome.principal_curvatures(0.0)[0]
    k1 = dome.principal_curvatures(0.5)[0]
    check("the offset surface flattens as the wall grows",
          k1 < k0 and abs(1.0 / k1 - (35.0 + 0.5)) < 1e-9,
          "1/k at h=0 is %.6f, at h=0.5 is %.6f (35.5 expected)"
          % (1.0 / k0, 1.0 / k1))


# --------------------------------------------------------------------------- #
# the pole -- where the meridian meets the axis
#
# ADDED 2026-09-08 after `alpha` found this suite could not have caught the
# defect it was written for. Every station above is strictly inside a patch
# (`[1.0, 5.0, 12.0]`), and the one call that reaches the frame's own midpoint,
# `principal_curvatures`, is fed a span whose midpoint is 5.0. Nothing
# evaluated at u = 0, so nothing divided by rho = 0, and a dome apex returned
# `k2 = nan`, `normal = (1, nan, nan)` and `to_world = (35.01, nan, nan)` --
# at the one point a dome print naturally starts.
#
# The finite-difference probe above cannot be the comparator here: it computes
# `dx / r0` itself, so it divides by the same zero. The comparator is the LIMIT
# taken from the ordinary division path a nanometre off the pole, which is a
# different code path and was already correct to all 16 digits.
# --------------------------------------------------------------------------- #
def test_a_dome_pole_is_umbilic_and_finite():
    """k1 == k2 at the pole, and it equals the limit approached from beside it."""
    fails = []
    for label, convex, want in (("dome", True, 1.0 / 35.0),
                                ("bowl", False, -1.0 / 35.0)):
        fr = mew_frame.RevolutionFrame(
            mew_frame.ArcProfile(35.0, convex=convex), u0=0.0, u1=10.0)
        k1, k2 = (float(k) for k in fr.curvatures_at(0.0))
        near = float(fr.curvatures_at(1e-9)[1])
        if not (k1 == k2 == want):
            fails.append("%s pole k1=%r k2=%r want %r" % (label, k1, k2, want))
        if k2 != near:
            fails.append("%s pole k2 %r != the limit beside it %r"
                         % (label, k2, near))
    check("a dome pole is umbilic, finite, and equals the limit beside it",
          not fails,
          "dome and bowl: k1 == k2 == +-1/35, bit-identical to the value 1 nm "
          "off the pole" if not fails else str(fails))


def test_CTL_the_pole_station_probes_the_division_that_failed():
    """The station must exercise the 0/0, or its green means nothing.

    Re-runs the pre-fix expression -- a bare `x'/rho` -- at the same u the case
    above passes at, and requires it to be nan. Without this, the pole case
    could be passing on a frame that never divides by zero at all.
    """
    p = mew_frame.ArcProfile(35.0)
    dx = float(p.tangent(0.0)[0])
    rho = float(p.point(0.0)[1])
    with np.errstate(divide="ignore", invalid="ignore"):
        pre_fix = np.divide(dx, rho)
    check("CTL: the pole station probes the division that produced nan",
          rho == 0.0 and dx == 0.0 and np.isnan(pre_fix),
          "at u=0 the profile gives x'=%r, rho=%r, and the pre-fix x'/rho is %r"
          % (dx, rho, pre_fix))


def test_the_pole_places_and_orients_a_point():
    """`to_world` and `normal` must be finite at the pole and agree beside it."""
    fr = mew_frame.RevolutionFrame(mew_frame.ArcProfile(35.0), u0=0.0, u1=10.0)
    fails = []
    for v in (0.0, 2.5):
        p = np.asarray([float(c) for c in fr.to_world(0.0, v, 0.01)])
        n = np.asarray([float(c) for c in fr.normal(0.0, v)])
        if not np.all(np.isfinite(p)) or not np.all(np.isfinite(n)):
            fails.append("v=%r gives p=%r n=%r" % (v, p, n))
            continue
        if abs(np.linalg.norm(n) - 1.0) > 1e-15:
            fails.append("v=%r normal is not unit: %r" % (v, n))
        # every azimuth names the SAME point at a pole
        p0 = np.asarray([float(c) for c in fr.to_world(0.0, 0.0, 0.01)])
        if np.max(np.abs(p - p0)) > 0.0:
            fails.append("v=%r moved the pole: %r vs %r" % (v, p, p0))
    # ...and it is continuous with a point just off the pole.
    near = np.asarray([float(c) for c in fr.to_world(1e-6, 0.0, 0.01)])
    at = np.asarray([float(c) for c in fr.to_world(0.0, 0.0, 0.01)])
    gap = float(np.linalg.norm(near - at))
    check("the pole places and orients a point, and is continuous beside it",
          not fails and gap < 1e-5,
          "pole at %r, unit normal, azimuth-independent; 1 um off the pole is "
          "%.2e mm away" % (list(at), gap) if not fails else str(fails))


def test_a_cone_apex_is_refused_not_answered():
    """A sphere's pole is umbilic; a cone's apex has no tangent plane at all.

    This is the distinction `alpha` asked for rather than papered over: a fix
    that returned k1 unconditionally would invent a curvature for a cone apex,
    where none exists.
    """
    apex = mew_frame.RevolutionFrame(
        mew_frame.LineProfile(0.0, half_angle=0.3), u0=1.0, u1=9.0)
    answered = []
    for what, call in (("curvatures_at", lambda: apex.curvatures_at(0.0)),
                       ("normal", lambda: apex.normal(0.0, 0.0)),
                       ("to_world", lambda: apex.to_world(0.0, 0.0, 0.0))):
        try:
            call()
            answered.append(what)
        except mew_frame.AxisSingularity:
            pass
    # ...and the same cone is answered perfectly well away from its apex.
    ok_off = np.isfinite(float(apex.curvatures_at(1.0)[1]))
    check("a cone apex is refused, and the same cone answers away from it",
          not answered and ok_off,
          "3 entry points raise AxisSingularity; k2 at u=1 is %.6f"
          % float(apex.curvatures_at(1.0)[1])
          if not answered else "answered at the apex: %s" % (answered,))


def test_the_pole_survives_being_one_element_of_an_array():
    """The guard must be per-point, not per-call: real paths pass arrays."""
    fr = mew_frame.RevolutionFrame(mew_frame.ArcProfile(35.0), u0=0.0, u1=10.0)
    u = np.array([0.0, 1.0, 5.0, 12.0])
    k2 = np.asarray(fr.curvatures_at(u)[1], dtype=np.float64)
    y = np.asarray(fr.to_world(u, 2.0, 0.0)[1], dtype=np.float64)
    scalar = [float(fr.curvatures_at(t)[1]) for t in u]
    check("a pole mixed into an array of ordinary points is handled per-point",
          np.all(np.isfinite(k2)) and np.all(np.isfinite(y))
          and list(k2) == scalar and y[0] == 0.0,
          "4 stations including the pole: k2 all finite and identical to the "
          "scalar calls, pole maps to y = 0"
          if np.all(np.isfinite(k2)) else "k2 = %r" % (k2,))


# --------------------------------------------------------------------------- #
# developability is enforced, not decorative
# --------------------------------------------------------------------------- #
def _flat_solver_calls(frame, u0=0.0):
    """One tiny L-shaped path handed to each solver IN ITS OWN CURRENCY.

    BIRCH'S CURRENCY RULE (R1-2, `723c0b9`) is why this is not one array for
    all three. `frame` says what a path's three columns mean, and a
    NON-DEVELOPABLE collector -- a dome, a bowl, a torus -- reaches the two
    chart-measuring solvers as WORLD xyz in the collector's own frame, not as
    chart `(u, v, h)`. `gcode_to_fibers.apply_sag_approx` is where the real
    pipeline converts (`gcode_to_fibers.py:5060-5069`), and
    `gui_surface.build_surface_preview` does the same for the preview, so a
    fixture that hands them chart columns is testing a call the program never
    makes.

    That is exactly what this helper did until 2026-09-14: `723c0b9` moved the
    dome fixtures to world and missed this one, so both solvers raised
    `ChartShearExcess` -- OFF the pole as readily as on it, which is the tell
    that it was never a pole problem. The exact solver keeps chart currency;
    `apply_sag_approx`'s conversion is deliberately at that seam so it does
    (`gcode_to_fibers.py:5057`)."""
    p = [np.array([[u0, 0.0, 0.0], [u0 + 1.0, 0.0, 0.0],
                   [u0 + 1.0, 1.0, 0.0]])]
    if getattr(frame, "developable", True):
        w = p
    else:
        w = [np.column_stack(frame.to_world(q[:, 0], q[:, 1], q[:, 2]))
             for q in p]
    return (("the exact sag solver",
             lambda: gcode_to_fibers.apply_sag(p, 0.02, frame=frame)),
            ("the approximate sag solver",
             lambda: sag_approx.drape_sag(w, None, 0.02, frame=frame)),
            ("the preview drape",
             lambda: preview_sag.drape_sag(w, None, 0.02, frame=frame)))


def test_a_dome_is_solved_off_the_pole_and_refused_on_it():
    """The five-axis slice 19 contract for the two solvers that still measure
    through the CHART, and slice 2's supersession of it for the one that no
    longer does.

    Until 2026-09-09 all three solvers called `require_developable` and a dome
    was refused everywhere. That was the right refusal for the wrong reason:
    since slice 8 every LENGTH goes through `mew_frame.metric` per point and is
    exact in any chart, so the only raw chart distance left was `_SegIndex`'s
    binning -- and slice 7 measured the frame's own chart inside that index's
    12.5% band by a factor of 8 everywhere except near the axis of revolution.
    So the refusal became a DISC and the surface solvable outside it, for all
    three solvers, until `20260911-132550-arbitrary-collector-surfaces` slice 2
    (2026-09-11): `gcode_to_fibers._SegIndex` stopped binning a doubly-curved
    collector's contacts by the chart at all (`_footprint_frame`, WORLD
    footprint instead) and `rest_heights` measures the exact WORLD distance
    to each candidate (`_world_aug_array`), so `apply_sag` -- "the exact sag
    solver" below -- has no chart accuracy question left to gate and skips
    `require_chart` entirely rather than widen its band
    (`gcode_to_fibers.apply_sag`, the `_footprint_frame(frame) is None` guard).
    `sag_approx.drape_sag` and `preview_sag.drape_sag` still measure in the
    global chart and are untouched -- that is slice 3's, not this one's.

    Both directions are asserted here, on ONE frame, because either alone is
    passable by a mistake: a solver that accepts everything passes the first
    half, and one that still refuses every dome passes the second.
    """
    dome = mew_frame.RevolutionFrame(mew_frame.ArcProfile(35.0), u0=0.0, u1=10.0)
    # ALL THREE accept a dome now, pole included, each in its own currency.
    # Until 2026-09-14 this asserted that the approximate and preview solvers
    # still refused the pole. That expectation was never measuring the pole:
    # the fixture handed those two CHART columns after `723c0b9` moved them to
    # world (see `_flat_solver_calls`), so they refused u = 5 mm just as
    # readily -- a currency error reading as a pole gate. In world currency
    # there is no global-chart singularity left for them to hit.
    refused_off, refused_on = [], []
    # OFF the pole: 5 mm along the meridian, an ordinary place to print.
    for label, call in _flat_solver_calls(dome, u0=5.0):
        try:
            call()
        except Exception as exc:                       # noqa: BLE001
            refused_off.append("%s (%s)" % (label, type(exc).__name__))
    # ON it: the pattern starts at u = 0, which IS the axis.
    for label, call in _flat_solver_calls(dome, u0=0.0):
        try:
            call()
        except Exception as exc:                       # noqa: BLE001
            refused_on.append("%s (%s)" % (label, type(exc).__name__))
    ok = not refused_off and not refused_on
    check("a dome solves off the pole and on it, for all three solvers",
          ok,
          "3 solvers accept u = 5 mm and u = 0, each in its own currency "
          "(exact: chart; approximate and preview: world, per "
          "gcode_to_fibers.apply_sag_approx). Pole exclusion is %.4f mm at "
          "d = 20 um and no longer gates any of them"
          % float(np.max(dome.chart_exclusion(np.array([0.0]), 0.02)))
          if ok else "refused off the pole: %s; refused on it: %s"
                     % (refused_off, refused_on))


def test_CTL_a_developable_frame_is_not_refused():
    """The other direction: the gate must let a cone and a cylinder through."""
    got = []
    for label, frame in (("cylinder", mew_frame.CylinderFrame(3.0)),
                         ("cone", mew_frame.RevolutionFrame(
                             mew_frame.LineProfile(3.0, half_angle=0.2),
                             u0=0.0, u1=9.0)),
                         ("flat", mew_frame.PlanarFrame())):
        try:
            frame.require_developable("a test")
        except mew_frame.NonDevelopableFrame:
            got.append(label)
    check("CTL: the developability gate passes cylinder, cone and plane",
          not got, "3 developable frames allowed"
          if not got else "wrongly refused %s" % (got,))


def test_the_frame_refuses_to_guess_rather_than_answering_wrong():
    """metric_v, period_v and unroll have no honest scalar answer here."""
    dome = mew_frame.RevolutionFrame(mew_frame.ArcProfile(35.0), u0=0.0, u1=10.0)
    cone = mew_frame.RevolutionFrame(
        mew_frame.LineProfile(3.0, half_angle=0.2), u0=0.0, u1=9.0)
    fails = []
    for label, fr in (("dome", dome), ("cone", cone)):
        try:
            fr.metric_v(0.1)
            fails.append("%s.metric_v answered" % label)
        except mew_frame.NonDevelopableFrame:
            pass
        try:
            fr.unroll(1.0, 2.0, 3.0)
            fails.append("%s.unroll answered" % label)
        except NotImplementedError:
            pass
        if fr.period_v is not None:
            fails.append("%s publishes a scalar period_v" % label)
    # ...and the honest per-u query does answer, and varies along a cone.
    a, b = cone.period_v_at(0.0), cone.period_v_at(9.0)
    if not b > a:
        fails.append("period_v_at does not grow along a cone")
    check("the frame refuses metric_v / period_v / unroll rather than guessing",
          not fails, "3 refusals x 2 shapes; cone circumference %.3f -> %.3f mm"
          % (a, b) if not fails else str(fails))


def test_mount_axial_is_the_end_of_the_span_nearest_the_mount():
    """`mount_axial` -- where the collector's BOTTOM EDGE sits along its own
    axis, and therefore where `machine_model.collector_origin` stands the part.

    IT HAD NO GUARD UNTIL 2026-09-10, and that was found by an injection rather
    than by reading: swapping its `min` for a `max` left both
    `test_machine_model` and `test_five_axis_gcode` green, because both use the
    number on BOTH sides of their comparisons, where it cancels. A quantity that
    decides where a part is drawn and cancels out of every check is not
    measured by them.

    The comparator is the profile itself, sampled independently here, and the
    direction is asked SHAPE BY SHAPE rather than by re-running the rule: a
    dome's meridian runs downhill from the pole so the answer is at `u1`, a
    cone's runs uphill from its base so it is at `u0`. Getting the two the same
    way round is the whole content of the function.
    """
    import numpy as _np

    def _extremes(f):
        us = _np.linspace(f.u0, f.u1, 513)
        xs, _rho = f.profile.point(us)
        return float(_np.min(xs)), float(_np.max(xs))

    dome = mew_frame.RevolutionFrame(mew_frame.ArcProfile(10.0), u0=0.0,
                                     u1=12.0)
    lo, hi = _extremes(dome)
    check("a dome seats on the LOW end of its span, not its apex",
          abs(dome.mount_axial() - lo) < 1e-9 and abs(hi - lo) > 1.0,
          "mount_axial %.6f, span x in [%.6f, %.6f]"
          % (dome.mount_axial(), lo, hi))
    check("...and that end is u1, the rim -- the pole is the FAR end",
          abs(dome.mount_axial()
              - float(dome.profile.point(dome.u1)[0])) < 1e-9)

    cone = mew_frame.RevolutionFrame(
        mew_frame.LineProfile(10.0, half_angle=math.radians(20.0)),
        u0=0.0, u1=12.0)
    lo, hi = _extremes(cone)
    check("a cone seats on the LOW end too, which for it is u0",
          abs(cone.mount_axial() - lo) < 1e-9
          and abs(cone.mount_axial()
                  - float(cone.profile.point(cone.u0)[0])) < 1e-9,
          "mount_axial %.6f, span x in [%.6f, %.6f]"
          % (cone.mount_axial(), lo, hi))
    check("...so the two shapes seat on OPPOSITE ends of their own spans, "
          "which one rule has to get right",
          abs(dome.mount_axial() - float(dome.profile.point(dome.u0)[0])) > 1.0
          and abs(cone.mount_axial()
                  - float(cone.profile.point(cone.u1)[0])) > 1.0)

    # A TORUS can take its minimum in the INTERIOR of the span, which is why the
    # rule samples instead of testing the two ends. Built so it does.
    tor = mew_frame.RevolutionFrame(
        mew_frame.ArcProfile(10.0, centre_rho=20.0), u0=25.0, u1=38.0)
    # THE COMPARATOR IS CLOSED FORM, not a second sampling. `ArcProfile(R)` has
    # x(s) = R cos(s/R), whose minimum over a span containing s = pi R is
    # exactly -R -- so the span below straddles it and the true answer is known
    # without sampling anything. Comparing two sampled minima instead would
    # measure the difference between two grids: a 257-point grid over 13 mm
    # sits ~3e-5 mm above a smooth minimum, which is the bound asserted here
    # rather than a tolerance tuned until it passed.
    at_true, x_true = math.pi * 10.0, -10.0
    got = tor.mount_axial()
    check("a torus takes its seat in the INTERIOR of the span, and finds it",
          tor.u0 < at_true < tor.u1
          and x_true <= got < x_true + 1.0e-3,
          "mount_axial %.6f vs the exact %.6f at u = %.4f of [%.1f, %.1f]"
          % (got, x_true, at_true, tor.u0, tor.u1))
    check("...and it is NOT either end of the span, which is what makes "
          "sampling necessary",
          min(abs(got - float(tor.profile.point(tor.u0)[0])),
              abs(got - float(tor.profile.point(tor.u1)[0]))) > 0.1,
          "ends are x = %.4f and %.4f"
          % (float(tor.profile.point(tor.u0)[0]),
             float(tor.profile.point(tor.u1)[0])))

    # `surface_x` shifts the whole collector along its axis and the seat has to
    # go with it -- one term, and the only one that is not the profile's.
    moved = mew_frame.RevolutionFrame(mew_frame.ArcProfile(10.0), u0=0.0,
                                      u1=12.0, surface_x=4.5)
    check("surface_x carries the seat with the collector",
          abs(moved.mount_axial() - (dome.mount_axial() + 4.5)) < 1e-9)

    # WHAT DOES NOT STAND ON A MOUNT answers None rather than a number, because
    # a plate is the machine's body and a rod is gripped at its ends.
    check("a plane and a cylinder do not stand on a mount",
          mew_frame.PlanarFrame().mount_axial() is None
          and mew_frame.CylinderFrame(3.0).mount_axial() is None)



def test_under_nozzle_is_the_vertical_question_and_covers_is_not():
    """`covers` opens a CONE; the nozzle looks straight down.

    `covers` projects a point radially onto the meridian and asks whether that
    lands inside the collector's span. Radially, from the axis -- so the region
    it answers yes for is a cone, and a cone is wider than the collector at
    every height above it. On the dome preset the gap was 4.83 mm of XY
    and 0.49 s of approach: the travel fibre started, and the jet snapped onto
    it, while the nozzle was still outside the collector.

    THE CONTROL IS `covers` ITSELF, on the same point. A nozzle held just
    outside the cap's own footprint and well above it has to read False here
    and True there, or this is measuring nothing.
    """
    R = 10.0
    frame = mew_frame.RevolutionFrame(mew_frame.ArcProfile(R), u0=0.0, u1=12.0)
    x_rim, rho_rim = frame.profile.point(12.0)
    foot = abs(rho_rim)                  # the cap's own footprint radius, mm
    x_pole, _rho = frame.profile.point(0.0)
    # the nozzle, 30 mm above the pole, looking down the axis at the collector
    down = (-1.0, 0.0, 0.0)
    high = x_pole + 30.0

    inside = (high, 0.9 * foot, 0.0)
    outside = (high, 1.1 * foot, 0.0)
    check("a nozzle over the cap has collector under it",
          frame.under_nozzle(inside, down),
          "footprint radius %.3f mm, nozzle at rho %.3f mm"
          % (foot, 0.9 * foot))
    check("...and one just outside the footprint does NOT",
          not frame.under_nozzle(outside, down),
          "nozzle at rho %.3f mm, %.3f mm outside the rim"
          % (1.1 * foot, 0.1 * foot))
    check("CONTROL: `covers` says yes to that same outside point",
          frame.covers(*outside),
          "which is the cone, and is the defect this replaced")
    check("...and with no nozzle axis it IS `covers`, unchanged",
          frame.under_nozzle(outside, None) == frame.covers(*outside))
    # the unbounded collectors answer yes to everything, axis or not
    for f in (mew_frame.PlanarFrame(), mew_frame.CylinderFrame(1.5)):
        check("%s has collector under the nozzle everywhere"
              % type(f).__name__,
              f.under_nozzle((9.0, 9.0, 9.0), down)
              and f.under_nozzle((9.0, 9.0, 9.0), None))


def test_a_touchdown_is_always_ON_the_collector():
    """Whatever the nozzle's angle, the point the fibre is laid at has h = 0.

    `_touchdown` bisects on the SIGN of `_ray_h`, and that sign flips for two
    different reasons: the ray reached the surface, or it crossed the span
    BOUNDARY with the surface already above it -- a nozzle looking past the rim
    edge. In the second case the ray's own stopping point is UNDER the
    collector, and laying fibre there buries it. Measured before the projection
    was added: the five-axis preset's whole lead-in fibre sat 0.169 mm under the
    dome -- eight fibre diameters -- and every suite stayed green, this one
    included, because its own fixture never crossed a rim that way.

    So the second half here is a ray BUILT to cross the boundary from under the
    rim, and it is checked both ways: the raw ray point has to be under the
    surface (or the first half is measuring nothing) and the returned point on
    it.
    """
    R = 10.0
    frame = mew_frame.RevolutionFrame(mew_frame.ArcProfile(R), u0=0.0, u1=12.0)

    # every ray is aimed AT a point on the cap, from a range of angles, so the
    # sweep is about the ANSWER rather than about whether anything was hit
    worst, n = 0.0, 0
    for u in (1.0, 4.0, 8.0, 11.0, 11.9):
        target = np.asarray(frame.to_world(u, 0.0, 0.0), dtype=np.float64)
        nrm = np.asarray(frame.normal(u, 0.0), dtype=np.float64)
        away = np.cross(nrm, np.array([0.0, 0.0, 1.0]))
        if np.linalg.norm(away) < 1e-9:
            away = np.cross(nrm, np.array([0.0, 1.0, 0.0]))
        away = away / np.linalg.norm(away)
        for tilt_deg in (0.0, 15.0, 30.0, 45.0):
            th = math.radians(tilt_deg)
            d = -(math.cos(th) * nrm + math.sin(th) * away)
            p = target - 20.0 * d
            hit = frame._touchdown(p, d)
            if hit is None:
                continue
            n += 1
            _u, _v, h = frame.from_world(*hit[1])
            worst = max(worst, abs(float(h)))
    check("every touchdown lands ON the surface",
          n >= 15 and worst < 1.0e-6,
          "%d rays hit, worst |h| %.2e mm" % (n, worst))

    # the boundary case: a ray that is already INSIDE the sphere, out past the
    # rim, and travels into the span from under it
    a = np.asarray(frame.to_world(17.0, 0.0, 2.0), dtype=np.float64)
    b = np.asarray(frame.to_world(13.0, 0.0, -1.5), dtype=np.float64)
    d = b - a
    d = d / np.linalg.norm(d)
    hit = frame._touchdown(a, d)
    if hit is None:
        bad("a ray crossing the rim from under it lands somewhere", "no hit")
    else:
        s_hit, q = hit
        _u, _v, h = frame.from_world(*q)
        _u2, _v2, raw = frame.from_world(*(a + s_hit * d))
        check("CONTROL: where that ray stops IS under the collector",
              float(raw) < -0.01,
              "raw ray point %.4f mm under the surface -- what would have been "
              "laid" % float(raw))
        check("...and the touchdown returned is on the surface instead",
              abs(float(h)) < 1.0e-6, "|h| %.2e mm" % abs(float(h)))


def main():
    test_closed_form_curvatures_match_finite_differences()
    test_CTL_the_finite_difference_probe_sees_a_planted_error()
    test_the_tangent_is_unit_and_matches_finite_differences()
    test_a_constant_radius_profile_reproduces_CylinderFrame()
    test_the_flat_frame_is_still_the_literal_no_op()
    test_a_sphere_is_isotropic_and_a_torus_is_not()
    test_concave_is_representable_and_flips_both_signs()
    test_a_cone_is_developable_and_a_dome_is_not()
    test_the_normal_is_unit_perpendicular_and_agrees_with_to_world()
    test_outward_is_anchored_on_the_sphere_where_it_is_unambiguous()
    test_the_offset_surface_flattens_as_the_wall_grows()
    test_a_dome_pole_is_umbilic_and_finite()
    test_under_nozzle_is_the_vertical_question_and_covers_is_not()
    test_a_touchdown_is_always_ON_the_collector()
    test_CTL_the_pole_station_probes_the_division_that_failed()
    test_the_pole_places_and_orients_a_point()
    test_a_cone_apex_is_refused_not_answered()
    test_the_pole_survives_being_one_element_of_an_array()
    test_a_dome_is_solved_off_the_pole_and_refused_on_it()
    test_CTL_a_developable_frame_is_not_refused()
    test_the_frame_refuses_to_guess_rather_than_answering_wrong()
    test_mount_axial_is_the_end_of_the_span_nearest_the_mount()
    print("\n%d passed, %d failed, %d total"
          % (len(_PASS), len(_FAIL), len(_PASS) + len(_FAIL)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
