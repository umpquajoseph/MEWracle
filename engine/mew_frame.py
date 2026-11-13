"""The deposition FRAME -- the surface the jet lands on, as an interface.

The flat solver in `gcode_to_fibers` works in a
plane and a mandrel works on a cylinder, so rather than teach the solver
"cylinder" it is taught "a parameterised surface with a normal field and a
metric". A cone or a tapered mandrel is then a new class here, not a second
solver. Pure math: stdlib + numpy, no VTK, no I/O, no GUI.

    (u, v, h)   the SURFACE frame the solver works in: u along the collector's
                first principal direction (the mandrel AXIS, zero curvature
                there), v across it (circumferential ARC LENGTH at the bare
                collector), h outward along the normal -- deposited wall height.
    world       machine XYZ in the COLLECTOR'S OWN (co-rotating) frame, where
                deposited fibre is static, so the viewer spins one actor (§4.6).

CYLINDER CONVENTION, chosen to agree with Neuhaus's reference implementation so
the placement model ports without a sign hunt: x = mandrel axis, +z points AT THE
NOZZLE, phi = azimuth from top dead centre, so world = (u, r*sin(phi),
r*cos(phi)) with r = R0 + h. The rotary word `A` is in DEGREES and is the
right-hand-rule rotation about +x, so a material point under the nozzle when the
mandrel reads A sits at body azimuth phi = radians(A) -- no sign flip.

THREE THINGS THIS DELIBERATELY GETS RIGHT, each a silent wrong number otherwise:
  1. `period_v` is `2*pi*R0`, the BARE COLLECTOR radius, never the local
     `r = R0 + h`. The seam is a property of the mandrel, not of the wall, and a
     period growing with the wall would put one physical place in two different
     cells as the print goes on (§3.2.1).
  2. `metric(u, h) = (1 + k1*h, 1 + k2*h)`. The unroll is NOT isometric -- the
     fibre rides the PARALLEL surface h out along the normal, and a fixed step
     there is longer (or, on a concave patch, shorter) by that factor in each
     principal direction, so a span measured in the chart is wrong by it at
     wall height h (§3.5, term C). A cylinder has k1 = 0, which is why one
     scalar (`metric_v`, now superseded) sufficed for four axes.
  3. `normal_curvature` is written from PRINCIPAL curvatures,
     `k1*cos^2(psi) + k2*sin^2(psi)` (Euler's theorem), never `sin^2(psi)/R`:
     a cylinder is k1 = 0, k2 = 1/R0, a sphere k1 = k2, a saddle opposite signs.

AND THE ONE IT CANNOT. The unroll is exact only for a DEVELOPABLE surface (zero
Gaussian curvature, k1*k2 = 0), which is why the flat sag solver applies to a
cylinder or a cone unchanged. A sphere is not developable (Theorema Egregium),
so a non-developable frame must set `developable = False` and callers that need
the flat solver must check it.
"""

import copy
import math

import numpy as np


class AxisSingularity(ValueError):
    """A query landed where the meridian meets the axis and the surface has no
    normal there.

    Distinct from `NonDevelopableFrame` because the caller's options differ. A
    non-developable surface is fine to query and merely cannot be flattened; a
    genuine axis singularity -- a CONE APEX -- has no tangent plane at all, so
    there is no answer to give and no tolerance that would produce one.

    A sphere's pole is NOT this. It is umbilic: the limit exists, both
    curvatures agree there, and `RevolutionFrame` returns it.
    """


class NonDevelopableFrame(ValueError):
    """A doubly-curved frame reached something that can only take a flat unroll.

    Its own type rather than a bare ValueError because the caller's choice is
    real: refuse the surface, or move to a local chart.
    """


class ChartUnusable(ValueError):
    """The `(u, v)` chart is not a good enough RULER where the pattern goes.

    The parent of the two ways that happens, so a caller that only wants to
    know "can this be solved" catches one type. They are separate subclasses
    because the user's fix is different in each case, and because ONE of them
    was implemented first and the other found by measurement afterwards -- see
    `ChartShearExcess`, which is the dominant term and has no patch scale.
    """


class ChartPoleExclusion(ChartUnusable):
    """A path came inside the disc where `(u, v)` stops being a usable ruler.

    Distinct from `NonDevelopableFrame` because it is a statement about ONE
    PLACE rather than about the whole surface, and the caller's fix is
    different in kind: move the pattern off the pole, or print a collector
    whose axis the pattern never approaches. Nothing about the geometry has
    given out -- a sphere's pole is umbilic and every frame query answers there
    (`RevolutionFrame._k2`). It is the coordinates: `v = rho*phi` divides by a
    radius going to zero, so two points a fibre apart on the surface can be an
    arbitrary distance apart in the chart, and the contact index measures in
    the chart.

    Distinct from `mew_project.ChartSingularity`, which is the same pole and a
    different question -- that guard is NUMERICAL (a finite-difference stencil
    straddling the axis, 20 steps = 0.02 mm) and this one is ACCURACY at the
    scale a contact is looked for, which is 8x larger at a 20 um fibre. A path
    can clear the projector's guard and still be refused here.
    """


class ChartShearExcess(ChartUnusable):
    """The chart's u and v axes are too far from perpendicular where the
    pattern sits, so a chart distance is not a surface distance at ANY scale.

    THE TERM THE POLE DISC DOES NOT BOUND, and the reason there are two
    exceptions here rather than one. The chart puts `(u, v)` at azimuth
    `phi = v/rho(u)`, so a line of constant `v` spirals wherever the meridian's
    radius varies, and the first fundamental form carries a shear
    `c = rho'*phi` (see `RevolutionFrame.metric`). That is a function of the
    ABSOLUTE azimuth: it does not shrink as the patch shrinks, so no exclusion
    radius and no patch scale can bound it. A harness that only ever passes
    `phi0 = 0.0` cannot see it: that is the one meridian where `c` vanishes
    identically.

    Measured on the five-axis defaults: turning a design about
    the collector's own axis -- a RIGID MOTION, same surface, same fibres, same
    contacts -- moved the solved geometry by 12.85 um on a 10 mm dome and
    10.87 um on a torus, against 0.000000 um on a cylinder, where `rho' = 0`
    and the shear is identically zero. 12.85 um is 2.3x the whole effect this
    slice set out to add.

    THE USER'S FIX IS THE PATTERN'S ANGULAR WIDTH, not its position: `c` is
    bounded by `|rho'| * max|phi|`, so a narrower band around the collector
    passes where a wide one does not. The message says how narrow.
    """


#: The most a chart may OVERSTATE a distance before `_SegIndex` starts missing
#: a segment that is physically in contact. Not a tolerance anyone chose:
#: `gcode_to_fibers._hash_cell` is `1.5*d` and its 3x3 neighbourhood guarantee
#: needs `d + cell/4 <= cell`, i.e. `d <= 0.75*cell`, i.e. 12.5% of slack.
POLE_BAND = 0.125

#: Patch scales of clearance the chart needs from the axis of revolution.
#: Near the axis `rho ~ u` and the chart's overstatement goes as `rho'*L/rho`,
#: so the band above is reached at `rho ~ 8*L*rho'` -- measured, and confirmed
#: as a MECHANISM rather than a fitted number (predicted 0.16 mm,
#: measured 0.175 and 0.194 for the two cheap charts at L = one 20 um fibre).
#: The frame's own global chart crossed the band at 0.079 mm there, so this is
#: the CONSERVATIVE side of the measurement by 2x, deliberately: the number
#: that is wrong in the safe direction refuses a print, and the one that is
#: wrong in the other direction ships a scaffold with contacts missing.
POLE_PATCHES = 1.0 / POLE_BAND

#: The most a chart may overstate before a caller that has ABSORBED the
#: overstatement into its own reach becomes too expensive to run.
#:
#: NOT an accuracy band, and that is the whole point of it. `local_offsets`
#: measures through the metric's own Jacobian at the query point, which is the
#: `normal` chart -- 0.0003% at contact scale on a 5 mm dome -- and that figure
#: does not degrade as the shear grows, because a linear map applied to both
#: points is exact whatever its skew. What DOES grow with the shear is the
#: candidate block: `_SegIndex.rest_heights` widens to
#: `k = ceil(s*inflate/cell + 1/4)` cells and reads `(2k+1)^2` of them, so the
#: 9 cells a flat solve looks at become 81 at `k = 4`. Nine times the flat cost
#: is where a preview stops being a preview, so `k <= 4` is the cap; with
#: `cell = 1.5*d` (`gcode_to_fibers._hash_cell`) and a contact reach `s = d`
#: that is `inflate <= 1.5*(4 - 0.25) = 5.625`, i.e. 462.5% of overstatement.
CHART_COST_BAND = 4.625

#: The most a chart may overstate before an APPROXIMATE engine that never
#: corrects the chart distance is refused. Not the cost band, deliberately, and
#: the two must not be conflated: `CHART_COST_BAND` says "this is exact and
#: merely expensive", which is a statement `sag_approx` and `preview_sag.drape_sag`
#: cannot make -- one finds contacts between chart segment PAIRS, the other bins a
#: chart RASTER, and neither takes `local_offsets` anywhere near its contact test.
#:
#: SO THIS IS THE MEASURED BAND, AND IT IS EXACTLY AS WIDE AS THE EVIDENCE.
#: Against the exact solver on a dome, through `sag_bench.compare_outputs`, with
#: the pattern's angular extent widened to walk the shear:
#:
#:     overstates   sag_approx mean   preview_sag mean
#:        48.6%          2.084 um          7.338 um
#:        66.9%          1.916 um         12.928 um     <- the shipped default
#:        75.9%          1.788 um         16.585 um
#:        80.4%          1.975 um         17.260 um
#:        81.9%          2.743 um         17.388 um
#:
#: `sag_approx` does not degrade with the shear at all across that range -- it is
#: NON-MONOTONE, 1.788 um at 75.9% against 2.084 um at 48.6% -- so what moves it
#: is the pattern's size, not the chart. `preview_sag` does grow, and saturates
#: near 17.4 um. Both stay under a 21.81 um fibre throughout. The max for either
#: is ~198-300 um at EVERY shear including a cone's 4.9%, so it is the method's
#: and bounds nothing about the chart.
#:
#: 0.85 is just past the widest measured (81.9%, where the projection stops
#: widening on this dome), which is the whole justification: nothing else stops
#: a design more sheared than the one measured point from shipping silently
#: degraded, so this refuses past where it has been looked at. Widening it needs
#: another row in that table,
#: not another argument. The exact solver measures locally and is not held here.
CHART_APPROX_BAND = 0.85

#: `require_chart(ruler=...)` -> the shear band that ruler earns, and the two
#: strings the refusal uses to say why. A THREE-WAY and not a boolean, because a
#: boolean lets one band cover two different justifications: `cost_band=True`
#: on `sag_approx` would reuse a COST argument as an ACCURACY allowance for an
#: engine that never corrects the chart. Naming the ruler makes
#: the two impossible to confuse, and gives the approximate engines a band that
#: stops where their evidence does.
_SHEAR_BANDS = {"raw": POLE_BAND,          # measures raw chart deltas
                "local": CHART_COST_BAND,  # measures through `local_offsets`
                "approx": CHART_APPROX_BAND}
_BAND_WHAT = {
    "raw": "the contact index can absorb",
    "local": "a widened contact index can still afford",
    "approx": "this approximate engine has been measured against the exact "
              "solver up to",
}
_BAND_WHY = {
    "raw": "Two points a fibre apart on the collector then read further apart "
           "than that in the chart and the index drops the contact silently.",
    "local": "The contact index measures through the local chart, so it is not "
             "WRONG here -- it is expensive: the candidate block widens with "
             "the overstatement and past this band it costs more than nine "
             "times a flat solve.",
    "approx": "This engine measures in the global chart and does not correct "
              "for the shear, so the band is not a tolerance it satisfies by "
              "construction -- it is how far it has actually been measured "
              "(measured 48.6% to 81.9%, mean error 1.8-2.7 um for the "
              "approximate solver and 7.3-17.4 um for the preview drape, on a "
              "21.81 um fibre). Past "
              "it there is no evidence, so it refuses rather than degrading "
              "quietly. The EXACT sag solver measures locally and will take "
              "this design.",
}


