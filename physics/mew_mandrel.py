"""Term A -- the cylindrical placement deviation (Neuhaus 2025 Eqs 1-5).

Neuhaus, Loewner, Heymann, Webering, Synofzik, Blume & Blume, "Fiber deviation
and optimized toolpath strategies in melt electrowriting of tubular scaffolds",
Materials & Design 254 (2025) 114147, doi 10.1016/j.matdes.2025.114147, CC BY.
The model is Neuhaus's, ported and cited, never claimed;
what MEWracle adds is running it inside a deposition model, not beside one.

WHAT IT PREDICTS. On a cylinder the suspended and the deposited fibre are not
collinear seen along the surface normal, so the jet lands off the programmed
path and deflects until collinearity is restored -- outside the toolpath,
against the rotation, >100 um at 6 mm diameter, half a 0.25 mm spacing. The
deviation is unchanged from 0.5 to 4 mm line spacing and vanishes at 0 and 90
degrees of winding, so only the electrostatic pull's DIRECTION enters: no field
solver here, and no sensitivity to voltage.

PORTED FROM Neuhaus's reference implementation, validated against Fig 3 over 18
cases with both degenerate controls swinging to zero.

FRAME (Eq 3), as in `mew_frame.CylinderFrame`: x = mandrel axis, (y, z)
transverse, +z at the nozzle, phi = azimuth from top dead centre. Lengths MM,
angles radians unless a name says _deg.

THE THREE TRAPS -- each gives wrong numbers rather than an error:
  1. `S` IS MEASURED FROM THE MANDREL AXIS, so it tracks the radius at a fixed
     gap: `S = r + 0.75*gap`. Holding Fig 3D's printed "S = 3.8" flat gives
     251 um at r = 2 against a measured ~136, worsening as the mandrel shrinks.
  2. `S` ALSO FALLS WITH PRINT SPEED (`S(v) = 3.8 - 0.002*v`, Neuhaus's own
     best-agreement variant): a constant S over-predicts by ~27% at 100 mm/s.
     That slope is one rig's fit, so it is a Calibration field defaulting to 0.
  3. AT alpha = 90 THE ROOT SITS ON THE DOMAIN EDGE `phi = R_cir/r`, so
     `n_roots == 0` is the CORRECT answer there and must clamp, not raise. At
     alpha = 0 guard on sin(alpha); use `cot = cos/sin`, never 1/tan.

ON FOR EVERY ROTARY FRAME: `placement_for_frame` builds a `ContactPlacement`
whenever the frame is rotary, both lag radii DERIVED (`lag_radii_from_jet`)
unless `Calibration.mandrel_lag_axial/_circ` override the ratio. A flat frame
takes no placement branch at all and is byte-identical to the pre-mandrel code.
"""

import math

import numpy as np

#: Neuhaus's calibrated constants, read from the Fig 3 panel insets. Test
#: fixtures, and the values to type into a Calibration when reproducing the
#: paper -- NOT defaults (they are one rig, PCL Mn 45k, 1 mm gap, 0.25 mm
#: nozzle, 10 kPa, 2 kV, 85 degC, fibre 31 +/- 3 um).
NEUHAUS_FIG3 = {
    #                 R_ax    R_cir   S      r     notes
    "3A": dict(R_ax=0.845, R_cir=0.63, S=3.8,  r=3.0),   # 20 mm/s, winding sweep
    "3B": dict(R_ax=1.47,  R_cir=1.1,  S=3.75, r=3.0),   # 40 mm/s, spacing sweep
    "3D": dict(R_ax=0.91,  R_cir=0.72, S=3.8,  r=3.0),   # 20 mm/s, radius sweep
}

#: Fig 3C panel inset: the fitted jet-lag ellipse radii vs print speed (mm/s).
#: Evaluated at v = 20 these give 0.911 / 0.711, reproducing Fig 3D's stated
#: 0.91 / 0.72 -- a consistency check worth keeping as a regression assertion.
#: `R_ax > R_cir` always: the physical signature of a collector that curves away
#: circumferentially but not axially.
def fig3c_lag_radii(speed):
    """Neuhaus Fig 3C's fitted (R_ax, R_cir) in mm at print speed `speed` mm/s."""
    return (13.25 - 15.1 * speed ** -0.0674,
            4.96 - 6.61 * speed ** -0.1475)


# --------------------------------------------------------------------------- #
# The model -- Neuhaus Eqs 1-5
# --------------------------------------------------------------------------- #
def contact_point(phi, r, R_ax, R_cir):
    """Eq 1 -- the locus of possible contact points.

    An ELLIPSE on the mandrel surface, semi-axes `R_ax` (axial) and `R_cir`
    (circumferential ARC LENGTH). On a flat collector this locus is a CIRCLE of
    radius = the jet lag; the curvature is the whole difference.
    """
    q = min(1.0, max(-1.0, (phi * r) / R_cir))     # clip: phi may reach the edge
    return np.array([R_ax * math.cos(math.asin(q)),
                     r * math.sin(phi),
                     r * math.cos(phi)])


def toolpath_point(phi, r, alpha):
    """Eq 5's `t` -- where the PROGRAMMED path sits at azimuth `phi`.

    Note it shares y and z with `contact_point`: at a given azimuth the two
    differ only AXIALLY, and Eq 5's `sin(alpha)` is what turns that axial
    difference into an offset measured across the winding direction.
    """
    cot = math.cos(alpha) / math.sin(alpha)
    return np.array([phi * r * cot, r * math.sin(phi), r * math.cos(phi)])


def _residual_scalar(phi, r, alpha, S, R_ax, R_cir):
    """Eq 4 -- coplanarity of the winding, normal and jet vectors.

    Its ROOT is the deposition equilibrium: the point where the suspended and
    the deposited fibre are collinear again VIEWED ALONG THE SURFACE NORMAL.
    Only the DIRECTION of the electrostatic pull enters -- never its magnitude.
    """
    c = contact_point(phi, r, R_ax, R_cir)
    j = np.array([0.0, 0.0, S]) - c                                  # Eqs 2-3
    w = np.array([math.cos(alpha),
                  math.sin(alpha) * math.cos(-phi),
                  math.sin(alpha) * math.sin(-phi)])
    n = np.array([0.0, math.sin(phi), math.cos(phi)])
    return float(np.dot(w, np.cross(n, j)))


def _residual_vec(phis, r, alpha, S, R_ax, R_cir):
    """`_residual_scalar` over an array of phi, for the bracketing scan.

    Vectorised because the scan is 4001 points and this is called per toolpath
    segment downstream; the scalar form stays as the readable statement of the
    equation; the two agreed to 1e-12 when last checked.
    """
    phis = np.asarray(phis, dtype=np.float64)
    q = np.clip((phis * r) / R_cir, -1.0, 1.0)
    cx = R_ax * np.cos(np.arcsin(q))
    sp, cp = np.sin(phis), np.cos(phis)
    cy, cz = r * sp, r * cp
    jx, jy, jz = -cx, -cy, S - cz
    wx = np.full_like(phis, math.cos(alpha))
    sa = math.sin(alpha)
    wy, wz = sa * cp, -sa * sp            # cos(-phi), sin(-phi)
    nx = 0.0
    ny, nz = sp, cp
    # n x j
    kx = ny * jz - nz * jy
    ky = nz * jx - nx * jz
    kz = nx * jy - ny * jx
    return wx * kx + wy * ky + wz * kz


def solve_phi(r, alpha, S, R_ax, R_cir, n_scan=4001, n_bisect=90):
    """Bracket-and-bisect over the legal domain `phi` in [0, R_cir/r].

    Returns `(phi, n_roots)`. `n_roots` is returned rather than swallowed so
    trap 3 stays VISIBLE: `n_roots == 0` means the root sits on the domain edge
    and has been clamped, which is the correct answer at alpha = 90, not a
    failure.

    Exactly one root was found at every point swept in the reference
    (alpha 0-90 deg, lag 0.05-1 mm, diameter 4-10^4 mm), so bracket-and-bisect
    suffices. That is EMPIRICAL over that range, not proved.
    """
    hi = R_cir / r
    xs = np.linspace(0.0, hi, n_scan)
    fs = _residual_vec(xs, r, alpha, S, R_ax, R_cir)
    idx = np.nonzero(fs[:-1] * fs[1:] < 0.0)[0]
    if len(idx) == 0:
        return (hi if abs(fs[-1]) < abs(fs[0]) else 0.0), 0
    a, b = xs[idx[0]], xs[idx[0] + 1]
    fa = _residual_scalar(a, r, alpha, S, R_ax, R_cir)
    for _ in range(n_bisect):                     # ~1e-27 mm; cheap, so exact
        m = 0.5 * (a + b)
        fm = _residual_scalar(m, r, alpha, S, R_ax, R_cir)
        if fa * fm <= 0.0:
            b = m
        else:
            a, fa = m, fm
    return 0.5 * (a + b), len(idx)


