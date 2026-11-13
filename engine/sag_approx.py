"""sag_approx.py -- the APPROXIMATE sag solver (the "Sag solver: approximate"
choice), for MEWracle Lite and the desktop export. NOT the scaffold-preview embed:
that keeps `preview_sag.py`, built for instant response on every keystroke; this
engine is ~5x more accurate and ~5x slower.

THE PHYSICS IS THE EXACT SOLVER'S, not a new model. Per layer, bottom-up in
DEPOSITION order, per fibre: supports where the fibre crosses the layer below, plus
droop-reachable floor peaks and same-layer self-crossings; a span^2/d parabola
between supports, relaxed onto a floor with no peak to pin (as many contacts as
it takes, each stretch a span of the law), peaks pinned only where the relaxed
droop rests on them, and a lower segment lying on the fibre's own path never
taken for a crossing;
crests rounded to a one-diameter bend radius; then a rest on the round
cross-section of whatever is below, never through it and never below the bed.
Every law is IMPORTED from gcode_to_fibers -- `_sag_law`, `_round_crests`,
`_tangent_touchdowns`,
`_seg_hits_xy`, `_wall_weight`, `_layer_ids`, `crest_extent`, SAG_*/CREST_* -- so
the two solvers cannot drift.

WHAT MAKES IT FAST: the exact solver samples the floor under every fibre each
HALF-DIAMETER against a diameter-scale spatial hash of every solved output segment.
Here contacts are found ANALYTICALLY between COMMANDED-centreline segment pairs
(~900 x ~2 mm per layer) in a coarse hash: each pair yields its closest approach in
closed form -- the exact solver's "floor local peak" IS a point of closest approach
-- and contact HEIGHTS interpolate the lower fibre's solved (arc, z) profile there.
Sagging moves only Z, so the commanded XY is the laid XY and the pair geometry is
exact, not rasterised. Pure numpy: no Tk, no VTK, no physics import (the fusion
and droop laws arrive from the caller as numbers), same number of polylines out as
in; `sag_bench.py compare sag_approx` is the accuracy comparator.

Known departures, all bounded by design: peak supports sit at the pair's closest
approach rather than a fine-grid sample (a <= half-diameter shift, and the truer
position); a near-parallel run gets ONE candidate and the rest-clamp does not follow
the lower fibre's bumps; the fusion wall/junction angle uses the commanded segment
direction; loop entries are detected on the paths as GIVEN; and self-contact is
NARROWER, lifting a point onto its own earlier strand only near a DETECTED crossing.
A floor peak is pinned where the CONVERGED droop rests on it, as in the exact solver,
but the relaxation that decides it runs over the candidate peaks only, where the
exact solver relaxes over the whole fine-grid floor.

ACCEPTED DEPARTURE -- the fibre sinks part-way into the fibre
beneath it at isolated points, worst where the lower fibre's profile climbs a steep
wall inside ONE commanded segment, because `_rest_at` evaluates each committed
segment at the perpendicular FOOT only. Worst case across the five `sag_bench` cases
is 0.87 d; mean |dz| against exact is 0.372 um with a per-sample rest and 0.370 um
without, for 1.20-1.36x the solve -- no gain for the cost, so it is not done.
"""

import math

import numpy as np

import gcode_to_fibers as g2f

# diagnostic seam: point this at a dict and `_solve_fibre` stashes its
# support/floor internals there (sag_diag/sag_probe use it; None in service)
_DEBUG = None


# --------------------------------------------------------------------------- #
# THE CHART SEAM -- what the approximate engines and the deposition relay
# measure WITH, so that one global grid stops being the currency
# --------------------------------------------------------------------------- #
#
# Every engine downstream of the projector is a 2-D machine: this one bins
# segment pairs in (col 0, col 1) and treats col 2 as height above the
# deposition surface, `preview_sag` rasterises the same two columns, and
# `mew_deposition`'s contact tracker integrates in them while col 2 rides
# through untouched. That is not a defect and it is not being removed -- a
# surface IS two-dimensional. What was wrong is that the two columns were
# always ONE chart, the frame's own `(u, v = rho*phi, h)`, whose azimuthal
# ruler divides by a radius that goes to zero at a dome's pole. A design
# centred there does not merely lose accuracy: `require_chart` refuses it, and
# with the refusal bypassed the same columns wound the drawn path 5.47 turns
# about the collector axis where the projector turns 1.02.
#
# So the currency between the projector and these engines is WORLD XYZ in the
# collector's own frame plus the frame, and a chart is an internal measuring
# device chosen per collector rather than something a path is stored in. Two
# implementations, and the discriminator is `frame.developable`:
#
#   `GlobalChart` -- the frame's own `(u, v, h)`. A plane, a cylinder and a cone
#     have a chart that IS an isometry or understates, which `_SegIndex`'s own
#     contract calls safe, so they keep it and keep every pinned digest. Its
#     three measuring methods are the three expressions the engines used to
#     inline, in the same order, so "keeps its digest" is arithmetic rather
#     than hope.
#     ITS CONVERSION IS A SECOND QUESTION and `holds_chart` answers it, never
#     `developable`. Which chart to MEASURE in is a fact about the surface;
#     whether the caller is already holding chart columns is a fact about the
#     read-back, and the two agree only on the cylinder. See `holds_chart`.
#   `PolarChart` -- `(u*cos phi, u*sin phi, h)` about the collector's axis, for
#     a frame that is not developable. This is the pole's own normal chart: `u`
#     is geodesic arc from the pole along the meridian and stays exact, the
#     azimuth is built into the two Cartesian columns so there is no `v` seam
#     to cross and no radius to divide by, and the apex is an ORDINARY INTERIOR
#     POINT of it rather than a removable singularity. It overstates only
#     azimuthally, by `u / |r|` where `r` is the parallel radius of the offset
#     surface -- 2.2% at 3.6 mm of extent on a 10 mm dome against the global
#     chart's 67% at the same shipped default (quoted in
#     `mew_frame.local_offsets`), and 15.6% at u = 9.2 mm where the global
#     chart is that same 67%. It is better than the chart it replaces at both
#     placements, which is why there is no atlas and no switch on shape.
#
# ITS INVERSE IS CLOSED FORM AND THAT IS WHY THIS CHART AND NOT ANOTHER. The
# deposition tracker MOVES a point within the surface (by the lag, millimetres),
# so a chart the engines can only go one way through is no use to it. Here
# `u = hypot(X, Y)`, `phi = atan2(Y, X)`, `v = rho(u)*phi`, and the frame's own
# `to_world` finishes the job -- no ray cast, no Newton step, no private frame
# method. The design-centred exponential map is more accurate (measured
# 0.0003%) and was rejected for exactly this: inverting it needs a geodesic
# march per point.

_POLE_EPS = 1e-12                  # |u| below which the pole's limits are taken


def holds_chart(frame):
    """Does this caller's `paths` already carry chart columns?

    THE ONE PLACE THAT ANSWERS IT, so the three seams that must agree --
    `GlobalChart`'s conversion, `preview_sag.drape_sag` and
    `gcode_to_fibers.apply_sag_approx` -- cannot each decide it differently.

    The answer is a property of the frame's READ-BACK and not of its surface;
    `mew_frame.DepositionFrame.chart_currency` carries the reasoning and the
    defect that came of confusing the two.
    """
    return frame is None or bool(getattr(frame, "chart_currency", False))


class GlobalChart(object):
    """The frame's own `(u, v, h)` -- the developable case.

    Holds no state beyond the frame, so two callers that build one from the
    same frame measure identically (`surface_ptimes` and `laid_fibers` do
    exactly that). `frame=None` is the flat bed, where every method is the
    identity and no frame call is made at all.

    ITS CONVERSION IS THE FRAME'S OWN `from_world` / `to_world`, and it is the
    IDENTITY only where the caller is already holding chart columns
    (`holds_chart` -- the flat bed and the four-axis mandrel). An unconditional
    identity would be correct for those two callers and wrong for every other
    developable collector: a CONE is developable and its callers hold world, so
    world Z would be read as height above the surface.
    """

    def __init__(self, frame=None):
        self.frame = frame
        #: see `holds_chart`; cached because `from_world` is called per fibre.
        self._identity = holds_chart(frame)

    # -- what the engines ask of a chart ---------------------------------- #
    @property
    def period(self):
        return None if self.frame is None else getattr(self.frame,
                                                       "period_v", None)

    @property
    def curved(self):
        return self.frame is not None and self.frame.is_curved()

    def from_world(self, paths):
        """World XYZ -> the frame's own `(u, v, h)`; identity where the caller
        already holds them (`holds_chart`).

        `frame.from_world` per path rather than `unwrap_path`: this chart is
        reached only on a DEVELOPABLE surface, where `v` has no azimuth seam to
        unwrap (a cone's pattern does not wrap, `RevolutionFrame.period_v` is
        None) -- and the four-axis cylinder, which does wrap, never gets here
        because it is `holds_chart`.
        """
        if self._identity:
            return paths
        f = self.frame
        return [np.column_stack(f.from_world(np.asarray(p, dtype=np.float64)[:, 0],
                                             np.asarray(p, dtype=np.float64)[:, 1],
                                             np.asarray(p, dtype=np.float64)[:, 2]))
                for p in paths]

    def to_world(self, charts):
        if self._identity:
            return charts
        f = self.frame
        return [np.column_stack(f.to_world(np.asarray(c, dtype=np.float64)[:, 0],
                                           np.asarray(c, dtype=np.float64)[:, 1],
                                           np.asarray(c, dtype=np.float64)[:, 2]))
                for c in charts]

    def principal_step(self, X, Y, h, dX, dY):
        """A chart step as the SURFACE sees it, in the principal frame.

        `mew_frame.metric`'s own form, spelled in that order so a cylinder's
        drape cannot move a bit."""
        if self.frame is None:
            return dX, dY
        a, b, c = self.frame.metric(X, Y, h)
        return a * dX, b * (dY - c * dX)

    def chord_drop(self, span, eu, ev, X, Y, h):
        if self.frame is None:
            return 0.0
        # Y IS THE SECOND CHART COORDINATE and is passed because a frame
        # whose curvature varies across the chart needs both. Every frame this
        # chart serves is developable and ignores it.
        return self.frame.chord_drop_at(span, eu, ev, X, h, v=Y)

    def curvatures(self, X, Y, h):
        return self.frame.curvatures_at(X, h)

    def heading(self, X, Y, h, dX, dY):
        """None -- "this chart hands its raw step through".

        `mew_deposition._lay_one` reads the heading it gives a placement
        straight off the chart columns, and on this chart that is what it has
        always done. The shear that step carries where the chart is sheared is
        an OPEN seam with its own measurement, not
        something to close here as a side effect: closing it would move a cone's
        contact placement with no suite asking it to. None is therefore the
        answer, and the caller keeps its own arithmetic byte for byte.
        """
        return None

    def chart_offset(self, X, Y, h, ou, ov):
        """The inverse of `heading` -- unreachable, because `heading` is None.

        Present so the protocol is complete and a reader is not left wondering
        which chart answers this.
        """
        return ou, ov

    def require_ruler(self, what, pts, patch, ruler="raw"):
        if self.frame is None:
            return
        self.frame.require_chart(what, pts[:, 0], patch, v=pts[:, 1],
                                 h=pts[:, 2], ruler=ruler)

    def overstatement(self, pts):
        if self.frame is None:
            return 0.0
        import mew_frame
        return float(np.max(mew_frame.overstatement_from_metric(
            *self.frame.metric(pts[:, 0], pts[:, 1], pts[:, 2]))))