def overstatement_from_metric(a, b, c):
    """How much the chart OVERSTATES a step, worst direction, as a fraction.

    The arithmetic of `RevolutionFrame.chart_overstatement`, lifted out so that a
    caller holding `(a, b, c)` already -- `_SegIndex.rest_heights` evaluates them
    per query point and must not evaluate them twice -- gets the SAME number the
    gate was checked with. Two spellings of this formula is how a gate comes to
    pass a solve the solve then cannot run.

    The chart maps a step `(du, dv)` to `(a*du, b*(dv - c*du))` -- `metric`'s own
    form -- so the Jacobian is `J = [[a, 0], [-b*c, b]]` and a chart length over
    the physical one is at most `1/sigma_min(J)`. Written from the eigenvalues of
    `J'J = [[a^2 + (b*c)^2, -b^2*c], [-b^2*c, b^2]]` rather than by calling an
    eigensolver, because this runs on every point of every fibre.

    Zero on a plane and on a cylinder, exactly: there `c = 0` and `a = 1`, so `J`
    is diagonal with `b >= 1` and the chart never overstates at all -- it
    UNDERSTATES by the offset stretch, which `_SegIndex`'s own contract says is
    safe (extras cost time and cannot change a result).
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    c = np.asarray(c, dtype=np.float64)
    A = a * a + (b * c) ** 2
    C = b * b
    B = -(b * b) * c
    half = 0.5 * (A + C)
    gap = np.sqrt(np.maximum(0.0, (0.5 * (A - C)) ** 2 + B * B))
    lam_min = np.maximum(half - gap, 1e-300)
    return np.maximum(0.0, 1.0 / np.sqrt(lam_min) - 1.0)


def local_offsets(abc, du, dv):
    """Chart offsets -> physical offsets in the patch FLATTENED AT ONE POINT.

    `abc` is `metric(u, v, h)` evaluated at that point, and `(du, dv)` are chart
    offsets measured FROM it. The result is the same displacement as the surface
    sees it, so `hypot` of the pair is a physical distance and the angle between
    two such pairs is a surface angle.

    THIS IS THE LOCAL CHART, and there is nothing more to it than one linear map.
    `metric`'s own docstring says so: "using the exponential map instead would
    mean first converting `(du, dv)` into its axes, which needs this same
    Jacobian". Slice 7 measured what that buys -- the exponential map at the patch
    centre reads 0.0003% against the exact great-circle distance at contact scale,
    where the frame's raw global `(u, v)` reads 1.54% on a 5 mm dome and 67% at the
    shipped five-axis default.

    ONE SPELLING, ON PURPOSE. `gcode_to_fibers` and `sag_approx` both measure
    contacts and `sag_approx` imports every law from `gcode_to_fibers` so the two
    solvers cannot drift; a second copy of this map would be exactly that drift.

    Scalars or arrays, in every argument.
    """
    a, b, c = abc
    return (np.multiply(a, du),
            np.multiply(b, np.subtract(dv, np.multiply(c, du))))


class OffsetSurfaceFold(ValueError):
    """The wall has grown past the collector's centre of curvature.

    Only reachable on a CONCAVE patch, where `k < 0` and the offset stretch
    `1 + k*h` SHRINKS with wall height instead of growing. At `k*h = -1` the
    parallel surface has collapsed to the focal line -- every point of the
    parallel through that patch is one point -- and past it the surface has
    folded through itself and `metric` would return a negative length.

    Its own type because the caller's choice is real and is not a tolerance:
    the wall is too tall for this radius of curvature, so either the collector
    is less concave or the print is shorter. There is no fraction-of-the-pole
    band here, deliberately -- the fold is an exact geometric fact, not a
    modelled one, and a band would be an invented constant sitting in front of
    it. Concave lag has such a band and it is `mew_mandrel`'s, not this
    module's: a different mechanism (`1/(1 + k*H)`, nozzle gap) with a
    different pole.
    """


class DepositionFrame(object):
    """Interface + the planar implementation (the identity case).

    `PlanarFrame` IS this class: every method below is already the flat answer,
    so "mandrel mode off" is literally the base class doing nothing. That is the
    cheapest possible statement of "mandrel off is byte-identical" -- there is
    no flat-vs-curved branch to get wrong, because flat is the no-op.
    """

    #: principal curvatures of the BARE collector, 1/mm. u lies along k1.
    k1 = 0.0
    k2 = 0.0
    #: period of the v axis in mm, or None when v does not wrap.
    period_v = None
    #: can (u, v) be flattened without stretching? k1*k2 == 0.
    developable = True
    #: does this frame consume the rotary word?
    rotary = False
    #: DO THIS FRAME'S CALLERS HOLD CHART COLUMNS RATHER THAN WORLD XYZ?
    #:
    #: The currency between the projector and the sag / deposition engines is
    #: WORLD XYZ in the collector's own frame plus the frame, and the chart is
    #: an internal measuring device (`sag_approx`'s section comment is the
    #: rule).  `False` says so and is the default, so a NEW frame -- an
    #: imported CAD collector, say -- is right without opting in.
    #:
    #: `True` is the one legacy exception and it is a fact about the READ-BACK,
    #: not about the surface: a frame whose `unroll` is a working machine ->
    #: (u, v, h) map has already handed its callers chart columns by the time
    #: an engine sees them, so converting again would be a second conversion of
    #: an already-converted path.  That is the four-axis mandrel and nothing
    #: else -- `RevolutionFrame.unroll` RAISES precisely so the three-axis
    #: parser cannot be reused on a five-axis file, and a frame that cannot
    #: unroll cannot be holding chart columns.
    #:
    #: `developable` is NOT the answer to this question: it is a fact about the
    #: SURFACE and answers it correctly only by coincidence. A cylinder is
    #: developable AND unrolls, so the two agree there.  A CONE is developable and
    #: does NOT unroll, so it took the identity chart while its callers held
    #: world -- world Z read as height above the collector, which put the
    #: exported fibre 0.28-0.38 mm inside the cone and collapsed the preview
    #: onto one parallel.
    chart_currency = False

    def __init__(self, nozzle_gap=None):
        #: nominal nozzle-to-BARE-collector distance, mm, or None if unknown.
        self.nozzle_gap = nozzle_gap

    # -- coordinates ------------------------------------------------------- #
    def unroll(self, x, y, z, a_deg=0.0):
        """Machine words -> the surface frame (u, v, h). Scalars in, scalars out."""
        return x, y, z

    def to_world(self, u, v, h):
        """Surface frame -> collector-fixed XYZ. Accepts scalars or arrays."""
        return u, v, h

    def from_world(self, x, y, z):
        """Collector-fixed XYZ -> the surface frame (u, v, h). The inverse.

        NEEDED BY THE FIVE-AXIS READ-BACK AND BY NOTHING BEFORE IT.
        `unroll` takes MACHINE WORDS, which is the four-axis question and has
        no answer on a five-axis file (`RevolutionFrame.unroll` says so):
        two rotary words mean nothing without the joints that drive them. So
        `gcode_to_fibers.FiveAxisReader` answers the machine half -- machine
        XYZ + two rotary words -> the point in the COLLECTOR'S own XYZ, using
        the same kinematic chain the emitter used -- and this answers the
        surface half. Two steps, each with one authority, and the frame never
        has to know what a rotary word is.

        Every implementation must round-trip `to_world` to numerical precision;
        `unwrap_path` is how a whole polyline comes back, and it is where the
        azimuth's branch cut is dealt with.
        """
        return x, y, z

    def metric_v(self, h):
        """Arc length actually travelled per unit of `v`, at wall height h.

        1.0 on a plane. On a cylinder this is `r/R0` and it is term C: a span
        that reads `dv` in the unrolled frame is physically `metric_v(h)*dv`
        long, and the droop law wants the physical one.

        SUPERSEDED by `metric`, which answers both axes and takes a position.
        No production module calls this any more; it survives as the
        CYLINDER'S BYTE PIN -- `test_mandrel_sag` asserts against this exact
        spelling, and
        `metric` is required to return it unchanged.
        """
        return 1.0

    def metric(self, u, v, h=0.0):
        """The chart's first fundamental form at (u, v, h), as `(a, b, c)`.

        THE FORM. A chart step `(du, dv)` lands, on the deposition surface, at
        the locally-Euclidean displacement

            (a*du,  b*(dv - c*du))

        so its physical length is `hypot(a*du, b*(dv - c*du))`. That is the
        full first fundamental form and not a stand-in for one:

            E = a**2 + (b*c)**2      F = -(b**2)*c      G = b**2

        It is returned FACTORED rather than as `(E, F, G)` for one reason and
        it is not taste: every call site would otherwise have to take a square
        root to get back to a length, `sqrt(m*m)` is not `m` in float64, and
        the cylinder's byte guards would move. Factored, a cylinder is
        `(1.0, metric_v(h), 0.0)` and every call site keeps the arithmetic it
        already had, exactly.

        WHY ONE SCALAR WAS NOT ENOUGH. Two independent things stretch a step,
        and `metric_v` carried only the first:

          b, the OFFSET stretch. The fibre rides the parallel surface h out
            along the normal, which is bigger by `1 + k*h` in each principal
            direction -- the same `k/(1 + k*h)` fact `principal_curvatures`
            documents, read backwards. On a cylinder `k1 = 0`, so `a` is 1 and
            the whole correction is `b = 1 + h/R0 = (R0 + h)/R0`. That is the
            number `metric_v` returned, and why one scalar survived four axes.
          c, the CHART SHEAR, and this is the one a curved meridian adds. The
            chart puts `(u, v)` at azimuth `phi = v/rho(u)`, so a line of
            constant v is not a meridian: it spirals wherever `rho` varies,
            and the u and v axes stop being perpendicular. `c = rho'*phi` and
            it vanishes exactly when `rho' = 0` -- a cylinder, and only there.

        Dropping `c` is not a small approximation. Measured on a projected
        serpentine (`mew_project`) at a 15-layer wall: mean 1.3% and worst
        7.5% of span on a 35 mm dome, mean 3.3% and worst 34% on a 5 mm one,
        against 5e-4% and 8e-3% for this form. Both against the world chord
        from `to_world`, which neither expression is derived from.

        AND WHY THE CHART QUESTION DOES NOT REACH HERE. Slice 7 measured how
        wrong a chart's reported distance is when the metric is treated as one
        constant, and found the exponential map the best of three. Evaluated
        per point, the form above is exact in ANY chart, so there is nothing
        left for a better chart to fix: using the exponential map instead would
        mean first converting `(du, dv)` into its axes, which needs this same
        Jacobian. The chart does not change (`(u, v)` keeps its meaning); what
        changes is that its metric is now computed rather than assumed.

        Scalars or arrays, in every argument.
        """
        return (1.0, 1.0, 0.0)

    # -- surface geometry -------------------------------------------------- #
    def principal_curvatures(self, h=0.0):
        """(k1, k2) of the DEPOSITION surface at wall height h, 1/mm.

        The fibre being laid does not sit on the bare collector once a wall has
        built up -- it sits on the parallel (offset) surface h out along the
        normal, whose principal curvatures are `k / (1 + k*h)`. On a cylinder
        that is `1/R0 -> 1/(R0 + h)`, which is the `r_local` term B uses; on a plane it
        is 0 either way, and on a sphere
        `1/R -> 1/(R + h)`. One formula, every implementation, and no branch on
        which surface this is.
        """
        h = float(h) if np.ndim(h) == 0 else np.asarray(h, dtype=np.float64)
        return (self.k1 / (1.0 + self.k1 * h),
                self.k2 / (1.0 + self.k2 * h))

    def normal_curvature(self, psi, h=0.0):
        """Euler's theorem: normal curvature at helix angle `psi` FROM u (rad).

        The chord a span takes between two supports sits below the design
        surface by `k_n*s^2/8` -- exactly the parabola the droop law already
        draws, which is why term B enters as an effective coefficient rather
        than as new machinery (§3.4).

        `h` is the wall height the span is being laid at: the curvature that
        bends the chord is the DEPOSITION surface's, not the bare collector's
        (see `principal_curvatures`). h = 0 (the default) is the bare surface,
        which is what every pre-phase-4 caller meant.
        """
        k1, k2 = self.principal_curvatures(h)
        c, s = np.cos(psi), np.sin(psi)
        return k1 * c * c + k2 * s * s

    def chord_drop(self, span, psi, h=0.0):
        """Depth of the chord below the surface over `span`, at helix angle psi.

        SUPERSEDED for the sag path by `chord_drop_at`, which takes the span's
        direction as the surface sees it and the position it is laid at. This
        spelling survives as the general-surface statement of Euler's theorem
        and as the CYLINDER'S BYTE PIN at h = 0 (`test_mandrel_sag`); no production module calls it any more.
        """
        return 0.125 * self.normal_curvature(psi, h) * span * span

    def _curvatures_here(self, u, h=0.0, v=None):
        """(k1, k2) of the deposition surface AT meridian position `u`.

        `v` IS THE SECOND CHART COORDINATE, AND IT IS OPTIONAL BECAUSE NO
        SURFACE OF REVOLUTION NEEDS IT. On a plane, a cylinder, a cone, a dome,
        a bowl and a torus the curvature is a function of the meridian alone --
        that is what being a surface of revolution means -- so `u` was the
        whole answer and this signature took only `u`. An IMPORTED CAD SOLID is
        the first collector where it is not: a filleted cube curves at 1/9.5 on
        a fillet and not at all on the flat top, and which of those a point is
        on is a fact about both coordinates. Used by `mew_cad.CadFrame`;
        every other frame ignores it, so no
        digest moves by a byte.

        The per-point curvature query the sag path needs, and it is PRIVATE on
        purpose. `mew_mandrel.placement_for_frame` routes on
        `hasattr(frame, "curvatures_at")` -- a capability test -- so publishing that name here would silently move
        every cylinder onto the five-axis placement. A mesh frame overrides
        this hook the same way `RevolutionFrame` does.

        The base answer IGNORES `u`, and on this class's surfaces that is exact
        rather than a simplification: a plane and a cylinder have the same
        curvature at every point of the meridian, so the per-point query and
        `principal_curvatures` return the same numbers. It stops being exact
        the moment the meridian bends, which is what `RevolutionFrame`
        overrides it for.
        """
        return self.principal_curvatures(h)

    def chord_drop_at(self, span, eu, ev, u=0.0, h=0.0, v=None):
        """Depth of a chord below the surface, from the direction it really has.

        `span` is the PHYSICAL span; `(eu, ev)` is that span's direction as the
        SURFACE sees it, in the principal frame (meridian, parallel), which is
        exactly the vector `mew_frame.metric` hands a caller back:

            eu = a*du,   ev = b*(dv - c*du)

        for a chart step `(du, dv)`. It need not be a unit vector -- only its
        direction is read -- so a call site passes the same two numbers it
        already formed to get `span` itself, and the length and the angle
        cannot come from two different directions. That they DID is the defect
        this method replaces: `chord_drop(span, psi, h)` took `psi` from the
        CHART, and a chart angle is not a surface angle wherever the chart is
        sheared or the two principal stretches differ.

        Two things are being fixed together and they are one class -- the
        surface quantity Euler's theorem is asked for was not the surface
        quantity supplied:

          THE ANGLE. `(eu, ev)` is the tangent direction on the DEPOSITION
            surface, so Euler gets the direction the fibre actually runs in.
            Measured against the exact chord (the chord's midpoint dropped onto
            the surface along its normal, from `to_world`/`normal` alone): on a
            5 mm mandrel at a 15-layer wall the chart angle costs -8.3% of the
            drop at a 30 degree heading and -5.5% at 45; on a 15 degree cone a
            chart-AXIAL span has a real drop that the chart angle reports as
            exactly zero, because a line of constant `v` is not a meridian.
          THE PLACE. `_curvatures_here(u, h)` instead of the frame's
            span-midpoint `principal_curvatures(h)`. On a cone the local k2 is
            +33%/-20% of the midpoint's, on a torus -91%/+96%; on a plane, a
            cylinder and a sphere it is the same number, which is why one
            curvature survived four axes.

        The two must be fixed TOGETHER: with the place fixed and the angle
        still the chart's, a torus is -50% at a 45 degree heading, worse than
        either defect alone.

        SPELLED AS EULER'S THEOREM, NOT AS `(k1*eu**2 + k2*ev**2)/(eu**2 +
        ev**2)`. The two are the same number in real arithmetic and not always
        the same float64, and the difference is a byte statement: on a BARE
        cylinder `(eu, ev)` reduces to the chart direction and
        `_curvatures_here` to `principal_curvatures`, so this spelling
        reproduces `chord_drop(span, psi, h)` exactly, to the last bit, and a
        drape at h = 0 cannot move. It also costs no more than the expression
        it replaces -- that one already took an arctan2, a cos and a sin.
        """
        k1, k2 = self._curvatures_here(u, h, v)
        psi = np.arctan2(np.abs(ev), np.abs(eu))
        c, s = np.cos(psi), np.sin(psi)
        return 0.125 * (k1 * c * c + k2 * s * s) * span * span

    def normal(self, u, v, h=0.0):
        """Outward unit normal at (u, v), in collector-fixed XYZ.

        The second of section 5 rule 3's three queries -- point, normal,
        geodesic step -- and the reason the projection layer never needs an
        analytic parameterisation: a triangle mesh answers this one too.
        Constant `+h` on a plane, radial on a cylinder, and on a surface of
        revolution it tilts with the meridian.
        """
        z = np.zeros_like(np.asarray(u, dtype=np.float64))
        return z, z, np.ones_like(z)

    def require_developable(self, what):
        """Raise unless this frame can be flattened without stretching.

        The unroll is exact only for `k1*k2 == 0`, and until now that limit was
        a docstring: `developable` was set in two places, no caller branched on
        it, and a sphere handed to the flat sag solver would have produced
        silently wrong geometry rather than an error. `what` names the caller so
        the message says which operation cannot take this surface.

        NO LONGER THE SAG SOLVERS' GATE -- `require_chart` is, and this is what
        it falls back on for a frame that has no chart story of its own. See
        that method for why the doubly-curved case stopped being a refusal.
        """
        if not self.developable:
            raise NonDevelopableFrame(
                "%s needs a developable frame (the unroll is exact only where "
                "k1*k2 == 0); %s has k1=%r k2=%r. The fix is a LOCAL "
                "chart, not a wider tolerance here."
                % (what, type(self).__name__, self.k1, self.k2))

    def require_chart(self, what, u, patch, v=None, h=0.0, ruler="raw"):
        """Raise unless the (u, v) chart is a good enough RULER at scale `patch`.

        THE QUESTION THE SAG SOLVERS ACTUALLY ASK, and it is narrower than
        "is this surface developable". Every LENGTH the solve
        forms goes through `metric` per point, which is exact in any chart, so
        the only place a raw chart distance survives is the contact index:
        `_SegIndex` bins segments by `(u, v)` in physical mm and answers "which
        of you are within one fibre DIAMETER of this point". A chart that
        overstates that distance drops a contact.

        THE BAND IS NOT A JUDGEMENT CALL. `gcode_to_fibers._hash_cell` is
        `1.5 * diameter` and the 3x3 neighbourhood guarantee needs
        `diameter + cell/4 <= cell`, so a chart may overstate by at most 12.5%
        (`POLE_BAND`) before the index starts missing a segment that is
        physically in contact. Understating is safe -- extras cost time and
        cannot change a result.

        AND SLICE 7 MEASURED IT. At
        contact scale the frame's own global `(u, v)` overstates by 1.54% on a
        5 mm dome and 0.51% on a 15 mm one, both inside the band by 8x. The one
        place it does not hold is the POLE, where `v = rho*phi` divides by a
        radius going to zero -- and that is exactly where a dome serpentine
        naturally starts, so it is excluded deliberately rather than
        discovered. That is the whole of the doubly-curved refusal now: a disc,
        not a surface.

        `patch` is the scale the chart is being used as a ruler at, in mm --
        one fibre diameter for the contact index, which is the scale every
        number above was measured at. The base class answers for the surfaces
        whose chart IS an isometry and otherwise defers to
        `require_developable`, so a frame that publishes no chart story of its
        own (a future mesh collector) is still refused rather than assumed.

        `v` and `h` complete the position. A caller that has them MUST pass
        them: `RevolutionFrame` has a second failure that depends on `v` and
        not on `patch` at all, and the pole test alone is the one that shipped
        first and was wrong -- see `ChartShearExcess`. They are optional only
        so that the plane and the cylinder, where neither test can fire, keep a
        one-argument call.

        `ruler` names what the caller measures distances with -- see
        `RevolutionFrame`'s override, the only place it selects anything, for the
        three values and what each earns. Accepted and ignored here: a frame
        whose chart IS an isometry has no shear to band, and one with no chart
        story of its own is refused whichever ruler the caller brought.
        """
        self.require_developable(what)

    def gap_at(self, u, v, h):
        """Nozzle-to-DEPOSITION-SURFACE distance at a point (§11 choice 3).

        The laws that take a gap (jet lag `H*tan(theta)`, and through it the
        placement model's `S`) must take it PER POINT, not as a constant: on a
        curved collector the gap varies along the path, and Saha measured the
        field strength falling 1.08e6 -> 4.91e5 V/m across a dome on that
        variation alone. The answer is `nominal - h` -- the point is the
        signature. It has NO CALLERS, and it models a FIXED nozzle: the mandrel
        generator lifts the nozzle one fibre diameter per ply, so
        `ContactPlacement.at_surface_radius` correctly passes its gap through
        unchanged rather than calling this.
        """
        if self.nozzle_gap is None:
            return None
        return self.nozzle_gap - h

    # -- the interface callers must go through (five-axis spec 5, rule 1) ---- #
    # Nothing outside this module may read `.k1`, `.k2` or `.radius`. A mesh
    # frame has no single global radius and computes curvature PER QUERY POINT,
    # so a bare attribute read is the assumption that breaks the arbitrary-STL
    # upgrade. Each method below replaces one such read at its former call
    # sites; nothing fails if one is put back.

    def covers(self, x, y, z):
        """Is this collector-frame point over the COLLECTOR'S OWN EXTENT?

        Not "is it close to the surface" -- the question is lateral: is there
        collector underneath it, or is the nozzle out beside the part. It is
        what decides where a lead-in travel becomes deposited fibre: on a rig
        whose g-code zero is off the collector (every five-axis rig -- the
        home is picked off the part on purpose, `gcode_player.py:4591`) the
        head flies in from beside the collector, and only the stretch after it
        crosses the rim is material lying on anything.

        TRUE ON AN UNBOUNDED COLLECTOR, which is the whole of the answer for
        the flat plate and the mandrel: a plane has no rim, and a rod's chart
        runs the length of the file. That is not a shortcut -- it is why the
        three- and four-axis lead-in behaviour is BYTE-IDENTICAL through this
        change, and they are the comparator it was built against (the flat
        plate and the mandrel already had correct travel-fibre
        behaviour). A surface of revolution is the
        only collector that answers anything else, and it answers with its own
        `u0..u1` span rather than with a distance threshold.
        """
        return True

    def under_nozzle(self, p, d):
        """Is there collector DIRECTLY UNDER the nozzle at this commanded point?

        `covers` asks whether the point's own surface coordinate is inside the
        collector's span, which on a curved collector is a RADIAL question --
        it opens a cone from the axis, and a cone is wider than the collector
        at any height above it. Measured on the dome preset: it answered yes
        while the nozzle was still 4.83 mm outside the collector in XY and
        0.49 s from being over it, which is how the travel fibre came to start
        -- and the jet with it -- before the nozzle had arrived.

        This asks the physical question instead: cast the NOZZLE's own axis
        `d` from the commanded point `p` (both in collector coordinates, so a
        five-axis rig's rotary pose is already in them) and see whether it
        enters the collector.

        TRUE HERE, and that is the whole of the answer for the plane and the
        mandrel: both are unbounded, `covers` is True on them everywhere, and
        their lead-in is the comparator this was built against. `d` is None
        wherever the caller has no nozzle axis to offer (any three- or
        four-axis parse), and then this is `covers` and nothing more.
        """
        return True

    def outermost_along(self, p, d):
        """How far along `d` from `p` the collector's OUTERMOST surface on the
        line `p + s*d` lies, mm -- or None when the line misses the collector.

        Negative when that surface is behind `p`, so with `d` the nozzle axis a
        point is clear of the part exactly when this is <= 0. It is what the
        five-axis travel router asks (`scaffold_gcode._travel_waypoints`): how
        far a commanded point must rise along the nozzle to stand over
        everything beneath it, posed as the collector is at that instant.

        Answered by the frame's own `_touchdown`, cast back from beyond the
        collector's reach, so the first surface met is the outermost one and a
        non-convex part is answered the same way the lead-in is laid. None
        here: the plane and the mandrel are unbounded and no five-axis travel
        is routed over them.
        """
        touch = getattr(self, "_touchdown", None)
        if touch is None:
            return None
        p = np.asarray(p, dtype=np.float64)
        d = np.asarray(d, dtype=np.float64)
        n = float(np.linalg.norm(d))
        if not n:
            return None
        d = d / n
        far = float(np.linalg.norm(p)) + 2.0 * self._reach() + 1.0
        hit = touch(p + far * d, -d)
        return None if hit is None else far - float(hit[0])

    def lay_lead_in(self, pts, dirs=None):
        """Drop a LEAD-IN travel chain onto this collector's surface, in place.

        A NO-OP HERE, AND THAT IS THE CONTRACT for the plane and the mandrel:
        their lead-in behaviour is the comparator this feature was built against
        and must not move by a micron. Only a collector the
        approach can arrive from OUTSIDE overrides it -- the same collectors
        whose `covers` can answer False.

        Gating this in the parser instead was measured wrong: keyed on "is this
        the first chain", it fired on a 4-axis mandrel too and lengthened its
        approach fibre from 40 mm to 40.0281 mm
        (`test_rotary_mount::a tube starts at its own origin`).
        """
        return

    def is_curved(self):
        """Do the curved geometric terms exist at all on this frame?

        The exact predicate `frame.k1 or frame.k2 or frame.period_v is not None`. False on a plane,
        where both metric stretches are 1 and every principal curvature is 0, so the caller
        skips the arithmetic outright and the flat solve stays byte-identical
        rather than merely equal.
        """
        k1, k2 = self.principal_curvatures(0.0)
        return bool(k1 or k2 or self.period_v is not None)

    def surface_radius(self, h=0.0):
        """Radius of curvature of the DEPOSITION surface at wall height h, mm.

        `1 / k2(h)` -- the curved principal direction -- which on a cylinder is
        `R0 + h`, the local surface radius Neuhaus's `r` means and the number
        `ContactPlacement.at_surface_radius` takes. `inf` on a plane, which is
        the right answer and is never reached: `placement_for_frame` returns
        None for anything flat, so the per-layer hook that consumes this cannot
        fire there.

        A doubly-curved frame has TWO of these and the placement model needs
        both semi-axes, which this method does not give.
        """
        k = self.principal_curvatures(h)[1]
        return math.inf if not k else 1.0 / k

    def seat_offset(self):
        """World translation that puts the BARE surface datum at the origin.

        `-to_world(0, 0, 0)`: zero on a plane, `(0, 0, -R0)` on a cylinder. The
        wrapped fibre measures from the collector's own centre while the
        standoff, the origin triad and every clearance check measure from a
        SURFACE, so one of the two has to be shifted and this is the shift.
        With a machine model the identical translation arrives inside
        `bed_group` (`machine_model.RotaryAxis.seat`), so the two paths agree by
        construction rather than by two copies of a constant.
        """
        return np.zeros(3)

    def mount_axial(self):
        """The collector's own AXIAL coordinate where it sits on its mount, mm --
        or None when the frame is not a body that stands on anything.

        A five-axis collector is a real part with a bottom edge, and that edge is
        what bolts to the table. Nothing else in this program knew where it was:
        the collector's frame origin is a construction point (the centre of
        curvature on a dome, so a point in mid-air INSIDE the part), and the
        machine model nailed that origin to the joint's picked axis point -- so a
        dome hung its rim `x(u1)` above the table it was supposed to be sitting
        on. Measured on the development rig: 3.624 mm on an R=10 dome spanning 0-12 mm,
        and proportionally more on a bigger one.

        THE FRAME ANSWERS IT, never the machine model, for the same reason
        `seat_offset` is the frame's: the number is a property of the collector's
        own geometry, and a machine page that invented one would be the
        `rotary.axis_height` failure again. `None` here means "this frame does
        not stand on a mount", which is the plane (the plate is the machine's
        body, not the frame's) and the cylinder (a mandrel is gripped by a chuck
        at its ENDS, and its placement is `RotaryAxis.seat` -- untouched).
        """
        return None

    def body_spec(self):
        """The collector BODY to draw and to mount, or None when there is none.

        A kind-tagged descriptor rather than a number, because the renderer's
        question ("what solid is the collector?") is not the solver's question
        ("how does it curve?") and only the second one generalises as a scalar.
        A plane has no body -- the build plate is the machine's, not the
        frame's. A cylinder answers `{"kind": "cylinder", "radius": R0}`. A
        surface of revolution will answer with its profile, which is what lets
        the player grow a dome body without reaching for an attribute that a
        mesh frame would not have.
        """
        return None

    def surface(self):
        """The collector as a `mew_surface` query object, in the same world XYZ
        `to_world` answers in: `settle`, `height`, `curvature`,
        `normal_curvature`, batched.

        THIS IS THE CAPABILITY A FIVE-AXIS CONSUMER ASKS, instead of asking the
        frame what shape it is. Every frame
        answers it. The base class is the flat bed: z = 0, out along +Z.
        """
        import mew_surface
        return mew_surface.PlaneSurface()

    def with_nozzle_gap(self, gap):
        """This frame, with a different nominal nozzle gap. Nothing else moves.

        Two call sites rebuilt a `CylinderFrame` from `frame.radius` and
        `frame.surface_z` purely to attach the run's working distance -- a
        reconstruction that hardcodes which subclass the frame is. A shallow
        copy is subclass-agnostic, so a frame family this module has not met yet
        inherits it working.
        """
        out = copy.copy(self)
        out.nozzle_gap = gap
        return out


class PlanarFrame(DepositionFrame):
    """The flat collector. Every method is inherited; nothing is overridden."""


class CylinderFrame(DepositionFrame):
    """A rotating mandrel of radius `radius`, its axis along machine X.

    surface_z: the g-code Z that corresponds to the BARE mandrel surface, i.e.
      the height datum. 0.0 means Z is measured outward from the surface (what
      you get by jogging down to touch and zeroing, and what our generator
      emits); pass `radius` for a rig whose Z is measured from the mandrel AXIS.
    """

    rotary = True
    #: The one frame whose callers hold chart columns: `parse_gcode` unrolls a
    #: four-axis file through `unroll` below before any engine sees it. See
    #: `DepositionFrame.chart_currency`.
    chart_currency = True

    def __init__(self, radius, surface_z=0.0, nozzle_gap=None):
        if not radius > 0.0:
            raise ValueError("mandrel radius must be positive, got %r" % (radius,))
        DepositionFrame.__init__(self, nozzle_gap=nozzle_gap)
        self.radius = float(radius)          # R0, the BARE mandrel radius
        self.surface_z = float(surface_z)
        self.k1 = 0.0                        # along the axis: a straight line
        self.k2 = 1.0 / self.radius          # around it
        self.period_v = 2.0 * math.pi * self.radius
        self.developable = True              # k1*k2 == 0

    # -- coordinates ------------------------------------------------------- #
    def unroll(self, x, y, z, a_deg=0.0):
        """(X, A, Z) -> (u, v, h). Y is not part of a 4-axis mandrel frame.

        `v = R0 * radians(A)` is UNWRAPPED on purpose: it is the arc length the
        surface has turned through since the start of the program, so a helix is
        a STRAIGHT LINE here and the solver's straight-segment machinery is
        exact. Wrapping happens in the contact index (`period_v`), where it is a
        binning question, not in the coordinate.
        """
        return x, self.radius * math.radians(a_deg), z - self.surface_z

    def to_world(self, u, v, h):
        phi = np.divide(v, self.radius)
        r = np.add(self.radius, h)
        return u, r * np.sin(phi), r * np.cos(phi)

    def from_world(self, x, y, z):
        """The inverse of `to_world`. `v` comes back on the PRINCIPAL branch.

        `atan2` answers in (-pi, pi], so a helix that has turned three times
        comes back inside one revolution: this is the point inverse, and the
        unwrapping that makes `v` the arc length turned SINCE THE START OF THE
        PROGRAM -- the thing that makes a helix straight -- is a property of a
        PATH, not of a point. `unwrap_path` does it.
        """
        r = np.hypot(y, z)
        return (x, self.radius * np.arctan2(y, z), r - self.radius)

    def metric_v(self, h):
        return (self.radius + h) / self.radius

    def metric(self, u, v, h=0.0):
        # `k1 = 0` along the axis, so `a` is EXACTLY 1.0 and every call site's
        # `a * du` is bit-exact; `rho' = 0`, so the shear `c` is exactly 0.0
        # and `dv - 0.0*du` is `dv` exactly. That is what makes this signature
        # change a BYTE statement on a cylinder rather than a tolerance. `b` is
        # `metric_v`'s expression VERBATIM, not `1 + k2*h`: the two are the
        # same number in real arithmetic and not always the same float64 (k2 is
        # `1/R0`, so `1 + (1/R0)*h` rounds three times against `(R0 + h)/R0`'s
        # once). Same reasoning, and the same stakes, as `surface_radius`.
        return (1.0, (self.radius + h) / self.radius, 0.0)

    def normal(self, u, v, h=0.0):
        phi = np.divide(v, self.radius)
        z = np.zeros_like(np.asarray(phi, dtype=np.float64))
        return z, np.sin(phi), np.cos(phi)

    # -- the interface, spelled so the arithmetic does not move -------------- #
    def surface_radius(self, h=0.0):
        # `R0 + h` verbatim, NOT `1/k2(h)`. They are the same number in real
        # arithmetic and not always the same float64: k2(h) is
        # `(1/R0)/(1 + h/R0)` and its reciprocal rounds twice more. The digest
        # guards are a byte statement, so the identity is spelled exactly the
        # way the call sites spelled it inline.
        return self.radius + h

    def seat_offset(self):
        return np.array([0.0, 0.0, -self.radius])

    def body_spec(self):
        return {"kind": "cylinder", "radius": self.radius}

    def surface(self):
        """The mandrel as an unbounded straight profile of radius R0 about X,
        the surface `to_world(u, v, 0)` draws."""
        import mew_surface
        return mew_surface.RevolutionSurface(LineProfile(self.radius),
                                             u0=None, u1=None)