def deviation(r, alpha_deg, S, R_ax, R_cir):
    """Eq 5 -- fibre deviation from the programmed path, in MILLIMETRES.

    Returns `(D_mm, phi, n_roots)`. `cot` rather than `1/tan` so alpha = 90 is
    exact; alpha = 0 returns early because `sin(alpha)` zeroes Eq 5 anyway.

    Zero jet lag (`R_ax = R_cir = 0`) gives exactly 0 -- the OFF case, and one of
    the two controls that prove this is not a constant-answer instrument.

    **CALLERS MUST CAP UPSTREAM. This is a literal port of Eq 5 and is left
    unclamped on purpose**, so that a check against Fig 3 compares against
    something nobody has modified. Past the tangency bound
    `phi_t = arccos(r/(r+H))` it TURNS OVER BY DESIGN -- D peaks and then falls
    while the lag keeps growing -- because Eq 1's contact locus has left the
    hemisphere facing the nozzle (MANDREL_LAG_DERIVATION.md 7). The deposition
    path never gets there: `cap_circumferential_lag` holds `R_cir` at the bound
    where the radii are FORMED. A hand call with your own radii does not, and
    `deviation_from_calibration` is such a call.
    """
    if R_ax <= 0.0 or R_cir <= 0.0:
        return 0.0, 0.0, 1
    alpha = math.radians(alpha_deg)
    sa = math.sin(alpha)
    if abs(sa) < 1e-12:
        return 0.0, 0.0, 1
    phi, n_roots = solve_phi(r, alpha, S, R_ax, R_cir)
    c = contact_point(phi, r, R_ax, R_cir)
    t = toolpath_point(phi, r, alpha)
    return float(np.linalg.norm(c - t)) * sa, phi, n_roots


def support_length(r, gap, speed=None, frac=0.75, speed_coeff=0.0):
    """`S` -- the support-vector length, from GEOMETRY rather than a fit.

    S is measured from the mandrel AXIS to a virtual point below the nozzle at
    roughly 3/4 of the surface-to-nozzle gap, so it tracks the mandrel radius
    (trap 1). `speed_coeff` is trap 2: Neuhaus's best-agreement variant is
    `-0.002` mm per mm/s, but that is one rig's fit, so it defaults to 0 here
    and lives in the Calibration.

    Neuhaus calls S the model's weak point ("predicting or measuring the length
    S... remains difficult") and fits it -- but `S = r + 0.75*gap - 0.002*v`
    reproduces all four Fig 3 panels, so it may be geometry plus one speed
    coefficient rather than a free parameter. One rig is not two.
    """
    S = r + frac * gap
    if speed is not None and speed_coeff:
        S += speed_coeff * speed
    return S


def tangency_arc(radius, gap):
    """`phi_t = arccos(r/(r+H))`, radians -- the farthest round the mandrel a jet
    from the nozzle can possibly touch.

    A TAUT jet is a straight line from the nozzle, so it meets the cylinder no
    farther round than the tangent point; the field pulls any real jet toward the
    collector, so a curved jet touches SOONER. `phi_t` is therefore a strict
    upper bound on the contact azimuth and needs no parameter and no fit
    (MANDREL_LAG_DERIVATION.md 12.5, derived and checked numerically).

    It supersedes the crude 90 deg reach limit of 5.3, which was an artifact of
    testing one geometry: the shipped `deviation()`'s own turnover sits at
    1.11-1.18 x phi_t across seven published rigs (6.2% spread) against an 86%
    spread when read against 90 deg.
    """
    radius, gap = float(radius), float(gap)
    if radius <= 0.0 or gap <= 0.0:
        return math.pi           # no cylinder, or no gap: nothing to bound
    return math.acos(radius / (radius + gap))


def cap_circumferential_lag(radius, gap, R_cir):
    """`(R_cir_capped, arc_deg, saturated)` -- hold the contact arc at `phi_t`.

    THE ONE PLACE THE BOUND IS APPLIED. `lag_radii_from_jet` and
    `ContactPlacement.resolve` both call this rather than each spelling out the
    arccos, because two copies of it are how they drift apart.

    WHY CLAMP RATHER THAN REFUSE. The saturation is physical -- the contact
    azimuth genuinely cannot pass the tangent point -- so the capped value is an
    upper bound on a real quantity, not a fudge. Refusing would turn a
    thin-mandrel export into a dead worker. Clamping SILENTLY is the other thing
    ruled out: past the bound the unclamped `deviation()` returns numbers that
    move the WRONG WAY (at r = 1.5, H = 3.5 it peaks at 1444.6 um at an 81.2 deg
    arc and then FALLS while the lag keeps growing, 7), and nobody noticed that
    until someone swept it. So the fact travels -- `laid_fibers` puts it in
    `info["lag_arc_saturated"]` and the converter logs it.

    The cost of the clamp is small by construction: `D` at `phi_t` is 97.9% of
    that unclamped peak, which sits just PAST the bound.

    ONLY the circumferential radius is capped. `R_ax` is the along-track trail on
    a surface that does not curve along the direction of travel, so tangency does
    not bound it, and 12.5 forbids subtracting any further margin for the
    curved-jet tightening -- that is real but only bounded to
    `phi_c/phi_t` in 0.13-0.74 by an uncalibrated toy, i.e. UNMEASURED.
    """
    R_cir = float(R_cir)
    if R_cir <= 0.0 or float(radius) <= 0.0:
        return R_cir, 0.0, False
    cap = float(radius) * tangency_arc(radius, gap)
    if R_cir <= cap:
        return R_cir, math.degrees(R_cir / float(radius)), False
    return cap, math.degrees(cap / float(radius)), True


def circumferential_lag(lag_distance, radius, gap):
    """`R_cir` in mm of ARC, UNCAPPED -- the exact form, the paper's section 2.3.6.

    `R_cir = r * (theta - arcsin(r*sin(theta)/(r + H)))`, with
    `tan(theta) = R_ax/H` and every angle in radians.

    WHAT THETA IS. On a flat collector the jet leaves the nozzle and lands
    `R_ax` downstream across a gap `H`, so it arrives at `theta` from the
    collector's normal. The statement this form makes is that the same angle
    survives the curvature: the jet still meets the CYLINDER at `theta` to the
    LOCAL normal, which on a curved surface is no longer the nozzle's radial
    direction. Solving that triangle -- nozzle at `r + H` from the axis,
    contact at `r`, the two normals `phi` apart -- gives
    `(r + H)*sin(theta - phi) = r*sin(theta)`, i.e. the expression above, and
    the arc it subtends is `r*phi`.

    IT SUPERSEDES `R_ax * r/(r+H)`, which was the SMALL-ANGLE reading of the
    same picture: the two agree to first order in theta and part company as the
    lag grows past the gap. At r = 3, H = 1, R_ax = 0.845 this returns 0.589
    where the ratio form returned 0.634.

    THE PLANAR LIMIT IS EXACT AND UNCHANGED. `theta - arcsin(sin(theta)/(1 +
    H/r)) -> tan(theta)*H/r` as `r -> inf`, so `r*phi -> H*tan(theta) = R_ax`:
    the ellipse becomes Neuhaus's planar circle, which is the one property
    `r/(r+H)` was kept for in the first place.

    IT IS SELF-BOUNDING, which the ratio form was not. As `theta -> 90 deg`
    the arc tends to `r*arccos(r/(r+H))` -- exactly `tangency_arc`, the
    farthest round the mandrel a straight jet can reach -- from BELOW and
    without ever touching it. So `cap_circumferential_lag` can no longer fire
    on this path, and the `info["lag_arc_saturated"]` channel it feeds now
    stays quiet for a derived R_cir. The cap call is kept because a CALIBRATED
    ratio (`Calibration.mandrel_lag_circ/_axial`) still reaches the same
    formation sites and is still unbounded.

    THE COST, stated because it is the reason to look twice. Against Neuhaus
    Fig 3C's fitted radii over 20-100 mm/s at r = 3, H = 1 this is **20.7%
    RMS** where `R_ax*r/(r+H)` was **2.0%**, and it is low on all three Fig 3
    panels (-6.5% / -16.9% / -12.8% on 3A / 3B / 3D). End to end the 45 deg
    deviation moves less -- 3.5% and 6.8% low on 3A and 3D against Neuhaus's
    own measured pair -- because `deviation()` is much flatter in R_cir than
    R_cir is in R_ax. Either the constant-normal-angle premise is not what sets
    the landing azimuth, or Neuhaus's independently fitted R_cir absorbs
    something this geometry does not carry; nothing here settles which.

    WHY THE AGREEMENT IS SPENT ANYWAY. `R_ax*r/(r+H)` is the FIRST-ORDER term
    of this expression, and theta is nowhere small in melt electrowriting: over
    Fig 3's own cases it runs 40.2 deg (3A), 42.3 deg (3D), 55.8 deg (3B) and
    65.4 deg at 100 mm/s, where the ratio form stands +7.6%, +8.8%, +20.7% and
    +39.5% above the exact arc. So the ratio form is never evaluated inside its
    own regime, and its error grows fastest exactly where the published fit
    likes it best. An approximation with no valid range in the geometry it
    ships for is not defensible on its agreement, whatever that agreement is --
    which is the decision, and it is a decision about the PREMISE,
    not about the residual above.
    """
    R_ax, r, H = float(lag_distance), float(radius), float(gap)
    if R_ax <= 0.0 or r <= 0.0 or H <= 0.0:
        return R_ax
    theta = math.atan2(R_ax, H)
    # r*sin(theta) < r < r + H, so the arcsin is always in domain; the clip is
    # against float64 only, never against a real geometry.
    q = min(1.0, max(-1.0, r * math.sin(theta) / (r + H)))
    return r * (theta - math.asin(q))