class PolarChart(object):
    """`(u*cos phi, u*sin phi, h)` about the collector's axis of revolution.

    See the section comment above for why this chart and not the design-centred
    exponential map. Everything here is asked of the frame through its public
    interface plus `profile`, which is the meridian and knows nothing of an
    azimuth, so a frame that publishes a profile serves this chart whatever its
    shape -- including one read back from CAD.
    """

    def __init__(self, frame):
        self.frame = frame

    @property
    def period(self):
        """None, and that is the point.

        The azimuth is inside the two Cartesian columns, so a pattern wide
        enough to wrap the collector and meet itself has no seam to cross --
        case 3 of the spec's "what anywhere means" comes free here rather than
        needing a periodic index."""
        return None

    @property
    def curved(self):
        return self.frame.is_curved()

    # -- the two directions, both closed form ---------------------------- #
    def from_world(self, paths):
        out = []
        for p in paths:
            p = np.asarray(p, dtype=np.float64)
            u, v, h = self.frame.from_world(p[:, 0], p[:, 1], p[:, 2])
            u = np.asarray(u, dtype=np.float64)
            phi = self._phi(u, np.asarray(v, dtype=np.float64))
            out.append(np.column_stack([u * np.cos(phi), u * np.sin(phi),
                                        np.asarray(h, dtype=np.float64)]))
        return out

    def to_world(self, charts):
        out = []
        for c in charts:
            c = np.asarray(c, dtype=np.float64)
            u = np.hypot(c[:, 0], c[:, 1])
            phi = np.arctan2(c[:, 1], c[:, 0])
            _x, rho = self.frame.profile.point(u)
            x, y, z = self.frame.to_world(u, np.multiply(rho, phi), c[:, 2])
            out.append(np.column_stack([
                np.broadcast_to(np.asarray(x, dtype=np.float64), u.shape),
                np.broadcast_to(np.asarray(y, dtype=np.float64), u.shape),
                np.broadcast_to(np.asarray(z, dtype=np.float64), u.shape)]))
        return out

    def _phi(self, u, v):
        """The azimuth `from_world` put into `v = rho(u)*phi`, divided back out.

        Exactly `RevolutionFrame._phi`'s inverse and it must be, or the round
        trip is not the identity: `from_world` writes `rho*atan2(sgn*y, sgn*z)`
        with `sgn` the sign of the SIGNED parallel radius, and re-deriving the
        azimuth from `(y, z)` alone would land a dome half a turn away. At the
        pole `rho == 0` and every azimuth names the same point, so 0 is as good
        as any and `u` is 0 there anyway."""
        _x, rho = self.frame.profile.point(u)
        rho = np.asarray(rho, dtype=np.float64)
        safe = np.where(rho == 0.0, 1.0, rho)
        return np.where(rho == 0.0, 0.0, np.divide(v, safe))

    # -- the local scales, which is the whole of the chart's arithmetic --- #
    def _scales(self, X, Y, h):
        """`(u, cos phi, sin phi, radial scale, azimuthal scale)`.

        Radial: `u` is arc length on the BARE meridian, and the offset surface
        stretches it by `1 + k1*h` -- the same `a` `mew_frame.metric` forms,
        from the same bare `k1`.
        Azimuthal: a chart step `u*dphi` is physically `|r|*dphi` with
        `r = rho + h*x'` the offset surface's signed parallel radius, so the
        scale is `|r| / u`. At the pole both go to 1: `rho -> +-u` and
        `x' -> 0`, so `|r|/u -> 1`, and the limit is taken rather than divided
        because 0/0 there is not a defect but the chart being regular.
        """
        X = np.asarray(X, dtype=np.float64)
        Y = np.asarray(Y, dtype=np.float64)
        u = np.hypot(X, Y)
        safe_u = np.where(u > _POLE_EPS, u, 1.0)
        cs = np.where(u > _POLE_EPS, X / safe_u, 1.0)
        sn = np.where(u > _POLE_EPS, Y / safe_u, 0.0)
        k1 = np.asarray(self.frame.curvatures_at(u, 0.0)[0], dtype=np.float64)
        _x, rho = self.frame.profile.point(u)
        dx, _drho = self.frame.profile.tangent(u)
        r = np.abs(np.asarray(rho, dtype=np.float64)
                   + np.multiply(h, np.asarray(dx, dtype=np.float64)))
        az = np.where(u > _POLE_EPS, r / safe_u, 1.0)
        return u, cs, sn, 1.0 + np.multiply(k1, h), az

    def principal_step(self, X, Y, h, dX, dY):
        """A chart step as the SURFACE sees it, in the principal frame.

        The principal directions on a surface of revolution are the meridian
        and the parallel; in THIS chart the meridian is radial and the parallel
        tangential, so the step is rotated into `(radial, tangential)` by the
        azimuth and then scaled. Returning it in the principal frame is what
        lets `mew_frame.chord_drop_at` be called unchanged -- its `(eu, ev)` is
        a physical direction in that frame, never a chart one, and its own
        docstring is what says so."""
        _u, cs, sn, rad, az = self._scales(X, Y, h)
        return (rad * (dX * cs + dY * sn), az * (-dX * sn + dY * cs))

    def chord_drop(self, span, eu, ev, X, Y, h):
        return self.frame.chord_drop_at(span, eu, ev, np.hypot(X, Y), h)

    def curvatures(self, X, Y, h):
        return self.frame.curvatures_at(np.hypot(X, Y), h)

    def heading(self, X, Y, h, dX, dY):
        """The step in the principal (meridian, parallel) frame -- `principal_step`.

        `mew_mandrel.CurvedPlacement.resolve` reads a HEADING off these two
        numbers and resolves the jet lag into along- and across-track parts by
        it, so a chart angle here is the same defect `chord_drop_at` was written
        to fix one layer down. This chart can answer it: it has no shear at all
        and its two scales are known, so the physical direction is one rotation
        and two multiplications away.
        """
        return self.principal_step(X, Y, h, dX, dY)

    def chart_offset(self, X, Y, h, ou, ov):
        """The exact inverse of `heading`, so an offset cannot leak a heading.

        `_lay_one` stores the placement's lateral offset back in CHART columns,
        because that is where the tracker's input lives. Undo the scaling and
        then the rotation, in that order -- the reverse of `principal_step`'s
        rotate-then-scale.
        """
        _u, cs, sn, rad, az = self._scales(X, Y, h)
        a = np.divide(ou, rad)
        b = np.divide(ov, az)
        return (a * cs - b * sn, a * sn + b * cs)

    def require_ruler(self, what, pts, patch, ruler="raw"):
        """The ruler question, asked of THIS chart rather than the frame's.

        `frame.require_chart` asks whether the frame's own `(u, v)` is a good
        enough ruler, and the answer at a dome's pole is no -- which is the
        refusal this chart exists to make unnecessary. So the pole test is not
        re-asked here; what IS asked is the question `POLE_BAND` was derived
        from: does this chart overstate a step at contact scale by enough that
        `_SegIndex`'s 3x3 neighbourhood can drop a segment physically in
        contact. `ruler` selects the band the caller has earned, exactly as the
        frame's version does.
        """
        import mew_frame
        band = (mew_frame.CHART_APPROX_BAND if ruler == "approx"
                else mew_frame.POLE_BAND)
        over = self.overstatement(pts)
        if over > band:
            raise mew_frame.ChartShearExcess(
                "%s cannot use this collector's pole chart where the pattern "
                "goes: it overstates a step by %.1f%% at the widest, against a "
                "%.1f%% band. This chart is exact along the meridian and "
                "overstates the azimuth by u/|r|, so what is out of band is "
                "the pattern's DISTANCE FROM THE POLE, not the pole -- at the "
                "pole itself it overstates by 0. Move the design in, or "
                "shrink it." % (what, 100.0 * over, 100.0 * band))

    def overstatement(self, pts):
        """Worst chart-over-physical ratio minus 1, over the points given.

        The chart is orthogonal and diagonal in `(radial, tangential)` -- no
        shear at all, which the global chart cannot say -- so the worst
        direction is simply the smaller of the two scales and no eigenvalue
        work is needed. Understating is safe by `_SegIndex`'s own contract, so
        only a scale below 1 counts.
        """
        pts = np.asarray(pts, dtype=np.float64)
        _u, _cs, _sn, rad, az = self._scales(pts[:, 0], pts[:, 1], pts[:, 2])
        worst = np.minimum(np.abs(rad), np.abs(az))
        return float(np.max(np.maximum(
            0.0, 1.0 / np.maximum(worst, 1e-300) - 1.0)))


def chart_for(frame):
    """The chart the engines measure in, from the collector alone.

    Deterministic in `frame` and nothing else, which is the contract two
    callers lean on: `mew_deposition.surface_ptimes` builds one to time a path
    and `laid_fibers` builds another to lay it, and a chart that depended on
    the design would let those two disagree.
    """
    if frame is None or getattr(frame, "developable", True):
        return GlobalChart(frame)
    if getattr(frame, "profile", None) is None:
        # A KNOWN LIMIT, and it is one `elif` to lift. `PolarChart` is the
        # POLE's normal chart and needs a meridian: `rho(u)` IS its azimuthal
        # scale, and a surface with no axis of revolution has no azimuth to
        # scale. An imported CAD surface therefore needs a
        # different chart, and the one to build is the design-centred TANGENT
        # PLANE chart, which needs only `point` and `normal` and so is answered
        # by a triangle mesh too. What stops it being here already is its
        # INVERSE: the two sag engines only ever move Z, so a forward-only
        # chart would serve them, but `mew_deposition`'s tracker moves a point
        # INSIDE the surface by the lag, and inverting an orthographic
        # projection needs a ray/surface intersection or `mew_project`'s
        # geodesic march -- neither of which any frame publishes.
        # Refused by name rather than crashing on a missing attribute.
        raise NotImplementedError(
            "%s is neither developable nor a surface of revolution, so neither "
            "chart here fits it: the global (u, v) is not a ruler and the pole "
            "chart needs a meridian. The arbitrary-collector path is a "
            "design-centred tangent-plane chart, which needs only point() and "
            "normal() -- see the section comment above `GlobalChart`."
            % (type(frame).__name__,))
    return PolarChart(frame)