# --------------------------------------------------------------------------- #
# The five-axis collector: one surface of revolution, several profiles.
#
# A profile curve spun about the machine X axis covers dome, cone, curved tube,
# bowl and torus with ONE surface class instead of a class per shape, and the
# cylinder falls out of it as the constant-radius case. The convention is the
# cylinder's, unchanged, so nothing above has a sign to hunt: x is the axis of
# revolution, +z points AT THE NOZZLE, phi is azimuth from top dead centre.
#
# The profile is parameterised by its own ARC LENGTH s (unit speed), which is
# what makes `u` a real distance on the surface rather than a parameter: on a
# cone or a dome a fixed step in a raw angle is a different distance at every
# radius, and that is precisely the mistake the flat solver cannot survive.
# --------------------------------------------------------------------------- #


class Profile(object):
    """A unit-speed meridian curve `(x(s), rho(s))` in the axis/radius plane.

    Three closed-form answers, never finite differences: the point, the unit
    tangent, and the meridian curvature. `test_five_axis_frame.py` checks all
    three AGAINST finite differences of the parameterisation, so the closed
    form cannot be graded by its own arithmetic.

    SIGN, and it is the one thing here worth getting right: curvature is
    positive where the surface curves AWAY from the outward normal, matching
    `CylinderFrame`'s `k2 = +1/R0` and the `k/(1 + k*h)` offset rule, which
    only shrinks a curvature as the wall grows if convex is positive. Concave
    is therefore negative and is representable rather than refused -- the
    five-axis spec puts concave in v1 because Terranova's
    validation collector carries a concave region and is our only external
    comparator.
    """

    #: True when the meridian is straight, i.e. k1 == 0 everywhere, which is
    #: what makes the swept surface developable.
    straight = False

    def point(self, s):
        """(x, rho) at meridian arc length s."""
        raise NotImplementedError

    def tangent(self, s):
        """(dx/ds, drho/ds) -- a UNIT vector, since s is arc length."""
        raise NotImplementedError

    def curvature(self, s):
        """Meridian curvature, convex-positive: `x'' rho' - x' rho''`."""
        raise NotImplementedError

    def invert(self, x, rho, s_hint=0.0):
        """`(s, h)`: the meridian point nearest `(x, rho)`, and its offset.

        The inverse of `point(s) + h*normal(s)` in the (axis, radius) half
        plane, which is the meridian half of `RevolutionFrame.from_world`.
        CLOSED FORM in both implementations -- a nearest-point root find would
        have been the general answer and is not needed for a line or a circle,
        and one that converged to a different branch on some points of a path
        would produce a `v` discontinuity indistinguishable from real geometry.

        `s_hint` picks the branch where the meridian revisits itself (a full
        circle does, every `2*pi*R`), by taking the solution nearest to it. A
        caller passes the middle of the span it expects. It is IGNORED where
        the meridian is injective, which is every straight one.

        `h` is SIGNED along the outward normal, so it is positive outside a
        dome and positive inside a bowl -- the same sign convention `to_world`
        takes, reached by inverting the same expression rather than by a second
        rule about which side is which.
        """
        raise NotImplementedError