def lag_radii_from_jet(lag_distance, radius, gap, model=None):
    """(R_ax, R_cir) from the FLAT jet lag plus geometry.

    **R_ax is the flat-collector lag.** Axially the collector does not curve
    along the direction of travel, so pass `lag_distance` straight from
    `MEWModel.solve(...).lag_distance`; do NOT reimplement a lag law here,
    because two lag laws in one codebase is how they drift apart.

    **R_cir is `circumferential_lag`** -- read that docstring for the form, the
    planar limit, the self-bounding property and what it costs against Fig 3C.
    Both are parameter-free.

    TWO LIMITS this function cannot fix, and the first one CHANGED SHAPE when
    the exact form replaced the ratio. The r-dependence is still validated at
    ONE radius (Neuhaus measured the ellipse at r = 3 mm only) and Fig 3D's
    radius sweep still mildly disfavours ANY r-dependence; what is new is that
    the exact form no longer reproduces the fitted radii themselves, so "the
    ratio is right at r = 3" is no longer available as a defence either. And
    our flat lag law caps at 1.148*H against Hrynevich's 2.86*H asymptote and
    Neuhaus's measured 2.18*H at 100 mm/s, running up to 31% under the curve it
    was fitted to beyond ~6*CTS; term A inherits that ceiling, and fixing it in
    `mew_physics` would improve term A for free.
    """
    R_ax = float(lag_distance)
    # `model` is the seam for a user-replaced R_cir law (`MEWModel.
    # circumferential_lag`); without one the shipped Eq. 7 answers directly.
    R_cir = (circumferential_lag(R_ax, radius, gap) if model is None
             else model.circumferential_lag(R_ax, radius, gap))
    # The contact azimuth cannot pass the tangent point -- see
    # `cap_circumferential_lag`. `circumferential_lag` already respects that
    # bound by construction, so this call is a no-op on the derived path and is
    # kept only so the two formation sites stay spelled the same way; the other
    # is `ContactPlacement.resolve`, which a CALIBRATED ratio still reaches.
    return R_ax, cap_circumferential_lag(radius, gap, R_cir)[0]


# --------------------------------------------------------------------------- #
# 5-axis (doubly-curved collector) generalisations -- CURVATURE variables.
#
# WIRED IN through `CurvedPlacement` and hence
# `placement_for_frame` -- but only for a frame that answers `curvatures_at`,
# i.e. the five-axis family. `lag_radii_from_jet`, `cap_circumferential_lag`
# and `ContactPlacement` are untouched, so every 4-axis byte is untouched with
# them. Each function is the same geometry as its 4-axis counterpart above,
# rewritten so a caller with two principal curvatures (k1, k2) instead of one
# radius can use it per direction on a general (5-axis) doubly-curved patch;
# `lag_arc_from_curvature` is `circumferential_lag`'s exact arc, rearranged
# only for float64 near k = 0 and extended to the concave sign.
# Nothing here is a new law -- MANDREL_LAG_DERIVATION.md 5.1, 12.5 and 12.9
# are the authority, and 12.9's "frozen" decision is the do-not-re-derive
# boundary: this section only changes the variable (curvature k = 1/radius
# instead of radius), never the expression.
# --------------------------------------------------------------------------- #
#: How close to the pole a CONCAVE patch may be evaluated, as a fraction of
#: `k*gap = -1`. `lag_radii_from_curvature` raises past
#: `k*gap <= -CONCAVE_POLE_FRACTION` and names the number it refused on; a
#: caller that must proceed there decides its own fallback.
#:
#: WHAT THE POLE IS, and it is not a numerical artifact. On a concave patch of
#: radius `R = 1/|k|` the nozzle sits INSIDE the curvature, at `R - gap` from
#: the centre, so 5.1's projection identity reads `R_2 = R_1 * R/(R - gap) =
#: R_1/(1 + k*gap)` with k < 0 -- the same expression, the same derivation,
#: the sign of k doing all the work. It diverges exactly at `gap = R`, the
#: nozzle AT the centre of curvature, where a trail of any length subtends no
#: angle at the nozzle and the arc it maps to on the collector is unbounded.
#: There is no version of this law that survives its own pole, so the only
#: question is where short of it to stop.
#:
#: WHY A HALF. It is a DECLARED regime, not a measured one: nothing published
#: measures jet lag on a concave collector at all (five-axis spec 8), so no
#: data can place this bound and a number that pretends otherwise would be
#: worse than an arbitrary one. What a half buys, stated so it can be argued
#: with: the factor reaches 2.0 there -- the lag doubles -- and its
#: sensitivity `d(factor)/d(k*gap) = factor^2` reaches 4, so a 10% error in
#: the gap or in the local curvature is still at most a 20% error in the lag.
#: Past it the error grows faster than the number does, which is where a
#: prediction stops being worth showing.
#:
#: NOT BORROWED FROM THE CONVEX BOUND. The convex side has an independent
#: admissibility limit at `k*gap < 1/A ~= 0.349` (MANDREL_LAG_DERIVATION.md
#: 5.3: the flat asymptote has to fit on the collector), and reusing 0.349
#: here would look better sourced than it is -- its mechanism is a REACH
#: CEILING, which is exactly the thing a concave surface does not have
#: (`tangency_arc_from_curvature` returns inf for k < 0, and for the same
#: reason: the surface curves toward the nozzle). Borrowing a number across
#: mechanisms is how an unmeasured bound acquires false authority.
CONCAVE_POLE_FRACTION = 0.5