# --------------------------------------------------------------------------- #
# pair geometry
# --------------------------------------------------------------------------- #

def _seg_closest(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
    """Vectorised closest approach between 2D segment pairs A(t), B(u).
    Returns (t, u, h2): clamped [0,1] params and the squared XY distance.
    Standard two-pass clamp (solve, clamp u, re-derive t) -- exact for the
    crossing and interior cases; the doubly-clamped corner case can sit a
    hair off optimal, which only ever makes h2 an over-estimate the
    per-point rest evaluation later corrects."""
    d1x = ax1 - ax0
    d1y = ay1 - ay0
    d2x = bx1 - bx0
    d2y = by1 - by0
    rx = ax0 - bx0
    ry = ay0 - by0
    a = d1x * d1x + d1y * d1y
    e = d2x * d2x + d2y * d2y
    b = d1x * d2x + d1y * d2y
    c = d1x * rx + d1y * ry
    f = d2x * rx + d2y * ry
    denom = a * e - b * b
    safe_d = np.where(denom > 1e-18, denom, 1.0)
    t = np.where(denom > 1e-18, np.clip((b * f - c * e) / safe_d, 0.0, 1.0), 0.0)
    safe_e = np.where(e > 1e-18, e, 1.0)
    u = np.clip((b * t + f) / safe_e, 0.0, 1.0)
    safe_a = np.where(a > 1e-18, a, 1.0)
    t = np.clip((b * u - c) / safe_a, 0.0, 1.0)
    px = ax0 + d1x * t
    py = ay0 + d1y * t
    qx = bx0 + d2x * u
    qy = by0 + d2y * u
    h2 = (px - qx) ** 2 + (py - qy) ** 2
    return t, u, h2


def _simplify_idx(pts, eps):
    """Douglas-Peucker in XY -> sorted kept-vertex indices (always keeps the
    endpoints). Contact geometry runs on the simplified polyline: a physics-
    relaid path carries half-diameter-spaced vertices along straight runs
    that chop every crossing into dozens of candidate pairs (measured 5.6 s
    on the relaid default; the raw path solves in 1.7 s). eps is far below
    the contact tolerance, so the pair geometry is unchanged to within it.
    Iterative stack, vectorised distance per interval -- a straight run
    costs one pass."""
    n = len(pts)
    if n <= 2:
        return np.arange(n, dtype=np.int64)
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    x = pts[:, 0]
    y = pts[:, 1]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        dx = x[b] - x[a]
        dy = y[b] - y[a]
        seg2 = dx * dx + dy * dy
        xi = x[a + 1:b]
        yi = y[a + 1:b]
        if seg2 < 1e-24:
            d2 = (xi - x[a]) ** 2 + (yi - y[a]) ** 2
        else:
            # perpendicular distance to the chord (chord is a straight
            # baseline, so no clamp needed for the split decision)
            cross = (xi - x[a]) * dy - (yi - y[a]) * dx
            d2 = cross * cross / seg2
        k = int(np.argmax(d2))
        if d2[k] > eps * eps:
            m = a + 1 + k
            keep[m] = True
            stack.append((a, m))
            stack.append((m, b))
    return np.flatnonzero(keep).astype(np.int64)


class _Grid:
    """Coarse spatial hash over 2D segments (cells of several fibre widths,
    entries sampled every half-cell along each segment -- the same 3x3
    coverage guarantee as the exact solver's `_hash_cell`, at commanded-
    segment scale instead of solved-output scale). `query` returns unique
    (query segment, stored segment) candidate pairs."""

    def __init__(self, cell):
        self.cell = float(cell)
        self._key_chunks = []
        self._sid_chunks = []
        self._k = None
        self._s = None
        self.n = 0

    def _samples(self, x0, y0, x1, y1):
        L = np.hypot(x1 - x0, y1 - y0)
        cnt = np.maximum(2, (np.ceil(L / (0.5 * self.cell))).astype(np.int64) + 1)
        tot = int(cnt.sum())
        sid = np.repeat(np.arange(x0.size, dtype=np.int64), cnt)
        k = (np.arange(tot, dtype=np.int64)
             - np.repeat(np.cumsum(cnt) - cnt, cnt))
        frac = k / np.repeat(cnt - 1, cnt)
        px = x0[sid] + (x1 - x0)[sid] * frac
        py = y0[sid] + (y1 - y0)[sid] * frac
        gx = np.floor(px / self.cell).astype(np.int64)
        gy = np.floor(py / self.cell).astype(np.int64)
        return sid, gx, gy

    def add(self, x0, y0, x1, y1, sid0):
        if x0.size == 0:
            return
        sid, gx, gy = self._samples(x0, y0, x1, y1)
        self._key_chunks.append(g2f._pack_cells(gx, gy))
        self._sid_chunks.append(sid + sid0)
        self.n = max(self.n, sid0 + x0.size)
        self._k = None

    def _build(self):
        if self._k is None:
            if self._key_chunks:
                k = np.concatenate(self._key_chunks)
                s = np.concatenate(self._sid_chunks)
                order = np.argsort(k, kind="stable")
                self._k = k[order]
                self._s = s[order]
            else:
                self._k = np.zeros(0, dtype=np.int64)
                self._s = np.zeros(0, dtype=np.int64)

    _NX = np.array([-1, 0, 1, -1, 0, 1, -1, 0, 1], dtype=np.int64)
    _NY = np.array([-1, -1, -1, 0, 0, 0, 1, 1, 1], dtype=np.int64)

    def query(self, x0, y0, x1, y1):
        self._build()
        empty = np.zeros(0, dtype=np.int64)
        if self._k.size == 0 or x0.size == 0:
            return empty, empty
        qid, gx, gy = self._samples(x0, y0, x1, y1)
        # drop consecutive duplicate cells (a segment samples its own cell
        # several times); zig-zag revisits survive but only cost duplicates
        # that the final unique() removes
        keep = np.r_[True, (qid[1:] != qid[:-1]) | (gx[1:] != gx[:-1])
                     | (gy[1:] != gy[:-1])]
        qid, gx, gy = qid[keep], gx[keep], gy[keep]
        nq = qid.size
        qid9 = np.repeat(qid, 9)
        keys = g2f._pack_cells((gx[:, None] + self._NX).ravel(),
                               (gy[:, None] + self._NY).ravel())
        lo = np.searchsorted(self._k, keys, side="left")
        hi = np.searchsorted(self._k, keys, side="right")
        cnt = hi - lo
        # sort-based dedupe (numpy 2.x routes int unique through a hash table
        # that measured ~4x slower than a plain sort on these key volumes),
        # done as it expands: a corridor of retraced legs repeats each pair
        # once per shared cell (`g2f._unique_run_pairs`)
        pair = g2f._unique_run_pairs(qid9, lo, cnt, self._s, self.n)
        if pair.size == 0:
            return empty, empty
        return pair // self.n, pair % self.n


# --------------------------------------------------------------------------- #
# the committed lower structure
# --------------------------------------------------------------------------- #

class _Structure:
    """Everything already laid: commanded-centreline segments for contact
    FINDING (coarse grid), each fibre's solved (arc, z) profile for contact
    HEIGHT. The layer-at-a-time twin of `_SagState`'s all_lower/prev_layer
    pair: `prev_lo:prev_hi` is the previous layer's segment slice."""

    def __init__(self, cell, period=None):
        self.cell = float(cell)
        self.period = None if period is None else float(period)
        self.clear()

    def clear(self):
        # The planar hash is a little leaner and is deliberately retained for
        # the flat path.  A cylinder needs the exact solver's periodic
        # seam machinery: split each turn, fold it into one circumference, then
        # ghost only the seam-adjacent pieces.  The approximate solver still
        # owns its analytic contact and rest calculations below; this index only
        # supplies candidates in the right surface neighbourhood.
        self.grid = (_Grid(self.cell) if self.period is None
                     else g2f._SegIndex(self.cell, period=self.period))
        self._chunks = []
        self._stored_global_chunks = []
        self.f_arc = []
        self.f_z = []
        self.n = 0
        self.prev_lo = 0
        self.prev_hi = 0
        self._flat = None

    def commit(self, fibres):
        """fibres: list of (base_xy_pts, cum, out_arcs, out_z) -- one entry
        per solved fibre of ONE layer, committed together so same-layer
        fibres never support each other (exact solver rule)."""
        start = self.n
        for base, cum, arcs, zo in fibres:
            if len(base) < 2:
                continue
            fid = len(self.f_arc)
            self.f_arc.append(np.ascontiguousarray(arcs))
            self.f_z.append(np.ascontiguousarray(zo))
            x0 = base[:-1, 0]
            y0 = base[:-1, 1]
            x1 = base[1:, 0]
            y1 = base[1:, 1]
            if self.period is None:
                self.grid.add(x0, y0, x1, y1, self.n)
            else:
                before = self.grid.n
                self.grid.add_fiber(base)
                # _SegIndex stores split/ghost pieces.  Map each of those back
                # to this structure's original segment id, which owns its arc
                # position and solved height profile.
                oid, _t0, _t1 = self.grid.seg_provenance()
                self._stored_global_chunks.append(self.n + oid[before:])
            self._chunks.append(dict(
                x0=x0, y0=y0, dx=x1 - x0, dy=y1 - y0,
                slen=cum[1:] - cum[:-1], arc0=cum[:-1],
                fid=np.full(x0.size, fid, dtype=np.int64)))
            self.n += x0.size
        self.prev_lo, self.prev_hi = start, self.n
        self._flat = None

    def global_segments(self, stored_ids):
        """Original structure segment id for periodic-index piece ids."""
        if self.period is None:
            return stored_ids
        if not self._stored_global_chunks:
            return np.zeros(0, dtype=np.int64)
        return np.concatenate(self._stored_global_chunks)[stored_ids]

    def flat(self):
        if self._flat is None:
            keys = ("x0", "y0", "dx", "dy", "slen", "arc0", "fid")
            if self._chunks:
                cat = {k: np.concatenate([c[k] for c in self._chunks])
                       for k in keys}
            else:
                cat = {k: np.zeros(0, dtype=np.float64) for k in keys}
                cat["fid"] = np.zeros(0, dtype=np.int64)
            cat["len2"] = np.maximum(cat["dx"] ** 2 + cat["dy"] ** 2, 1e-18)
            self._flat = cat
        return self._flat

    def z_at(self, fid, arc):
        """Solved centreline height of committed fibre `fid` at arc position
        `arc` along it (grouped np.interp, one call per distinct fibre)."""
        out = np.empty(fid.size, dtype=np.float64)
        if fid.size == 0:
            return out
        order = np.argsort(fid, kind="stable")
        of = fid[order]
        bounds = np.r_[np.flatnonzero(np.r_[True, of[1:] != of[:-1]]), of.size]
        for a, b in zip(bounds[:-1], bounds[1:]):
            f = int(of[a])
            idx = order[a:b]
            out[idx] = np.interp(arc[idx], self.f_arc[f], self.f_z[f])
        return out


class _Pairs:
    """Candidate contact pairs of one query fibre against the structure:
    parallel arrays over pairs whose closest approach is within contact
    reach `s`.

    `PAIR_ARRAYS` NAMES THE PER-PAIR ONES, and that is not decoration -- it is
    what `sag_bench`'s `leaky` fault injector needs. That injector drops every
    7th candidate to prove the accuracy check can go red, and it did so by
    filtering EVERY entry of `__slots__` with the same index mask. One entry is
    not a per-pair array: `periodic` is a property of the whole set (whether
    these pairs were found through the frame's seam at all -- read as a plain
    truth value at `_rest_at` and in `_solve_fibre`), so masking it would raise
    `TypeError: 'bool' object is not subscriptable` and
    `python tools/sag_bench.py invariants sag_approx` would reach no verdict.

    So the list lives HERE, beside the thing it describes, rather than as a
    hardcoded exclusion in the harness: a slot added to this class in future is
    a per-pair array or it is not, and only this class knows which.
    """

    #: the slots that carry ONE VALUE PER PAIR and may be filtered by a mask
    PAIR_ARRAYS = ("qi", "sj", "t", "u", "h2", "arcq", "sin_t", "sink", "hw",
                   "qx0", "qy0", "qx1", "qy1", "qt0", "qt1",
                   "sx0", "sy0", "sdx", "sdy", "st0", "st1")

    #: the six of those that exist ONLY under a periodic frame, and are None
    #: otherwise. `_find_pairs` reads the stored segment's endpoints straight
    #: out of `struct.flat()` on a non-periodic design and never materialises
    #: them, because `_SegIndex` has not split anything at a seam there. They
    #: are named because a caller that masks every per-pair array has to know
    #: that six of them may be None -- which is the SECOND layer of task
    #: 20260908-150952: with the `periodic` bool handled, `leaky` then died on
    #: `TypeError: 'NoneType' object is not subscriptable`, nine rows and one
    #: red proof further into the run.
    PAIR_ARRAYS_PERIODIC_ONLY = ("sx0", "sy0", "sdx", "sdy", "st0", "st1")

    #: and the whole-set scalars, which must not be masked at all
    PAIR_SCALARS = ("periodic",)

    __slots__ = PAIR_ARRAYS + PAIR_SCALARS

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw[k])

    @property
    def size(self):
        return self.qi.size