class LineProfile(Profile):
    """A straight meridian: a cylinder at `half_angle` 0, a cone otherwise.

    `rho0` is the radius at s = 0 and `half_angle` is measured from the axis of
    revolution, so a positive angle opens the cone out along +x. k1 is exactly
    zero, so the swept surface is DEVELOPABLE and the flat sag solver applies
    to it unchanged -- the reason a cone was always the cheap next shape.
    """

    straight = True

    def __init__(self, rho0, half_angle=0.0, x0=0.0):
        self.rho0 = float(rho0)
        self.half_angle = float(half_angle)
        self.x0 = float(x0)
        self._c = math.cos(self.half_angle)
        self._s = math.sin(self.half_angle)

    def point(self, s):
        s = np.asarray(s, dtype=np.float64)
        return self.x0 + s * self._c, self.rho0 + s * self._s

    def tangent(self, s):
        z = np.zeros_like(np.asarray(s, dtype=np.float64))
        return self._c + z, self._s + z

    def curvature(self, s):
        return np.zeros_like(np.asarray(s, dtype=np.float64))

    def invert(self, x, rho, s_hint=0.0):
        """Project onto the line. Injective, so `s_hint` is unused."""
        dx = np.subtract(x, self.x0)
        dr = np.subtract(rho, self.rho0)
        s = dx * self._c + dr * self._s
        # the outward normal of a straight meridian is (-rho', x') = (-sin, cos)
        return s, -dx * self._s + dr * self._c


class ArcProfile(Profile):
    """A circular meridian of radius `arc_radius`: dome, bowl, sphere, torus.

    The meridian circle is centred at `(centre_x, centre_rho)` in the
    axis/radius plane and swept about the axis, so ONE class is four shapes:

      centre_rho = 0                  a sphere, and any cap of it -- a DOME
      centre_rho > arc_radius         a TORUS
      convex=False                    the same surface printed on its INSIDE:
                                      k < 0, the concave case

    A BOWL IS `convex=False` AND `t0 = pi`, AND THE SECOND HALF IS NOT
    COSMETIC. `convex=False` on its own flips the outward normal and moves
    nothing, so at the default `t0 = 0` the bowl comes out on the SAME cap as
    the dome -- pole at `x = +R` -- with its printable face pointing away from
    the nozzle instead of at it: a cup mounted mouth-down (measured in world
    millimetres off the player's own actor matrices; see
    `scaffold_gcode.surface_frame`). `t0 = pi` puts `s = 0` at the LOWER pole,
    so the cup's bottom is the part's bottom and the cavity opens upward. The
    normal points at the circle's centre either way; what `t0` chooses is which
    side of that centre the surface sits on.

    `t0` is where s = 0 sits on the meridian circle, in radians, measured the
    usual way from +x. On a sphere k1 == k2 exactly, which is the degeneracy
    section 2.1 of the spec turns into a decision: the lag anisotropy factor is
    a pure scale there, absorbed by `Calibration.lag_amplitude`, so the
    SIMPLEST v1 shape cannot be wrong from the one part of the law that is
    disfavoured. A torus is where the factor becomes a live prediction.
    """

    def __init__(self, arc_radius, centre_x=0.0, centre_rho=0.0, t0=0.0,
                 convex=True):
        if not arc_radius > 0.0:
            raise ValueError("arc radius must be positive, got %r"
                             % (arc_radius,))
        self.arc_radius = float(arc_radius)
        self.centre_x = float(centre_x)
        self.centre_rho = float(centre_rho)
        self.t0 = float(t0)
        self.convex = bool(convex)
        # Traversal direction. Reversing it flips the outward normal, which is
        # the whole mechanism by which the same circle is a dome one way and a
        # bowl the other -- no second class, and no sign switch bolted onto the
        # curvature afterwards where it could disagree with the normal.
        self._sigma = -1.0 if convex else 1.0

    def _t(self, s):
        s = np.asarray(s, dtype=np.float64)
        return self.t0 + self._sigma * s / self.arc_radius

    def point(self, s):
        t = self._t(s)
        return (self.centre_x + self.arc_radius * np.cos(t),
                self.centre_rho + self.arc_radius * np.sin(t))

    def tangent(self, s):
        t = self._t(s)
        return -self._sigma * np.sin(t), self._sigma * np.cos(t)

    def curvature(self, s):
        # `x'' rho' - x' rho'` works out to the constant -sigma/arc_radius; the
        # test still measures it against finite differences rather than taking
        # this line's word for it.
        z = np.zeros_like(np.asarray(s, dtype=np.float64))
        return z - self._sigma / self.arc_radius

    def invert(self, x, rho, s_hint=0.0):
        """Read the meridian circle in polar coordinates about its own centre.

        A point at offset h sits at `centre + (R - sigma*h)*(cos t, sin t)`, so
        the RADIUS from the centre carries h and the ANGLE carries s. Both fall
        out of one `atan2` and one `hypot`; there is no iteration and no
        tolerance. The `- sigma*h` is the traversal direction doing its second
        job: on a dome (sigma = -1) further from the centre is further OUT, on
        a bowl it is further in, which is the same one fact that makes the two
        shapes one class.
        """
        dx = np.subtract(x, self.centre_x)
        dr = np.subtract(rho, self.centre_rho)
        d = np.hypot(dx, dr)
        h = self._sigma * (self.arc_radius - d)
        s = self._sigma * self.arc_radius * (np.arctan2(dr, dx) - self.t0)
        # The meridian closes on itself every 2*pi*R, so `s` is only defined
        # modulo that; take the turn nearest the hint. Without this a cap
        # straddling t = pi comes back with a jump of one whole circumference
        # in the middle of a fibre.
        period = 2.0 * math.pi * self.arc_radius
        return s + np.round(np.divide(np.subtract(s_hint, s), period)) * period, h