def lag_arc_from_curvature(lag_distance, k, gap):
    """`(R, held)` -- `circumferential_lag`'s exact arc on ONE principal
    direction of curvature `k`, and whether a concave patch held it at its
    reach bound.

    SAME TRIANGLE, EITHER SIGN. `circumferential_lag` solves the nozzle at
    `r + H` from the axis, the contact at `r`, and the jet meeting the surface
    at the flat-collector angle `theta` (`tan theta = L/H`) to the LOCAL
    normal: `R = r*(theta - asin(r*sin(theta)/(r + H)))`. With `r = 1/k` that
    expression is also the concave answer when `k < 0` -- the nozzle then sits
    at `|r| - H` from the centre of curvature, inside it, and the law of sines
    on the same three points gives `|r|*(asin(|r|*sin(theta)/(|r| - H)) -
    theta)`, which is the signed form term for term. k = 0 is the flat lag.

    COMPUTED WITHOUT THE CANCELLATION. The literal form subtracts two angles
    that agree to `k*H` of each other, so at a nearly flat direction (a dome's
    pole, the `k = 1e-9` a test uses to stay off the cylinder delegate) it
    keeps only `-log10(k*H)` fewer digits and then multiplies the loss by
    `1/k`. Written as one arcsine, with `c = 1/(1 + k*H)` and `1 - c` formed
    as `k*H/(1 + k*H)`,

        sin(phi) = sin(theta) * (1 - c)(1 + c) / (sqrt(1 - (c sin theta)^2)
                                                  + c cos theta),

    and `R = phi/k`, which tends to `H tan(theta) = L` exactly as k -> 0.

    CONVEX NEVER HOLDS: `c*sin(theta) < 1` for every k > 0, and the arc tends
    to `tangency_arc_from_curvature` from below without touching it, which is
    `circumferential_lag`'s self-bounding property carried over.

    CONCAVE CAN RUN OUT OF TRIANGLE. From inside the curvature the steepest
    incidence any jet can make with the surface normal is `asin(1 + k*H)`
    (the ray perpendicular to the nozzle's own radius). A flat lag whose
    `theta` is steeper has no landing point at that angle, so the premise the
    exact form rests on -- the flat angle survives the curvature -- cannot be
    carried there. The arc is HELD at the value it reaches at that incidence,
    `(1/|k|)*acos(1 + k*H)`, continuous with the form below it and monotone,
    and `held` is True so the caller can report an upper bound rather than a
    prediction (`CurvedPlacement` routes it into `saturation`). At the
    admitted edge `k*H = -CONCAVE_POLE_FRACTION` that is `theta` past 30 deg,
    i.e. `L > 0.58*H`.

    Raises past `CONCAVE_POLE_FRACTION` exactly as `lag_radii_from_curvature`
    always has; the exact form is finite up to the pole, but the regime is a
    declared one and moving it is not this function's decision.
    """
    L, k, H = float(lag_distance), float(k), float(gap)
    kh = k * H
    if kh <= -CONCAVE_POLE_FRACTION:
        raise ValueError(
            "lag_arc_from_curvature: k=%r with gap=%r gives k*gap=%r, "
            "at or past %r of the pole at k*gap = -1 -- the nozzle would "
            "sit %.1f%% of the way from a concave surface to its centre "
            "of curvature (radius %r mm), where the projection factor is "
            "%r and diverging. Nothing published measures lag on a "
            "concave collector at all, so this refuses rather than "
            "extrapolating; a caller that must proceed there decides its "
            "own fallback (see CONCAVE_POLE_FRACTION)."
            % (k, H, kh, CONCAVE_POLE_FRACTION, 100.0 * min(-kh, 9.99),
               1.0 / abs(k), (1.0 / (1.0 + kh)) if kh != -1.0 else math.inf))
    if L <= 0.0 or H <= 0.0 or k == 0.0:
        return L, False
    theta = math.atan2(L, H)
    s, co = math.sin(theta), math.cos(theta)
    c = 1.0 / (1.0 + kh)
    disc = 1.0 - (c * s) ** 2
    if disc <= 0.0:
        # concave only: the incidence is past the steepest one the patch
        # offers. `(1/|k|)*acos(1 - x)` written as `2*asin(sqrt(x/2))` for the
        # same float64 reason `tangency_arc_from_curvature` gives.
        return (1.0 / -k) * 2.0 * math.asin(math.sqrt(0.5 * -kh)), True
    one_minus_c = kh / (1.0 + kh)
    sin_phi = s * one_minus_c * (1.0 + c) / (math.sqrt(disc) + c * co)
    return math.asin(max(-1.0, min(1.0, sin_phi))) / k, False


def lag_radii_from_curvature(lag_distance, k1, k2, gap):
    """(R1, R2) -- one lag semi-axis per principal direction, each
    `lag_arc_from_curvature` of that direction's own curvature.

    THE SAME LAW AS THE CYLINDER PATH. `lag_radii_from_jet` carries
    `circumferential_lag`'s exact arc (the paper's section 2.3.6), and this is that arc in
    curvature variables, so a cylinder (k1 = 0 along the axis, k2 = 1/r round
    it) reproduces `lag_radii_from_jet(lag_distance, r, gap)` to 1e-12
    relative. It does not use the small-angle projection identity
    `L/(1 + k*gap)`, which stands 0.4-11.5% above the exact arc on a cylinder.
    That identity is the exact arc's first-order term in `L/H`, which is why
    every sign and limit below matches it.

    NOT BIT-IDENTICAL to `lag_radii_from_jet`: the arithmetic is rearranged to
    stay accurate near k = 0 (see `lag_arc_from_curvature`), so agreement is
    to a relative tolerance, never bitwise.

    LIMITS this function must reduce to:
      - FLAT: k1 = k2 = 0 gives `R1 = R2 = lag_distance` exactly.
      - CYLINDER: k1 = 0, k2 = 1/r reproduces `lag_radii_from_jet` as above,
        at any (r, gap) -- the exact arc never reaches the tangency bound, so
        no cap is involved on this path.
      - SPHERE: k1 == k2 makes the pair ISOTROPIC (both semi-axes equal),
        which is degenerate with a single global `Calibration.lag_amplitude`
        fit from ONE print (12.9's "as built" k) -- a sphere alone cannot
        distinguish "the anisotropy factor is isotropic here" from "there is
        no anisotropy factor and amplitude absorbed all of it". Separating
        them needs a doubly-curved patch with k1 != k2.

    THE r-DEPENDENCE IS DISFAVOURED; KEPT ONLY FOR THE PLANAR LIMIT. Neuhaus's
    raw radius-sweep prints (r in [2, 3] mm) rank this factor's r-dependence
    LAST of three hypotheses (MANDREL_LAG_DERIVATION.md 11, item 2), and the exact
    arc is 20.7% RMS against Fig 3C's fitted radii where the retired ratio was 2.0% (`circumferential_lag`'s docstring). It
    ships because it is parameter-free, has the correct planar limit, and is
    the form the cylinder path ships; this function inherits every caveat.

    CONCAVE (k < 0) -- ADMITTED, BOUNDED, AND EVERY NUMBER IS A PREDICTION.
    Concave is in v1 so the model can be compared against
    Terranova, whose validation collector carries a concave region
    and is the only external comparator this model has. A concave direction
    returns a LONGER arc than the flat lag; past the steepest incidence the
    patch allows it is held at its reach bound (`lag_arc_from_curvature`),
    and `k*gap <= -CONCAVE_POLE_FRACTION` raises. Nothing published measures
    jet lag on a concave collector at all (five-axis spec 8), so an admitted
    concave number is a PREDICTION and the software must label it one
    wherever it is shown -- `CurvedPlacement.concave` is how that fact travels
    out of a run, the same way `saturation` carries the tangency clamp.
    """
    return (lag_arc_from_curvature(lag_distance, k1, gap)[0],
            lag_arc_from_curvature(lag_distance, k2, gap)[0])


def tangency_arc_from_curvature(k, gap):
    """The tangency-bound TRAIL LENGTH, `(1/k) * arccos(1/(1+k*gap))` mm --
    `tangency_arc`'s bound written in curvature variables, and already
    multiplied through by the radius (this is the LENGTH `cap` that
    `cap_circumferential_lag` computes as `radius * tangency_arc(radius,
    gap)`, not the angle `phi_t` alone -- MULTIPLY the return value by `k`
    for the angle in radians, as `cap_lag_radius_by_curvature` below does.
    Dividing is the plausible-looking mistake and is wrong by `r^2` on a
    cylinder, where `k = 1/r`.

    SAME IDENTITY AS `tangency_arc`. `radius * arccos(radius/(radius+gap)) ==
    (1/k) * arccos(1/(1+k*gap))` at `k = 1/radius` (MANDREL_LAG_DERIVATION.md
    12.5); this is not a new bound, only the curvature-variable form of the
    same one, so a doubly-curved 5-axis patch can apply it per principal
    direction with that direction's own normal curvature.

    FLAT LIMIT: as k -> 0+ this diverges (`sqrt(2*gap/k) -> inf`), matching
    12.5's stated `r*phi_t -> sqrt(2rH)` as the collector flattens -- a flat
    collector has no reachability limit.

    AND IT IS COMPUTED AS `2*asin(sqrt(x/2))`, NOT `acos(1-x)`, BECAUSE THE
    LITERAL FORM FAILS THAT LIMIT IN FLOAT64 -- backwards, and silently. With
    `x = k*gap/(1 + k*gap)`, `acos(1/(1+k*gap))` is `acos` of a number
    approaching 1 from below, where the derivative is `1/sqrt(2x)` and every
    ulp of the argument becomes `sqrt(2*eps)` of the answer: measured
    against the identity above, the literal form is 0.13% out at
    `k = 1e-14`, 12.6% out at `1e-16`, and returns EXACTLY 0.0 at `1e-18`,
    where `1 + k*gap` rounds to 1 and `acos(1.0)` is 0. Zero is not a large
    bound, it is the tightest possible one -- a nearly flat direction would
    have had its lag clamped to nothing, which is the opposite of "a flat
    collector has no reachability limit" and would have been a plausible small
    number rather than an error. The two forms agree to 2.2e-16 relative at
    every rig in 12.5's seven-rig table, so this is a repair at the limit and
    not a change to any geometry anyone has printed.

    k <= 0 (FLAT OR CONCAVE): returns `math.inf`. Flat (k=0) is the divergent
    limit above, evaluated exactly rather than approached. Concave (k<0) has no
    tangency horizon by a different mechanism -- the surface curves TOWARD the
    nozzle rather than away, so there is no azimuth past which the surface
    normal turns away from it. No published rig has a concave collector -- this
    is outside every measured regime, same caveat as
    `lag_radii_from_curvature`'s.

    WHY A FLOAT AND NOT A SENTINEL STRING. This returned
    the string `"unbounded"` until nothing called it yet made the switch free.
    `math.inf` is the right shape because the ONLY thing a caller does with
    this value is compare a candidate radius against it -- `R <= inf` is
    already True for every finite R, so an unbounded direction needs no branch
    at all to come out uncapped. A string forces `if cap == "unbounded"` at
    every call site before any arithmetic, and a caller that forgets gets a
    TypeError deep inside a comparison rather than the correct answer. The one
    thing that still branches is REPORTING (`cap_lag_radius_by_curvature`'s
    `arc_deg`), which is a display decision, not a clamp.
    """
    k = float(k)
    gap = float(gap)
    if k <= 0.0:
        return math.inf
    if gap <= 0.0:
        return 0.0            # no gap: nothing to bound
    x = (k * gap) / (1.0 + k * gap)          # = 1 - 1/(1 + k*gap), stably
    return (1.0 / k) * 2.0 * math.asin(math.sqrt(0.5 * x))