#: query segments per block in `_find_pairs`. Candidates run to ~700 a
#: segment over a 15-layer stack, so a block's unpruned set stays near 0.7M
#: pairs (~0.3 GB of temporaries) however long the fibre is.
_PAIR_BLOCK = 1024


def _near_pairs(struct, fl, base, s, periodic):
    """One block of `_find_pairs`: the candidate pairs of the query segments
    of `base` (block-local ids), their closest approach, pruned to contact
    reach. A list of the 17 per-pair arrays (the six periodic-only ones None
    on a flat design), or None when none is in reach."""
    x0 = base[:-1, 0]
    y0 = base[:-1, 1]
    x1 = base[1:, 0]
    y1 = base[1:, 1]
    if not periodic:
        qi, sj = struct.grid.query(x0, y0, x1, y1)
        qx0, qy0, qx1, qy1 = x0[qi], y0[qi], x1[qi], y1[qi]
        qt0 = np.zeros(qi.size, dtype=np.float64)
        qt1 = np.ones(qi.size, dtype=np.float64)
        sx0 = sy0 = sdx = sdy = st0 = st1 = None
    else:
        qx0a, qy0a, qx1a, qy1a, qoid, qta, qtb = struct.grid.query_segments(
            x0, y0, x1, y1)
        qpiece, stored = struct.grid.seg_pairs(qx0a, qy0a, qx1a, qy1a)
        qi = qoid[qpiece]
        sj = struct.global_segments(stored)
        qx0, qy0 = qx0a[qpiece], qy0a[qpiece]
        qx1, qy1 = qx1a[qpiece], qy1a[qpiece]
        qt0, qt1 = qta[qpiece], qtb[qpiece]
        sg = struct.grid._seg_array()[stored]
        sx0, sy0 = sg[:, 0], sg[:, 1]
        sdx, sdy = sg[:, 3] - sx0, sg[:, 4] - sy0
        _soid, sta, stb = struct.grid.seg_provenance()
        st0, st1 = sta[stored], stb[stored]
    if qi.size == 0:
        return None
    if periodic:
        t, u, h2 = _seg_closest(qx0, qy0, qx1, qy1,
                                sx0, sy0, sx0 + sdx, sy0 + sdy)
    else:
        t, u, h2 = _seg_closest(qx0, qy0, qx1, qy1,
                                fl["x0"][sj], fl["y0"][sj],
                                fl["x0"][sj] + fl["dx"][sj],
                                fl["y0"][sj] + fl["dy"][sj])
    # prune with SLACK: the two-pass clamp OVER-estimates h2 in the doubly-
    # clamped corner case, and a pair pruned here never clamps at all --
    # measured as scattered partial penetrations. The
    # per-point evaluation downstream recomputes h exactly, so slack only
    # admits extra candidates, never error.
    keep = h2 < (1.5 * s) ** 2
    if not keep.any():
        return None
    return [None if a is None else a[keep]
            for a in (qi, sj, t, u, h2, qx0, qy0, qx1, qy1, qt0, qt1,
                      sx0, sy0, sdx, sdy, st0, st1)]


def _find_pairs(struct, base, cum, s, sink_of):
    """All (query segment, committed segment) pairs within contact reach,
    with closed-form closest approach, the contact-arc window each pair can
    influence, and its fusion sink. `sink_of(sin_t)` -> per-pair sink."""
    periodic = struct.period is not None
    fl = struct.flat()
    n_seg = len(base) - 1
    # The candidates are found, measured and pruned one block of query
    # segments at a time, then joined. Unblocked, a fibre laid over a 15-layer
    # stack drew 3.7M candidates on a four-scaffold 8 mm plate, and
    # `_seg_closest`'s ~20 temporaries over them took the export worker to
    # 3.6 GB, failing Generate on an ordinary machine. The prune keeps few, so
    # a block's temporaries are what peaks. Same pairs in the same order:
    # each block's pairs come out sorted by query segment, the blocks run in
    # segment order, and everything up to the prune is per pair.
    parts = []
    for a in range(0, n_seg, _PAIR_BLOCK):
        b = min(n_seg, a + _PAIR_BLOCK)
        got = _near_pairs(struct, fl, base[a:b + 1], s, periodic)
        if got is not None:
            got[0] += a
            parts.append(got)
    if not parts:
        return None
    (qi, sj, t, u, h2, qx0, qy0, qx1, qy1, qt0, qt1,
     sx0, sy0, sdx, sdy, st0, st1) = (
        None if parts[0][k] is None else
        (parts[0][k] if len(parts) == 1
         else np.concatenate([p[k] for p in parts]))
        for k in range(17))
    del parts
    qlen = cum[qi + 1] - cum[qi]
    qpar = qt0 + t * (qt1 - qt0)
    arcq = cum[qi] + qpar * qlen
    dqx = qx1 - qx0
    dqy = qy1 - qy0
    lq = np.hypot(dqx, dqy)
    # BOTH the cross product and the length come from the same structure
    # vector. `_SegIndex` splits a helical pass at every period boundary, so
    # under a periodic frame `sj`'s ORIGINAL segment is up to 5x the stored
    # piece: mixing the two scales sin_t by that ratio (measured 3240/3600
    # pairs wrong at a 40-deg winding). A 45-deg tube crosses at 90 deg and the
    # 1.0 clamp hid it, which is why the mandrel suite's 45-deg fixture did not.
    sdx_e = sdx if periodic else fl["dx"][sj]
    sdy_e = sdy if periodic else fl["dy"][sj]
    ls = np.hypot(sdx_e, sdy_e)
    den = np.maximum(lq * ls, 1e-18)
    sin_t = np.minimum(np.abs(dqx * sdy_e - dqy * sdx_e) / den, 1.0)
    # arc half-window along the query within which this pair can be in
    # contact: |h| grows as sin_t per unit arc away from the closest
    # approach, so ~s/sin_t; a near-parallel (grazing) pair's window must
    # grow to the WHOLE segment -- capping it early leaves the rest of a
    # coincident overlap contact-free and the fibre dives to the bed there
    # (measured: -0.7 d mean bias on every 0-deg layer before this)
    hw = np.minimum(qlen, 1.2 * s / np.maximum(sin_t, 1e-9))
    if periodic:
        u = st0 + u * (st1 - st0)
    return _Pairs(qi=qi, sj=sj, t=t, u=u, h2=h2, arcq=arcq, sin_t=sin_t,
                  sink=sink_of(sin_t), hw=hw, periodic=periodic,
                  qx0=qx0, qy0=qy0, qx1=qx1, qy1=qy1, qt0=qt0, qt1=qt1,
                  sx0=sx0, sy0=sy0, sdx=sdx, sdy=sdy, st0=st0, st1=st1)