class RevolutionFrame(DepositionFrame):
    """A surface of revolution about machine X -- the five-axis collector.

    (u, v, h) keeps the cylinder's meaning exactly: u is arc length along the
    MERIDIAN, v is arc length around the parallel AT THE BARE COLLECTOR, h is
    outward along the normal. On a constant-radius profile every formula below
    collapses to `CylinderFrame`'s, which is how a cylinder stays one surface
    and not a special case.

    THREE THINGS THIS FRAME REFUSES TO GUESS, each of them a plausible wrong
    number if it answered instead:

      `metric_v(h)` -- one scalar cannot describe a chart whose stretch varies
        along u, which it does on every profile except a constant-radius one.
        The cylinder case is answered exactly; everything else raises and points
        at `metric`, which replaces the scalar with the full first
        fundamental form. Guessing here is the global-chart assumption section
        5 rule 2 exists to stop.
      `period_v` -- the v axis wraps after `2*pi*rho(u)`, which is a FUNCTION of
        u on anything but a cylinder, so there is no scalar period to publish.
        None means "does not wrap", and `period_v_at` is the honest query.
      `unroll` -- a five-axis file carries TWO rotary words and their machine
        convention is not settled here; `machine_model` owns the kinematics
        and `gcode_to_fibers` the parser. Raising is what stops a three-axis unroll being
        quietly reused on a surface it does not describe.
    """

    rotary = True

    def __init__(self, profile, u0=0.0, u1=None, surface_x=0.0,
                 nozzle_gap=None):
        DepositionFrame.__init__(self, nozzle_gap=nozzle_gap)
        self.profile = profile
        #: the meridian arc-length span the collector actually spans, mm.
        self.u0 = float(u0)
        self.u1 = None if u1 is None else float(u1)
        self.surface_x = float(surface_x)
        # k1/k2 are the curvatures at the MIDDLE of the span -- a representative
        # value for the `is_curved` predicate and for a caller that only asks
        # "which way does this bend", never a substitute for the per-point
        # query. Section 5 rule 1 is why nothing outside this module reads them.
        k1, k2 = self.curvatures_at(self.u_mid)
        self.k1 = float(k1)
        self.k2 = float(k2)
        self.developable = bool(profile.straight)
        self.period_v = None

    @property
    def u_mid(self):
        return self.u0 if self.u1 is None else 0.5 * (self.u0 + self.u1)

    # -- the surface, per point -------------------------------------------- #
    def covers(self, x, y, z):
        """The base class's question, answered by this collector's own span.

        A surface of revolution is BOUNDED along its meridian -- `u0..u1` is
        the arc the physical part actually is -- so a point is over it when its
        meridian coordinate falls inside that span. `from_world` is what reads
        the coordinate, so the rim this tests against is the same rim
        `to_world` draws, not a second description of it.

        The AZIMUTH is not tested. `v` comes back on the principal branch and a
        full surface of revolution closes on itself, so every azimuth is over
        the part; only the meridian has ends. An open span (`u1 is None`) is
        unbounded above and defers to the base answer for that side.

        The HEIGHT is not tested either, and that is deliberate. `h` says how
        far off the surface the point sits, which is the standoff's business
        and varies along any real path; folding it in here would make a
        lateral question depend on a vertical tolerance nobody set.
        """
        return self._u_in_span(self.from_world(x, y, z)[0])

    def _span_bounds(self, span=True):
        """`(lo, hi)` for a `span` argument, or None where there is no gate.

        THREE SPELLINGS, because three different questions are asked of this
        surface and they do not share one answer:

          `True`      the PRINTED span, `u0..u1` -- everything that lays fibre;
          `False`     no gate at all, the whole meridian -- a convex closed
                      profile run round to the ball or the tube it belongs to;
          `(lo, hi)`  an explicit meridian window, which is what the
                      SILHOUETTE is (`_silhouette_span`).
        """
        if span is False:
            return None
        if span is not True:
            lo, hi = float(span[0]), float(span[1])
            return (lo, hi) if lo <= hi else (hi, lo)
        if self.u1 is None:
            return (self.u0, float("inf"))
        return ((self.u0, self.u1) if self.u0 <= self.u1
                else (self.u1, self.u0))

    def _u_in_span(self, u, span=True):
        """Is this meridian coordinate inside the window `span` names?"""
        b = self._span_bounds(span)
        if b is None:
            return True
        u = np.asarray(u, dtype=np.float64)
        return bool(np.all((u >= b[0]) & (u <= b[1])))

    def _silhouette_span(self):
        """The printed span run out to the WIDEST PARALLEL each way, or None.

        Where a collector ends is geometric rather than material: during the
        LEAD-IN, the collector
        extends as far as its silhouette seen from the nozzle, and the nozzle is
        over the part whenever it is over that outline. Seen down the axis of
        revolution that outline is a disc, and the edge of the disc is the
        meridian's widest parallel. Run the meridian past that and it turns
        back INWARD, which is the far side of the body and is not part of the
        silhouette at all -- it is the far wall the old unbounded reading kept
        finding.

        A STRAIGHT meridian has no widest parallel: a cone's radius grows
        without end, so there is nothing to stop at and this answers None. The
        caller keeps the printed span there, which is what `outermost_along`
        measured a cone needs.

        Walked rather than solved, because `profile` is an arbitrary `Profile`
        and a closed-form turning point would not serve all four shapes.
        Memoised: the profile does not move.
        """
        if hasattr(self, "_silhouette_span_mm"):
            return self._silhouette_span_mm
        per = getattr(self.profile, "arc_radius", None)
        out = None
        if per is not None and self.u1 is not None:
            lo, hi = self._span_bounds(True)
            step = 2.0 * math.pi * float(per) / 2048.0
            ends = []
            for end, way in ((hi, 1.0), (lo, -1.0)):
                us = end + way * step * np.arange(1, 1025)
                rr = np.abs(np.asarray(self.profile.point(us)[1], float))
                seq = np.concatenate(
                    [[abs(float(self.profile.point(end)[1]))], rr])
                dec = np.nonzero(np.diff(seq) < 0.0)[0]
                ends.append(end if not dec.size or not dec[0]
                            else float(us[dec[0] - 1]))
            out = (min(ends), max(ends))
        self._silhouette_span_mm = out
        return out

    def _reach(self, span=True):
        """Farthest the collector's own surface gets from the frame origin, mm.
        Bounds the ray march below; sampled off the profile, never assumed.

        `span=False` samples the WHOLE meridian instead of the printed arc,
        because that is the surface `outermost_along` marches over. A closed
        meridian (a circle) is sampled over one period; an open one (a line)
        has no farthest point, so the printed span's reach is doubled, which is
        the same slack the march already carries. A `(lo, hi)` window is
        sampled over itself, at the same resolution as the free case."""
        b = self._span_bounds(span)
        key = ("_reach_mm" if span is True
               else "_reach_free_mm" if span is False
               else "_reach_win_%r_%r_mm" % b)
        if getattr(self, key, None) is None:
            u0, u1 = self.u0, (self.u0 + 1.0 if self.u1 is None else self.u1)
            if span is False:
                per = getattr(self.profile, "arc_radius", None)
                if per is None:
                    u0, u1 = u0 - (u1 - u0), u1 + (u1 - u0)
                else:
                    u0, u1 = 0.0, 2.0 * math.pi * float(per)
            elif span is not True:
                u0, u1 = b
            uu = np.linspace(u0, u1, 65 if span is not True else 33)
            pts = [self.profile.point(float(u)) for u in uu]
            setattr(self, key,
                    float(max(math.hypot(x, rho) for x, rho in pts)))
        return getattr(self, key)

    def _ray_h(self, p, d, s, span=True):
        """`h` at `p + s*d`, or +inf where the ray is OFF the collector's span.

        Treating off-span as "infinitely far above" is what lets one scalar
        function answer both questions below: the ray is inside the collector
        exactly where this is non-positive, whether it left the span or simply
        has not reached the surface yet.

        `span=False` drops that gate and answers off the whole meridian --
        `profile.invert` is closed form and defined everywhere, so `h` is too.
        Only `outermost_along` asks that way; its docstring says why."""
        u, _v, h = self.from_world(*(p + s * d))
        if span is not False and not self._u_in_span(u, span):
            return float("inf")
        return float(h)

    def _touchdown(self, p, d, span=True):
        """Where the nozzle's axis first enters the collector: (s, point), or
        None if it never does.

        Coarse scan for the first non-positive sample, then BISECT -- on the
        SIGN of `_ray_h`, not on a tangency, so a ray grazing the rim converges
        on the rim itself instead of on an ill-conditioned double root. A coarse
        scan that finds nothing is re-run around the smallest `h` it did see,
        which is the thin chord a grazing ray cuts.

        No closed form is used on purpose: the profile is an arbitrary
        `Profile`, and a sphere's solution would not serve the cone, the bowl or
        the torus. A non-convex profile is answered by the FIRST entry, which is
        the topmost surface under the nozzle -- the one the fibre lands on.
        """
        p = np.asarray(p, dtype=np.float64)
        d = np.asarray(d, dtype=np.float64)
        n = float(np.linalg.norm(d))
        if not n:
            return None
        d = d / n
        smax = float(np.linalg.norm(p)) + 2.0 * self._reach(span)

        def bracket(ss):
            prev = None
            best, best_h = None, None
            for s in ss:
                h = self._ray_h(p, d, float(s), span)
                if h <= 0.0:
                    return (float(prev) if prev is not None else 0.0,
                            float(s)), None
                if np.isfinite(h) and (best_h is None or h < best_h):
                    best, best_h = float(s), h
                prev = s
            return None, best

        br, near = bracket(np.linspace(0.0, smax, 65))
        if br is None:
            if near is None:
                return None
            step = smax / 64.0
            br, _ = bracket(np.linspace(max(0.0, near - step),
                                        near + step, 65))
            if br is None:
                return None
        a, b = br
        for _ in range(60):
            mid = 0.5 * (a + b)
            if self._ray_h(p, d, mid, span) <= 0.0:
                b = mid
            else:
                a = mid
        s = 0.5 * (a + b)
        # ...AND THE ANSWER IS PUT BACK ON THE SURFACE. The bisection converges
        # on the sign of `_ray_h`, which flips either because the ray reached
        # the surface -- where this is a no-op, h is already 0 to 1e-9 -- or
        # because it crossed the span BOUNDARY with the surface already above
        # it, which is a nozzle looking past the rim edge. There the ray's own
        # point is under the collector (measured: 0.311 mm under, on
        # `test_five_axis_gcode`'s fixture) and the material actually lands on
        # the rim, so the rim is what is returned.
        u, v, _h = self.from_world(*(p + s * d))
        return s, np.asarray(self.to_world(u, v, 0.0), dtype=np.float64)

    def outermost_along(self, p, d):
        """The base class's question, answered over the WHOLE surface of
        revolution rather than over the PRINTED span.

        `covers` and `_touchdown` stop at `u0..u1`, the arc a fibre may be laid
        on, and everything that lays fibre wants exactly that. A TRAVEL asks a
        different question: the nozzle has to clear the physical collector, and
        a collector does not end where the pattern does -- a torus is a whole
        ring and a bowl a whole cap whatever band of one this design prints on.

        Asked WITH the span, the router saw no part at all just outside the
        band and left those stretches on the straight line: the torus's lead-in
        stayed 2.699 mm inside the tube (at u = -4.46, before the band starts)
        and the bowl's 2.440 mm inside its wall (at u = 18.12, past the band's
        end). `test_five_axis_matrix`'s oracle -- an independent closed form,
        the unbounded surface of revolution -- calls both a crash, and on the
        rig they are one.

        Answering without the span is the CONSERVATIVE of the two readings. It
        routes over part that may not be there, which costs a millimetre of
        travel height, where the span reading dives into part that may be.
        Nothing else asks this question -- `covers`, `under_nozzle`, the sag
        solver and the projection all keep the span.

        WHERE THE PART ENDS IS THE SILHOUETTE: during the LEAD-IN, the collector
        extends as far as its
        silhouette seen from the nozzle, and the nozzle is over the part
        whenever it is over that outline. `_silhouette_span` turns that into a meridian
        window: the printed span run out to the widest parallel each way, which
        is the edge of the outline seen down the axis.

        THREE READINGS, one rule, and the two that were already right do not
        move.

          A CONVEX closed meridian (a dome, a torus) keeps the UNGATED answer.
          Running its circle out gives the ball and the tube, and both are a
          subset of the silhouette solid -- neither carries the stalk the real
          part stands on -- so the ungated reading can only clear MORE than the
          silhouette asks, never less. Tightening it to the equator would move
          two shapes that are correct today to buy nothing.

          A STRAIGHT meridian (a cone) keeps the PRINTED span, because it has
          no widest parallel to stop at: its radius grows without end, so an
          extension invents part indefinitely. Measured: the cone's lead-in was
          read against material at u = 102 mm, 90 mm past the band, and the
          route answered with a 100 mm plunge and climb (Z 103.0 -> 1.4 -> 89.6)
          and still ran 1.512 mm inside.

          A CONCAVE closed meridian (a bowl) takes the SILHOUETTE WINDOW, and
          that is the change. Ungated it was measured asking for a 34.4 mm rise
          -- the FAR wall of the sphere, the upper hemisphere, read as the
          outermost surface -- and the window is exactly what excludes that:
          past the widest parallel the meridian turns back inward and is no
          longer the cup. Gated to the printed BAND instead, the router saw no
          part just past the rim and left the lead-in on the straight line;
          measured on the rig's own preset, two samples 0.6 mm past
          the band's end still read 0.082 mm inside the cup.

        `k1` is the meridian curvature at the middle of the span, positive
        where the surface curves away from the outward normal: `> 0` is
        convex and curved, `0` straight, `< 0` concave.
        """
        if self.k1 > 0.0:
            span = False                        # the ball, or the tube
        else:
            span = self._silhouette_span()      # the cup's outline...
            if span is None:
                span = True                     # ...or, on a cone, the band
        p = np.asarray(p, dtype=np.float64)
        d = np.asarray(d, dtype=np.float64)
        n = float(np.linalg.norm(d))
        if not n:
            return None
        d = d / n
        far = float(np.linalg.norm(p)) + 2.0 * self._reach(span) + 1.0
        hit = self._touchdown(p + far * d, -d, span=span)
        return None if hit is None else far - float(hit[0])

    def silhouette_radius(self):
        """How far the collector's OUTLINE reaches from its axis, mm.

        The disc `_silhouette_span` traces out, seen down the axis of
        revolution: the largest `|rho|` the collector's own meridian reaches.
        A straight meridian has no silhouette span, so its outline is the
        printed one's -- a cone's radius grows without end and nothing bounds
        it but the part.
        """
        if getattr(self, "_silhouette_radius_mm", None) is None:
            win = self._silhouette_span() or True
            lo, hi = self._span_bounds(win)
            uu = np.linspace(lo, hi, 129)
            self._silhouette_radius_mm = float(
                np.max(np.abs(np.asarray(self.profile.point(uu)[1], float))))
        return self._silhouette_radius_mm

    def over_silhouette(self, p):
        """Is this collector-frame point over the collector's own OUTLINE?

        Where a collector ends: the nozzle is over the part
        whenever it is over that outline. `to_world` writes
        `(y, z) = r*(sin phi, cos phi)` about machine X, so the distance from
        the axis is `hypot(y, z)` and the outline is a disc of
        `silhouette_radius` about it. No chart call and no span test: this is
        the FOOTPRINT question, which the meridian span is not.
        """
        p = np.asarray(p, dtype=np.float64)
        return bool(math.hypot(float(p[1]), float(p[2]))
                    <= self.silhouette_radius())

    def under_nozzle(self, p, d):
        """The base class's question, answered by casting the nozzle's axis
        AND requiring the nozzle to be over the collector's own outline.

        No nozzle axis -> the lateral answer, unchanged.

        THE RAY ALONE IS NOT THE QUESTION, AND A CUP IS WHERE THAT SHOWS. A
        ray cast test asks "does material leaving this nozzle reach the
        collector", and on a CONCAVE collector the answer is yes from a long
        way off: a cup catches a ray from a wide cone of directions, so the
        nozzle's axis entered the bowl's printed band, at its rim, from 35 mm
        outside the part. `gcode_to_fibers.take_travel` clips the lead-in by
        walking back while this answers True, so nothing was clipped at all;
        the whole 35 mm approach was laid on the rim, and
        `Player._lead_in_jet_gate` -- which lights the jet on the anchor's own
        first point -- drew a 35.03 mm jet from frame zero against a bound of
        3.79 mm, so on screen the jet stayed attached to the bowl for the
        whole simulation.

        The outline is the other half of the same sentence that settled
        `outermost_along`, and it costs the convex shapes nothing: measured on
        the rig's own preset, the dome's travel fibre starts 0.2389 mm from
        the rim, the cone's 0.0419 and the torus's 0.1462, all of them well
        inside their own outline before the ray ever fires.
        """
        if d is None:
            return self.covers(*p)
        if not self.over_silhouette(p):
            return False
        return self._touchdown(p, d) is not None

    def lay_lead_in(self, pts, dirs=None):
        """The base class's no-op, overridden because an approach to a BOUNDED
        collector arrives from off it.

        A lead-in is a MACHINE move, so its commanded points are wherever the
        axes happen to be rather than on the surface -- unlike every designed
        deposition point, which the surface generator projected there. Drawn as
        commanded, the clipped lead-in starts 5.609 mm off a 10 mm dome and
        hangs beside it before landing: ~250 fibre diameters of material
        attached to nothing, which is the thing this exists to avoid: the
        travel fibre shown is the one laid ON the collector.

        What this asserts is that the material lies on the collector, which is
        true. WHERE along the surface it lands during an approach is the aerial
        jet's business and is not claimed here any more than anywhere else --
        the physics relay reshapes this centreline exactly as it reshapes a
        designed one.

        THE LAST POINT IS LEFT ALONE, bit for bit: `merge_travels` splices the
        chain in sharing that vertex with the fibre it leads into, and it is
        already where that fibre starts.
        """
        for i in range(len(pts) - 1):
            # ALONG THE NOZZLE'S OWN AXIS wherever the caller knows it. Dropping
            # a point RADIALLY is a fall toward the collector's axis, and near
            # the rim -- where the surface is steep -- that lands well INWARD of
            # the nozzle: it left a bare band between the rim and the start of
            # the travel fibre, and a fibre appearing inward of the head read as
            # deposition starting early: a real travel fibre has
            # no gap between the rim and its start, because it cannot begin
            # partway up the collector out of nothing.
            # Material goes where the jet points, so the touchdown is the ray's
            # own entry point -- and the first one lands ON the rim, which is
            # also the vertex `take_travel` starts the chain at, so the fibre
            # begins exactly where the collector does.
            d = None if not dirs else dirs[i]
            hit = None if d is None else self._touchdown(pts[i], d)
            if hit is not None:
                pts[i] = [float(c) for c in hit[1]]
                continue
            u, v, _h = self.from_world(*pts[i])
            pts[i] = list(self.to_world(u, v, 0.0))

    def curvatures_at(self, u, h=0.0):
        """(k1, k2) of the DEPOSITION surface at meridian u and wall height h.

        k1 is the meridian curvature, k2 the parallel's `x'/rho`. Both are then
        pushed onto the offset surface by the same `k/(1 + k*h)` the base class
        uses, so a wall built up on a dome curves less than the dome does --
        one rule, every surface, exactly as `principal_curvatures` documents.
        """
        dx, _drho = self.profile.tangent(u)
        _x, rho = self.profile.point(u)
        k1 = self.profile.curvature(u)
        k2 = self._k2(dx, rho, k1)
        return (np.divide(k1, 1.0 + k1 * h), np.divide(k2, 1.0 + k2 * h))

    def metric(self, u, v, h=0.0):
        """`(a, b, c)` -- the base class documents the form. Four things here:

        THE SHEAR IS THE NEW TERM AND IT IS THE MERIDIAN'S. `c = rho'*phi`,
        with `phi = v/rho(u)` the same azimuth `to_world` uses. It is zero
        exactly where `rho' = 0`, which is a cylinder and nothing else, and it
        is why a doubly-curved collector could not be served by one scalar.

        THE POLE IS ANSWERED, AS A LIMIT, AND THE CALLER MUST NOT LEAN ON IT.
        At `rho = 0` a dome is umbilic: `curvatures_at` resolves `x'/rho`'s
        removable 0/0 to k1, `_phi` gives 0, so this returns
        `(1 + k1*h, 1 + k1*h, 0)` -- isotropic and finite, and the limit of the
        surrounding values. It matters because the pole is where a dome
        serpentine naturally starts. But the CHART is genuinely singular there
        and this form says so honestly a hair away rather than at the point:
        `c = rho'*v/rho` diverges as `rho -> 0` at fixed `v != 0`, so E
        diverges with it, because the chart's u-lines really do spiral into the
        pole. A path that reaches the pole with `v -> 0` alongside (the only
        kind `mew_project.march` will produce) stays bounded. Marching near the
        pole is refused a level up, by `mew_project.ChartSingularity`; this
        module reports the metric it has rather than duplicating that guard.
        A CONE APEX still raises `AxisSingularity` from `_k2`: no tangent
        plane, so no metric either.

        CONCAVE SHRINKS, AND CAN FOLD. `k < 0` gives a stretch below 1, and at
        `k*h = -1` the parallel surface has collapsed onto the centre of
        curvature. Refused there -- see `OffsetSurfaceFold`. At realistic walls
        it is nowhere near: a 15-layer 20 um wall on a 5 mm bowl is
        `k*h = -0.06`, a stretch of 0.94, 17x from the fold.

        THE BARE CURVATURES ARE THE RIGHT ONES. `curvatures_at(u, h)` returns
        the OFFSET surface's `k/(1 + k*h)`; the stretch that gets there is
        `1 + k*h` off the BARE `k`, so this asks at h = 0 and applies h itself.
        Asking at h and forming `1 + k_h*h` would apply the offset twice.
        """
        k1, k2 = self.curvatures_at(u, 0.0)
        a = 1.0 + np.multiply(k1, h)
        b = 1.0 + np.multiply(k2, h)
        self._refuse_fold(a, b, h)
        _x, rho = self.profile.point(u)
        _dx, drho = self.profile.tangent(u)
        return (a, b, np.multiply(drho, self._phi(v, rho)))

    @staticmethod
    def _refuse_fold(m1, m2, h):
        m = np.minimum(np.asarray(m1, dtype=np.float64),
                       np.asarray(m2, dtype=np.float64))
        if not np.all(m > 0.0):
            worst = float(np.min(m))
            raise OffsetSurfaceFold(
                "the deposition surface has folded through the collector's "
                "centre of curvature: the smaller principal stretch "
                "`1 + k*h` is %r at wall height h = %r, and a stretch <= 0 is "
                "not a length. This is a CONCAVE patch whose radius of "
                "curvature is no larger than the wall built on it. Lower the "
                "wall, or use a less concave collector."
                % (worst, float(np.max(np.asarray(h, dtype=np.float64)))))

    # -- where the meridian meets the axis ---------------------------------- #
    #
    # `k2 = x'/rho` and `phi = v/rho` both divide by the distance to the axis,
    # and a DOME'S POLE sits at rho = 0 -- which is exactly where a dome print
    # naturally starts. Two different things happen there and conflating them
    # is how one of them becomes a silently wrong number:
    #
    #   x' -> 0 as rho -> 0   the meridian meets the axis PERPENDICULAR to it.
    #                         `x'/rho` is 0/0 and the limit exists: L'Hopital
    #                         gives `x''/rho'`, and with x' = 0 the meridian
    #                         curvature is `k1 = x'' rho'` on a unit-speed
    #                         profile where rho' = +-1, so the limit is exactly
    #                         k1. The pole is UMBILIC -- a sphere curves the
    #                         same way in every direction there -- and every
    #                         query has a finite answer.
    #   x' != 0 at rho = 0    a CONE APEX. `x'/rho` genuinely diverges, there
    #                         is no tangent plane, and no limit to return.
    #                         Refused with `AxisSingularity` rather than
    #                         answered, for the same reason `metric_v` refuses.
    #
    # Only an EXACT zero is treated as the pole. There is no tolerance band,
    # deliberately: one nanometre off the pole the ordinary division is already
    # correct to all 16 digits (measured: rho = -1e-9 gives
    # 0.02857142857142857 against a limit of 1/35), so a band would replace
    # good arithmetic with an assumption over a region where none is needed.

    @staticmethod
    def _pole(rho):
        return np.asarray(rho, dtype=np.float64) == 0.0

    def _k2(self, dx, rho, k1):
        """`x'/rho`, with the pole's removable 0/0 resolved to k1."""
        pole = self._pole(rho)
        if not pole.any():
            return np.divide(dx, rho)      # the ordinary path, untouched
        self._refuse_apex(dx, pole)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(pole, k1, np.divide(dx, rho))

    def _refuse_apex(self, dx, pole):
        """Raise if any pole is a cone apex rather than an umbilic pole."""
        bad = pole & (np.asarray(dx, dtype=np.float64) != 0.0)
        if bad.any():
            raise AxisSingularity(
                "the meridian meets the axis at a non-zero slope (x' = %r), "
                "which is a CONE APEX: the surface has no tangent plane there, "
                "so neither the normal nor the circumferential curvature "
                "exists. A sphere's pole is not this -- it is umbilic and is "
                "answered. Move the query off the apex, or model the collector "
                "as the truncated cone it physically is."
                % (float(np.asarray(dx).ravel()[0]),))

    def _phi(self, v, rho):
        """Azimuth `v/rho`, defined as 0 at a pole.

        Every azimuth names the same point at a pole, so the value is arbitrary
        there -- but it must be FINITE, because it is multiplied by a radius
        that is itself zero and `0 * nan` is nan, not 0. That multiplication is
        why a dome apex came back as `(35.01, nan, nan)` from `to_world` rather
        than merely carrying a nan curvature.
        """
        pole = self._pole(rho)
        if not pole.any():
            return np.divide(v, rho)       # the ordinary path, untouched
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(pole, 0.0, np.divide(v, rho))

    def principal_curvatures(self, h=0.0):
        """The base-class signature, answered at the span's middle.

        A general surface's curvature is a function of WHERE and this signature
        has no u, so it answers for the representative point the constructor
        picked. `curvatures_at` is the real query and is what the projection
        and lag layers must use.
        """
        return (np.divide(self.k1, 1.0 + self.k1 * h),
                np.divide(self.k2, 1.0 + self.k2 * h))

    def _curvatures_here(self, u, h=0.0, v=None):
        """The base class's per-point hook, answered by the real query.

        `v` IS IGNORED, and on this frame that is exact rather than a
        simplification: a surface of revolution curves the same way all the way
        round a parallel. The base class documents why the argument exists.

        This is the whole of `RevolutionFrame`'s share of `chord_drop_at`: the
        base class's version returns the span-midpoint `principal_curvatures`,
        which is a representative number this frame's own docstring warns is
        never a substitute for the per-point query -- and on a curved meridian
        the sag law was reading exactly that. One line, and Euler's theorem is
        evaluated where the fibre is.
        """
        return self.curvatures_at(u, h)

    def period_v_at(self, u):
        """Circumference of the parallel through u, mm -- the local v period."""
        _x, rho = self.profile.point(u)
        return 2.0 * math.pi * float(rho)

    def chart_exclusion(self, u, patch):
        """Clearance from the axis this chart needs at `u`, mm, at scale `patch`.

        `POLE_PATCHES * patch * |rho'|`, per point. The `|rho'|` is what makes
        this one expression cover four shapes rather than a dome's number
        applied to everything: the chart's shear is `rho'*phi`, so a surface
        whose meridian runs PARALLEL to the axis has no exclusion at all
        (a cylinder, `rho' = 0`, exactly the frame whose unroll is an isometry)
        and a shallow cone has a proportionally smaller one (`|rho'| = sin` of
        its half angle). A dome at its pole has `|rho'| = 1` and gets the full
        `8*patch` measured there.
        """
        _dx, drho = self.profile.tangent(u)
        return POLE_PATCHES * float(patch) * np.abs(
            np.asarray(drho, dtype=np.float64))

    def chart_overstatement(self, u, v, h=0.0):
        """How much the chart OVERSTATES a step, worst direction, as a fraction.

        The chart maps a step `(du, dv)` to the physical displacement
        `(a*du, b*(dv - c*du))` -- `metric`'s own form -- so the Jacobian is
        `J = [[a, 0], [-b*c, b]]` and a chart length divided by the physical
        one is at most `1/sigma_min(J)`. Written from the eigenvalues of
        `J'J = [[a^2 + (b*c)^2, -b^2*c], [-b^2*c, b^2]]` rather than by calling
        an eigensolver, because this runs on every point of every fibre.

        Zero on a plane and on a cylinder, exactly: there `c = 0` and `a = 1`,
        so `J` is diagonal with `b >= 1` and the chart never overstates at all
        -- it UNDERSTATES by the offset stretch, which `_SegIndex`'s own
        contract says is safe (extras cost time and cannot change a result).

        The arithmetic itself is `overstatement_from_metric`, because a caller
        that already holds `(a, b, c)` per point -- `_SegIndex.rest_heights`
        evaluates them to measure through -- must get the same number the gate
        was checked with.
        """
        return overstatement_from_metric(*self.metric(u, v, h))

    def require_chart(self, what, u, patch, v=None, h=0.0, ruler="raw"):
        """The base class's question, and it takes TWO answers on this surface.

        A doubly-curved collector is no longer refused outright. Two separate
        things can still make its `(u, v)` a bad ruler, they fail in different
        places, and only the first was in the spec:

          THE POLE, a DISC around the axis of revolution, where `v = rho*phi`
            divides by a vanishing radius. `chart_exclusion` gives the radius
            and `POLE_PATCHES` says where the number comes from. This one does
            scale with the patch.
          THE SHEAR, and it has NO scale. `c = rho'*phi` grows with the
            ABSOLUTE azimuth, so it is not made smaller by looking at a smaller
            patch and no exclusion radius bounds it. `ChartShearExcess`
            carries the measurement; the short version is that turning a dome
            print about its own axis, a rigid motion, moved the solved geometry
            by 12.85 um while a cylinder moved by exactly zero.

        `v` is therefore not optional in practice -- omitted, only the pole is
        checked, and the caller is trusting the term that turned out to be
        dominant. It defaults to None rather than 0.0 so that omitting it is a
        visible choice at the call site instead of a silently flattering one.

        BOTH REFUSALS NAME A NUMBER THE USER CAN ACT ON. The pole's is a
        distance to move by; the shear's is the angular half-width the pattern
        would have to fit inside, because `c` is bounded by `|rho'|*max|phi|`
        and the pattern's angular extent is the only part of that a designer
        controls.

        `ruler` NAMES WHAT THE CALLER MEASURES WITH, and it selects the SHEAR
        band only (`_SHEAR_BANDS`). It is a three-way and not a boolean on
        purpose: one `cost_band` flag covering both non-raw cases would reuse a
        COST argument as an ACCURACY allowance for an
        engine that never corrects the chart. The three are not interchangeable.

          "raw" (the default) -- the caller measures raw chart deltas, so the
            shear is an ACCURACY error that silently drops a support. Held to
            `POLE_BAND`, the 12.5% a contact index can absorb. Every developable
            frame's solve, and anything that has not said otherwise.
          "local" -- the caller widens its own reach by `chart_overstatement`
            and measures through `local_offsets`, so the shear is exact for it
            and only expensive: `_SegIndex.rest_heights` widens `k` and reads
            `(2k+1)^2` cells, and past `CHART_COST_BAND` the widened block stops
            being affordable rather than stops being right.
            `gcode_to_fibers.apply_sag` on a doubly-curved frame.
          "approx" -- the caller measures in the global chart and does NOT
            correct for the shear, so the band is not something it satisfies by
            construction; it is how far it has been MEASURED, which is
            `CHART_APPROX_BAND` and no further. `sag_approx` and
            `preview_sag.drape_sag`. The number is in that constant's own note.

        The POLE is unchanged by all three -- there the Jacobian is genuinely
        singular (`c = rho'*v/rho`), so no ruler helps.
        """
        patch = float(patch)
        if not patch > 0.0:
            raise ValueError("the chart's patch scale must be positive, got %r"
                             % (patch,))
        u = np.atleast_1d(np.asarray(u, dtype=np.float64))
        _x, rho = self.profile.point(u)
        rho = np.abs(np.atleast_1d(np.asarray(rho, dtype=np.float64)))
        need = np.atleast_1d(self.chart_exclusion(u, patch))
        need = np.broadcast_to(need, rho.shape)
        i = int(np.argmin(rho - need))
        if rho[i] < need[i]:
            raise ChartPoleExclusion(
                "%s cannot use this collector's (u, v) chart where the pattern "
                "goes: at meridian u = %.6g mm it comes %.6g mm from the axis "
                "of revolution, inside the %.6g mm POLE EXCLUSION for a %.6g mm "
                "patch (%s). The chart is `v = rho*phi` and rho -> 0 there, so "
                "two points one fibre apart on the collector can be any "
                "distance apart in the chart -- and the contact index measures "
                "in the chart, so it would drop the supports rather than "
                "report anything. The surface itself is fine: a dome's pole is "
                "umbilic. Move the pattern off the pole (a %.6g mm shift is "
                "enough), or print a collector whose axis it never approaches."
                % (what, float(u[i]), float(rho[i]), float(need[i]), patch,
                   type(self).__name__, float(need[i] - rho[i])))
        if v is None:
            return
        v = np.broadcast_to(np.atleast_1d(np.asarray(v, dtype=np.float64)),
                            u.shape)
        over = np.broadcast_to(
            np.atleast_1d(self.chart_overstatement(u, v, h)), u.shape)
        j = int(np.argmax(over))
        band = _SHEAR_BANDS[ruler]
        if over[j] > band:
            _dx, drho = self.profile.tangent(u[j])
            slope = abs(float(np.asarray(drho).ravel()[0]))
            # `c = rho'*phi` and the band is on `c`, so the widest azimuth this
            # collector can carry is band/|rho'|. Reported as a HALF-width
            # in degrees and as an arc length at this radius, because the
            # designer's field is a pattern width in mm.
            phi_ok = (band / slope) if slope > 0.0 else float("inf")
            raise ChartShearExcess(
                "%s cannot use this collector's (u, v) chart where the pattern "
                "goes: at meridian u = %.6g mm, azimuth %.4g rad, the chart "
                "overstates a distance by %.1f%% in its worst direction, past "
                "the %.1f%% %s (%s). This is the "
                "chart's SHEAR, `c = rho'*phi`: the u and v axes are not "
                "perpendicular wherever the meridian's radius varies, and it "
                "grows with ABSOLUTE azimuth, so it is not a pole and no "
                "exclusion radius bounds it. %s Keep the pattern inside "
                "+-%.4g rad (%.2f deg, about %.3f mm of arc at this radius) of "
                "the chart's v = 0, or use a collector whose meridian is "
                "straighter here (|rho'| = %.4g)."
                % (what, float(u[j]), float(v[j] / max(float(rho[j]), 1e-300)),
                   100.0 * float(over[j]), 100.0 * band,
                   _BAND_WHAT[ruler],
                   type(self).__name__, _BAND_WHY[ruler],
                   phi_ok, math.degrees(phi_ok),
                   phi_ok * float(rho[j]), slope))

    def to_world(self, u, v, h):
        """(u, v, h) -> collector-fixed XYZ. Scalars or arrays.

        The point is taken h out along the surface normal, which on a curved
        meridian moves it along BOTH axis and radius -- `x - h*rho'` and
        `rho + h*x'`. On a cylinder `x' = 1, rho' = 0` and it reduces to
        `(u, (R0+h) sin phi, (R0+h) cos phi)`, term for term.
        """
        x, rho = self.profile.point(u)
        dx, drho = self.profile.tangent(u)
        self._refuse_apex(dx, self._pole(rho))
        phi = self._phi(v, rho)
        r = rho + np.multiply(h, dx)
        return (x - np.multiply(h, drho) + self.surface_x,
                r * np.sin(phi), r * np.cos(phi))

    def _off_collector(self, s, h):
        """How far `(s, h)` is from the collector as it is actually defined, mm.

        The wall height plus whatever part of `s` overhangs the printed span.
        A tie-break, not a tolerance: it is only ever compared between the two
        branches of one query, and the right branch scores the deposition
        height itself -- microns -- against millimetres or tens of them.
        """
        s = np.asarray(s, dtype=np.float64)
        out = np.abs(np.asarray(h, dtype=np.float64))
        out = out + np.maximum(0.0, self.u0 - s)
        if self.u1 is not None:
            out = out + np.maximum(0.0, s - self.u1)
        return out

    def from_world(self, x, y, z):
        """The inverse of `to_world`, in the two halves the surface has.

        The meridian half is `profile.invert`, in the (axis, radius) plane --
        and the radius it is given is `hypot(y, z)`, the distance from the AXIS
        of revolution, which is the one quantity a rotation about that axis
        cannot change. The azimuth half is `atan2(y, z)`, matching the
        `(r sin phi, r cos phi)` `to_world` writes, and `v = rho(u)*phi` puts
        it back in the chart's arc-length units at the BARE collector -- the
        same `rho` the forward map divided by, so the two cannot disagree.

        `v` COMES BACK ON THE PRINCIPAL BRANCH, in (-pi, pi] times rho, for the
        reason `CylinderFrame.from_world` gives: unwrapping is a path property.
        The meridian's own branch IS resolved here, against the middle of the
        collector's span, because a cap straddling the profile circle's t = pi
        would otherwise return two u values a circumference apart for two
        neighbouring points of one fibre.

        AND THE PROFILE'S RADIUS IS SIGNED, WHICH IS THE TRAP. `hypot(y, z)` is
        a distance and is never negative; `profile.point`'s rho is a
        COORDINATE and on a DOME it is negative past the pole -- s = 0 sits on
        the axis and the traversal runs `t` downwards, so `rho = R sin(t) < 0`
        for the whole printed span. The forward map is consistent with itself
        there (a negative r and an azimuth turned by pi name the same world
        point), so nothing upstream ever had to notice. Read back through
        `+hypot` alone a 10 mm dome came home 18.4 mm wrong in u, on a 12 mm
        span, while a bowl, a cone and a torus -- whose rho stays positive --
        were exact to 2e-15.

        SO BOTH SIGNS ARE TRIED, AND THE AMBIGUITY IS REAL RATHER THAN
        NUMERICAL. A world point does not name one meridian point: a sphere's
        profile crosses every parallel TWICE, once either side of its pole, and
        the two are the same distance from the axis and the same distance from
        the centre. What separates them is the COLLECTOR -- which arc of the
        meridian circle the physical part is, `u0..u1`, and how far off its
        surface the point sits. So the branches are scored by exactly that:
        `|h|`, plus however far `s` falls outside the span. Nearest-to-`u_mid`
        alone is not enough and was measured wrong -- on a torus the far branch
        came back 0.55 mm CLOSER to the span's middle than the right one, while
        sitting 30 mm off the collector.
        """
        y = np.asarray(y, dtype=np.float64)
        z = np.asarray(z, dtype=np.float64)
        xa = np.subtract(x, self.surface_x)
        r = np.hypot(y, z)
        up, hp = self.profile.invert(xa, r, s_hint=self.u_mid)
        un, hn = self.profile.invert(xa, -r, s_hint=self.u_mid)
        take_pos = self._off_collector(up, hp) <= self._off_collector(un, hn)
        u = np.where(take_pos, up, un)
        h = np.where(take_pos, hp, hn)
        # `sgn` is the sign of the SIGNED parallel radius `r = rho + h*x'` the
        # branch above chose. `to_world` writes (y, z) = r*(sin phi, cos phi),
        # so with r negative the azimuth is turned by pi and reading `atan2(y,
        # z)` straight would put the point on the far side of the collector.
        sgn = np.where(take_pos, 1.0, -1.0)
        _x, rho = self.profile.point(u)
        return u, np.multiply(rho, np.arctan2(sgn * y, sgn * z)), h

    def normal(self, u, v, h=0.0):
        """Outward unit normal in collector-fixed XYZ.

        `(-rho', x')` in the meridian plane, spun to azimuth phi. It does not
        depend on h: a parallel surface shares its normals with the surface it
        is offset from, which is the same fact `principal_curvatures` uses.
        """
        _x, rho = self.profile.point(u)
        dx, drho = self.profile.tangent(u)
        self._refuse_apex(dx, self._pole(rho))
        phi = self._phi(v, rho)
        zero = np.zeros_like(np.asarray(phi, dtype=np.float64))
        return (-drho + zero, dx * np.sin(phi), dx * np.cos(phi))

    def surface_radius(self, h=0.0):
        k = self.principal_curvatures(h)[1]
        return math.inf if not k else 1.0 / k

    def seat_offset(self):
        _x, rho = self.profile.point(self.u_mid)
        return np.array([0.0, 0.0, -float(rho)])

    def mount_axial(self):
        """The lowest point of the SPANNED surface along the axis of revolution.

        The span IS the collector -- `gui_surface._build_surface_collector_fields`
        says so in as many words ("the part of the surface that physically
        exists") -- so the part's bottom edge is the minimum of `x(u)` over
        `[u0, u1]`, plus `surface_x`. One rule covers every shape rather than a
        per-shape table: a dome's meridian runs downhill from the pole, so the
        minimum is at `u1`; a cone's runs uphill from its base, so it is at `u0`;
        a torus can take it in the interior, which is why this samples rather
        than testing the two ends.

        A one-point span (`u1 is None`) is a single parallel with no extent, and
        its own x is the answer.
        """
        if self.u1 is None:
            x, _rho = self.profile.point(self.u0)
            return float(x) + self.surface_x
        us = np.linspace(self.u0, self.u1, 257)
        xs, _rho = self.profile.point(us)
        return float(np.min(xs)) + self.surface_x

    def body_spec(self):
        return {"kind": "revolution", "profile": self.profile,
                "u0": self.u0, "u1": self.u1}

    def surface(self):
        """The spanned surface, `u0..u1` of the profile, in closed form."""
        import mew_surface
        return mew_surface.RevolutionSurface(self.profile, u0=self.u0,
                                             u1=self.u1,
                                             surface_x=self.surface_x)

    # -- and the three it refuses to guess ---------------------------------- #
    def metric_v(self, h):
        if not (self.profile.straight
                and getattr(self.profile, "half_angle", 1.0) == 0.0):
            raise NonDevelopableFrame(
                "metric_v is ONE scalar and this surface's v-stretch varies "
                "along u; a general surface needs the full first fundamental "
                "form (E, F, G). Returning a representative number here would "
                "assume one global (u, v) chart, which this surface does not "
                "have.")
        _x, rho = self.profile.point(self.u0)
        rho = float(rho)
        return (rho + h) / rho

    def unroll(self, x, y, z, a_deg=0.0):
        raise NotImplementedError(
            "a five-axis file carries TWO rotary words and their machine "
            "convention is not settled in this module. Reusing the three-axis "
            "unroll here would put every point on a surface this frame does "
            "not describe.")