def cap_lag_radius_by_curvature(k, gap, R):
    """`(R_capped, arc_deg, saturated)` -- `cap_circumferential_lag`'s clamp,
    generalised to a single semi-axis on curvature `k`.

    `cap_circumferential_lag` states explicitly that "only R_cir is capped;
    the along-track R_ax is on a surface that does not curve along the
    direction of travel" -- true on a cylinder, FALSE on a general
    doubly-curved 5-axis patch, where the along-track direction can have its
    own nonzero normal curvature too. So this function is direction-agnostic:
    call it once per semi-axis with THAT direction's own k, rather than
    reaching for the 4-axis "only one direction is capped" rule.

    `arc_deg` is `None`, not a number, when k <= 0 -- see
    `tangency_arc_from_curvature`: a flat or concave direction has no
    tangency horizon, so there is no bound to report an angle for, and
    `saturated` is always `False` in that case. Note this is the ONLY branch
    the infinite bound needs: the clamp itself (`R <= cap`) is already correct
    for `cap = inf` without one, and the branch exists so the REPORTED angle
    stays `None` instead of becoming a meaningless 0 deg (k=0) or a negative
    one (k<0).
    """
    R = float(R)
    k = float(k)
    if R <= 0.0:
        return R, 0.0, False
    cap = tangency_arc_from_curvature(k, gap)
    if not math.isfinite(cap):
        return R, None, False
    if R <= cap:
        return R, math.degrees(R * k), False
    return cap, math.degrees(cap * k), True


# --------------------------------------------------------------------------- #
# The Calibration seam
# --------------------------------------------------------------------------- #
def deviation_from_calibration(cal, r, alpha_deg, gap, speed=None):
    """The deviation in mm for a `mew_physics.Calibration`, or 0.0 when OFF.

    OFF is the default and is EXACT, not small: with `mandrel_lag_axial` /
    `mandrel_lag_circ` unset the jet has no lag, and zero lag gives identically
    zero deviation. `S` comes from `support_length` unless the calibration
    overrides it with a fitted scalar.

    **This is the STANDALONE Neuhaus evaluator, not the deposition path, and it
    is the one caller that reads the two radii as ABSOLUTE lengths.** That is
    what makes it a faithful port of Eqs 1-5 and it is why the Fig 3 fixture can
    quote his numbers directly. A print goes through
    `ContactPlacement` instead, which takes the lag from the jet model per speed
    and reads only the RATIO of these two -- see the note above them in
    `mew_physics.Calibration`. Two semantics for one pair of fields; check which
    one you are in before comparing numbers.
    """
    R_ax = getattr(cal, "mandrel_lag_axial", None)
    R_cir = getattr(cal, "mandrel_lag_circ", None)
    if not R_ax or not R_cir:
        return 0.0
    S = getattr(cal, "mandrel_support_mm", None)
    if S is None:
        S = support_length(
            r, gap, speed=speed,
            frac=getattr(cal, "mandrel_support_frac", 0.75),
            speed_coeff=getattr(cal, "mandrel_support_speed_coeff", 0.0))
    return deviation(r, alpha_deg, S, R_ax, R_cir)[0]


# --------------------------------------------------------------------------- #
# The contact PLACEMENT -- how Eq 1-5 and the deposition tracker compose
# --------------------------------------------------------------------------- #

# Eq 1's c(phi), unrolled to
# (u = axial, v = arc), is EXACTLY the ellipse (u/R_ax)^2 + (v/R_cir)^2 = 1
# about the nozzle -- the locus the contact tracker's steady state already
# traces.  Eq 4 and the tracker are rival selection rules on ONE locus, not two
# effects, so ADDING Eq 5's deviation to a tracker that already leans sideways
# double-counts: at alpha = 45 the two cross-track answers are 107.5 and 84.9 um
# with the SAME sign, against a measured 87.  The composition that works
# decomposes about the direction of travel -- ALONG-track to the tracker's
# scalar lag, CROSS-track to a lateral PRE-OFFSET of the nozzle path -- and so
# `mew_physics.integrate_contact_path_2nd` needs no change at all.  SIGN TRAP:
# this is the PREDICTION (where the fibre lands, +D); Neuhaus's Strategy-1
# COMPENSATION, which a generator would apply, is -D.  Do not swap them.


def unrolled_contact(alpha_deg, r, S, R_ax, R_cir):
    """Eq 4's equilibrium contact, unrolled about the nozzle: `(u, v) >= 0` mm.

    `u` is axial and `v` is circumferential ARC LENGTH, both measured from the
    nozzle's projection on the surface, in the quadrant Neuhaus parameterises
    (his travel direction is `-(cos alpha, sin alpha)`, so his contact is at
    positive u and v).  `ContactPlacement` maps that quadrant onto an arbitrary
    heading with the cylinder's two mirror symmetries.

    Zero lag gives (0, 0) exactly -- the OFF case, and the reason mandrel mode
    costs nothing when the placement model is not calibrated.

    NB no special case at alpha = 0: `_residual_vec` carries `sin(alpha)` and
    `cos(alpha)`, never Eq 3's `cot`, so the scan is well-behaved there and
    returns phi = 0, i.e. a pure axial trail of R_ax.  Eq 3's divergence lives
    in `toolpath_point`, which this function does not call.
    """
    if R_ax <= 0.0 or R_cir <= 0.0:
        return 0.0, 0.0
    phi, _n_roots = solve_phi(r, math.radians(alpha_deg), S, R_ax, R_cir)
    q = min(1.0, max(-1.0, (phi * r) / R_cir))
    return R_ax * math.cos(math.asin(q)), r * phi


