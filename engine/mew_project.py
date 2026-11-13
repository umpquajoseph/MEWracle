"""Pattern PROJECTION -- a flat serpentine laid onto a curved collector.

A three-axis scaffold is a pattern drawn in a
plane; a four-axis tube is the same pattern wrapped, and wrapping is free
because a cylinder is DEVELOPABLE -- a straight line in `(u, v)` is already a
geodesic there. On a dome it is not, and that single fact is why this module
exists: laid straight in the chart, a pass curves visibly across the surface,
its spacing to the next pass drifts, and the drift is not small.

WHAT IT PRODUCES. A flat pattern in `(p, q)` millimetres becomes a path on the
surface in which

  * every pass is a GEODESIC -- the straightest curve the surface admits, which
    is what a fibre pulled taut between two points on a collector does, and
  * the commanded interfibre spacing is arc length ON THE SURFACE, not a
    parameter difference that means a different distance at every radius.

THE MAP IS FERMI (geodesic normal) COORDINATES about one base geodesic:

    RAIL   the geodesic through the pattern origin in the ACROSS direction.
           Arc length along it is the pattern's `q`, so the spacing between
           passes is exact by construction and needs no correction factor.
    RIB    from the rail point at `q`, the geodesic in the ALONG direction,
           arc length `p`. The along direction is `N x T_rail`, and because the
           rail is a geodesic its tangent is parallel-transported -- so that
           cross product IS the parallel transport of the pattern's along axis.
           There is no transport integrator to get wrong.

    Phi(p, q) = the point reached that way.

Exact on the two axes; off them the spacing follows a Jacobi field, which on a
surface of constant Gaussian curvature is exactly `cos(sqrt(K) * p)`. That is
the honest statement of what projecting a flat pattern onto a curved surface
costs. Measured against the closed form: on a 10 mm dome the spacing at the end of an
8 mm pass is 0.921062 of commanded against a predicted 0.921061. It is a
PREDICTION about the print and not an error in the arithmetic -- a flat sheet
genuinely does not lie on a sphere, and the pattern that ends up on the
collector is the one the projection describes.

THE CURRENCY IS WORLD XYZ, NOT THE SURFACE-WIDE CHART. A march carries `(P, T)`, a
world point on the surface
and a world unit tangent in its tangent plane, and one integrator step is taken
in a LOCAL chart built at `P` and thrown away afterwards:

    e1 = T,  e2 = N x T          orthonormal, in the tangent plane at P
    Psi(a, b) = settle(P + a*e1 + b*e2)

-- the tangent plane dropped back onto the collector. `Psi` is smooth wherever
the surface is, which INCLUDES a dome's pole: the pole is an ordinary point of
a sphere and only the `(u, v)` chart was ever singular there. So a pass may run
straight through the apex and out the other side, and the refusal that used to
stop it (`POLE_GUARD_STEPS`, a cap on the step as `rho` fell) is gone from the
march. Two refusals are NOT gone and must not be deleted together with it: a
CONE APEX has no tangent plane at all, so there is no `N` to build `e1, e2`
from, and it still raises; and the machine's reach is not this module's
business either way.

WHY A LOCAL CHART AND NOT WORLD-SPACE RK4 ON `T' = -k_n(T, T) N`. Both are
chart-free. The local chart keeps the geodesic ODE, the RK4 and the stencil
EXACTLY as they were -- what changed is only where the nine stencil points come
from -- so the three comparators that certified the chart version certify this
one on the same arithmetic. It also needs no curvature query on the surface,
which a mesh would have to estimate.

SECTION 5 RULE 3, and it is why the surface sits behind an adapter. The
projector may ask the surface three questions and no others -- POINT, NORMAL,
and GEODESIC STEP. What changed with the currency is what those take: `normal`
and `geodesic_step` now take world points, and the geodesic step needs one
thing a chart gave for free -- SETTLE, "the point of the collector nearest this
one". That is a closest-point query, which is what a triangle mesh and a CAD
kernel both answer natively (`GeomAPI_ProjectPointOnSurf`), and which
`FrameSurface` answers exactly, without iteration, through the frame's own
analytic inverse. This file must not reach for `unroll`, `period_v`,
`metric_v`, `profile` or `surface_z`.

CURVATURE IS PER POINT. A `RevolutionFrame`'s published principal curvatures
are the span's MIDPOINT values -- a representative number so `is_curved()` has
an answer -- and using them as the curvature of anywhere else is the
global-chart assumption rule 2 forbids. Nothing here reads them; the geodesic
step gets its curvature from finite differences of the local chart at the point
it is standing on.

Pure math: stdlib + numpy. No VTK, no I/O, no GUI.
"""

import contextlib
import importlib
import math
import os
import sys

import numpy as np

import mew_frame


class ChartSingularity(ValueError):
    """A surface query could not be answered where it was asked.

    Distinct from `mew_frame.AxisSingularity` only in who raises it: this
    module re-raises that one with the projection's own context, because a CONE
    APEX is a genuine geometric singularity (no tangent plane, so no normal to
    stand the nozzle on) while everything else this exception carries is a
    numerical failure of a chart.

    A DOME'S POLE DOES NOT RAISE IT: the surface is perfectly well behaved at
    a pole, and it is only the `(u, v)` coordinates that stop describing it.
    The march does not use those coordinates.
    """


def _dot(a, b):
    """Row-wise 3-vector dot product, `(..., 3) -> (...)`.

    THE SPELLING IS LOAD-BEARING. This replaced `ndarray.dot` when the marching
    below gained a batch axis, and the requirement on that change was that the
    projected path come out BIT-identical, not merely close. Measured over
    200000 random pairs: `np.sum(a*b, axis=-1)` and the written-out
    `a0*b0 + a1*b1 + a2*b2` are both identical to `ndarray.dot` on every one,
    and `np.einsum("ij,ij->i", a, b)` differs on 30% of them -- the same 30% a
    deliberately reversed summation order differs on. So do not "tidy" this
    into an einsum or a matmul.
    """
    return np.sum(a * b, axis=-1)


def _unit_rows(v):
    """`v` normalised along its last axis, refusing a zero row."""
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    if not np.all(n > 0.0):
        raise ChartSingularity(
            "a zero-length tangent direction: the pattern's along axis is "
            "parallel to the collector's normal there, so there is no "
            "direction on the surface for it to run in.")
    return v / n