#: Ceiling on the pieces one segment may be cut into. It exists for the pole:
#: the turning rate of a parallel is `1/rho`, which diverges on the axis of
#: revolution, so without a ceiling one segment grazing a dome's pole would try
#: to allocate an unbounded array. `mew_project` REFUSES to march near the pole
#: for the same reason; this is the renderer's version of the same guard, and it
#: degrades to a very fine polyline rather than to an error because a viewer
#: must still draw something.
MAX_SUBSTEPS = 100000


def _substeps(frame, a, b, max_step_deg):
    """Pieces each segment is cut into so the WORLD polyline stays smooth.

    The measure is how far the drawn curve TURNS over the segment, capped at
    `max_step_deg` per piece. A move in `v` at radius rho is an arc of that
    radius, so it turns `|dv| / rho`; a move in `u` follows the meridian and
    turns `|du| * |k1|`. The two are added, which is exact for a move along
    either axis and conservative for a diagonal.

    THE CYLINDER KEEPS ITS OWN ARITHMETIC, and that is not a style choice. The
    shipped expression is `ceil(|dv| / (R0 * radians(deg)))`; the general one
    reaches the same real number by different float64 operations, and `ceil`
    turns a one-ulp disagreement into a different point count and a different
    STL. The digest guards are a byte statement, so the cylinder branch is
    spelled exactly as it shipped -- with the bare radius attribute replaced by
    `surface_radius(0.0)`, which `CylinderFrame` returns as `R0 + 0.0`: the
    same float, reached through the section 5 rule 1 interface.
    """
    if frame.period_v is not None:              # a cylinder: unchanged
        step_v = abs(frame.surface_radius(0.0) * math.radians(max_step_deg))
        if step_v > 0:
            return np.maximum(
                1, np.ceil(np.abs(b[:, 1] - a[:, 1]) / step_v)).astype(np.int64)
        return np.ones(len(a), dtype=np.int64)

    curv = getattr(frame, "curvatures_at", None)
    if curv is None:
        raise NotImplementedError(
            "%s is curved but publishes no per-point curvature, so there is no "
            "honest way to choose a substep: `principal_curvatures` is ONE "
            "number for the whole surface and using it here would be the "
            "global-chart assumption the five-axis spec's section 5 rule 2 "
            "forbids. Implement `curvatures_at(u, h)`."
            % (type(frame).__name__,))

    u_mid = 0.5 * (a[:, 0] + b[:, 0])
    k1 = np.abs(np.asarray(curv(u_mid, 0.0)[0], dtype=np.float64))
    _x, y, z = frame.to_world(u_mid, np.zeros_like(u_mid), 0.0)
    rho = np.hypot(y, z)
    turn = np.abs(b[:, 0] - a[:, 0]) * k1
    np.add(turn, np.divide(np.abs(b[:, 1] - a[:, 1]), rho,
                           out=np.full_like(rho, np.inf), where=rho > 0.0),
           out=turn)
    k = np.ceil(turn / math.radians(max_step_deg))
    return np.clip(np.nan_to_num(k, nan=1.0, posinf=MAX_SUBSTEPS),
                   1, MAX_SUBSTEPS).astype(np.int64)