def _rest_at(arcs, px, py, pairs, struct, s, floor, protect=None):
    """Rest height of a fibre of contact reach `s` at query points
    (px, py) = fibre(arcs): the max over nearby committed segments of
    z_lower + sqrt(s^2 - h^2) - sink, never below `floor` (the bed).
    The twin of `_SegIndex.rest_heights` with fusion folded in per-candidate
    (continuous, as that function's docstring argues). `protect` masks
    points whose contacts must not fuse (the merged linking-loop entry)."""
    floor_a = np.asarray(floor, dtype=np.float64)
    out = (np.full(arcs.size, float(floor_a)) if floor_a.ndim == 0
           else floor_a.copy())
    if pairs is None or pairs.size == 0 or arcs.size == 0:
        return out
    if arcs.size > 1 and np.any(arcs[1:] < arcs[:-1]):
        # callers may pass unsorted arcs (self-crossing probes); the window
        # bracketing below needs them sorted -- sort, solve, un-sort
        order = np.argsort(arcs, kind="stable")
        inv = np.empty_like(order)
        inv[order] = np.arange(order.size, dtype=np.int64)
        res = _rest_at(arcs[order], px[order], py[order], pairs, struct, s,
                       floor_a[order] if floor_a.ndim else floor,
                       protect[order] if protect is not None else None)
        return res[inv]
    fl = struct.flat()
    lo = np.searchsorted(arcs, pairs.arcq - pairs.hw, side="left")
    hi = np.searchsorted(arcs, pairs.arcq + pairs.hw, side="right")
    cnt = hi - lo
    tot = int(cnt.sum())
    if tot == 0:
        return out
    # The candidates (every pair x every query point in its arc window) are
    # expanded a block of pairs at a time, at most _REST_BLOCK_CANDIDATES per
    # block (a single pair over the budget is still one block). A 20-layer
    # tube on a 6 mm mandrel has ~23M of them, and the dozen float64 arrays
    # built per candidate below took the solve past 3.5 GB when expanded at
    # once. The arithmetic per candidate is unchanged and the result is a
    # running maximum into `out`, so the blocks give the same bits in any order.
    ends = np.cumsum(cnt)
    a = 0
    while a < pairs.size:
        b = int(np.searchsorted(ends, (ends[a] - cnt[a])
                                + _REST_BLOCK_CANDIDATES, side="right"))
        b = max(b, a + 1)
        _rest_block(out, arcs, px, py, pairs, struct, s, fl, protect,
                    lo, cnt, a, min(b, pairs.size))
        a = b
    return out


#: candidates one `_rest_at` block expands; ~15 float64 arrays of this length
#: are alive at once, so 2M keeps a block near 250 MB
_REST_BLOCK_CANDIDATES = 2_000_000


def _rest_block(out, arcs, px, py, pairs, struct, s, fl, protect, lo, cnt,
                a, b):
    """`_rest_at`'s candidate expansion and contact maximum for pairs a..b-1,
    folded into `out` in place."""
    cnt = cnt[a:b]
    tot = int(cnt.sum())
    if tot == 0:
        return
    cpt = (np.arange(tot, dtype=np.int64)
           - np.repeat(np.cumsum(cnt) - cnt, cnt)
           + np.repeat(lo[a:b], cnt))
    cpr = np.repeat(np.arange(a, b, dtype=np.int64), cnt)
    j = pairs.sj[cpr]
    X = px[cpt]
    Y = (struct.grid._wrap_q(py[cpt]) if pairs.periodic else py[cpt])
    if pairs.periodic:
        x0, y0 = pairs.sx0[cpr], pairs.sy0[cpr]
        dx, dy = pairs.sdx[cpr], pairs.sdy[cpr]
        len2 = np.maximum(dx * dx + dy * dy, 1e-18)
    else:
        x0, y0 = fl["x0"][j], fl["y0"][j]
        dx, dy, len2 = fl["dx"][j], fl["dy"][j], fl["len2"][j]
    t = np.clip(((X - x0) * dx + (Y - y0) * dy) / len2, 0.0, 1.0)
    hx = X - (x0 + dx * t)
    hy = Y - (y0 + dy * t)
    h2 = hx * hx + hy * hy
    ok = h2 < s * s
    if not ok.any():
        return
    cpt, cpr, j, t, h2 = cpt[ok], cpr[ok], j[ok], t[ok], h2[ok]
    if pairs.periodic:
        t = pairs.st0[cpr] + t * (pairs.st1[cpr] - pairs.st0[cpr])
    arc_u = fl["arc0"][j] + t * fl["slen"][j]
    z = struct.z_at(fl["fid"][j], arc_u)
    height = z + np.sqrt(s * s - h2)
    sink = pairs.sink[cpr]
    if protect is not None:
        sink = np.where(protect[cpt], 0.0, sink)
    height = height - sink
    np.maximum.at(out, cpt, height)


# --------------------------------------------------------------------------- #
# per-fibre solve
# --------------------------------------------------------------------------- #

def _self_crossings_of(base, cum, cell, period=None):
    """(lo_arc, hi_arc, sin_t) of every XY self-crossing of one polyline --
    the twin of g2f._self_intersections, on the coarse grid."""
    if period is not None:
        return g2f._self_intersections(base, cum, cell, period=period)
    n = len(base) - 1
    if n < 3:
        return []
    x0 = base[:-1, 0]
    y0 = base[:-1, 1]
    x1 = base[1:, 0]
    y1 = base[1:, 1]
    g = _Grid(cell)
    g.add(x0, y0, x1, y1, 0)
    qi, sj = g.query(x0, y0, x1, y1)
    keep = sj < qi - 1
    qi, sj = qi[keep], sj[keep]
    if qi.size == 0:
        return []
    hit, t, u = g2f._seg_hits_xy(x0[qi], y0[qi], x1[qi], y1[qi],
                                 x0[sj], y0[sj], x1[sj], y1[sj])
    if not hit.any():
        return []
    qh, sh, th, uh = qi[hit], sj[hit], t[hit], u[hit]
    arc_a = cum[qh] + th * (cum[qh + 1] - cum[qh])
    arc_b = cum[sh] + uh * (cum[sh + 1] - cum[sh])
    dax = x1[qh] - x0[qh]
    day = y1[qh] - y0[qh]
    dbx = x1[sh] - x0[sh]
    dby = y1[sh] - y0[sh]
    la = np.hypot(dax, day)
    lb = np.hypot(dbx, dby)
    okl = (la > 1e-12) & (lb > 1e-12)
    sin_t = np.where(okl, np.abs(dax * dby - day * dbx)
                     / np.where(okl, la * lb, 1.0), 1.0)
    lo = np.minimum(arc_a, arc_b)
    hi = np.maximum(arc_a, arc_b)
    return list(zip(lo.tolist(), hi.tolist(), sin_t.tolist()))