# --------------------------------------------------------------------------- #
# The surface, as the three queries of section 5 rule 3
# --------------------------------------------------------------------------- #
class FrameSurface(object):
    """A `mew_frame.DepositionFrame` behind the projector's three queries.

    THE ADAPTER IS THE CONTRACT. Everything parametric lives here and nothing
    above it: the marcher below is written against `normal_at`, `settle` and
    `geodesic_step` alone, all three in world millimetres, so a mesh collector
    arrives as a sibling of this class rather than as a rewrite of the
    projector. This is also the honest place for the finite differences --
    differencing a local chart IS using the surface's own geometry, and a mesh
    adapter would build the same local chart by walking triangles.

    SETTLE IS THE QUERY THE CHART USED TO HIDE. `settle(Q)` is the point of the
    deposition surface nearest `Q`, and on a `RevolutionFrame` it is exact and
    non-iterative: `from_world` reads the meridian in polar coordinates about
    the meridian circle's own centre (`mew_frame.ArcProfile.invert` -- one
    `atan2`, one `hypot`, no tolerance), and `to_world` puts the answer back at
    the deposition height. It is also BRANCH-INDEPENDENT at a pole, which is
    what makes the local chart smooth there: `from_world` resolves the two
    meridian branches a world point admits, and on a dome the far branch comes
    back with a NEGATIVE profile radius and an azimuth turned by pi, which
    `to_world` maps to the same world point term for term. So a stencil that
    straddles the pole -- half its points resolved one way and half the other
    -- still returns one smooth patch of surface.

    CHART COORDINATES ARE AN OUTPUT, NOT THE CURRENCY. `point`, `normal` and
    `chart` still speak `(u, v)`, because the sag solver, `mew_deposition` and
    the generator's own readouts still consume them and slices 2, 3 and 5 of
    this task are what move them. Nothing in the march calls them.

    THE GEODESIC STEP is the geodesic ODE integrated by RK4 in the LOCAL chart
    at the current point. A curve is a geodesic when its acceleration has no
    component IN the surface, which in a chart is

        [E F; F G] (a'', b'') = -(X_aa a'^2 + 2 X_ab a'b' + X_bb b'^2).(X_a, X_b)

    -- the first fundamental form on the left, and the extrinsic acceleration
    projected back onto the tangent basis on the right. Written this way it
    needs the chart's first and second derivatives and no Christoffel
    bookkeeping, and it is exact for any chart, orthogonal or not. The local
    chart is very nearly orthonormal at its own origin (it is the tangent plane
    dropped onto the surface), which is a numerical convenience and not
    something the arithmetic assumes.

    BOTH NUMERICAL KNOBS SCALE WITH THE COLLECTOR'S OWN RADIUS, and that is
    required, not a tidy-up. In the `(u, v)` chart a CYLINDER is free: its chart is
    developable, the geodesic ODE's right-hand
    side is identically zero, and RK4 had nothing to truncate. The local chart
    is the tangent plane dropped back on, which is curved on a cylinder too --
    a circumferential arc `s` lands at `a = r tan(s / r)` -- so the two error
    terms that were dormant are now live and each has a length scale:

      `fd_step`   a central SECOND difference has truncation of order
                  `f'''' h^2` against round-off of order `eps |X| / h^2`, and
                  they balance at `h ~ eps^(1/4) * R`. MEASURED, against the
                  closed-form great circle on a sphere and against McColl Eq
                  6.1 on a cylinder: the optimum is
                  3e-4 mm on a 10 mm dome and 5e-5 mm on a 1.5 mm one, so the
                  rule is `FD_FRACTION * R`. The shipped 1e-3 is within the
                  flat part of that curve at 10 mm and an order too coarse at
                  1.5 mm, where it cost 7.6e-6 mm against the 2.3e-7 mm the
                  McColl comparator holds this module to.
                  THE FRACTION IS A TRADE and `probe_fd_fraction.py` is the
                  table it was picked from, because the two families of
                  comparator want opposite things. An ENDPOINT probe (McColl,
                  the great circle) measures accumulated position and prefers
                  a smaller `h`; a DIFFERENTIAL probe (Clairaut's swing,
                  geodesic curvature) central-differences the output polyline,
                  sees stencil jitter amplified by `1 / ds`, and prefers a
                  larger one. At 3e-5 the McColl residual is 4.5e-8 mm and the
                  dome's Clairaut swing stops converging; at 2e-4 the swing is
                  1.7e-9 mm and McColl is 1.0e-6. 6e-5 -- `eps^(1/4) / 2` --
                  is where both are comfortably inside their bars: McColl
                  1.3e-7 mm, dome swing 1.1e-8 mm on a 4.115 mm mean, torus
                  swing 3.2e-5 mm converging fourth-order, and the apex
                  geodesic 6.7e-9 mm from the closed-form great circle.
      `step`      RK4's own truncation goes as `(ds / R)^4`: measured 6.8e-5 mm
                  at `ds / R = 0.167` on the McColl cylinder and 4e-9 mm at
                  0.015 on a 10 mm dome, a clean fourth power between them. So
                  a step is SUBDIVIDED until `ds / R <= STEP_FRACTION`, which
                  at 0.03 is free on every 10 mm collector (the shipped
                  0.25 mm step is already inside it) and costs six substeps on
                  the 1.5 mm cylinder.

    `R` is the local radius of curvature and is READ OFF THE PATCH'S OWN
    STENCIL -- the second fundamental form `[[X_aa.N, X_ab.N], [X_ab.N,
    X_bb.N]]` in a chart that is orthonormal at its origin -- so it is per
    point and nothing here reads the frame's published span-midpoint
    curvatures. `fd_step` is settled ONCE, on the first patch a surface builds,
    because a finite-difference step that varied row by row would make a
    batched march disagree with a lone one; the substep count is per row and
    depends only on that row's own state, which is what the pole cap it
    replaces did.

    Pass `fd_step=` WITH `fd_scale=False` to pin it, which is what the
    diagnostics that measured the rule above do.
    """

    #: NOT a bound on anything this module does: the refusal radius, in
    #: finite-difference steps, that a march in the `(u, v)` chart would need.
    #: Nothing refuses a design on it; it is kept as the measured scale of that
    #: chart's pole trouble.
    POLE_GUARD_STEPS = 20.0

    #: `fd_step = FD_FRACTION * R`. Measured, see the class docstring.
    FD_FRACTION = 6.0e-5
    #: one RK4 step may span at most this much of a radian of the surface's own
    #: curvature before it is subdivided. Measured, see the class docstring.
    STEP_FRACTION = 0.03
    #: the same cap for a QUADRIC patch, which needs a finer one and not for a
    #: reason about the surface. A stencil patch re-differences the chart at
    #: every RK4 stage, so its step is fourth-order accurate; a quadric patch
    #: freezes the second form at the patch origin, which makes the step
    #: SECOND-order, and the fix is to re-centre the patch more often.
    #: MEASURED against a great circle on an imported R10 sphere, where the
    #: error falls by exactly 4.00x per halving:
    #:     0.03 -> 1.8739 um    0.0075 -> 0.1172 um    0.001875 -> 0.0096 um
    #:
    #: SET TO `STEP_FRACTION`, which is a decision about what the number is FOR.
    #: 0.001875 would make an imported collector's machine words match the
    #: analytic one's to the emitter's last printed place -- a digit-matching
    #: bar. It costs 16x the substeps, and since every substep on an imported
    #: collector is an OpenCascade search rather than a cosine, it is 6x the
    #: wall clock of the whole generator: on the shipped
    #: `test collector.step` at the 5-axis-default design, 69 s against 11 s.
    #:
    #: The bar it is set to now is PHYSICAL. Measured end to end on both
    #: shipped collectors, against the 0.001875 output line for line, 0.03
    #: moves the path by at most 1.5 um and the rotary words by 0.008 deg. The
    #: fibre is 22 um, so the whole deviation is a fifteenth of one fibre
    #: diameter, and it is the same cap every OTHER collector in the program
    #: has always taken. 0.0075 is the middle if a finer one is ever wanted:
    #: 0.1 um, at 24 s.
    #:
    #: A QUADRIC PATCH IS STILL THE FINER-STEPPED ONE and that has not changed
    #: -- it freezes the second form at the patch origin, so its step is second
    #: order where a stencil patch's is fourth. The two caps being equal is not
    #: a claim that the two patches are equally accurate; it is that the
    #: quadric's error at this cap is already far below anything the machine
    #: can express.
    QUADRIC_STEP_FRACTION = STEP_FRACTION
    #: a flat patch has no radius, and a patch whose stencil reads a curvature
    #: smaller than this is treated as flat: `fd_step` stays at the bootstrap
    #: and no step is subdivided. 1e-4 /mm is a 10 m radius.
    FLAT_CURVATURE = 1.0e-4

    def __init__(self, frame, height=0.0, fd_step=1.0e-3, fd_scale=True):
        self.frame = frame
        #: wall height the pattern is projected at -- the deposition surface,
        #: not the bare collector, once a wall has built up.
        self.height = float(height)
        #: the BOOTSTRAP difference step: what the first stencil is taken at,
        #: and what `fd_step` stays if `fd_scale` is off. `gui_surface`
        #: reads the signature default, so it must stay a plain float.
        self.fd_step = float(fd_step)
        self._fd_scale = bool(fd_scale)
        self._tuned = not self._fd_scale
        #: see `POLE_GUARD_STEPS`: read by the page, not by the march.
        self.pole_guard = self.POLE_GUARD_STEPS * float(fd_step)

    # -- query 1: the point ------------------------------------------------- #
    def point(self, uv):
        """World XYZ of the surface point at chart coordinates `uv`."""
        uv = np.asarray(uv, dtype=np.float64)
        return self._points(uv[..., 0], uv[..., 1])

    def _points(self, u, v):
        try:
            x, y, z = self.frame.to_world(u, v, self.height)
        except mew_frame.AxisSingularity as exc:
            raise ChartSingularity(_APEX_MSG % (exc,))
        return _stack3(x, y, z)

    # -- query 2: the normal, in world currency ----------------------------- #
    def normal(self, uv):
        """Outward unit normal at chart `uv`, world XYZ."""
        uv = np.asarray(uv, dtype=np.float64)
        return self._normal_uv(uv[..., 0], uv[..., 1])

    def normal_at(self, world):
        """Outward unit normal at a WORLD point on (or near) the surface."""
        u, v = self._chart(np.asarray(world, dtype=np.float64))
        return self._normal_uv(u, v)

    def _normal_uv(self, u, v):
        try:
            nx, ny, nz = self.frame.normal(u, v, self.height)
        except mew_frame.AxisSingularity as exc:
            raise ChartSingularity(_APEX_MSG % (exc,))
        n = _stack3(nx, ny, nz)
        return n / np.linalg.norm(n, axis=-1, keepdims=True)

    # -- the query the local chart is built out of -------------------------- #
    def settle(self, world):
        """The point of the DEPOSITION surface nearest `world`. `(..., 3)`.

        The fourth query, and the one a mesh or a CAD kernel answers most
        naturally of all four. See the class docstring for why it is smooth
        across a pole.

        A FRAME THAT PUBLISHES `settle_world` IS ASKED DIRECTLY, for the same
        reason `patch` asks `local_patch`: the route through the chart is two
        expensive queries where the surface can answer in one. Spelled out on
        `mew_cad.CadFrame`, `from_world` is a nearest-point search that finds
        the surface point and then throws it away to return the tangent-plane
        coordinates of it, and `_points` casts a ray back down the chart to
        find that same point again. The round trip is an identity -- the chart
        ray through `(u, v)` passes through the very point the search
        returned, by construction -- and it cost a
        `BRepExtrema_DistShapeShape` plus a `BRepIntCurveSurface_Inter` per
        settled point. MEASURED on the parity design: 86,099 searches and
        58,745 of the 59,167 ray casts in the whole run came through here.

        A frame without the hook -- the plane, the cylinder, every
        `RevolutionFrame` -- takes the chart route unchanged, so every
        collector that worked before this is bit-identical after it.
        """
        w = np.asarray(world, dtype=np.float64)
        hook = getattr(self.frame, "settle_world", None)
        if hook is not None:
            try:
                return hook(w.reshape(-1, 3), self.height).reshape(w.shape)
            except mew_frame.AxisSingularity as exc:
                raise ChartSingularity(_APEX_MSG % (exc,))
        u, v = self._chart(w)
        return self._points(u, v)

    def chart(self, world):
        """`(u, v)` of a world point, as `(..., 2)`.

        An OUTPUT of the projection, for the consumers that still speak the
        chart -- the sag solver, `mew_deposition`, and the designer page's span
        readout. A mesh adapter raises here and those consumers are what slices
        2, 3 and 5 of this task move off it.
        """
        u, v = self._chart(np.asarray(world, dtype=np.float64))
        return _stack2(u, v)

    def _chart(self, w):
        try:
            u, v, _h = self.frame.from_world(w[..., 0], w[..., 1], w[..., 2])
        except mew_frame.AxisSingularity as exc:
            raise ChartSingularity(_APEX_MSG % (exc,))
        return u, v

    # -- tangent-space bookkeeping the projector needs ---------------------- #
    def tangential(self, world, direction):
        """`direction` with its normal component removed, normalised.

        Batched: `world` and `direction` are `(..., 3)`. The component out of
        the tangent plane is dropped, so a caller may hand in an approximate
        heading -- which is what a stored placement does.
        """
        n = self.normal_at(world)
        d = np.asarray(direction, dtype=np.float64)
        return _unit_rows(d - n * _dot(d, n)[..., np.newaxis])

    def along_default(self, world):
        """The default `along`: the MERIDIAN, read without the chart.

        At a world point the meridian direction is fixed by the axis of
        revolution and the normal alone: the parallel runs along
        `e_phi = (0, z, -y) / rho`, and the meridian is `e_phi x N`. Both are
        world quantities, so this is the same direction the old
        `meridian_direction` read off `X_u` -- measured equal on a dome, a
        bowl, a torus and a cone -- with no `(u, v)` in it.

        AT A POLE `e_phi` HAS NO VALUE AND ONE IS CHOSEN: `(0, 1, 0)`, the
        parallel direction at world azimuth zero. It is A CHOICE, and the
        reason no rule can do better is worth stating, because it is the one
        place in this module where the apex is not simply an ordinary point.
        A meridian through a pole is one great circle, and the two halves of it
        leave the pole in OPPOSITE world directions -- so "the meridian
        direction, pointing away from the pole" has two answers at the pole and
        neither is the limit of the other. Approaching along world azimuth 0
        gives this one; approaching along azimuth pi gives its negative.
        MEASURED: on `surface_frame`'s own dome, whose profile radius is
        negative over its whole span, the chart's `v = 0` meridian sits at
        world azimuth pi, so this rule and `meridian_direction` AGREE at the
        pole and differ by a sign everywhere else. Off the pole the sign this rule and
        `meridian_direction` agree on is set by the profile's own sign of rho,
        positive on a bowl, a cone and a torus and negative on this dome
        (`mew_frame.py:1683-1692`); AT the pole this rule's branch answers a
        FIXED world direction with no chart in it, `(0, 1, 0) x N`, and on this
        dome that happens to be the same half of the meridian the chart rule's
        `v = 0` limit approaches from.

        Which is why `project_pattern` uses `meridian_direction` when it was
        handed chart coordinates and this when it was handed a world point --
        a caller who typed `Centered at = 0` in the designer gets the meridian
        its own `v = 0` names, and a caller with a stored world
        placement gets a rule that needs no chart. Pass `along` explicitly and
        neither is consulted.
        """
        w = np.asarray(world, dtype=np.float64)
        n = self.normal_at(w)
        rho = np.hypot(w[..., 1], w[..., 2])
        pole = rho == 0.0
        safe = np.where(pole, 1.0, rho)
        e_phi = np.stack((np.zeros_like(rho),
                          np.where(pole, 1.0, w[..., 2] / safe),
                          np.where(pole, 0.0, -w[..., 1] / safe)), axis=-1)
        return _unit_rows(np.cross(e_phi, n))

    def meridian_direction(self, uv):
        """The world tangent along increasing `u` at chart `uv`.

        A central difference of the point query along `u`, which is what the
        chart march's `X_u` was. It is the CHART's meridian, so at a pole it
        answers for the side of the pole `u` is measured from rather than
        having to choose one -- see `along_default` for why that distinction
        exists at all.
        """
        uv = np.asarray(uv, dtype=np.float64)
        e = np.zeros_like(uv)
        e[..., 0] = self.fd_step
        return _unit_rows(self.point(uv + e) - self.point(uv - e))

    # -- query 3: the geodesic step, in world currency ---------------------- #
    def patch(self, world, tangent):
        """The LOCAL chart at `world` whose `+a` axis is `tangent`.

        The first one a surface builds also SETTLES `fd_step`, by reading the
        radius of curvature off its own stencil -- see the class docstring for
        why that is measured once per surface rather than per row.

        A FRAME THAT PUBLISHES `local_patch` IS ASKED INSTEAD OF DIFFERENCED.
        The hook hands back the point, the basis and the surface's second
        fundamental form there, all exactly, and the patch is then a quadric
        that needs no surface query of its own -- see `_LocalPatch.
        _derivatives` for what that is and why it is the same quantities. It
        exists for the one surface where a query is expensive enough to matter:
        `mew_cad.CadFrame`, where every query is an OpenCascade search. A frame
        without the hook -- the plane, the cylinder, every `RevolutionFrame` --
        takes the stencil exactly as before, and `fd_step` is settled for it
        exactly as before.
        """
        w = np.asarray(world, dtype=np.float64)
        t = np.asarray(tangent, dtype=np.float64)
        hook = getattr(self.frame, "local_patch", None)
        if hook is not None:
            shape = w.shape
            try:
                p, e1, e2, L, M, P = hook(w.reshape(-1, 3), t.reshape(-1, 3),
                                          self.height)
            except mew_frame.AxisSingularity as exc:
                raise ChartSingularity(_APEX_MSG % (exc,))
            rows = shape[:-1]
            # `fd_step` is the stencil's step and the quadric takes none, so
            # nothing is tuned here -- but the flag is set so a later stencil
            # patch on this surface cannot fire the tuning either, which would
            # cost nine queries for a number no patch on it will read.
            self._tuned = True
            return _LocalPatch(self, p.reshape(shape), e1.reshape(shape),
                               e2.reshape(shape),
                               form=(L.reshape(rows), M.reshape(rows),
                                     P.reshape(rows)))
        n = self.normal_at(w)
        e1 = _unit_rows(t - n * _dot(t, n)[..., np.newaxis])
        e2 = np.cross(n, e1)
        if not self._tuned:
            self._tuned = True
            k = float(np.max(_LocalPatch(self, w, e1, e2).curvature()))
            if k > self.FLAT_CURVATURE:
                self.fd_step = self.FD_FRACTION / k
        return _LocalPatch(self, w, e1, e2)

    def geodesic_step(self, world, tangent, ds):
        """One RK4 step of arc length `ds` along the geodesic through `world`.

        `(P, T)` world in, `(P, T)` world out, batched over a leading axis.
        `T` is a unit world tangent, so the marching parameter is arc length ON
        THE SURFACE and a caller asking for a spacing in millimetres gets one.

        SUBDIVIDED WHERE THE STEP IS COARSE against the surface's own radius of
        curvature, per row, re-centring the chart at every substep. The count
        depends on that row's own curvature and its own `ds` and on nothing
        else in the batch, which is what keeps a geodesic marched in company
        bit-identical to the same one marched alone.
        """
        ds = np.asarray(ds, dtype=np.float64)
        patch = self.patch(world, tangent)
        frac = (self.QUADRIC_STEP_FRACTION if patch.form is not None
                else self.STEP_FRACTION)
        m = np.maximum(1.0, np.ceil(np.abs(ds) * patch.curvature()
                                    / frac)).astype(np.intp)
        if np.all(m == 1):
            return patch.step(ds)
        P = np.array(world, dtype=np.float64, copy=True)
        T = np.array(tangent, dtype=np.float64, copy=True)
        sub = ds / m
        todo = np.arange(len(m), dtype=np.intp)
        j = 0
        while todo.size:
            P[todo], T[todo] = patch.step(sub[todo])
            j += 1
            todo = todo[m[todo] > j]
            if todo.size:
                patch = self.patch(P[todo], T[todo])
        return P, T