def densify(frame, pts, max_step_deg=2.0):
    """Surface-frame polyline -> (dense surface points, their VERTEX PARAMETER).

    The parameter is the position along the ORIGINAL polyline in vertex units --
    `2.5` is halfway along the segment from `pts[2]` to `pts[3]` -- which is what
    lets anything running parallel to the geometry (print times, per-point
    diameters, a colour) be resampled onto the dense points with one `np.interp`
    instead of being recomputed.

    Flat, or fewer than two points: the input verbatim, `arange(N)`.
    """
    pts = np.asarray(pts, dtype=np.float64)
    n = len(pts)
    if n < 2 or not frame.is_curved():
        return pts, np.arange(n, dtype=np.float64)
    # Vectorised, and deliberately bit-identical to the per-segment Python loop
    # this replaces: the same `a + (b - a) * (i / k)` in the same order, with the
    # same float64 rounding, just evaluated for every (segment, substep) pair at
    # once. It matters because it is not a small cost -- the tube preview wraps
    # ~0.5 M points per rebuild, and the loop was a third of the whole build.
    a = pts[:-1]
    b = pts[1:]
    k = _substeps(frame, a, b, max_step_deg)
    total = int(k.sum())
    seg = np.repeat(np.arange(n - 1, dtype=np.int64), k)
    # i = 1..k within each segment, from a running offset
    i = (np.arange(total, dtype=np.int64)
         - np.repeat(np.cumsum(k) - k, k) + 1)
    f = i / k[seg]
    out = np.empty((total + 1, pts.shape[1]), dtype=np.float64)
    out[0] = pts[0]
    out[1:] = a[seg] + (b[seg] - a[seg]) * f[:, None]
    par = np.empty(total + 1, dtype=np.float64)
    par[0] = 0.0
    par[1:] = seg + f
    return out, par