def _solve_fibre(base, btimes, struct, diameter, mult, bed, is_loop,
                 fuse_sink, fuse_wall, band, sag_law=None, chart=None):
    """Sag one fibre against the committed structure. Returns
    (arcs, xo, yo, zo, times): the resampled sagged centreline in the
    commanded path's arc space. The flow is `_SagState._solve_layer`'s,
    with analytic pair contacts in place of the sampled floor."""
    cum, total = g2f._arc_lengths(base)
    # contact geometry runs on the SIMPLIFIED polyline (same XY to within a
    # tenth-diameter); output sampling and z profiles stay full-resolution
    keep_s = _simplify_idx(base, 0.1 * diameter)
    base_s = base[keep_s]
    cum_s = cum[keep_s]
    radius = diameter / 2.0
    s = diameter                       # equal tubes: contact at one diameter
    merge_tol = radius
    eps_z = 0.02 * diameter
    droop = sag_law if sag_law is not None else g2f._sag_law
    angular = (band is not None and fuse_wall is not None
               and fuse_wall != fuse_sink)
    fuse_any = fuse_sink > 0.0 or (fuse_wall or 0.0) > 0.0

    if angular:
        lo_b, hi_b = band

        def sink_of(sin_t):
            w = g2f._wall_weight(np.minimum(sin_t, 1.0), (lo_b, hi_b))
            return fuse_wall * w + fuse_sink * (1.0 - w)
    elif fuse_any:
        def sink_of(sin_t):
            return np.full(np.shape(sin_t), fuse_sink, dtype=np.float64)
    else:
        def sink_of(sin_t):
            return np.zeros(np.shape(sin_t), dtype=np.float64)

    pairs = _find_pairs(struct, base_s, cum_s, s, sink_of)
    curved = chart is not None and chart.curved

    def rest(arcs_q, protect=None):
        qx = np.interp(arcs_q, cum, base[:, 0])
        qy = np.interp(arcs_q, cum, base[:, 1])
        return _rest_at(arcs_q, qx, qy, pairs, struct, s, bed, protect)

    # --- supports: exact crossings over the previous layer ----------------- #
    contacts = []
    # each crossing's round top along this fibre (exact: `_support_caps`)
    cap_arcs, cap_half = [], []
    if pairs is not None and struct.prev_hi > struct.prev_lo:
        prev = (pairs.sj >= struct.prev_lo) & (pairs.sj < struct.prev_hi)
        if prev.any():
            fl = struct.flat()
            qi = pairs.qi[prev]
            sj = pairs.sj[prev]
            if pairs.periodic:
                hit, t, _u = g2f._seg_hits_xy(
                    pairs.qx0[prev], pairs.qy0[prev],
                    pairs.qx1[prev], pairs.qy1[prev],
                    pairs.sx0[prev], pairs.sy0[prev],
                    pairs.sx0[prev] + pairs.sdx[prev],
                    pairs.sy0[prev] + pairs.sdy[prev])
            else:
                hit, t, _u = g2f._seg_hits_xy(
                    base_s[qi, 0], base_s[qi, 1],
                    base_s[qi + 1, 0], base_s[qi + 1, 1],
                    fl["x0"][sj], fl["y0"][sj],
                    fl["x0"][sj] + fl["dx"][sj], fl["y0"][sj] + fl["dy"][sj])
            if hit.any():
                if pairs.periodic:
                    qh = pairs.qi[prev][hit]
                    t0 = pairs.qt0[prev][hit]
                    ta = t0 + t[hit] * (pairs.qt1[prev][hit] - t0)
                    arcs_x = cum_s[qh] + ta * (cum_s[qh + 1] - cum_s[qh])
                    p0 = np.column_stack([pairs.sx0[prev][hit],
                                          pairs.sy0[prev][hit]])
                    d = np.column_stack([pairs.sdx[prev][hit],
                                         pairs.sdy[prev][hit]])
                    # back from the query piece's frame (exact's shift)
                    b0 = base_s[qh, :2] + t0[:, None] * (base_s[qh + 1, :2]
                                                         - base_s[qh, :2])
                    p0 = p0 - (np.column_stack([pairs.qx0[prev][hit],
                                                pairs.qy0[prev][hit]]) - b0)
                    q = np.column_stack([
                        pairs.qx1[prev][hit] - pairs.qx0[prev][hit],
                        pairs.qy1[prev][hit] - pairs.qy0[prev][hit]])
                else:
                    qh = qi[hit]
                    arcs_x = cum_s[qh] + t[hit] * (cum_s[qh + 1] - cum_s[qh])
                    sh = sj[hit]
                    p0 = np.column_stack([fl["x0"][sh], fl["y0"][sh]])
                    d = np.column_stack([fl["dx"][sh], fl["dy"][sh]])
                    q = base_s[qh + 1, :2] - base_s[qh, :2]
                # chords of this fibre's own path are not crossings (exact:
                # `_crossing_contacts` / `_same_path`)
                reach = 2.0 * np.hypot(d[:, 0], d[:, 1]) + g2f._SAME_PATH_TOL
                real = ~g2f._same_path(p0, p0 + d, base_s[:, :2], cum_s,
                                       arcs_x, reach)
                contacts.extend(arcs_x[real].tolist())
                q, d = q[real], d[real]
                lq, ld = np.hypot(q[:, 0], q[:, 1]), np.hypot(d[:, 0], d[:, 1])
                ok = (lq > 1e-12) & (ld > 1e-12)
                sin_x = np.where(ok, np.abs(q[:, 0] * d[:, 1] - q[:, 1] * d[:, 0])
                                 / np.where(ok, lq * ld, 1.0), 1.0)
                cap_arcs.extend(arcs_x[real].tolist())
                cap_half.extend((diameter / np.maximum(
                    sin_x, g2f._CAP_MIN_SIN)).tolist())

    def resolve(contact_arcs, forced=()):
        sups = [0.0]
        for a in sorted(contact_arcs) + [total]:
            if a - sups[-1] > merge_tol:
                sups.append(a)
        sups[-1] = total
        sa = np.asarray(sups, dtype=np.float64)
        protect = None
        if is_loop and len(sa):
            protect = np.zeros(len(sa), dtype=bool)
            protect[0] = True
        sh = rest(sa, protect)
        if is_loop and len(sh):
            # linking-loop entry continuity: the entry MERGES with the layer
            # below instead of resting a diameter on top (exact :2236-2248)
            sh[0] = max(bed, sh[0] - diameter)
        for a, h in forced:
            k = int(np.argmin(np.abs(sa - a)))
            if h > sh[k]:
                sh[k] = h
        return sups, sh

    sups, sup_h = resolve(contacts)

    def curved_extra(a, b, hL, hR, span):
        """Cylinder terms B/C, ported from `_SagState._solve_layer`.

        The analytic contact approximation operates in the same unrolled
        surface coordinates as the exact solver.  Its bridge depth must still
        use the offset-surface metric and its chord must sit below a curved
        collector; otherwise the fast path finds the right contacts but draws
        the wrong tube between them.
        """
        ua, ub = np.interp(a, cum, base[:, 0]), np.interp(b, cum, base[:, 0])
        va, vb = np.interp(a, cum, base[:, 1]), np.interp(b, cum, base[:, 1])
        du, dv = ub - ua, vb - va
        L = np.hypot(du, dv)
        ok = L > 0.0
        Ls = np.where(ok, L, 1.0)
        cu = np.where(ok, du / Ls, 1.0)      # SIGNED; see `_curved_extra`
        cv = np.where(ok, dv / Ls, 0.0)
        hmid = 0.5 * (hL + hR)
        umid = 0.5 * (ua + ub)
        vmid = 0.5 * (va + vb)
        # the step as the SURFACE sees it: one direction, length and angle, and
        # on a developable collector these are the same two expressions this
        # function would inline, in the same order
        eu, ev = chart.principal_step(umid, vmid, hmid, cu, cv)
        span_p = span * np.hypot(eu, ev)
        return (droop(span_p, diameter, mult) - droop(span, diameter, mult)
                + chart.chord_drop(span_p, eu, ev, umid, vmid, hmid))

    def span_parab(qa, a, b, hL, hR, depth=None):
        # one span's droop; `parab` is this over the support table, and the
        # tangent touchdown draws the sub-span it pins with it (exact solver's
        # `span_parab`, including its `qa=None` / `depth=` calls)
        span = b - a
        if depth is None:
            depth = droop(span, diameter, mult)
            if curved:
                depth = depth + curved_extra(a, b, hL, hR, span)
        if qa is None:
            return depth
        tt = np.where(span > 0.0, (qa - a) / np.where(span > 0.0, span, 1.0),
                      0.0)
        chord = hL * (1.0 - tt) + hR * tt
        return chord - depth * 4.0 * tt * (1.0 - tt)

    def parab(qa):
        sa = np.asarray(sups, dtype=np.float64)
        shh = np.asarray(sup_h, dtype=np.float64)
        k = np.clip(np.searchsorted(sa, qa, side="right") - 1,
                    0, len(sups) - 2)
        return span_parab(qa, sa[k], sa[k + 1], shh[k], shh[k + 1])

    # --- droop-reachable floor peaks (exact :2292-2323) -------------------- #
    contact_cache = None
    if pairs is not None and pairs.size:
        fl = struct.flat()
        j = pairs.sj
        zp = struct.z_at(fl["fid"][j], fl["arc0"][j] + pairs.u * fl["slen"][j])
        peak_h = zp + np.sqrt(np.maximum(s * s - pairs.h2, 0.0)) - pairs.sink
        contact_cache = (pairs.arcq, peak_h, pairs.sin_t)
        # A single closest-approach candidate per pair misses maxima across
        # the REST of the contact's neighbourhood (a grazing run's floor
        # carries the lower fibre's own bumps; an oblique crossing's ridge is
        # elongated). So each REACHABLE pair's contact window is scanned
        # against THAT PAIR ALONE -- one combo per probe, so the scan is
        # O(total probes) rather than O(probes x pairs) -- at half-diameter
        # steps on steep windows (exact's window law :2469-2471) and two
        # diameters along grazing runs. The scan's per-pair profile maxima
        # plus every pair's closest approach then face one shared test.
        pre = (peak_h > bed + eps_z) & (peak_h >= parab(pairs.arcq) - diameter)
        cand_parts = []
        reach = pre & (peak_h >= parab(pairs.arcq) - eps_z)
        if reach.any():
            cand_parts.append(pairs.arcq[reach])
        idxp = np.flatnonzero(pre)
        if idxp.size:
            grazing = pairs.hw[idxp] >= 4.0 * s
            win = np.where(grazing, pairs.hw[idxp],
                           np.minimum(50.0 * diameter,
                                      np.maximum(3.0 * diameter,
                                                 3.0 * diameter
                                                 / np.maximum(pairs.sin_t[idxp],
                                                              1e-9))))
            stepp = np.where(grazing, 3.0 * s,
                             2.0 * merge_tol)
            w_lo = np.maximum(pairs.arcq[idxp] - win, 0.0)
            w_hi = np.minimum(pairs.arcq[idxp] + win, total)
            cnt = np.maximum(3, np.ceil((w_hi - w_lo) / stepp)
                             .astype(np.int64) + 1)
            tot_p = int(cnt.sum())
            kk = (np.arange(tot_p, dtype=np.int64)
                  - np.repeat(np.cumsum(cnt) - cnt, cnt))
            frac = kk / np.repeat(cnt - 1, cnt)
            pa = np.repeat(w_lo, cnt) + np.repeat(w_hi - w_lo, cnt) * frac
            pp = np.repeat(idxp, cnt)
            jp = pairs.sj[pp]
            px_ = np.interp(pa, cum, base[:, 0])
            py_ = np.interp(pa, cum, base[:, 1])
            tp = np.clip(((px_ - fl["x0"][jp]) * fl["dx"][jp]
                          + (py_ - fl["y0"][jp]) * fl["dy"][jp])
                         / fl["len2"][jp], 0.0, 1.0)
            hx = px_ - (fl["x0"][jp] + fl["dx"][jp] * tp)
            hy = py_ - (fl["y0"][jp] + fl["dy"][jp] * tp)
            h2p = hx * hx + hy * hy
            zpp = struct.z_at(fl["fid"][jp],
                              fl["arc0"][jp] + tp * fl["slen"][jp])
            f = np.where(h2p < s * s,
                         zpp + np.sqrt(np.maximum(s * s - h2p, 0.0))
                         - pairs.sink[pp], -np.inf)
            first = np.r_[True, pp[1:] != pp[:-1]]
            last = np.r_[pp[1:] != pp[:-1], True]
            fm1 = np.r_[-np.inf, f[:-1]]
            fp1 = np.r_[f[1:], -np.inf]
            is_mx = (~first & ~last & np.isfinite(f)
                     & (f >= fm1) & (f > fp1) & (f > bed + eps_z))
            if is_mx.any():
                cand_parts.append(pa[is_mx])
        new_contacts = []
        if cand_parts:
            # dedupe to the merge grid, then one shared decision per
            # candidate -- exact's rule (:2317-2320): pin only where the
            # AGGREGATE floor is a droop-reachable LOCAL MAXIMUM. A
            # three-point test at +/- merge_tol with exact's plateau
            # semantics (>= behind, > ahead) drops candidates on flat runs
            # (a discretised arc riding the same arc below fakes chord
            # "crossings" there): pinning those shortens the neighbouring
            # spans, flattens the span^2 parabola that exact lets DIVE, and
            # widens every true crossing spike into a tent.
            ca = np.concatenate(cand_parts)
            ca = np.unique(np.round(ca / (0.5 * merge_tol)) * (0.5 * merge_tol))
            trip = np.clip(np.concatenate([ca - merge_tol, ca,
                                           ca + merge_tol]), 0.0, total)
            f3 = rest(trip)
            n_c = ca.size
            fm, f0, fp = f3[:n_c], f3[n_c:2 * n_c], f3[2 * n_c:]
            is_max = (f0 >= fm) & (f0 > fp) & (f0 > bed + eps_z)
            if is_max.any():
                # "Reachable" on the CONVERGED droop, as exact judges it
                # (`_relax_pins`). Exact relaxes over its whole fine-grid
                # floor; this engine over its candidate peaks only -- the
                # floor it has without a dense rest scan.
                sa = np.asarray(sups, dtype=np.float64)
                cp = g2f._support_caps(sa, cap_arcs, cap_half, merge_tol)
                k = np.clip(np.searchsorted(sa, ca, side="right") - 1,
                            0, len(sa) - 2)
                obs = (ca - sa[k] >= cp[k]) & (sa[k + 1] - ca >= cp[k + 1])
                pinned, chain, _hit = g2f._relax_pins(
                    ca, f0, sa, sup_h, span_parab, obs & is_max)
                is_max &= obs & (pinned | (f0 >= chain - eps_z))
            new_contacts = ca[is_max].tolist()
        if new_contacts:
            contacts.extend(new_contacts)
            sups, sup_h = resolve(contacts)

    # --- same-layer self-crossings as supports (exact :2325-2407) ---------- #
    crossings = _self_crossings_of(base_s, cum_s, struct.cell, struct.period)
    # A RETRACE -- a stroke laid straight back over the one before it -- rests on
    # itself without ever CROSSING itself, and no intersection test can see it
    # (g2f._self_near says why). Only asked when there is no crossing at all, so
    # every fibre that already had one keeps its existing path bit for bit; a
    # fibre that both crosses and retraces still gets only its crossings here,
    # which is this engine's windowed approximation and not a new residual.
    # Read on the COMMANDED polyline, not on `base_s`: the simplifier collapses a
    # straight out-and-back to its three corner vertices, which is two segments,
    # and `_self_pairs_xy` needs three to have a non-adjacent pair at all. The
    # crossing finder above can afford the coarse grid because a crossing is a
    # corner of the simplified path too; an overlap is not.
    self_retrace = (not crossings) and g2f._self_near(
        base, cum, struct.cell, diameter, struct.period)
    self_sinks = None
    if crossings:
        lo_arcs = np.array([c[0] for c in crossings], dtype=np.float64)
        hi_arcs = np.array([c[1] for c in crossings], dtype=np.float64)
        lx = np.interp(lo_arcs, cum, base[:, 0])
        ly = np.interp(lo_arcs, cum, base[:, 1])
        near = (np.hypot(lx - base[0, 0], ly - base[0, 1]) < diameter
                if is_loop else None)
        rh_lo = _rest_at(lo_arcs, lx, ly, pairs, struct, s, bed, near) \
            if pairs is not None else np.full(lo_arcs.size, bed)
        z_lo = np.maximum(parab(lo_arcs), rh_lo)
        if is_loop and near is not None and near.any():
            # the lower strand at the entry corner is merged a diameter down;
            # the crossing over it lands a diameter lower too (exact :2369-2386)
            z_lo[near] = np.maximum(bed, z_lo[near] - diameter)
        contacts.extend(hi_arcs.tolist())
        self_rest = z_lo + diameter
        sin_x = np.array([c[2] for c in crossings], dtype=np.float64)
        cap_arcs.extend(hi_arcs.tolist())
        cap_half.extend((diameter / np.maximum(
            sin_x, g2f._CAP_MIN_SIN)).tolist())
        self_sinks = sink_of(sin_x)
        if fuse_any:
            self_rest = np.maximum(z_lo, self_rest - self_sinks)
        if _DEBUG is not None:
            _DEBUG.update(z_lo=z_lo.copy(), rh_lo=rh_lo.copy(),
                          parab_lo=parab(lo_arcs), self_rest=self_rest.copy())
        sups, sup_h = resolve(contacts,
                              zip(hi_arcs.tolist(), self_rest.tolist()))

    # --- output samples (exact :2409-2477) --------------------------------- #
    # `res`: the exact solver's sag resolution, read from its module so the two
    # sample the drape identically at every setting (g2f.set_sag_resolution)
    res = g2f.sag_resolution()
    sag_step = (g2f.SAG_STEP_D * diameter
                / math.sqrt(max(1.0, mult)) / res)
    tol = g2f.SAG_TOL_D * diameter
    sa = np.asarray(sups, dtype=np.float64)
    shh = np.asarray(sup_h, dtype=np.float64)
    span = np.diff(sa)
    free = np.asarray(droop(span, diameter, mult), dtype=np.float64)
    if curved and span.size:
        free = free + curved_extra(sa[:-1], sa[1:], shh[:-1], shh[1:], span)
    midchord = 0.5 * (shh[:-1] + shh[1:])
    eff = np.minimum(free, np.maximum(0.0, midchord - bed))
    live = np.flatnonzero(eff > tol)
    parts = [cum, sa]
    # Crest reach per SUPPORT: each owns the wider of its two sides (g2f
    # crest_extent, same call, same arguments).
    crest_w = np.zeros(sa.size, dtype=np.float64)
    if span.size:
        w_span = g2f.crest_extent(span, free, eff, diameter,
                                  np.abs(np.diff(shh)))
        crest_w[:-1] = w_span
        crest_w[1:] = np.maximum(crest_w[1:], w_span)
    if live.size:
        n_sp = np.minimum(int(g2f.SAG_MAX_SPAN_SAMPLES * res),
                          np.maximum(2, np.round(span[live] / sag_step)
                                     .astype(np.int64)))
        cnt = n_sp - 1                       # interior points per live span
        tot = int(cnt.sum())
        k = (np.arange(tot, dtype=np.int64)
             - np.repeat(np.cumsum(cnt) - cnt, cnt)) + 1
        frac = k / np.repeat(n_sp, cnt)
        parts.append(np.repeat(sa[live], cnt) + np.repeat(span[live], cnt) * frac)
    # Crest detail at EVERY support, both sides, at equal arc steps, reaching at
    # most half way to the neighbour -- the exact solver's rule verbatim, so the
    # two keep sampling the drape identically. Outside the `live` branch for the
    # same reason it is there: the crest belongs to the support, not to a
    # drooping span.
    if sa.size >= 2:
        step = g2f.CREST_STEP_D * diameter / res
        for w in (np.minimum(crest_w, 0.5 * (sa - np.r_[0.0, sa[:-1]])),
                  -np.minimum(crest_w, 0.5 * (np.r_[sa[1:], total] - sa))):
            ns = np.clip(np.ceil(np.abs(w) / step), 1,
                         int(g2f.CREST_MAX_SAMPLES * res)).astype(np.int64)
            for n in np.unique(ns):
                sel = ns == n
                frac = np.arange(1, n + 1, dtype=np.float64) / n
                parts.append((sa[sel, None] - w[sel, None] * frac).ravel())
    for lo, hi, sin_t in crossings:
        half = min(50.0 * diameter,
                   max(3.0 * diameter, 3.0 * diameter / max(sin_t, 0.15)))
        steps = int(math.ceil(half / merge_tol))
        win = np.arange(-steps, steps + 1, dtype=np.float64) * merge_tol
        parts.append(lo + win)
        parts.append(hi + win)
    if contact_cache is not None:
        # Clamp coverage: a steep contact's interval (~s/sin along the arc)
        # can be narrower than the span sampling, and a contact REJECTED as
        # a support (not an aggregate floor maximum) still physically lifts
        # the fibre -- without a sample inside its interval the clamp never
        # sees it and the fibre PHASES THROUGH (caught by sag_bench
        # invariants on the wavy case: two-layers-down crossings whose
        # candidates lost the local-max test to a neighbouring ridge). Add
        # a three-point sample at every contact that would lift above the
        # final parabola and is not already covered by a support.
        c_arc, c_h, c_sin = contact_cache
        sup_near = np.asarray(sups, dtype=np.float64)
        k_near = np.clip(np.searchsorted(sup_near, c_arc), 1,
                         len(sup_near) - 1)
        d_near = np.minimum(np.abs(c_arc - sup_near[k_near - 1]),
                            np.abs(c_arc - sup_near[np.minimum(
                                k_near, len(sup_near) - 1)]))
        lift = ((c_h > bed + eps_z) & (c_h >= parab(c_arc) - eps_z)
                & (d_near > 2.0 * merge_tol))
        if lift.any():
            ca_ = c_arc[lift]
            hw_c = np.minimum(0.5 * s / np.maximum(c_sin[lift], 0.2),
                              3.0 * diameter)
            parts.append(ca_)
            parts.append(ca_ - hw_c)
            parts.append(ca_ + hw_c)
    arcs = np.unique(np.concatenate(parts))
    arcs = arcs[(arcs >= 0.0) & (arcs <= total)]
    xo = np.interp(arcs, cum, base[:, 0])
    yo = np.interp(arcs, cum, base[:, 1])

    near_entry = None
    if is_loop and len(arcs):
        near_entry = np.hypot(xo - base[0, 0], yo - base[0, 1]) < diameter
    floor_arc = _rest_at(arcs, xo, yo, pairs, struct, s, bed, near_entry)
    if near_entry is not None:
        floor_arc[near_entry] = np.maximum(bed,
                                           floor_arc[near_entry] - diameter)
    # the free droop, relaxed onto a floor with no peak to pin (exact solver:
    # `_tangent_touchdowns`, same call, same order)
    z_par = g2f._tangent_touchdowns(
        arcs, parab(arcs), floor_arc, sups, sup_h, span_parab, near_entry,
        g2f._support_caps(sups, cap_arcs, cap_half, merge_tol))
    z_par = g2f._round_crests(arcs, z_par, g2f.CREST_RADIUS_D * diameter)
    zo = np.maximum(z_par, floor_arc)

    # --- rest on the fibre's OWN earlier strand (exact :2535-2546) --------- #
    if crossings and struct.period is None:
        order = np.argsort([c[1] for c in crossings])
        for ci in order:
            lo, hi, sin_t = crossings[ci]
            sink_x = float(self_sinks[ci]) if self_sinks is not None else 0.0
            half = min(50.0 * diameter,
                       max(3.0 * diameter, 3.0 * diameter / max(sin_t, 0.15)))
            p0 = np.searchsorted(arcs, hi - half, side="left")
            p1 = np.searchsorted(arcs, hi + half, side="right")
            if p0 >= p1:
                continue
            k0 = max(0, np.searchsorted(cum_s, lo - half, side="right") - 1)
            k1 = min(len(base_s) - 1, int(np.searchsorted(cum_s, lo + half,
                                                          side="left")))
            if k1 <= k0:
                continue
            ks = np.arange(k0, k1, dtype=np.int64)
            # only the EARLIER strand may support: cap at segments that end
            # before this crossing's later strand begins
            ks = ks[cum_s[ks + 1] < hi - merge_tol]
            if ks.size == 0:
                continue
            X = xo[p0:p1][:, None]
            Y = yo[p0:p1][:, None]
            sx0 = base_s[ks, 0][None, :]
            sy0 = base_s[ks, 1][None, :]
            sdx = (base_s[ks + 1, 0] - base_s[ks, 0])[None, :]
            sdy = (base_s[ks + 1, 1] - base_s[ks, 1])[None, :]
            l2 = np.maximum(sdx ** 2 + sdy ** 2, 1e-18)
            tt = np.clip(((X - sx0) * sdx + (Y - sy0) * sdy) / l2, 0.0, 1.0)
            hx = X - (sx0 + sdx * tt)
            hy = Y - (sy0 + sdy * tt)
            h2 = hx * hx + hy * hy
            ok = h2 < s * s
            if not ok.any():
                continue
            arc_u = (cum_s[ks][None, :]
                     + tt * (cum_s[ks + 1] - cum_s[ks])[None, :])
            z_lo_cur = np.interp(arc_u.ravel(), arcs, zo).reshape(arc_u.shape)
            lift = z_lo_cur + np.sqrt(np.maximum(s * s - h2, 0.0))
            lift = np.maximum(z_lo_cur, lift - sink_x)
            lift = np.where(ok, lift, -np.inf)
            best = lift.max(axis=1)
            seg = zo[p0:p1]
            zo[p0:p1] = np.maximum(seg, np.where(np.isfinite(best), best, seg))
    elif crossings or self_retrace:
        # The exact solver's print-ordered same-fibre contact sweep already
        # understands the periodic seam, and it is also the only shape that can
        # express a RETRACE: the windowed lift above hangs each contact off a
        # crossing ARC, and a retrace has none -- it is held along its whole
        # length.  Reusing it here retains the
        # approximate engine's analytic inter-layer contacts while preserving
        # the mandrel lattice made by a pass crossing its own earlier turns.
        zo = g2f._self_contact_heights(
            arcs, xo, yo, zo, diameter, struct.cell, fuse_sink=fuse_sink,
            fuse_wall=fuse_wall if angular else None,
            band=band if angular else None, period=struct.period)
    if crossings or self_retrace:
        # ...and carry the filament across any 180 degree FOLD, the one window
        # no self-contact can support by construction. The exact solver's
        # `_SagState` runs this right after its own self-contact pass, with the
        # same arc gap the pair admissibility uses; see `g2f._fold_bridge`.
        zo = g2f._fold_bridge(arcs, xo, yo, zo,
                              g2f._SELF_CONTACT_ARC * float(diameter))

    times = None
    if btimes is not None:
        times = np.interp(arcs, cum, btimes)
    if _DEBUG is not None:
        _DEBUG.update(sups=np.asarray(sups), sup_h=np.asarray(sup_h),
                      crossings=crossings, arcs=arcs, zo=zo, z_par=z_par,
                      floor_arc=floor_arc, pairs=pairs)
    return arcs, xo, yo, zo, times, base_s, cum_s