#: The one refusal the pole's departure does NOT take with it. A cone apex has
#: no tangent plane, so there is no normal to stand the nozzle on and no local
#: chart to integrate in -- a geometric fact about the collector, not a
#: property of any coordinates. `mew_frame` raises `AxisSingularity`; this
#: module re-raises it with the projection's context so a page can say which of
#: the two happened.
_APEX_MSG = ("the projection reached a CONE APEX, where the collector has no "
             "tangent plane and therefore no normal to stand the nozzle on: "
             "%s. Model the collector as the truncated cone it physically is, "
             "or move the pattern origin off the apex.")


def _stack3(x, y, z):
    """`(x, y, z)` component arrays as one `(..., 3)`.

    `broadcast_arrays` is what lets a frame answer one component as a scalar
    (a plane's, say), and it is not free -- it was 5% of the readout. When the
    three already agree, which every frame with a curved meridian gives, it is
    a no-op and is skipped.
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    z = np.asarray(z, dtype=np.float64)
    if not (x.shape == y.shape == z.shape):
        x, y, z = np.broadcast_arrays(x, y, z)
    return np.stack((x, y, z), axis=-1)


def _stack2(u, v):
    u = np.asarray(u, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)
    if u.shape != v.shape:
        u, v = np.broadcast_arrays(u, v)
    return np.stack((u, v), axis=-1)


class _LocalPatch(object):
    """The tangent plane at one point, dropped back onto the collector.

        Psi(a, b) = surface.settle(P0 + a*e1 + b*e2)

    `P0`, `e1`, `e2` are `(N, 3)`: a BATCH of N independent patches, one per
    geodesic in flight, so `march_many` takes one batched RK4 step per round
    rather than one per geodesic per step. Every operation is elementwise or a
    last-axis reduction, so a row of a batch is bit-identical to the same
    geodesic marched alone -- which is what lets `march` be `march_many` with
    one row and not a second implementation.

    A patch lives for exactly ONE step. It is built at the step's start, the
    step is taken in it, and the world point and tangent that come out are what
    survive; the next step builds a fresh one. That is what "integrate in the
    local frame at the current point" means in code, and it is why nothing here
    has to be valid further than one step from its origin.

    THE STEP STARTS AT THE ORIGIN WITH VELOCITY `(1, 0)`, by construction of
    `e1`. The derivatives there are computed once and reused for both the
    velocity normalisation and the RK4's first stage, so a step costs the same
    five stencils the chart version cost.
    """

    def __init__(self, surface, origin, e1, e2, form=None):
        self.surface = surface
        self.fd_step = surface.fd_step
        self.origin = origin
        self.e1 = e1
        self.e2 = e2
        #: `(L, M, P)` of the surface's second fundamental form in `(e1, e2)`,
        #: or None. See `_derivatives` -- with it, the patch IS a quadric and
        #: no surface query is taken at all.
        self.form = form
        self._zero = np.zeros(origin.shape[:-1] + (2,))
        #: the stencil at the origin, computed once and shared by the curvature
        #: read, the velocity normalisation and the RK4's first stage -- so a
        #: step still costs the five stencils the chart version cost.
        self.d0 = self._derivatives(self._zero)

    def curvature(self):
        """The larger principal curvature at the origin, per mm, per row.

        The chart is orthonormal at its own origin (it is the tangent plane
        dropped onto the surface), so the first fundamental form is the
        identity there and the principal curvatures are the eigenvalues of the
        SECOND form alone, `[[L, M], [M, P]]` with `L = X_aa . N` and so on.
        The larger magnitude is `|H| + sqrt(H^2 - K)` with `H = (L + P) / 2`
        and `K = L*P - M*M` -- the usual closed form, no eigen-solver.

        Read off the stencil that is already there, so it costs nothing, and
        measured at the point rather than taken from the frame's published
        span-midpoint pair (section 5 rule 1).
        """
        _Xa, _Xb, Xaa, Xab, Xbb = self.d0
        n = np.cross(self.e1, self.e2)
        L, M, P = _dot(Xaa, n), _dot(Xab, n), _dot(Xbb, n)
        h = 0.5 * (L + P)
        return np.abs(h) + np.sqrt(np.maximum(0.0, h * h - (L * P - M * M)))

    # -- the chart ---------------------------------------------------------- #
    def points(self, a, b):
        """`Psi` at local `(a, b)`, shapes `(N, K) -> (N, K, 3)`."""
        q = (self.origin[..., np.newaxis, :]
             + a[..., np.newaxis] * self.e1[..., np.newaxis, :]
             + b[..., np.newaxis] * self.e2[..., np.newaxis, :])
        return self.surface.settle(q)

    def point_at(self, ab):
        """`Psi` at one `(a, b)` per row, `(N, 2) -> (N, 3)`.

        SETTLED ONTO THE REAL SURFACE IN EITHER MODE. In the quadric mode the
        step is integrated on the surface's own second-order expansion, which
        is accurate to third order in the step but is not the surface; the
        point that leaves this patch and becomes the next one's origin is the
        true surface point, so nothing accumulates across a march.
        """
        return self.points(ab[..., 0, np.newaxis],
                           ab[..., 1, np.newaxis])[..., 0, :]

    def _derivatives(self, ab):
        """`(X_a, X_b, X_aa, X_ab, X_bb)` at `ab`. Two modes, one meaning.

        THE QUADRIC MODE, when the surface handed over its second fundamental
        form `(L, M, P)` in this patch's own basis. Then the patch IS

            Psi(a, b) = P0 + a*e1 + b*e2 + 0.5*(L a^2 + 2M ab + P b^2)*N

        -- the surface's second-order Taylor expansion over its tangent plane
        -- and every derivative below is that expression differentiated, with
        no surface query taken at all.

        THE STENCIL MODE otherwise, by one vectorised nine-point difference of
        `Psi(a, b) = settle(P0 + a*e1 + b*e2)`. Its sharing of shifted columns
        and its arithmetic are the chart version's verbatim; only where the
        nine points come from had changed. `probe_reduction_identity.py` is
        still the proof for the two reductions (`_dot` and `norm`), which is
        not free -- an `einsum` spelling of `_dot` differs from `ndarray.dot`
        on 30% of random 3-vectors.

        WHY BOTH, AND WHY THE STENCIL IS STILL THE DEFAULT. The two are the
        same quantities: a nine-point difference of a surface recovers its
        first and second derivatives, which is what a fundamental form is, and
        measured against each other on an imported dome and an imported block
        they agree to 9.5e-8 /mm -- the stencil's own difference error. So the
        quadric is not an approximation introduced here; it is the exact
        version of what the stencil estimates. It is taken only where a
        surface can answer exactly, because there the stencil costs 45 surface
        queries per RK4 step and on a CAD collector each of those is an
        OpenCascade search rather than a cosine. `RevolutionFrame` and the
        plane do not publish the capability and take the stencil unchanged, so
        every collector that worked before this is bit-identical after it.

        WHAT THE QUADRIC DOES NOT CARRY is the surface's THIRD derivatives:
        the stencil re-differences at each RK4 stage and so sees the second
        form vary across a step, while the quadric freezes it at the patch
        origin. `geodesic_step` already caps a step at `STEP_FRACTION` of a
        radian of the local curvature, which bounds that variation, and what
        it costs is measured end to end rather than argued.
        """
        if self.form is not None:
            L, M, P = self.form
            a = ab[..., 0]
            b = ab[..., 1]
            n = np.cross(self.e1, self.e2)
            # Psi_a = e1 + (L a + M b) N ;  Psi_b = e2 + (M a + P b) N
            fa = (L * a + M * b)[..., np.newaxis]
            fb = (M * a + P * b)[..., np.newaxis]
            return (self.e1 + fa * n,
                    self.e2 + fb * n,
                    L[..., np.newaxis] * n,
                    M[..., np.newaxis] * n,
                    P[..., np.newaxis] * n)
        a = ab[..., 0, np.newaxis]
        b = ab[..., 1, np.newaxis]
        h = self.fd_step
        # the four shifted columns once each, not nine times: `a + h` appears
        # three times in the stencil and `a - h` three more.
        ap_, am_, bp_, bm_ = a + h, a - h, b + h, b - h
        As = np.concatenate((a, ap_, am_, a, a, ap_, ap_, am_, am_), axis=-1)
        Bs = np.concatenate((b, b, b, bp_, bm_, bp_, bm_, bp_, bm_), axis=-1)
        P = self.points(As, Bs)
        c, ap, am, bp, bm, apbp, apbm, ambp, ambm = np.moveaxis(P, -2, 0)
        h2 = h * h
        return (0.5 * (ap - am) / h,
                0.5 * (bp - bm) / h,
                (ap - 2.0 * c + am) / h2,
                0.25 * (apbp - apbm - ambp + ambm) / h2,
                (bp - 2.0 * c + bm) / h2)

    # -- the geodesic ODE --------------------------------------------------- #
    def _accel(self, d, ab, vel):
        """Local acceleration `(a'', b'')` from precomputed derivatives `d`."""
        Xu, Xv, Xuu, Xuv, Xvv = d
        a, b = vel[..., 0, np.newaxis], vel[..., 1, np.newaxis]
        acc = Xuu * (a * a) + Xuv * (2.0 * a * b) + Xvv * (b * b)
        E, F, G = _dot(Xu, Xu), _dot(Xu, Xv), _dot(Xv, Xv)
        det = E * G - F * F
        if not np.all(det > 0.0):
            self._refuse_degenerate(ab, det)
        r0, r1 = -_dot(acc, Xu), -_dot(acc, Xv)
        return np.stack([(G * r0 - F * r1) / det, (E * r1 - F * r0) / det],
                        axis=-1)

    def accel(self, ab, vel):
        return self._accel(self._derivatives(ab), ab, vel)

    def _refuse_degenerate(self, ab, det):
        i = int(np.argmin(np.where(det > 0.0, np.inf, det)))
        bad = np.reshape(np.asarray(self.origin, dtype=np.float64),
                         (-1, 3))[min(i, self.origin.reshape(-1, 3).shape[0]
                                      - 1)]
        raise ChartSingularity(
            "the LOCAL chart degenerated near world (%r, %r, %r): the first "
            "fundamental form has determinant %r. The tangent plane there does "
            "not drop onto the collector one-to-one, which means the step "
            "reached further than the surface's own radius of curvature."
            % (float(bad[0]), float(bad[1]), float(bad[2]),
               float(np.ravel(det)[i])))

    def _unit(self, d, vel):
        """`vel` rescaled so the WORLD speed is exactly 1."""
        Xu, Xv = d[0], d[1]
        w = Xu * vel[..., 0, np.newaxis] + Xv * vel[..., 1, np.newaxis]
        n = np.linalg.norm(w, axis=-1)
        if not np.all(n > 0.0):
            raise ChartSingularity(
                "a zero-length direction in the local chart at world %r"
                % (np.reshape(self.origin, (-1, 3))[0],))
        return vel / n[..., np.newaxis]

    def world_direction(self, d, vel):
        """The local velocity `vel` as a world unit tangent."""
        w = (d[0] * vel[..., 0, np.newaxis]
             + d[1] * vel[..., 1, np.newaxis])
        return w / np.linalg.norm(w, axis=-1, keepdims=True)

    def step(self, ds):
        """One RK4 step of arc length `ds` from the patch's own origin.

        Returns `(P, T)` in world millimetres. `ds` is `(N,)`.
        """
        ds = ds[..., np.newaxis]
        ab = self._zero
        d0 = self.d0
        vel = self._unit(d0, np.tile(np.array([1.0, 0.0]), ab.shape[:-1] + (1,)))
        k1p, k1v = vel, self._accel(d0, ab, vel)
        k2p, k2v = self._rk(ab, vel, k1p, k1v, 0.5 * ds)
        k3p, k3v = self._rk(ab, vel, k2p, k2v, 0.5 * ds)
        k4p, k4v = self._rk(ab, vel, k3p, k3v, ds)
        ab_out = ab + (ds / 6.0) * (k1p + 2.0 * k2p + 2.0 * k3p + k4p)
        vel_out = vel + (ds / 6.0) * (k1v + 2.0 * k2v + 2.0 * k3v + k4v)
        d1 = self._derivatives(ab_out)
        vel_out = self._unit(d1, vel_out)
        return self.point_at(ab_out), self.world_direction(d1, vel_out)

    def _rk(self, ab, vel, dp, dv, dt):
        p = ab + dt * dp
        v = vel + dt * dv
        return v, self.accel(p, v)


# --------------------------------------------------------------------------- #
# Marching
# --------------------------------------------------------------------------- #
#: Steps one march may take before it is treated as stuck. With the pole cap
#: gone every step is `min(step, distance to the next stop)`, so a march that
#: is making progress cannot reach this; it catches a step that has stopped
#: advancing, which is a bug rather than a geometry.
MARCH_STEP_BUDGET = 200000


def march(surface, start, direction, stops, step=0.25):
    """Walk the geodesic from `start` in world direction `direction`.

    `start` is a world point on the surface, or chart `(u, v)` -- a length-2
    argument is read as the latter, which is what `project_pattern`'s callers
    still hand in. `stops` is a sorted sequence of NON-NEGATIVE arc lengths;
    the state is returned at each one, so a whole pass costs one march rather
    than one march per sample. `step` is the integrator's maximum step in mm
    and is cut short to land exactly on each stop -- a stop is never
    interpolated, because the spacing between passes is the number this module
    exists to get right.

    Returns `(world[N, 3], tangent[N, 3])`. It is `march_many` with one row, so
    the two cannot disagree by construction.
    """
    (w, t), = march_many(surface, [_as_world(surface, start)], [direction],
                         [stops], step=step)
    return w, t


def _as_world(surface, start):
    """A world point from either a world point or a chart `(u, v)`."""
    p = np.asarray(start, dtype=np.float64)
    return surface.point(p) if p.shape[-1] == 2 else p


def march_many(surface, starts, directions, stops, step=0.25):
    """`march`, for N independent geodesics walked in LOCKSTEP. Same answers.

    Returns `[(world[Ni, 3], tangent[Ni, 3]), ...]`, one pair per geodesic.
    Instead of N marches of a handful of RK4 steps each, this takes one batched
    RK4 step per ROUND and stops when the last geodesic has finished, so the
    surface is queried once per round rather than once per step per geodesic.

    THAT IS THE WHOLE POINT, and it is worth naming what it bought, because a
    projection is not obviously a batched problem. `project_pattern` pays one
    transverse walk per DISTINCT `q`, and with the turnarounds on every arc
    vertex has a `q` of its own -- 113 of them where the nine passes alone
    would have nine. Each of those walks is a different geodesic, so there is
    nothing to cache and nothing to skip; what there is, is 113 walks of ~9
    steps that can be taken together. The per-step cost is almost entirely
    Python and numpy call overhead on 9-point stencils, so batching them turns
    ~1960 sequential steps into ~20 rounds.

    THE STEP SCHEDULE IS PER-GEODESIC, not shared. Each carries its own arc
    length and its own next stop, and each round advances only the geodesics
    still short of theirs -- a finished one is dropped from the batch rather
    than stepped by zero, because the step renormalises and a zero-length step
    would still move the last bit of the tangent.
    """
    n = len(stops)
    stops = [np.asarray(s, dtype=np.float64) for s in stops]
    for s in stops:
        if s.size and s[0] < 0.0:
            raise ValueError("march takes non-negative arc lengths; got %r"
                             % (float(s[0]),))
    if not step > 0.0:
        raise ValueError("step must be positive, got %r" % (step,))
    out = [(np.empty((len(s), 3)), np.empty((len(s), 3))) for s in stops]
    if not n:
        return out
    pool = _pool_for(surface, sum(len(s) for s in stops))
    if pool is not None and n > 1:
        return _march_sharded(pool, surface, starts, directions, stops, step)

    P = np.asarray(starts, dtype=np.float64).reshape(n, 3).copy()
    T = surface.tangential(P, np.asarray(directions,
                                         dtype=np.float64).reshape(n, 3))
    walked = np.zeros(n)
    at = np.zeros(n, dtype=np.intp)             # the next stop to record
    budget = np.full(n, MARCH_STEP_BUDGET, dtype=np.int64)
    count = np.array([len(s) for s in stops], dtype=np.intp)

    while True:
        # record every geodesic that has reached its next stop -- possibly
        # several in a row, when consecutive stops fall inside one step's
        # tolerance.
        live = []
        for i in range(n):
            while at[i] < count[i] and walked[i] >= stops[i][at[i]] - 1e-15:
                out[i][0][at[i]] = P[i]
                out[i][1][at[i]] = T[i]
                at[i] += 1
            if at[i] < count[i]:
                live.append(i)
        if not live:
            return out
        sel = np.asarray(live, dtype=np.intp)
        target = np.array([stops[i][at[i]] for i in live])
        ds = np.minimum(step, target - walked[sel])
        P[sel], T[sel] = surface.geodesic_step(P[sel], T[sel], ds)
        walked[sel] += ds
        budget[sel] -= 1
        if not np.all(budget[sel] >= 0):
            i = int(sel[int(np.argmin(budget[sel]))])
            raise ChartSingularity(
                "a geodesic took %d steps without reaching its next stop "
                "(%.6g mm, reached %.6g mm) at world %r -- the march has "
                "stopped advancing."
                % (MARCH_STEP_BUDGET, float(stops[i][at[i]]),
                   float(walked[i]), P[i]))


def geodesic(surface, start, direction, length, step=0.25, samples=None):
    """A single geodesic of `length` mm, sampled `samples` times (or per `step`).

    The convenience wrapper the tests and the distortion measurements use;
    `march` is the primitive. Returns `(world, tangent)`.
    """
    n = int(samples) if samples else max(2, int(math.ceil(length / step)) + 1)
    return march(surface, start, direction, np.linspace(0.0, length, n),
                 step=step)


# --------------------------------------------------------------------------- #
# The projection itself
# --------------------------------------------------------------------------- #
class ProjectedPath(object):
    """A flat pattern, on the surface.

    `world` and `normal` are the PRIMARY content and are what the machine model
    turns into `X Y Z` plus two rotary words, since the nozzle is held along
    the normal. `tangent` is the path's own world direction at each sample --
    the third leg of the local frame, with `normal x tangent` completing it --
    and it is what a consumer needs instead of a chart velocity.

    `uv` is the chart coordinates, kept because the sag solver,
    `mew_deposition` and the designer page's span readout still speak them;
    slices 2, 3 and 5 of the arbitrary-collector task are what move those
    consumers. It is DERIVED from `world`, not the other way round, so on a
    collector with no chart it is simply absent.

    `pattern` is the flat `(p, q)` each point came from, kept so a caller can
    tell which pass a point belongs to without re-deriving it.
    """

    def __init__(self, world, normal, pattern, uv=None, tangent=None):
        self.world = world
        self.normal = normal
        self.pattern = pattern
        self.uv = uv
        self.tangent = tangent

    def __len__(self):
        return len(self.world)

    def local_frame(self):
        """`(tangent, binormal, normal)` -- the local frame at every sample."""
        return (self.tangent, np.cross(self.normal, self.tangent), self.normal)

    def arc_length(self):
        """Cumulative world chord length along the path, mm."""
        d = np.linalg.norm(np.diff(self.world, axis=0), axis=1)
        return np.concatenate(([0.0], np.cumsum(d)))


def project_pattern(surface, pattern, origin_uv, along=None, angle_deg=0.0,
                    step=0.25):
    """Map a flat `(p, q)` pattern onto the surface by Fermi coordinates.

    `origin_uv` is where the pattern's `(0, 0)` lands -- a world point, or
    chart `(u, v)` as the designer page still passes. `along` is the world
    tangent direction the pattern's `+p` axis points along; `angle_deg` rotates
    the pattern about the origin's normal, which is how a per-layer rotation is
    applied without moving the origin.

    `along` DEFAULTS TO THE MERIDIAN AND AT AN APEX THAT IS A CHOICE, not a
    derivation -- every azimuth names the same point at a pole. Pass it
    explicitly and nothing is assumed; leave it None and `along_default` says
    in as many words which meridian you get and why. Slice 5's stored heading
    is what will normally supply it.

    The rail is marched ONCE for all the `q` values present, and each `q`'s ribs
    once for all of that pass's `p` values, so the cost is the total arc length
    walked rather than the point count.
    """
    return project_patterns(surface, [(pattern, origin_uv, along, angle_deg)],
                            step=step)[0]


def project_patterns(surface, jobs, step=0.25):
    """`project_pattern` for several patterns on ONE surface, marched together.

    `jobs` is `[(pattern, origin_uv, along, angle_deg)]`; one `ProjectedPath`
    comes back per job. Every job's half-rails go out in ONE `march_many`, then
    every job's ribs in one more, then every point is charted in one query --
    the lockstep `march_many` already applies to the geodesics of one pattern,
    applied across patterns. A five-axis stack projects each distinct layer
    pattern (three of them on a 90-degree stack with linking loops), and one
    after another they were three sets of marches where one will do; under
    `parallel` that is what lets the rails of all three run at once.

    SAME ANSWERS AS ONE AT A TIME, because every row of every march is
    independent of its company -- the property `march_many` is built on.
    """
    setups = [_projection_setup(surface, *job) for job in jobs]

    rows = []                       # (job, sign, stops) per half-rail
    for k, (_pattern, _p0, _b0, qs) in enumerate(setups):
        for sign in (1.0, -1.0):
            want = np.unique(np.abs(qs[np.sign(qs) == sign]))
            if want.size:
                rows.append((k, sign, want))
    marched = march_many(surface, [setups[k][1] for k, _s, _w in rows],
                         [s * setups[k][2] for k, s, _w in rows],
                         [w for _k, _s, w in rows], step=step)
    rails = [{} for _ in setups]
    for (k, sign, want), (rp, rt) in zip(rows, marched):
        for i, q in enumerate(want):
            rails[k][sign * q] = (rp[i], sign * rt[i])
    for k, (_pattern, p0, b0, qs) in enumerate(setups):
        if np.any(qs == 0.0):
            rails[k][0.0] = (p0, b0)

    worlds, tangents, ribs = [], [], []
    for k, (pattern, _p0, _b0, _qs) in enumerate(setups):
        worlds.append(np.empty((len(pattern), 3)))
        tangents.append(np.empty((len(pattern), 3)))
        ribs += [(k,) + j for j in _rib_jobs(surface, rails[k], pattern,
                                             worlds[k], tangents[k])]
    marched = march_many(surface, [j[1] for j in ribs], [j[2] for j in ribs],
                         [j[3] for j in ribs], step)
    for (k, _p0, _dir, _want, sel, inv), (pw, pt) in zip(ribs, marched):
        worlds[k][sel] = pw[inv]
        tangents[k][sel] = pt[inv]

    # `normal_at(world)` IS `normal(chart(world))` -- it charts the points and
    # asks the normal there -- so the chart is taken once and handed to both
    # rather than searched for twice.
    cuts = np.cumsum([len(w) for w in worlds])[:-1]
    uv, normal = _chart_and_normal(surface, np.concatenate(worlds))
    return [ProjectedPath(w, n, s[0], uv=u, tangent=t)
            for w, n, u, t, s in zip(worlds, np.split(normal, cuts),
                                     np.split(uv, cuts), tangents, setups)]


def _projection_setup(surface, pattern, origin_uv, along=None, angle_deg=0.0):
    """`(pattern, origin world, across direction b0, distinct q)` for one job."""
    pattern = np.asarray(pattern, dtype=np.float64)
    if pattern.ndim != 2 or pattern.shape[1] != 2:
        raise ValueError("pattern must be (N, 2) in (p, q) mm, got %r"
                         % (pattern.shape,))
    origin = np.asarray(origin_uv, dtype=np.float64)
    charted = origin.shape[-1] == 2
    p0 = surface.point(origin) if charted else origin

    n0 = surface.normal_at(p0)
    if along is not None:
        a0 = _tangential(np.asarray(along, dtype=np.float64), n0)
    elif charted:
        # the meridian the caller's OWN `v = 0` names, which at a pole is not
        # the same as the one a world point alone can name -- see
        # `FrameSurface.along_default`.
        a0 = surface.meridian_direction(origin)
    else:
        a0 = surface.along_default(p0)
    if angle_deg:
        a0 = _rotate_about(a0, n0, math.radians(angle_deg))
    b0 = np.cross(n0, a0)
    return pattern, p0, b0, np.unique(pattern[:, 1])


def _rib_jobs(surface, rail, pattern, world, tangent):
    """Every transverse walk the projection needs, gathered into one flat list.

    `[(start world, world direction, stops, rows, inverse)]`, collected so
    `march_many` can take them together. The rows sitting exactly ON the rail
    need no walk and are written straight into `world` here.

    The rail is a geodesic, so its tangent is parallel-transported along it;
    `T x N` therefore transports the pattern's along axis exactly, with no
    transport integrator to accumulate error.
    """
    jobs = []
    items = list(rail.items())
    # the rail points' normals in ONE query, for the same reason `_march_rail`
    # batches its tangents: a `q` per arc vertex makes this a ~113-call loop.
    rps = np.asarray([rp for _q, (rp, _t) in items], dtype=np.float64)
    rts = np.asarray([t for _q, (_r, t) in items], dtype=np.float64)
    a_qs = np.cross(rts, _chart_and_normal(surface, rps)[1])
    for (q, (rp, _t_rail)), a_q, rt in zip(items, a_qs, rts):
        rows = np.flatnonzero(pattern[:, 1] == q)
        ps = pattern[rows, 0]
        for sign in (1.0, -1.0):
            sel = rows[np.sign(ps) == sign]
            if not sel.size:
                continue
            want, inv = np.unique(np.abs(pattern[sel, 0]),
                                  return_inverse=True)
            jobs.append((rp, sign * a_q, want, sel, inv))
        zero = rows[ps == 0.0]
        if zero.size:
            world[zero] = rp
            tangent[zero] = a_q
    return jobs



def _march_rail(surface, origin, b0, qs, step):
    """`{q: (world, world tangent of +q)}` for every `q` the pattern uses.

    The two half-rails go out together through `march_many` rather than one
    after the other. That is not a micro-optimisation: a stop is never
    interpolated, so the rail takes at least one integrator step PER `q`, and
    with the turnarounds on there are ~113 of those where the passes alone
    would give nine. Once the ribs were batched this loop was 70% of the
    readout's cost.
    """
    out = {}
    signs, wants = [], []
    for sign in (1.0, -1.0):
        want = np.unique(np.abs(qs[np.sign(qs) == sign]))
        if want.size:
            signs.append(sign)
            wants.append(want)
    if signs:
        rails = march_many(surface, [origin] * len(signs),
                           [s * b0 for s in signs], wants, step=step)
        for sign, want, (rp, rt) in zip(signs, wants, rails):
            for i, q in enumerate(want):
                out[sign * q] = (rp[i], sign * rt[i])
    if np.any(qs == 0.0):
        out[0.0] = (origin, b0)
    return out


# --------------------------------------------------------------------------- #
# Worker processes
# --------------------------------------------------------------------------- #
#: Below this many samples in one batched query the work stays in-process: a
#: small design (every test, the designer's readout) finishes before a pool of
#: processes would have finished importing.
MIN_PARALLEL_SAMPLES = 256

_PAR = {"workers": 0, "pool": None, "recipe": None}


@contextlib.contextmanager
def parallel(workers=None, frame=None):
    """Spread this block's big surface queries over `workers` processes.

    WHY PROCESSES. On an imported collector every query is an OpenCascade
    search, answered one point at a time, and measured on the 12 x 12 mm,
    10-layer design on `test collector.step` the projection was 27 s of a 48 s
    generation on one core of sixteen. The queries are
    independent -- `march_many` already walks its geodesics as independent
    rows, bit-identical alone or in company -- so they shard across processes
    with no change to any answer. Threads cannot do it: the kernel holds the
    interpreter lock through a search.

    ONLY A FRAME THAT PUBLISHES `worker_recipe` IS SHARDED, because a worker
    must build the same collector from scratch (`mew_cad.CadFrame`). Every
    other frame answers in microseconds and takes the in-process path
    unchanged, as does anything outside this block -- the default is serial.

    The pool is built on the first query big enough to need it
    (`MIN_PARALLEL_SAMPLES`) and shut down when the outermost block exits.
    `workers=None` is one fewer than the logical CPU count. A frozen build
    stays serial: its executable dispatches on its first argument and has no
    entry for a pool worker.

    `frame`, when given, STARTS the workers at once. Each one has to import the
    kernel and read the STEP file before it can answer anything -- seconds, not
    milliseconds -- and starting them here lets that happen while this process
    is still building patterns and walking rails, rather than after.
    """
    if workers is None:
        workers = max(1, (os.cpu_count() or 1) - 1)
    outer = _PAR["workers"]
    _PAR["workers"] = int(workers)
    try:
        if frame is not None:
            pool = _pool_for(FrameSurface(frame), MIN_PARALLEL_SAMPLES)
            if pool is not None:
                for _ in range(_PAR["workers"]):
                    pool.submit(_worker_ready)
        yield
    finally:
        _PAR["workers"] = outer
        if not outer:
            _shutdown_pool()


def _shutdown_pool():
    pool = _PAR["pool"]
    _PAR["pool"] = _PAR["recipe"] = None
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=True)


def _pool_for(surface, samples):
    """The worker pool for `surface`, or None where the work stays in-process."""
    if _PAR["workers"] < 2 or samples < MIN_PARALLEL_SAMPLES:
        return None
    if getattr(sys, "frozen", False):
        return None
    hook = getattr(surface.frame, "worker_recipe", None)
    recipe = hook() if hook is not None else None
    if recipe is None:
        return None
    if _PAR["pool"] is not None and _PAR["recipe"] == recipe:
        return _PAR["pool"]
    _shutdown_pool()
    import concurrent.futures
    import multiprocessing
    _PAR["pool"] = concurrent.futures.ProcessPoolExecutor(
        max_workers=_PAR["workers"],
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_worker_init, initargs=(recipe,))
    _PAR["recipe"] = recipe
    return _PAR["pool"]


def _surface_config(surface):
    """What a worker needs to stand up an identical `FrameSurface` on its frame."""
    return (surface.height, surface.fd_step, surface._fd_scale, surface._tuned)


_WORKER = {}


def _worker_init(recipe):
    # A WORKER DIES WITH ITS PARENT. The GUI cancels a run by terminating the
    # generator process, which runs no cleanup, and a pool worker blocked on
    # its task queue would otherwise outlive it holding a whole collector --
    # measured: all 15 still alive 10 s after the terminate.
    import multiprocessing
    import threading

    parent = multiprocessing.parent_process()
    if parent is not None:
        def _watch():
            parent.join()
            os._exit(1)
        threading.Thread(target=_watch, daemon=True).start()
    module, function, args = recipe
    _WORKER["frame"] = getattr(importlib.import_module(module), function)(*args)
    _WORKER["surfaces"] = {}


def _worker_ready():
    return True


def _worker_surface(config):
    got = _WORKER["surfaces"].get(config)
    if got is None:
        height, fd_step, fd_scale, tuned = config
        got = FrameSurface(_WORKER["frame"], height=height, fd_step=fd_step,
                           fd_scale=fd_scale)
        got._tuned = tuned
        _WORKER["surfaces"][config] = got
    return got


def _worker_march(config, starts, directions, stops, step):
    return march_many(_worker_surface(config), starts, directions, stops, step)


def _worker_chart_normal(config, world):
    surface = _worker_surface(config)
    uv = surface.chart(world)
    return uv, surface.normal(uv)


def _chunks(costs, parts):
    """Row indices dealt into `parts` groups of near-equal total cost, each in
    ascending order. Greedy: the dearest row goes to the lightest group."""
    groups = [[] for _ in range(max(1, min(parts, len(costs))))]
    loads = [0.0] * len(groups)
    for i in sorted(range(len(costs)), key=lambda k: -costs[k]):
        j = loads.index(min(loads))
        groups[j].append(i)
        loads[j] += costs[i]
    return [sorted(g) for g in groups if g]


def _march_sharded(pool, surface, starts, directions, stops, step):
    """`march_many`, its rows walked in worker processes. Same answers: a row
    is bit-identical marched alone or in any company, which is the property
    `march_many` is built on, so how the rows are grouped cannot show."""
    n = len(stops)
    P = np.asarray(starts, dtype=np.float64).reshape(n, 3)
    D = np.asarray(directions, dtype=np.float64).reshape(n, 3)
    # the cost of a geodesic is the arc it walks plus a stop per sample
    costs = [(float(s[-1]) / step if s.size else 0.0) + s.size for s in stops]
    config = _surface_config(surface)
    groups = _chunks(costs, _PAR["workers"])
    futures = [pool.submit(_worker_march, config, P[g], D[g],
                           [stops[i] for i in g], step) for g in groups]
    out = [None] * n
    for g, fut in zip(groups, futures):
        for i, row in zip(g, fut.result()):
            out[i] = row
    return out


def _chart_and_normal(surface, world):
    """`(surface.chart(world), surface.normal_at(world))`, charting ONCE."""
    pool = _pool_for(surface, len(world))
    if pool is None:
        uv = surface.chart(world)
        return uv, surface.normal(uv)
    config = _surface_config(surface)
    groups = np.array_split(np.arange(len(world)), _PAR["workers"])
    groups = [g for g in groups if g.size]
    futures = [pool.submit(_worker_chart_normal, config, world[g])
               for g in groups]
    parts = [f.result() for f in futures]
    return (np.concatenate([p[0] for p in parts]),
            np.concatenate([p[1] for p in parts]))


def _tangential(v, n):
    """`v` with its normal component removed, normalised."""
    t = v - n * np.dot(v, n)
    m = np.linalg.norm(t)
    if not m > 0.0:
        raise ValueError("the along direction is parallel to the surface normal")
    return t / m


def _rotate_about(v, axis, angle):
    """Rodrigues rotation of `v` about the unit `axis` by `angle` radians."""
    c, s = math.cos(angle), math.sin(angle)
    return v * c + np.cross(axis, v) * s + axis * np.dot(axis, v) * (1.0 - c)


# --------------------------------------------------------------------------- #
# The flat pattern
# --------------------------------------------------------------------------- #
#: G1 chord fidelity of a turnaround arc, in segments per half-turn. PORTED
#: from the three-axis default -- `scaffold_gcode.generate_scaffold_gcode`'s
#: `arc_segments=8` and the field that feeds it, `gui_scaffold.py:69`. Kept
#: equal to it on purpose: it is what makes a projected turnaround measure the
#: same join angle as the three-axis one (59.07 deg at the shipped knobs), which
#: is this feature's comparator. Coarsening it to buy back projection time
#: doubles the per-vertex turn and fails that comparison.
TURN_ARC_SEGMENTS = 8


def scaffold_pattern(length, width, spacing, *, angle_deg=0.0,
                     footprint_angle=0.0, shape="rectangle", shape_sides=64,
                     footprint_polygon=None, samples_per_pass=None,
                     sample_step=0.5, turn_radius=0.0,
                     arc_segments=TURN_ARC_SEGMENTS,
                     flip=False, vflip=False, reverse=False):
    """One LAYER's flat `(p, q)` pattern, built by the THREE-AXIS generator.

    Returns `(pattern, chart_angle_deg)`: an `(N, 2)` polyline in the pattern's
    own axis-aligned chart, and the angle `project_pattern` must be given so
    that chart lands on the surface turned the right way.

    WHY THIS EXISTS, and what `serpentine_pattern` could not do. That builder
    lays one fixed `length x width` rectangle of passes, and turning that ONE
    pattern rigidly per layer does not work. A rigid turn moves the pass
    ENDS, so on a stack with a layer rotation the passes of one layer run past
    the outermost fibre of the next -- 1.00 mm of straight overrun at each end
    at the shipped 6 x 4 mm defaults, where the three-axis generator has none.
    The three-axis generator has none because `_layer_passes` re-derives the
    hatch lines FRESH at every layer's own angle and clips them to one fixed
    footprint, so every layer's passes end on the same boundary.

    This function is that rule, called and not re-derived: `footprint_outline`,
    `_layer_passes` and `_flatten_passes` are imported from `scaffold_gcode`,
    exactly as `serpentine_pattern` imports `_turn_points` and for the same
    reason -- two collectors must not carry two copies of the law.

    TWO ROTATIONS, AND THEY ARE NOT THE SAME OPERATION. This is the distinction
    `_emit_one_scaffold` already draws and it is ported whole:

      * `angle_deg` -- the LAYER's fill angle. The hatch lines are generated at
        this angle against the UNMOVED footprint and clipped to it, so a
        rotated layer covers the same outline with differently-angled fibres.
      * `footprint_angle` -- a RIGID turn of the whole part, outline and fill
        together. `scaffold_gcode.py:1083` records what happens if this is
        implemented by re-clipping against a rotated polygon instead: the
        edge-parallel extreme pass is dropped and consecutive layers' corners
        desync, so the linking loops stop firing. It is therefore folded into
        the returned `chart_angle_deg` and never into the clip.

    WHY THE PATTERN COMES BACK AXIS-ALIGNED. `project_pattern` marches ONE rail
    across the DISTINCT `q` values and one rib per pass, grouping rows by exact
    float equality of `q` (`_rib_jobs`). A pass whose points carry a `q` each
    would cost a transverse geodesic walk per POINT and would not be one pass at
    all. So the passes are generated at `angle_deg` in the footprint's frame --
    the numerically robust path, the one the three-axis generator uses -- and
    then turned BACK by `angle_deg` into the chart, with each pass's `q` snapped
    to one value so the grouping is exact. The turn the layer actually wants is
    applied by `project_pattern` to the Fermi FRAME, which is where it was
    always applied.

    `samples_per_pass` forces a fixed sample count (the page's cheap preview);
    left None each pass is sampled at `sample_step` along ITS OWN length, which
    a shaped footprint needs -- a circle's passes are not all the same length.

    `flip`, `vflip` and `reverse` are the FOUR WAYS INTO THE SAME FIBRE SET
    that `_emit_one_scaffold` minimises travel over (`scaffold_gcode.py:1266`):
    which perpendicular side the scan starts from, which edge the two free ends
    sit on, and whether the path is walked backwards. The deposited grid is
    identical in all four -- only the entry and exit corners move, and which
    side each turnaround balloons to. `surface_layers` picks per layer, which
    is what shrinks a layer change from a hop across the whole footprint to a
    corner offset, and therefore what lets the linking loop's COINCIDENCE gate
    fire at all.
    """
    import scaffold_gcode                      # lazy: see `serpentine_pattern`

    if not spacing > 0.0:
        raise ValueError("spacing must be positive, got %r" % (spacing,))
    if turn_radius < 0.0:
        raise ValueError("turn_radius must be non-negative mm, got %r"
                         % (turn_radius,))
    poly = (scaffold_gcode.footprint_outline(shape, length, width, shape_sides,
                                             footprint_polygon)
            if shape and shape != "rectangle" else None)
    passes = scaffold_gcode._layer_passes(angle_deg, length, width, spacing,
                                          flip=bool(flip), polygon=poly)
    if not passes:
        raise ValueError("the footprint holds no passes at %g mm stepover"
                         % (spacing,))
    pts = scaffold_gcode._flatten_passes(passes, float(turn_radius),
                                         int(arc_segments), spacing,
                                         vflip=bool(vflip))

    # back into the chart: undo the layer's fill angle, so every straight pass
    # is a constant-`q` row again.
    th = math.radians(-float(angle_deg))
    c, s = math.cos(th), math.sin(th)
    flat = [(x * c - y * s, x * s + y * c) for (x, y, _l, _a) in pts]

    rows = []
    for kind, i, j in _pattern_runs(pts):
        if kind == "link":
            rows.append(np.asarray(flat[i + 1:j + 1], dtype=np.float64))
            continue
        q = sum(flat[k][1] for k in range(i, j + 1)) / float(j + 1 - i)
        ps = _resample_p([flat[k][0] for k in range(i, j + 1)],
                         samples_per_pass, sample_step)
        if rows:
            ps = ps[1:]                       # the run's first point is shared
        rows.append(np.column_stack([ps, np.full(len(ps), q)]))
    out = np.vstack(rows)
    if reverse:
        # `_reverse_path`'s geometry half. It also shifts the link flags, which
        # only the emitter needs; a pattern is just the traversal.
        out = out[::-1].copy()
    return out, float(angle_deg) + float(footprint_angle)


def _pattern_runs(pts):
    """`(kind, first, last)` inclusive point ranges of a `_flatten_passes` list.

    `kind` is `"pass"` or `"link"`. A `_flatten_passes` point tags the segment
    ENTERING it, so segment `k` runs `pts[k] -> pts[k+1]` and is a link exactly
    when `pts[k+1]`'s flag is set; a run is a maximal stretch of one kind, and
    consecutive runs SHARE their boundary point (the pass end is the arc's first
    anchor, and the arc's last point is the next pass's start).
    """
    n = len(pts)
    runs = []
    k = 0
    while k < n - 1:
        kind = bool(pts[k + 1][2])
        j = k
        while j < n - 1 and bool(pts[j + 1][2]) == kind:
            j += 1
        runs.append(("link" if kind else "pass", k, j))
        k = j
    return runs


def _resample_p(ps, samples_per_pass, sample_step):
    """The `p` values of one straight pass, sampled for projection.

    `ps` are the pass's own vertices (two for a straight pass). With
    `samples_per_pass` the count is forced; otherwise the pass is sampled at
    `sample_step` along ITS OWN extent, which is what a shaped footprint needs.
    Endpoints are preserved exactly, so a clipped pass still ends on the
    footprint boundary.
    """
    a, b = float(ps[0]), float(ps[-1])
    if samples_per_pass:
        per = max(2, int(samples_per_pass))
    else:
        per = max(2, int(math.ceil(abs(b - a) / float(sample_step))) + 1)
    return np.linspace(a, b, per)


def serpentine_pattern(length, width, spacing, samples_per_pass=None,
                       sample_step=0.5, turn_radius=0.0):
    """A flat serpentine in `(p, q)` mm -- passes along p, stepping over in q.

    Centred on the origin, so the middle pass is the rail and the projection's
    exact axis runs up the middle of the scaffold rather than along one edge.
    Returns `(N, 2)`; consecutive rows are a polyline.

    `turn_radius` is the turnaround radius in MILLIMETRES, and 0.0 (the
    default) is the shipped behaviour byte for byte: the turnarounds are then
    the single steps in `q` between passes, i.e. right angles. Above zero the
    step is replaced by the BALLOONING LASSO the three-axis generator has always
    drawn -- `scaffold_gcode._turn_circle` is imported and called rather than
    re-derived, so the two collectors cannot carry two copies of the law. Note
    the unit: the designer page's field is a MULTIPLE of the stepover (the
    three-axis `arc_radius_mult`, default 1.5), and the multiply happens in the
    page, not here.

    Two consequences of the ported law that a caller has to know:

      * The lasso is not a fillet. Both pass ends lie ON the circle and the
        fibre follows the reflex arc, so it meets the straight pass at an ANGLE
        for any radius above `spacing / 2`, degenerating to the tangent
        semicircle exactly at `spacing / 2`. That join angle is the three-axis
        one and is the point of porting rather than inventing.
      * It balloons OUTBOARD, so the pattern's `p` extent grows by
        `R + sqrt(R^2 - (spacing/2)^2)` at each end. On a bounded collector that
        eats margin, and `RevolutionFrame` does not bound itself to its own span
        `pattern_extent` returns the
        grown half-length so a caller can check before projecting.

    This is the PATTERN only. It knows nothing about a surface, which is the
    boundary that lets the same pattern be laid on a plate, a mandrel or a
    dome -- the projection is what differs, not the design.
    """
    if not spacing > 0.0:
        raise ValueError("spacing must be positive, got %r" % (spacing,))
    if turn_radius < 0.0:
        raise ValueError("turn_radius must be non-negative mm, got %r"
                         % (turn_radius,))
    n_pass = max(1, int(round(width / spacing)) + 1)
    per = (int(samples_per_pass) if samples_per_pass
           else max(2, int(math.ceil(length / sample_step)) + 1))
    q0 = -0.5 * spacing * (n_pass - 1)
    half = 0.5 * length
    turn = float(turn_radius) > 0.0
    if turn:
        # Lazy, and in this direction on purpose. `scaffold_gcode` already
        # imports THIS module lazily (`surface_layers`), so a module-level
        # import here would close the cycle; and importing the law beats
        # copying it, which is the "two copies" failure `surface_frame`'s own
        # docstring names.
        import scaffold_gcode
    rows = []
    for i in range(n_pass):
        ps = np.linspace(-half, half, per)
        if i % 2:
            ps = ps[::-1]
        rows.append(np.column_stack([ps, np.full(per, q0 + i * spacing)]))
        if turn and i < n_pass - 1:
            # The pass ends where it ends and the next begins directly across
            # the stepover, so the chord is pure `q` and equals the pitch --
            # the three-axis INTERIOR turnaround exactly. `outward` is the pass
            # travel direction, which is what picks the side the balloon bulges
            # to (`scaffold_gcode.py:712`).
            b = (float(rows[-1][-1, 0]), float(rows[-1][-1, 1]))
            a_next = (half if (i + 1) % 2 else -half, q0 + (i + 1) * spacing)
            outward = (-1.0, 0.0) if i % 2 else (1.0, 0.0)
            arc = scaffold_gcode._turn_points(b, a_next, outward,
                                              float(turn_radius),
                                              TURN_ARC_SEGMENTS)
            if arc:
                rows.append(np.asarray(arc, dtype=np.float64))
    return np.vstack(rows)


def pattern_extent(length, spacing, turn_radius=0.0):
    """The pattern's half-length in `p` mm, including any turnaround balloon.

    `length / 2` with turnarounds off. With them on the lasso reaches
    `R + sqrt(R^2 - (spacing/2)^2)` past each pass end -- `R` out from the
    circle centre, which itself sits that far off the chord midpoint
    (`scaffold_gcode._turn_circle`'s `dmc`) -- and `R` is clamped up to
    `spacing / 2` there, so a smaller commanded radius does not shrink it below
    the tangent semicircle.

    Exposed because `RevolutionFrame` will project a path clean off the end of
    its own collector without complaining, so switching
    turnarounds on can move a design that fitted into one that does not.
    """
    half = 0.5 * float(length)
    if not turn_radius > 0.0:
        return half
    r = max(float(turn_radius), 0.5 * float(spacing))
    return half + r + math.sqrt(max(0.0, r * r - (0.5 * float(spacing)) ** 2))


def turn_point_mask(pattern):
    """Boolean `(N,)`: which rows of a `serpentine_pattern` are TURNAROUND arc.

    A pass is `per >= 2` samples sharing ONE `q` -- literally the same float,
    from one `np.full` -- while every arc point carries a `q` of its own. So the
    passes are the `q` values that occur more than once and the arc is
    everything else. Derived from the returned array, which is why
    `serpentine_pattern` can keep its `(N, 2)` return and its fixed signature.

    Counting rather than latticing, because the lasso does NOT stay between the
    two passes it joins: at the shipped knobs its circle spans `q0 - 0.5` to
    `q0 + 1.0` for a 0.5 stepover, so the pattern's minimum `q` is an ARC point
    and any rule anchored on that minimum is anchored on the wrong row.

    It exists because the DELIVERED turn radius has to be quoted over arc
    vertices only. The two vertices where the lasso meets the straight pass are
    corners, not arc, and a corner has no radius; including them reports a
    number far below the commanded one that is an artefact of the join rather
    than a property of the turn.
    """
    q = np.asarray(pattern, dtype=np.float64)[:, 1]
    vals, counts = np.unique(q, return_counts=True)
    return ~np.isin(q, vals[counts > 1])