class ContactPlacement(object):
    """Where the jet's contact sits relative to the nozzle, on a cylinder.

    One call answers both halves of the Q10 decomposition for a heading:

        trail_mm     how far the contact lags ALONG the direction of travel;
                     feed the tracker `tau = trail_mm / v` and it lands there.
                     On a mandrel it falls toward `R_cir` as the winding angle
                     approaches 90 deg, where an isotropic tracker is 34% long.
        off_u/off_v  the lateral PRE-OFFSET to add to the nozzle path, i.e. Eq
                     5's deviation as a signed vector in the unrolled frame.

    THE TWO SYMMETRIES, and why there is no sign hunt.  The cylinder plus a
    fixed nozzle is symmetric under `u -> -u` and under `v -> -v`.  Neuhaus
    solves travel `-(cos alpha, sin alpha)` and finds the contact at
    `(+u_c, +v_c)`, so for ANY heading it is at
    `(-sign(d_u)*u_c, -sign(d_v)*v_c)` with alpha the angle between travel and
    the axis.  Reversing the rotation flips only `v`, which reproduces Neuhaus's
    own observed alternating line spacing for free.

    R_ax IS the flat jet lag, so it is passed in per call
    rather than stored -- it varies with speed along the path.  `lag_ratio` is
    a MEASURED R_cir/R_ax or None; with None the circumferential semi-axis
    comes from `circ_radius`, i.e. `circumferential_lag`'s exact arc, which is
    not a ratio.  Its r-dependence is Q7's weakest link, which is why this
    decomposition puts it on the ALONG-track half (whole effect <= 34%) rather
    than the cross-track half.  OFF is structural, not a flag.
    """

    def __init__(self, radius, gap, lag_ratio=None, support_mm=None,
                 support_frac=0.75, support_speed_coeff=0.0,
                 cache_deg=0.01, per_layer_registration=False,
                 circ_law=None):
        if not radius > 0.0:
            raise ValueError("mandrel radius must be positive, got %r" % (radius,))
        self.radius = float(radius)
        self.gap = float(gap)
        #: `R_cir/R_ax` when a rig MEASURED the pair, else **None**.
        #: None is the derived path, and it is None rather than a number
        #: because `circumferential_lag` is not a ratio: the exact form is
        #: non-linear in `R_ax`, so no single scalar stands for it. Ask
        #: `circ_radius(lag_mm)` instead of multiplying by this.
        self._lag_ratio_explicit = lag_ratio is not None
        self.lag_ratio = float(lag_ratio) if lag_ratio is not None else None
        self.support_mm = None if support_mm is None else float(support_mm)
        self.support_frac = float(support_frac)
        self.support_speed_coeff = float(support_speed_coeff)
        #: `f(R_ax, r, H) -> R_cir` when the user replaced the R_cir law
        #: (`MEWModel.circumferential_lag`, handed in by `placement_for_frame`),
        #: else None and `circumferential_lag` answers. A measured ratio still
        #: wins over it -- see `circ_radius`.
        self.circ_law = circ_law
        #: alpha quantum for the solve cache.  Eq 4 is a 4001-point scan plus 90
        #: bisections, far too slow to run per toolpath SEGMENT, and a curving
        #: path presents a continuum of headings -- without a quantum every
        #: sample misses.  D is smooth in alpha and zero at both ends, so the
        #: quantum is cheap: 0.01 deg bounds the table at 9000 entries per
        #: (lag, S) -- a single-feed print has one of each -- and costs at most
        #: 0.07 um of deviation, measured across the steps rather than on them
        #: (stepping by 0.0137 deg: a check that samples only exact multiples of
        #: the quantum is blind to it).
        self.cache_deg = float(cache_deg)
        #: Composition across LAYERS, not geometry of one layer -- see
        #: `Calibration.mandrel_per_layer_registration`. False (the default) says
        #: this placement is the whole scaffold's: the caller evaluates it once
        #: at the bare radius and never promotes a heading or re-derives `r`.
        #: The flag is carried HERE, on the object the deposition loop already
        #: holds, so the loop needs no extra argument and a hand-built placement
        #: defaults to the shipped behaviour.
        self.per_layer_registration = bool(per_layer_registration)
        #: None, or the record of the contact arc having SATURATED at the
        #: tangency bound during this run: `{"arc_deg", "min_speed", "n"}`.
        #: Accumulated here because `resolve` is the only place that sees both
        #: the clamp and the speed that caused it, and because the object is
        #: already the one thing `laid_fibers` holds for the whole export --
        #: which is how the fact reaches `info["lag_arc_saturated"]` and the log
        #: instead of being clamped in silence (`cap_circumferential_lag`).
        self.saturation = None
        #: A placement made by `at_surface_radius` reports its saturation UP to
        #: the one the caller holds -- otherwise the per-layer registration hook
        #: would clamp on a derived object nobody reads, and the log would go
        #: quiet exactly when the wall is thickest.
        self._sat_owner = None
        self._cache = {}
        self._surface_cache = {}

    def at_surface_radius(self, radius):
        """Placement on a parallel cylindrical deposition surface.

        **Only the per-layer registration HOOK reaches this from the deposition
        loop** (`Calibration.mandrel_per_layer_registration`, off by default,
        MANDREL_LAG_DERIVATION.md §12.8): the shipped default places every layer
        at the bare radius, so `mew_deposition` never calls it.

        ``CylinderFrame`` keeps coordinates unrolled at the bare radius, while a
        later fibre lands on the wall at ``R0 + h``.  Neuhaus's ``r`` is that
        local surface radius, entering both the support distance and the
        circumferential lag radius, so the bare surface reuses this instance and
        anything else caches an equivalent local model.  An explicitly
        calibrated R_cir/R_ax ratio survives; the geometric default is
        re-derived at the local radius.

        **The gap is passed through UNCHANGED, and that is not an oversight.**
        `generate_mandrel_gcode` commands layer `p` at `start_z + p*step` (step
        default the fibre diameter) over a Z datum at the BARE surface, so the
        nozzle steps OUT with the wall: the physical gap is CONSTANT, only `r` is
        layer-dependent, and `mew_frame.gap_at`'s `nominal - h` (no callers)
        models a fixed nozzle rather than this generator's lifting one.
        Shrinking the gap here was measured to make the per-layer
        rotational walk WORSE, +5.8/+11.3/+16.7 um -> +6.8/+13.3/+19.9 um over
        layers 1-3.
        """
        radius = float(radius)
        if abs(radius - self.radius) <= 1e-12:
            return self
        key = round(radius, 12)
        hit = self._surface_cache.get(key)
        if hit is None:
            hit = ContactPlacement(
                radius, self.gap,
                lag_ratio=(self.lag_ratio if self._lag_ratio_explicit else None),
                support_mm=self.support_mm, support_frac=self.support_frac,
                support_speed_coeff=self.support_speed_coeff,
                cache_deg=self.cache_deg,
                per_layer_registration=self.per_layer_registration,
                circ_law=self.circ_law)
            hit._sat_owner = self
            self._surface_cache[key] = hit
        return hit

    # -- the lag ellipse ---------------------------------------------------- #
    def circ_radius(self, lag_mm):
        """The UNCAPPED circumferential semi-axis for a flat lag `lag_mm`.

        THE ONE PLACE the two semantics of `mandrel_lag_axial/_circ` meet on
        this path. A calibrated pair is a RATIO applied to the jet model's lag
        (the amplitude is `Calibration.lag_amplitude`'s job -- see
        `placement_for_frame`); with no pair the geometry answers, and the
        geometry is `circumferential_lag`, not a multiplication -- or the
        user's own R_cir law (`circ_law`), which takes the geometry's place and
        never the measured ratio's.
        """
        lag_mm = float(lag_mm)
        if self._lag_ratio_explicit:
            return lag_mm * self.lag_ratio
        if self.circ_law is not None:
            return float(self.circ_law(lag_mm, self.radius, self.gap))
        return circumferential_lag(lag_mm, self.radius, self.gap)

    # -- the support vector ------------------------------------------------ #
    def support(self, speed=None):
        """`S`, measured from the mandrel AXIS.  See `support_length`'s trap 1."""
        if self.support_mm is not None:
            return self.support_mm
        return support_length(self.radius, self.gap, speed=speed,
                              frac=self.support_frac,
                              speed_coeff=self.support_speed_coeff)

    def _record_saturation(self, arc_deg, speed):
        """Remember that the arc was clamped, and the SLOWEST speed at which it
        was -- the number a user needs to know how far into the print's speed
        range the model stopped predicting and started bounding."""
        owner = self
        while owner._sat_owner is not None:
            owner = owner._sat_owner
        if owner is not self:
            owner._record_saturation(arc_deg, speed)
            return
        rec = self.saturation
        if rec is None:
            self.saturation = {"arc_deg": arc_deg, "min_speed": speed, "n": 1}
            return
        rec["n"] += 1
        rec["arc_deg"] = arc_deg
        if speed is not None and (rec["min_speed"] is None
                                  or speed < rec["min_speed"]):
            rec["min_speed"] = speed

    # -- the answer -------------------------------------------------------- #
    def resolve(self, du, dv, lag_mm, speed=None):
        """`(trail_mm, off_u, off_v)` for a travel direction `(du, dv)`.

        `(du, dv)` need not be normalised; only its direction is read.  A zero
        direction or a non-positive lag returns the flat answer
        `(max(lag, 0), 0, 0)` -- no deviation without a lag is one of the
        fixture's two controls, and it is what makes the below-CTS regime
        (where the lag law floors to zero) cost nothing here.
        """
        norm = math.hypot(du, dv)
        if norm <= 0.0 or lag_mm <= 0.0:
            return (lag_mm if lag_mm > 0.0 else 0.0), 0.0, 0.0
        ud, vd = du / norm, dv / norm
        ca, sa = abs(ud), abs(vd)
        alpha = math.degrees(math.atan2(sa, ca))
        S = self.support(speed)
        # Formation site 2 (see `cap_circumferential_lag`): the circumferential
        # radius is built here, so it is bounded here. `lag_mm` -- the along-track
        # input -- is untouched.
        R_cir, arc_deg, hit_cap = cap_circumferential_lag(
            self.radius, self.gap, self.circ_radius(lag_mm))
        if hit_cap:
            self._record_saturation(arc_deg, speed)
        key = (round(alpha / self.cache_deg), round(lag_mm, 9), round(S, 9))
        hit = self._cache.get(key)
        if hit is None:
            hit = unrolled_contact(self.cache_deg * round(alpha / self.cache_deg),
                                   self.radius, S, lag_mm, R_cir)
            self._cache[key] = hit
        u_c, v_c = hit
        cu = -u_c if ud >= 0.0 else u_c          # the two mirror symmetries
        cv = -v_c if vd >= 0.0 else v_c
        trail = -(cu * ud + cv * vd)             # = u_c*ca + v_c*sa >= 0
        lu, lv = -vd, ud                         # left of travel
        cross = cu * lu + cv * lv
        return trail, cross * lu, cross * lv