# --------------------------------------------------------------------------- #
# the public engine
# --------------------------------------------------------------------------- #

def drape_sag(paths, ptimes, diameter, sag_multiplier=1.0,
              sag_reset_layers=0, fuse_sink=0.0, fuse_wall=None, band=None,
              sag_law=None, frame=None, progress=None):
    """Sag `paths` (list of (N,3) polylines) with the exact solver's physics at
    preview speed. Returns (sagged_paths, sagged_times); `ptimes=None` -> times come
    back None. See the module docstring for the model and the departures from
    `gcode_to_fibers.apply_sag`.

    `fuse_sink` / `fuse_wall` (mm) are the ALREADY-EVALUATED junction/wall fusion
    sinks for this scaffold's diameter (the caller evaluates the physics law -- this
    module imports no physics); `band` is the (sin_lo, sin_hi) wall/junction blend
    band, None = no split. `sag_reset_layers` lays every Nth layer straight and
    re-datums onto the structure below (`_SagState.maybe_reset`'s period).

    `sag_law` is the droop law: a callable (span, diameter, sag_multiplier) -> depth,
    the SAME contract as `gcode_to_fibers.sag_depth`, evaluated by the CALLER exactly
    as the fusion law is. None falls back to `gcode_to_fibers._sag_law`. Pass the
    rig-calibrated physical law to match an exact solve run with `--physical-sag`,
    which the desktop ships by default (`gui_sim.py:691` sends it whenever
    physics-deposit is on). THE LAW MUST MATCH THE EXACT SIDE: running different laws
    on the two sides is their single largest source of disagreement -- matched laws
    agree to 0.27-0.29 um mean, mismatched to 3.83 um, 14x worse.

    A bottom-layer fibre rests its UNDERSIDE on the plate, not its centreline: the
    bed floor is `bed + diameter/2`, matching `apply_sag`'s `bed_at`. The commanded
    Z of a layer is the height of the surface that layer lands on, so a tube laid
    on it has its centre one radius up, on every caller; otherwise a bottom layer
    sits half a fibre inside the plate and its whole stack one radius low."""
    if not paths:
        return [], ([] if ptimes is not None else None)
    try:
        diameter = float(diameter)
    except (TypeError, ValueError):
        return paths, ptimes
    if not math.isfinite(diameter) or diameter <= 1e-9:
        # audit contract: bad diameter degrades to a no-op, never a crash
        return paths, ptimes
    mult = float(sag_multiplier)
    if not math.isfinite(mult) or mult <= 0.0:
        # contract (test_preview_sag): multiplier <= 0 is the OFF switch --
        # identity passthrough, not a zero-droop solve
        return paths, ptimes
    # The chart has to be a RULER where the pattern goes -- this engine finds
    # its contacts between chart segment PAIRS and rests fibres on a chart
    # structure, so an overstated distance loses a support with no symptom.
    # `mew_frame.require_chart` refuses the disc around the axis of revolution.
    # It does not refuse the whole surface, and it runs AFTER the
    # degrade-to-no-op checks above, because the patch scale it needs is the validated diameter, and
    # because a solve that is switched off does not use the chart at all.
    #
    # `ruler="approx"`, AND THAT BAND IS A MEASUREMENT RATHER THAN A MECHANISM.
    # It is deliberately NOT the exact solver's "local" band: this engine never
    # corrects the chart distance, and a COST argument is not an ACCURACY one.
    # The exact solver has a local chart, so it accepts a
    # dome at 67.0% of overstatement; this engine still measures in the global
    # chart, so holding it to the raw 12.5% band would refuse the Export panel's
    # "Approximate" choice on a collector the "Exact" choice runs -- one
    # dropdown apart. What the shear actually costs here is measured rather
    # than assumed: against the exact solver on the shipped-default dome the
    # mean disagreement is 1.916 um, where on a CONE at 4.9% this engine already
    # disagrees by 1.159 um, so the chart's whole share is 0.76 um against a
    # 21.81 um fibre -- and the 198 um MAX is identical on both shapes, so it is
    # the method's and not the chart's (through `sag_bench.compare_outputs`).
    # BLIND SPOT, stated: that is one point at 67%, and a far more
    # sheared collector has no measured bound here.
    reset_n = int(sag_reset_layers or 0)
    fuse_sink = max(0.0, float(fuse_sink or 0.0))
    fuse_wall = None if fuse_wall is None else max(0.0, float(fuse_wall))

    with_times = ptimes is not None
    arrs = [np.asarray(p, dtype=np.float64).reshape(-1, 3) for p in paths]
    # THE ONE CONVERSION. `paths` arrive as WORLD XYZ in the collector's own
    # frame and every line below measures in the CHART the frame selects; the
    # output goes back at the return. Identity on a flat bed and on a
    # developable collector, so neither can move a byte.
    chart = chart_for(frame)
    arrs = chart.from_world(arrs)
    loops = g2f.loop_entry_flags(arrs)
    if frame is not None:
        _pts = np.concatenate([np.asarray(p, dtype=np.float64) for p in arrs])
        chart.require_ruler("the approximate sag solver", _pts, diameter,
                            ruler="approx")

    # DEPOSITION-order layering (g2f._layer_ids_deposition), not global-Z: in
    # continuous-flow MEW whatever is laid later lands ON the structure laid
    # before it, whatever its commanded Z -- identical for every ascending-Z
    # scaffold, and the preview contract asserts the physical order
    fiber_layer, n_layers = g2f._layer_ids_deposition(arrs,
                                                      layer_tol=0.5 * diameter)
    bed = min(float(np.median(p[:, 2])) for p in arrs if len(p)) \
        if any(len(p) for p in arrs) else 0.0
    # underside-on-plate: the commanded Z is the bare SURFACE, so lift the floor
    # by one radius and the fibre's BOTTOM, not its centreline, lands on the bed
    # (exact's `bed_at`)
    bed += 0.5 * diameter

    cell = max(0.35, 6.0 * diameter)
    curved = chart.curved
    struct = _Structure(cell, period=chart.period)
    committed = 0
    last_top = None
    # The previous layer's tails, for the layer-change seam (see
    # `gcode_to_fibers.seam_tail_z`): the exact solver's `_SagState.prev_tails`.
    # Flat bed only, as there -- a collector's heights are along its normal.
    prev_tails = ()

    out = [None] * len(arrs)
    out_t = [None] * len(arrs) if with_times else None

    for li in range(n_layers):
        if progress is not None:
            # The exact solver takes the identical hook. Ticked at the TOP of
            # the layer because the `continue` below skips an empty one and the
            # bar must still account for it.
            progress(li, n_layers)
        members = [i for i in range(len(arrs)) if fiber_layer[i] == li]
        if not members:
            continue
        # periodic sag reset (exact `_SagState.maybe_reset`): forget the
        # structure below, re-datum onto its top, lay this layer straight
        if (reset_n >= 1 and committed and committed % reset_n == 0
                and last_top is not None):
            # `last_top` is the highest CENTRELINE below, so the new floor is
            # r_below + r_this above it -- one diameter, the contact distance.
            # Exact does the same in two halves (`maybe_reset` adds r_below,
            # `bed_at` adds r_this); the old `0.5 * diameter` here under the
            # underside lift was half a diameter short and drove a reset layer
            # into the one beneath it.
            bed = last_top + diameter
            struct.clear()
            prev_tails = ()           # a reset layer lies straight: no seam
        solved = []
        for i in members:
            base = g2f._dedupe(arrs[i])
            bt = None
            if with_times:
                keep = g2f._dedupe_idx(arrs[i])
                bt = np.asarray(ptimes[i], dtype=np.float64)[keep]
            if len(base) < 2:
                # No segment, so no droop to solve -- but it is still a fibre
                # laid on a surface, and `base`'s Z is that surface. Seat it the
                # way every solved point is seated (exact's twin of this branch
                # in `_SagState._solve_layer`), or a one-point band passes
                # through a radius low and is then the lowest thing in the run.
                out[i] = base + np.array([0.0, 0.0, 0.5 * diameter])
                if with_times:
                    out_t[i] = bt
                continue
            arcs, xo, yo, zo, times, base_s, cum_s = _solve_fibre(
                base, bt, struct, diameter, mult, bed, bool(loops[i]),
                fuse_sink, fuse_wall, band, sag_law, chart)
            if prev_tails:
                tail_z = g2f.seam_tail_z(prev_tails, base[0, 0], base[0, 1],
                                         diameter)
                if tail_z is not None:
                    xy_s = np.concatenate([[0.0], np.cumsum(np.hypot(
                        np.diff(xo), np.diff(yo)))])
                    zo = g2f.seam_ramp(zo, xy_s, tail_z, diameter)
            poly = np.column_stack([xo, yo, zo])
            out[i] = poly
            if with_times:
                out_t[i] = times
            solved.append((base_s, cum_s, arcs, zo))
        if solved:
            struct.commit(solved)
            committed += 1
            if not chart.curved and chart.period is None:
                prev_tails = tuple(
                    (float(out[i][-1, 0]), float(out[i][-1, 1]),
                     float(out[i][-1, 2]))
                    for i in members if out[i] is not None and len(out[i]) >= 2)
            top = max(float(np.max(s_[3])) for s_ in solved)
            last_top = top if last_top is None else max(last_top, top)

    # Back to WORLD, the currency this engine was handed; None-safe because a
    # degenerate fibre can still hold its own short array and nothing else can
    # be None here, but the helper costs nothing and says so.
    return chart.to_world(out), out_t