def wrap_polyline(frame, pts, max_step_deg=2.0):
    """Surface-frame polyline -> world polyline, densified across the curvature.

    A segment that is straight in (u, v) is a geodesic HELIX in world, so the
    two endpoints alone would draw a chord through the mandrel. Anything that
    renders or measures in world -- the viewer, the tube sweep, the radial
    clearance check -- has to walk it. The solver does not, and must not: it
    works in (u, v, h) where the segment really is straight.

    `pts` is (N, 3) in (u, v, h). Returns (M, 3) world XYZ, M >= N.
    """
    out, _par = densify(frame, pts, max_step_deg)
    return np.column_stack(frame.to_world(out[:, 0], out[:, 1], out[:, 2]))


def wrap_path(frame, pts, times=None, max_step_deg=2.0, parallel=None):
    """`wrap_polyline`, with any parallel per-point array carried across.

    Returns `(world, times)` -- `times` resampled onto the densified points, or
    None when none was given. The densification is what makes this necessary:
    a caller that wrapped the geometry and kept the old times would hand every
    consumer downstream two arrays of different lengths, and the ones that index
    positionally (the deposition reveal, the `.depo` cache) would silently
    misalign rather than raise.

    `parallel` is a list of FURTHER per-point arrays to carry across the same
    densification, returned as a third element `(world, times, carried)`. The live
    march needs it for the per-point DIAMETER, which the tube and the jet index
    positionally against the geometry -- the same misalignment `times` is carried
    to avoid. They ride the interpolation PARAMETER, not the time values, so an
    array that is not monotonic in time (a diameter is not) still lands on the
    right point.
    """
    dense, par = densify(frame, pts, max_step_deg)
    world = np.column_stack(frame.to_world(dense[:, 0], dense[:, 1], dense[:, 2]))
    n = len(np.asarray(pts, dtype=np.float64))
    idx = np.arange(n, dtype=np.float64)

    def _carry(a, what):
        a = np.asarray(a, dtype=np.float64)
        if len(a) != n:
            raise ValueError("%s must be parallel to pts (%d vs %d)"
                             % (what, len(a), n))
        return np.interp(par, idx, a)

    carried = ([_carry(a, "parallel array") for a in parallel]
               if parallel is not None else None)
    wt = None if times is None else _carry(times, "times")
    if parallel is None:
        return world, wt
    return world, wt, carried


def _v_period(frame, u):
    """The `v` wrap LENGTH at each `u`, mm, or None where v does not wrap.

    Absolute: `period_v_at` returns `2*pi*rho` and a dome's rho is negative
    past its pole (see `RevolutionFrame.from_world`), so the signed answer is
    a coordinate and this is a distance.
    """
    at = getattr(frame, "period_v_at", None)
    if at is not None:
        return np.abs(np.array([at(float(x)) for x in np.atleast_1d(u)],
                               dtype=np.float64))
    per = getattr(frame, "period_v", None)
    if per is None:
        return None
    return np.full(np.shape(np.atleast_1d(u)), abs(float(per)),
                   dtype=np.float64)


def unwrap_path(frame, world):
    """World polyline -> an (N, 3) surface-frame polyline, `v` made CONTINUOUS.

    The read-back counterpart of `wrap_path`, and the second half of what
    `gcode_to_fibers` needs to sag a file it did not keep the chart for:
    `FiveAxisReader` brings a five-axis file back to the collector's own XYZ,
    and this brings that to `(u, v, h)`.

    IT DOES NOT UNDENSIFY, and nothing here pretends the round trip is
    lossless in the other direction: `wrap_path` inserts points so a chart
    segment draws as the curve it really is, and those points are on the
    surface, so they come back as ordinary samples of the same fibre. What
    comes back is a denser polyline through the same geometry, not the
    polyline that went in.

    THE UNWRAP IS THE WHOLE REASON THIS IS A PATH FUNCTION. `from_world`
    answers per point and `atan2` puts every answer in one revolution, so a
    fibre crossing the chart's azimuth seam comes back with a jump of one
    circumference in the middle of it -- which the contact index and the sag
    solver would read as a real 2*pi*rho move, not as a coordinate artefact.
    Unwrapping uses the LOCAL period `2*pi*rho(u)`, because on anything but a
    cylinder the seam's width is a function of where you cross it.
    """
    w = np.asarray(world, dtype=np.float64)
    if w.ndim != 2 or w.shape[1] != 3:
        raise ValueError("world must be (N, 3), got %r" % (w.shape,))
    u, v, h = frame.from_world(w[:, 0], w[:, 1], w[:, 2])
    u = np.broadcast_to(np.asarray(u, dtype=np.float64), (len(w),)).copy()
    v = np.broadcast_to(np.asarray(v, dtype=np.float64), (len(w),)).copy()
    h = np.broadcast_to(np.asarray(h, dtype=np.float64), (len(w),)).copy()
    per = _v_period(frame, u)
    if per is not None and len(v) > 1:
        # THE SHIFT IS A WHOLE REVOLUTION AT THE POINT IT IS APPLIED TO, and on
        # anything but a cylinder that is not the same number as a whole
        # revolution at its neighbour. `to_world` reads the azimuth back as
        # `v / rho(u)`, so subtracting `k * 2*pi*rho(u_i)` from point i leaves
        # its world position EXACTLY where it was; subtracting an averaged
        # period does not, and that is the whole of the error. Measured on the
        # shipped 10 mm dome, apex-centred: the raw chart encode/decode is
        # exact to 2.7e-15 mm while the averaged shift put the read-back
        # 4.63 mm off its own world points.
        #
        # The branch count is counted in REVOLUTIONS rather than in `v`, for
        # the same reason: `v = rho*phi`, so a step that changes `rho` changes
        # `v` without any seam being crossed, and near the axis `rho` changes
        # fastest. `v / per` is the principal-branch azimuth in turns and is
        # bounded by +/- 1/2 (`from_world` returns `atan2`), so the per-step
        # count is one of -1, 0, +1 and is 0 across a POLE crossing -- which is
        # a half turn in azimuth, not a seam.
        #
        # On a cylinder `per` is one constant, so `turns` reduces to
        # `diff(v)/per` and the shift to the old `cumsum(step)`, term for term.
        turns = np.zeros_like(v)
        okv = per > 0.0
        turns[okv] = v[okv] / per[okv]
        k = np.cumsum(np.round(np.diff(turns)))
        v[1:] -= k * per[1:]
    return np.column_stack([u, v, h])