class CurvedPlacement(object):
    """Where the jet's contact sits relative to the nozzle on a DOUBLY-CURVED
    patch -- `ContactPlacement`'s five-axis peer, built from two principal
    curvatures instead of one radius.

    WHAT IT KEEPS AND WHAT IT REFUSES, because the two halves of the 4-axis
    answer generalise differently and pretending otherwise is the whole trap:

      THE LOCUS GENERALISES. Q10's note above `unrolled_contact` records that
      Eq 1's contact locus, unrolled, is EXACTLY the ellipse `(u/R_1)^2 +
      (v/R_2)^2 = 1` about the nozzle. Its two semi-axes are the one thing
      5.1's projection identity gives per principal direction, so on a general
      patch they are `lag_radii_from_curvature`'s pair, each clamped by
      `cap_lag_radius_by_curvature` with THAT direction's own curvature.
      `cap_circumferential_lag`'s "only R_cir is capped" is a cylinder
      statement -- it rests on the along-track direction sitting on a surface
      that does not curve along the direction of travel, which is false here.

      EQ 4 DOES NOT GENERALISE, so this class does not carry it. Eq 4 is a
      SELECTION rule on that locus and it is written in the cylinder's own
      geometry: `_residual_scalar`'s normal field is `(0, sin phi, cos phi)`
      and its support vector is `(0, 0, S)` with S measured from the mandrel
      AXIS. A doubly-curved patch has neither. Writing a replacement would be
      a new derivation of the thing MANDREL_LAG_DERIVATION.md 12.9 freezes, on
      a regime where nothing is measured, so this class uses the OTHER
      selection rule already in the codebase -- the contact tracker's, whose
      steady state sits on the locus directly downstream of the nozzle (the
      same Q10 note: "rival selection rules on ONE locus"). That gives the
      along-track trail, which is the half the deposition loop consumes, and
      NO cross-track offset. `resolve` therefore returns `off_u = off_v = 0`
      by refusal, not because a doubly-curved collector has no lateral
      deviation. On a cylinder Neuhaus measured one.

    SO A CYLINDER MUST NOT COME HERE, and does not: when one principal
    curvature is exactly zero the patch IS locally a cylinder, Eq 4's premise
    holds, and this class DELEGATES to a `ContactPlacement` at `1/k2` rather
    than answering with the weaker rule. That keeps the validated 4-axis
    answer wherever it is valid (a cone reaches it per meridian point), and
    means a `RevolutionFrame` whose profile is a straight line answers
    bit-for-bit what `CylinderFrame` answers. The step at that boundary is
    REAL, and it is measured rather than hidden.

    CONCAVE. Admitted, bounded at `CONCAVE_POLE_FRACTION`, and recorded: any
    patch with a negative principal curvature sets `self.concave`, which
    travels out of a run in `info["lag_concave_prediction"]` the way
    `saturation` carries the tangency clamp. Nothing published measures lag on
    a concave collector, so those numbers are predictions and the software
    must say so wherever it shows them.

    PER POINT, NOT PER SCAFFOLD. A cylinder has one radius; a dome's curvature
    is a function of where you are on it, so `at_curvatures` is this class's
    `at_surface_radius` -- the caller asks the frame (`curvatures_at(u, h)`)
    and hands the answer here. `per_point` is the flag `mew_deposition` reads
    to know it must do that; `at_surface_radius` is deliberately ABSENT so the
    cylinder-only per-layer hook cannot fire on a surface it does not
    describe.
    """

    #: read by `mew_deposition._lay_one`: this placement wants a per-segment
    #: curvature query, where a `ContactPlacement` is fixed for the scaffold.
    per_point = True

    def __init__(self, k1, k2, gap, lag_ratio=None, support_mm=None,
                 support_frac=0.75, support_speed_coeff=0.0,
                 cache_deg=0.01, per_layer_registration=False,
                 curv_quantum=1e-5):
        self.k1 = float(k1)
        self.k2 = float(k2)
        self.gap = float(gap)
        self._lag_ratio_explicit = lag_ratio is not None
        self.lag_ratio = None if lag_ratio is None else float(lag_ratio)
        self.support_mm = None if support_mm is None else float(support_mm)
        self.support_frac = float(support_frac)
        self.support_speed_coeff = float(support_speed_coeff)
        self.cache_deg = float(cache_deg)
        self.per_layer_registration = bool(per_layer_registration)
        #: curvature quantum for `at_curvatures`, 1/mm -- the cache key, and
        #: the same trade as `cache_deg`. On a dome every segment sees a
        #: slightly different curvature, so without a quantum the cache grows
        #: one entry per segment and (on a cone, where each entry is a
        #: delegated `ContactPlacement`) each entry carries its own Eq 4 scan
        #: table. The cost is bounded: `dR/dk = -R*gap/(1 + k*gap)`, so
        #: quantising k by `q` moves the lag by at most about `q*gap` relative
        #: near k = 0 -- 3.5e-5 at the default and a 3.5 mm gap, i.e. 0.035 um
        #: on a 1 mm lag, half the 0.07 um the alpha quantum already costs.
        self.curv_quantum = float(curv_quantum)
        self.saturation = None
        #: None, or the record that this run predicted on a CONCAVE patch:
        #: `{"k_min", "factor_max", "n"}`. `factor_max` is `1/(1 + k_min*gap)`,
        #: the exact arc's small-lag amplification and an upper bound on it at
        #: any lag. Same channel and same reason as
        #: `saturation` -- the fact has to leave the run, because no published
        #: measurement stands behind it.
        self.concave = None
        self._sat_owner = None
        self._curv_cache = {}
        #: Exactly flat: both curvatures zero, so the locus is the flat circle
        #: of radius `lag` and every heading trails by the lag itself.
        #: Answered as an early return rather than through the ellipse
        #: arithmetic, so the planar limit is EXACT and not one ulp off it.
        self._flat = (self.k1 == 0.0 and self.k2 == 0.0
                      and not self._lag_ratio_explicit)
        #: A locally CYLINDRICAL patch keeps Eq 4 -- see the class docstring.
        self._delegate = None
        if self.k1 == 0.0 and self.k2 > 0.0:
            self._delegate = ContactPlacement(
                1.0 / self.k2, self.gap, lag_ratio=self.lag_ratio,
                support_mm=self.support_mm, support_frac=self.support_frac,
                support_speed_coeff=self.support_speed_coeff,
                cache_deg=self.cache_deg,
                per_layer_registration=self.per_layer_registration)
            # so a clamp inside Eq 4's own formation site reaches the object
            # `laid_fibers` reads, exactly as `at_surface_radius`'s children do
            self._delegate._sat_owner = self
        elif self.k1 < 0.0 or self.k2 < 0.0:
            self._record_concave()

    # -- the two facts that must leave the run ----------------------------- #
    def _root(self):
        owner = self
        while owner._sat_owner is not None:
            owner = owner._sat_owner
        return owner

    def _record_saturation(self, arc_deg, speed):
        """`ContactPlacement._record_saturation`'s signature and semantics, so
        a delegated Eq 4 clamp and an ellipse-path clamp land in one record."""
        owner = self._root()
        if owner is not self:
            owner._record_saturation(arc_deg, speed)
            return
        rec = self.saturation
        if rec is None:
            self.saturation = {"arc_deg": arc_deg, "min_speed": speed, "n": 1}
            return
        rec["n"] += 1
        rec["arc_deg"] = arc_deg
        if speed is not None and (rec["min_speed"] is None
                                  or speed < rec["min_speed"]):
            rec["min_speed"] = speed

    def _record_concave(self):
        """Remember that this run predicted on a concave patch, the most
        negative curvature it did it at, and the largest amplification that
        implies -- the three numbers a reader needs to size the extrapolation.
        """
        self._root()._concave_seen(min(self.k1, self.k2), self.gap)

    def _concave_seen(self, k_min, gap):
        rec = self.concave
        if rec is None:
            self.concave = {"k_min": k_min,
                            "factor_max": 1.0 / (1.0 + k_min * gap), "n": 1}
            return
        rec["n"] += 1
        if k_min < rec["k_min"]:
            rec["k_min"] = k_min
            rec["factor_max"] = 1.0 / (1.0 + k_min * gap)

    # -- the patch next door ----------------------------------------------- #
    def _key(self, k1, k2):
        q = self.curv_quantum
        if not q > 0.0:
            return (k1, k2)
        return (round(k1 / q), round(k2 / q))

    def at_curvatures(self, k1, k2):
        """This placement re-evaluated at another point's principal curvatures.

        `at_surface_radius`'s peer, and the reason `per_point` exists: the
        caller asks the FRAME (`curvatures_at(u, h)`) and hands the pair here,
        so nothing outside `mew_frame` reads a curvature attribute (five-axis
        spec 5, rule 1) and a mesh frame later answers the same query.
        """
        k1, k2 = float(k1), float(k2)
        key = self._key(k1, k2)
        if key == self._key(self.k1, self.k2):
            return self
        hit = self._curv_cache.get(key)
        if hit is None:
            hit = CurvedPlacement(
                k1, k2, self.gap,
                lag_ratio=(self.lag_ratio if self._lag_ratio_explicit else None),
                support_mm=self.support_mm, support_frac=self.support_frac,
                support_speed_coeff=self.support_speed_coeff,
                cache_deg=self.cache_deg,
                per_layer_registration=self.per_layer_registration,
                curv_quantum=self.curv_quantum)
            hit._sat_owner = self
            if hit.concave is not None:      # it was built before it had an
                hit.concave = None           # owner, so re-report it upward
                hit._record_concave()
            self._curv_cache[key] = hit
        return hit

    # -- the lag ellipse ---------------------------------------------------- #
    def radii(self, lag_mm, speed=None):
        """`(R_1, R_2)` -- the two capped semi-axes, in the principal
        directions, and THE one place they are formed on this path.

        `lag_mm` is the flat jet lag, exactly as `ContactPlacement.resolve`
        takes it: the amplitude is `Calibration.lag_amplitude`'s job upstream
        and is never re-derived here (12.9 point 3).
        """
        lag_mm = float(lag_mm)
        if self._lag_ratio_explicit:
            # A rig that MEASURED the pair overrides the geometry, exactly as
            # `mandrel_lag_axial/_circ` do at 4 axes. Two directions is all a
            # measured ratio can say, so it is applied to the two principal
            # ones -- and it then asserts an anisotropy that is NOT
            # curvature-caused, which is 2.1's "both radii constant"
            # hypothesis in the only language a general surface speaks.
            R1, R2 = lag_mm, lag_mm * self.lag_ratio
        else:
            # the exact arc per direction; a concave direction past its
            # steepest incidence comes back HELD at its reach bound, which is
            # an upper bound for the same reason a tangency clamp is, so it
            # lands in the same record
            R1, held1 = lag_arc_from_curvature(lag_mm, self.k1, self.gap)
            R2, held2 = lag_arc_from_curvature(lag_mm, self.k2, self.gap)
            if held1:
                self._record_saturation(math.degrees(R1 * -self.k1), speed)
            if held2:
                self._record_saturation(math.degrees(R2 * -self.k2), speed)
        R1, arc1, sat1 = cap_lag_radius_by_curvature(self.k1, self.gap, R1)
        R2, arc2, sat2 = cap_lag_radius_by_curvature(self.k2, self.gap, R2)
        if sat1:
            self._record_saturation(arc1, speed)
        if sat2:
            self._record_saturation(arc2, speed)
        return R1, R2

    def resolve(self, du, dv, lag_mm, speed=None):
        """`(trail_mm, off_u, off_v)` for a travel direction `(du, dv)`.

        The same signature and the same guards as `ContactPlacement.resolve`,
        so the deposition loop needs no branch on which placement it holds.
        `off_u`/`off_v` are 0 on the ellipse path -- see the class docstring;
        that is a refusal to generalise Eq 4, not a prediction of no deviation.
        """
        if self._delegate is not None:
            return self._delegate.resolve(du, dv, lag_mm, speed=speed)
        norm = math.hypot(du, dv)
        if norm <= 0.0 or lag_mm <= 0.0:
            return (lag_mm if lag_mm > 0.0 else 0.0), 0.0, 0.0
        if self._flat:
            return float(lag_mm), 0.0, 0.0
        R1, R2 = self.radii(lag_mm, speed)
        if R1 <= 0.0 or R2 <= 0.0:
            return float(lag_mm), 0.0, 0.0
        # the ellipse's radius in the travel direction:
        # 1/sqrt((cos psi/R1)^2 + (sin psi/R2)^2), psi measured from u.
        return (1.0 / math.hypot(abs(du) / (norm * R1),
                                 abs(dv) / (norm * R2))), 0.0, 0.0


def placement_for_frame(frame, cal=None, model=None):
    """A placement for a rotary frame, or **None** for anything flat.

    A `ContactPlacement` for a cylinder, a `CurvedPlacement` for a frame that
    answers `curvatures_at` -- the five-axis family, where the collector has
    two principal curvatures and one radius is not an answer. Read
    `CurvedPlacement`'s docstring for what changes there and, more to the
    point, for what it refuses to carry across.

    This is the off-switch, and it is structural: `PlanarFrame` and `None` both
    return None, so a caller holding None takes no mandrel branch at all and its
    output is byte-identical to the flat code.

    `cal` is an optional `mew_physics.Calibration`: `mandrel_support_mm` and the
    two support knobs are read from it when present.  `R_ax`/`R_cir` are NOT --
    they are the jet lag, which the deposition model already predicts per speed
    and passes to `resolve`; only their RATIO is a property of the geometry.  If
    a calibration carries both measured radii, their ratio is used in place of
    Q7's derived arc (`circumferential_lag`).

    The lag's SIZE is `Calibration.lag_amplitude` (k), which scales the jet
    model's own law and so reaches `resolve` through the `lag_mm` argument.
    Deliberately one knob, not two: a rig that also pinned R_ax here would have
    two ways to set the same amplitude and no rule for which wins.

    `model` is the `MEWModel` the lay runs on. Its only use here is a
    user-replaced R_cir law (`mew_lawlang` law `rcir`), which it evaluates
    (`MEWModel.circumferential_lag`) so the law sees the rig's own constants;
    `cal` defaults to its calibration. With `cal` alone and an `rcir` law, a
    model is built on that calibration with the factory material and nozzle.
    The law reaches the CYLINDER only: `CurvedPlacement` (and the cylinder it
    delegates to on a cone) keeps Eq. 7, because a doubly-curved patch has no
    single `r_m` to hand it.
    """
    if cal is None and model is not None:
        cal = model.calibration
    if frame is None or not getattr(frame, "rotary", False):
        return None
    # Through the frame's interface, not its attributes: the placement is built
    # on the BARE surface, so every query is asked at h = 0. `gap_at` models a
    # FIXED nozzle (`nominal - h`) and this is its one caller -- correct here
    # precisely because h is 0; `at_surface_radius` is right not to call it as
    # the wall grows, because `generate_mandrel_gcode` lifts the nozzle one
    # layer step per layer and the physical gap is constant.
    gap = frame.gap_at(0.0, 0.0, 0.0)
    if not gap:
        return None
    ratio = None
    if cal is not None:
        r_ax = getattr(cal, "mandrel_lag_axial", None)
        r_ci = getattr(cal, "mandrel_lag_circ", None)
        if r_ax and r_ci:
            ratio = float(r_ci) / float(r_ax)
    kw = dict(
        lag_ratio=ratio,
        support_mm=None if cal is None else getattr(cal, "mandrel_support_mm", None),
        support_frac=0.75 if cal is None else getattr(cal, "mandrel_support_frac", 0.75),
        support_speed_coeff=(0.0 if cal is None else
                             getattr(cal, "mandrel_support_speed_coeff", 0.0)),
        per_layer_registration=(
            False if cal is None else
            bool(getattr(cal, "mandrel_per_layer_registration", False))))
    # WHICH PLACEMENT, and the test is a CAPABILITY rather than a class name.
    # `curvatures_at(u, h)` is the per-point query only the five-axis frame
    # family answers, and it is the query a mesh frame answers later too, so
    # routing on it neither names a subclass nor reads a curvature attribute
    # (five-axis spec 5, rules 1 and 2). A `CylinderFrame` does not have it and
    # therefore cannot reach the new branch -- which is what makes every 4-axis
    # digest still a digest of this function's own output.
    if hasattr(frame, "curvatures_at"):
        k1, k2 = frame.curvatures_at(getattr(frame, "u_mid", 0.0), 0.0)
        k1, k2 = float(k1), float(k2)
        if not (k1 or k2):
            return None       # a flat patch takes no placement branch at all
        return CurvedPlacement(k1, k2, gap, **kw)
    radius = frame.surface_radius(0.0)
    if not radius:
        return None
    circ_law = None
    if cal is not None and str((getattr(cal, "law_source", None) or {})
                               .get("rcir") or "").strip():
        if model is None:
            import mew_physics
            model = mew_physics.MEWModel(working_distance=gap, calibration=cal)
        circ_law = model.circumferential_lag
    return ContactPlacement(radius, gap, circ_law=circ_law, **kw)
