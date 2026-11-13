"""The collector as a surface you ask questions of, in world XYZ, many points at once.

WHY THIS MODULE EXISTS. Every five-axis consumer -- the sag solvers, the
deposition tracker, the preview, the player -- needs the same handful of facts
about the collector at a point: where the surface is under it, which way is out,
and how the surface bends there. Until this module each consumer got them by
asking what SHAPE the collector was (`profile`, `developable`,
`body_spec()["kind"]`) and computing them its own way, which is how an imported
collector came to generate correct g-code and then fail at every stage after
it. A consumer of this module asks the
questions and never the shape.

THE QUERIES, all batched over a leading axis of N points:

    settle(P)             -> Q, N, face      the nearest surface point, the
                                             outward unit normal there, and the
                                             face it lies on (-1 where the
                                             collector has no faces)
    curvature(Q, N, face) -> k1, k2, d1, d2  principal curvatures, CONVEX-
                                             POSITIVE against the outward
                                             normal (`mew_frame`'s sign), and
                                             their world unit directions
    normal_curvature(Q, N, face, T) -> kn    Euler: k1 (T.d1)^2 + k2 (T.d2)^2

    height(P)             -> h, Q, N, face   settle, plus the signed distance
                                             along the outward normal

THE NORMAL AT A BOUNDARY. Where the nearest point is on an edge or a span end,
the face normal there is not the direction to `P`: just past the rim of the
imported dome, a point one fibre radius out read the flat base's normal and a
height of -0.000024 mm instead of +0.0109. So
`settle` returns the normal of the OFFSET surface through `P` -- the gradient of
the distance, `+-(P - Q)/|P - Q|` -- wherever the foot is on a boundary
(`_boundary_normals`), and `h` is then the signed distance itself. At a face
interior the two are the same vector. Offset outwards, a fibre laid over a
convex edge follows a round of radius `h` about it, as a tube draped over a
corner does.

Three implementations, and a consumer never picks one: it asks its frame,
`DepositionFrame.surface()`.

  `PlaneSurface`        the flat bed (`PlanarFrame`).
  `RevolutionSurface`   a profile spun about machine X (`RevolutionFrame`, and
                        `CylinderFrame` as an unbounded straight profile), in
                        closed form from the profile.
  `CadSurface`          an imported STEP solid (`mew_cad.CadFrame`).

`face` is an integer label of the smooth piece a point settled on. A plane and
a surface of revolution are one piece, label 0; an import numbers its print
faces.

HOW AN IMPORT IS ANSWERED FAST AND STILL EXACTLY. `mew_cad.CadSolid` put every
query to the kernel one point at a time: 345 us a search, from a Python loop.
But a STEP collector is almost always built from ANALYTIC faces -- both shipped
parts are planes, cylinders and a sphere -- and the nearest point on an
analytic surface is closed form. So per face, for all N points at once:

  1. project onto the face's UNTRIMMED surface in closed form (plane, cylinder,
     cone, sphere, torus), which gives the distance `d_free` and the face
     parameters `(u, v)`;
  2. decide whether `(u, v)` is inside the face's TRIMMED region by testing it
     against the face's own boundary curves, sampled once in parameter space;
  3. take, per point, the nearest face whose projection is inside.

That is the kernel's answer, not an approximation of it, whenever a CERTIFICATE
holds -- the same argument `CadSolid._nearest_free` states, vectorised: the
untrimmed distance to any face is a lower bound on the trimmed distance to it,
so an inside projection no other face's lower bound undercuts IS the nearest
point of the whole deposition surface.

WHERE THE CERTIFICATE FAILS, THE KERNEL ANSWERS. Three cases, each detected and
never guessed: a projection within the boundary band of its face's trim curve
(the sampled boundary cannot decide it), a point whose nearest surface point is
on an EDGE (no inside projection is certified), and a face the closed forms do
not cover (a spline). Those points go to `CadSolid._nearest_exact` one at a
time. On a design that stays on faces they are rare; `stats` counts them so a
run that suddenly pays the kernel says so.
"""

import math

import numpy as np

#: Samples per boundary edge that is not a straight line in parameter space.
#: The band that sends a point to the kernel is MEASURED from these samples
#: (`_Trim`), so this trades kernel calls near boundaries against setup cost; it
#: does not trade accuracy.
EDGE_SAMPLES = 96

#: Two distances this close are a tie -- a crease, where two faces meet at the
#: same distance and there is no single nearest face. mm.
TIE = 1e-9

#: A nearest point further than this to the SIDE of the line through `P` along
#: the face normal is on a BOUNDARY -- an edge, or the end of a span -- not in a
#: face. mm. Rounding at a face-interior foot is ~1e-15 of the coordinates.
EDGE_LATERAL = 1e-9

#: The same test for a foot the KERNEL found (`CadSurface`), whose position is
#: only as good as `BRepExtrema_DistShapeShape`'s. Just past a TANGENT edge the
#: kernel snaps the foot onto the edge, sideways of `P` by the distance past it:
#: measured 1e-8 to 1e-7 mm at h = 0 on a kernel-built fillet, 9.1e-7 mm on the
#: imported block, and up to 1e-4 mm at h = 0.2 mm.
#: With `P` on the
#: surface, `P - Q` is then that snap and nothing else, TANGENT to the
#: surface, and taking it as the normal turned the normal 90 deg -- one false
#: 90 deg corner in the lay's development, drawn as a zigzag fibre on the
#: block. Below this the face normal is kept: on a tangent edge it is the
#: surface's normal, and within 1e-5 mm of a crease it is one of the rim's
#: normals. The snaps above it sit at h >= 0.02 mm, where they tilt the
#: normal by at most 0.09 deg. mm.
KERNEL_EDGE_LATERAL = 1e-5


class _Trim(object):
    """A face's trimmed region in its own parameter space, for many points.

    The boundary is every wire of the face, each edge's p-curve sampled in wire
    order and orientation, so the even-odd rule over all of them is inside-the-
    outer-minus-the-holes without having to know which wire is which.

    THE BAND IS MEASURED, not assumed. A curved p-curve sampled at `n` points
    departs from its chords by at most the sagitta; the band is twice the worst
    sagitta actually measured on this face (by sampling at double density),
    plus a floor. A point closer to the sampled boundary than that is not
    decided here -- `inside` returns -1 for it and the caller asks the kernel.
    """

    def __init__(self, face, occ):
        from OCP.BRepAdaptor import BRepAdaptor_Curve2d
        from OCP.BRepTools import BRepTools_WireExplorer
        from OCP.GeomAbs import GeomAbs_CurveType
        from OCP.TopAbs import TopAbs_REVERSED, TopAbs_WIRE
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS

        segs = []
        sag = 0.0
        ex = TopExp_Explorer(face, TopAbs_WIRE)
        while ex.More():
            wire = TopoDS.Wire(ex.Current())
            we = BRepTools_WireExplorer(wire, face)
            while we.More():
                edge = we.Current()
                c2 = BRepAdaptor_Curve2d(edge, face)
                t0, t1 = c2.FirstParameter(), c2.LastParameter()
                line = c2.GetType() == GeomAbs_CurveType.GeomAbs_Line
                n = 2 if line else EDGE_SAMPLES
                ts = np.linspace(t0, t1, 2 * n - 1)
                pts = np.array([(c2.Value(t).X(), c2.Value(t).Y()) for t in ts])
                if edge.Orientation() == TopAbs_REVERSED:
                    pts = pts[::-1]
                coarse = pts[::2]
                if not line:
                    mid = pts[1::2]
                    a, b = coarse[:-1], coarse[1:]
                    sag = max(sag, float(np.max(_seg_dist2(mid, a, b))))
                segs.append(np.column_stack([coarse[:-1], coarse[1:]]))
                we.Next()
            ex.Next()
        self.segs = (np.vstack(segs) if segs else np.zeros((0, 4)))
        span = np.ptp(self.segs.reshape(-1, 2), axis=0) if len(self.segs) \
            else np.ones(2)
        self.band = 2.0 * sag + 1e-9 * float(max(1.0, np.max(span)))
        self._grid()

    #: cells per side of the acceleration grid over the parameter box
    GRID = 64

    def _grid(self):
        """Classify a GRID x GRID raster of the parameter box ONCE.

        A cell no boundary segment comes within `band` of is uniformly inside
        or outside, so its centre's answer is every point's answer there. A
        cell the boundary DOES touch keeps its own segments and a REFERENCE
        point whose parity is known; a point there is inside iff the reference
        is, flipped once per cell segment the reference-to-point connector
        crosses. The connector stays inside the (convex) cell and every segment
        touching the cell is in its list, so that count is the whole story --
        the same even-odd answer as testing every segment of the face, at the
        cost of a few. All boundary cells are answered in one vectorised pass.
        """
        s = self.segs
        g = self.GRID
        self.lo = np.zeros(2)
        self.cellw = np.ones(2)
        self.state = np.zeros((g, g), dtype=np.int8)
        self.cell_row = np.full((g, g), -1, dtype=np.int64)
        self.cell_segs = np.zeros((0, 1, 4))
        self.cell_ref = np.zeros((0, 2))
        self.cell_par = np.zeros(0, dtype=bool)
        if not len(s):
            return
        pts = s.reshape(-1, 2)
        lo = pts.min(axis=0) - 2 * self.band
        hi = pts.max(axis=0) + 2 * self.band
        w = np.maximum((hi - lo) / g, 1e-300)
        self.lo, self.cellw = lo, w
        smin = np.minimum(s[:, :2], s[:, 2:]) - self.band
        smax = np.maximum(s[:, :2], s[:, 2:]) + self.band
        i0 = np.clip(((smin - lo) // w).astype(int), 0, g - 1)
        i1 = np.clip(((smax - lo) // w).astype(int), 0, g - 1)
        lists = {}
        for k in range(len(s)):
            for ia in range(i0[k, 0], i1[k, 0] + 1):
                for ib in range(i0[k, 1], i1[k, 1] + 1):
                    lists.setdefault((ia, ib), []).append(k)
        ca, cb = np.meshgrid(np.arange(g), np.arange(g), indexing="ij")
        centres = lo + (np.column_stack([ca.ravel(), cb.ravel()]) + 0.5) * w
        state = self._exact(centres, np.arange(0)).reshape(g, g)
        K = max(len(v) for v in lists.values()) if lists else 1
        # padding: a zero-length segment far outside the box, which neither
        # straddles a connector nor comes within the band of any point
        far = hi + 10.0 * (hi - lo) + 1.0
        segs = np.tile(np.array([far[0], far[1], far[0], far[1]]),
                       (len(lists), K, 1))
        refs = np.zeros((len(lists), 2))
        pars = np.zeros(len(lists), dtype=bool)
        rng = np.random.default_rng(0)
        for row, ((ia, ib), ks) in enumerate(sorted(lists.items())):
            segs[row, :len(ks)] = s[ks]
            cell_lo = lo + np.array([ia, ib]) * w
            # a reference point clear of every segment of the cell, so its
            # parity is well defined; the whole-face test gives that parity
            cand = np.vstack([cell_lo + 0.5 * w,
                              cell_lo + rng.uniform(0.1, 0.9, (16, 2)) * w])
            dist = np.min(_seg_dist2(cand, s[ks, :2], s[ks, 2:],
                                     pairwise=True), axis=1)
            j = int(np.argmax(dist))
            refs[row] = cand[j]
            pars[row] = self._exact(cand[j:j + 1], np.arange(0))[0] == 1
            self.cell_row[ia, ib] = row
            state[ia, ib] = -2
        self.state = state
        self.cell_segs, self.cell_ref, self.cell_par = segs, refs, pars

    def _exact(self, uv, which):
        """The whole-face segment test: even-odd crossing over ALL segments,
        and the band distance against segments `which`. +1 / 0 / -1. Used to
        build the raster; `inside` answers points without it."""
        n = len(uv)
        out = np.zeros(n, dtype=np.int8)
        if not n or not len(self.segs):
            return out
        s = self.segs
        cross = np.zeros(n, dtype=bool)
        near = np.full(n, np.inf)
        u0, v0, u1, v1 = s[:, 0], s[:, 1], s[:, 2], s[:, 3]
        step = max(1, 2000000 // max(1, len(s)))
        for i in range(0, n, step):
            pu = uv[i:i + step, 0:1]
            pv = uv[i:i + step, 1:2]
            straddle = (v0 > pv) != (v1 > pv)
            dv = np.where(v1 != v0, v1 - v0, 1.0)
            xint = u0 + (pv - v0) * (u1 - u0) / dv
            cross[i:i + step] = (np.count_nonzero(straddle & (pu < xint),
                                                  axis=1) % 2) == 1
            if len(which):
                near[i:i + step] = np.min(_seg_dist2(
                    uv[i:i + step], s[which, :2], s[which, 2:],
                    pairwise=True), axis=1)
        out[cross] = 1
        out[near <= self.band] = -1
        return out

    def inside(self, uv):
        """+1 inside, 0 outside, -1 undecided (within the band of the boundary)."""
        uv = np.asarray(uv, dtype=np.float64)
        n = len(uv)
        out = np.zeros(n, dtype=np.int8)
        if not n or not len(self.segs):
            return out
        g = self.GRID
        idx = np.floor((uv - self.lo) / self.cellw).astype(np.int64)
        on = np.nonzero(np.all((idx >= 0) & (idx < g), axis=1))[0]
        if not len(on):
            return out
        st = self.state[idx[on, 0], idx[on, 1]]
        out[on] = np.where(st == -2, 0, st)
        pend = on[st == -2]
        if not len(pend):
            return out
        row = self.cell_row[idx[pend, 0], idx[pend, 1]]
        S = self.cell_segs[row]                       # (m, K, 4)
        p = uv[pend][:, None, :]                      # (m, 1, 2)
        r = self.cell_ref[row][:, None, :]
        a, b = S[..., :2], S[..., 2:]
        # proper crossings of connector r-p with each segment a-b
        def orient(o, x, y):
            return ((x[..., 0] - o[..., 0]) * (y[..., 1] - o[..., 1])
                    - (x[..., 1] - o[..., 1]) * (y[..., 0] - o[..., 0]))
        o1 = orient(r, p, a)
        o2 = orient(r, p, b)
        o3 = orient(a, b, r)
        o4 = orient(a, b, p)
        crosses = ((o1 > 0) != (o2 > 0)) & ((o3 > 0) != (o4 > 0))
        par = self.cell_par[row] ^ ((np.count_nonzero(crosses, axis=1) % 2)
                                    == 1)
        res = np.where(par, 1, 0).astype(np.int8)
        near = np.min(_seg_dist2(p[:, 0, :][:, None, :].repeat(S.shape[1], 1)
                                 .reshape(-1, 2), a.reshape(-1, 2),
                                 b.reshape(-1, 2)).reshape(len(pend), -1),
                      axis=1)
        res[near <= self.band] = -1
        out[pend] = res
        return out


def _seg_dist2(p, a, b, pairwise=False):
    """Distance from points to segments a-b. Row-wise, or all pairs (N, M)."""
    if pairwise:
        p = p[:, None, :]
        a = a[None, :, :]
        b = b[None, :, :]
    ab = b - a
    L2 = np.sum(ab * ab, axis=-1)
    t = np.clip(np.sum((p - a) * ab, axis=-1) / np.where(L2 > 0, L2, 1.0),
                0.0, 1.0)
    d = p - (a + t[..., None] * ab)
    return np.sqrt(np.sum(d * d, axis=-1))


def _boundary_3d(face):
    """`(segments (M, 6), sagitta)` -- the face's boundary edges in WORLD,
    sampled, and the worst measured departure of the samples' chords from the
    true curves. Distance to the sampled polyline minus that sagitta is a LOWER
    bound on the distance to the true boundary."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_CurveType
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    segs, sag = [], 0.0
    ex = TopExp_Explorer(face, TopAbs_EDGE)
    while ex.More():
        e = TopoDS.Edge(ex.Current())
        c = BRepAdaptor_Curve(e)
        line = c.GetType() == GeomAbs_CurveType.GeomAbs_Line
        n = 2 if line else EDGE_SAMPLES
        ts = np.linspace(c.FirstParameter(), c.LastParameter(), 2 * n - 1)
        pts = np.array([(c.Value(t).X(), c.Value(t).Y(), c.Value(t).Z())
                        for t in ts])
        coarse = pts[::2]
        if not line:
            sag = max(sag, float(np.max(_seg_dist2(pts[1::2], coarse[:-1],
                                                   coarse[1:]))))
        segs.append(np.column_stack([coarse[:-1], coarse[1:]]))
        ex.Next()
    return (np.vstack(segs) if segs else np.zeros((0, 6))), sag


def _unit(v):
    m = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.where(m > 0, m, 1.0), m[..., 0]


def _boundary_normals(P, Q, N, outside, lateral=EDGE_LATERAL):
    """`N` with every BOUNDARY foot's normal replaced by the offset surface's.

    A foot is on a boundary where `P - Q` leaves the face normal's line by more
    than `lateral` (`EDGE_LATERAL`, or `KERNEL_EDGE_LATERAL` for a kernel
    foot). There the normal becomes `s (P - Q)/|P - Q|`, with `s`
    `outside(idx)` (+1 / -1 for those rows), so `(P - Q) . N` is the signed
    distance. Face-interior rows are returned untouched."""
    w = P - Q
    lat = np.linalg.norm(np.cross(w, N), axis=1)
    idx = np.nonzero(lat > lateral)[0]
    if not len(idx):
        return N
    u, _L = _unit(w[idx])
    N = N.copy()
    N[idx] = np.asarray(outside(idx), dtype=np.float64)[:, None] * u
    return N


class _Face(object):
    """One print face, answered in closed form. `kind` None = not analytic."""

    def __init__(self, fid, face, occ, outward):
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepTools import BRepTools
        from OCP.GeomAbs import GeomAbs_SurfaceType as T

        self.fid = fid
        self.face = face
        a = BRepAdaptor_Surface(face)
        self.u_period = a.UPeriod() if a.IsUPeriodic() else None
        self.v_period = a.VPeriod() if a.IsVPeriodic() else None
        self.uv_bounds = BRepTools.UVBounds_s(face)
        typ = a.GetType()
        self.kind = None
        ax = None
        if typ == T.GeomAbs_Plane:
            self.kind, ax = "plane", a.Plane().Position()
        elif typ == T.GeomAbs_Cylinder:
            g = a.Cylinder()
            self.kind, ax, self.R = "cylinder", g.Position(), g.Radius()
        elif typ == T.GeomAbs_Cone:
            g = a.Cone()
            self.kind, ax = "cone", g.Position()
            self.R, self.alpha = g.RefRadius(), g.SemiAngle()
        elif typ == T.GeomAbs_Sphere:
            g = a.Sphere()
            self.kind, ax, self.R = "sphere", g.Position(), g.Radius()
        elif typ == T.GeomAbs_Torus:
            g = a.Torus()
            self.kind, ax = "torus", g.Position()
            self.R, self.r = g.MajorRadius(), g.MinorRadius()
        if ax is not None:
            def v3(d):
                return np.array([d.X(), d.Y(), d.Z()], dtype=np.float64)
            self.O = v3(ax.Location())
            self.X = v3(ax.XDirection())
            self.Y = v3(ax.YDirection())
            self.Z = v3(ax.Direction())
            self.trim = _Trim(face, occ)
            self.edge3d, self.edge_sag = _boundary_3d(face)
            # sigma: outward = sigma * the closed form's own geometric normal.
            # Fixed per face by comparing, at the parameter-box centre, the
            # closed form's normal against the kernel's outward normal there --
            # so a left-handed placement or a reversed face cannot flip a sign
            # silently.
            self.sigma = 1.0
            u0, u1, v0, v1 = self.uv_bounds
            um, vm = 0.5 * (u0 + u1), 0.5 * (v0 + v1)
            if self.kind == "sphere":
                vm = 0.5 * vm          # keep off the pole, where dU x dV is 0
            n_k = outward(face, um, vm)
            n_g = self._geo_normal_uv(np.array([[um, vm]]))[0]
            self.sigma = 1.0 if float(np.dot(n_k, n_g)) >= 0.0 else -1.0
        # the box gap is a lower bound on the distance to the trimmed face
        from OCP.BRepBndLib import BRepBndLib
        from OCP.Bnd import Bnd_Box
        box = Bnd_Box()
        BRepBndLib.AddOptimal_s(face, box)
        c0, c1 = box.CornerMin(), box.CornerMax()
        self.lo = np.array([c0.X(), c0.Y(), c0.Z()])
        self.hi = np.array([c1.X(), c1.Y(), c1.Z()])

    # -- the closed forms --------------------------------------------------- #
    def _geo_normal_uv(self, uv):
        """The closed form's GEOMETRIC normal (away from the axis / centre / the
        plane's +Z) at parameters, the direction `project` reports."""
        u, v = uv[:, 0:1], uv[:, 1:2]
        X, Y, Z = self.X, self.Y, self.Z
        radial = np.cos(u) * X + np.sin(u) * Y
        if self.kind == "plane":
            return np.broadcast_to(Z, (len(uv), 3)).copy()
        if self.kind == "cylinder":
            return radial
        if self.kind == "cone":
            return math.cos(self.alpha) * radial - math.sin(self.alpha) * Z
        cv, sv = np.cos(v), np.sin(v)
        return cv * radial + sv * Z          # sphere and torus alike

    def project(self, P):
        """Untrimmed nearest point. `(Q, n_geo, d, uv, ok)`; `ok` False where
        the foot is not unique (on an axis, at a centre, past a cone apex)."""
        w = P - self.O
        X, Y, Z = self.X, self.Y, self.Z
        n = len(P)
        ok = np.ones(n, dtype=bool)
        if self.kind == "plane":
            h = w @ Z
            Q = P - h[:, None] * Z
            q = Q - self.O
            uv = np.column_stack([q @ X, q @ Y])
            return Q, np.broadcast_to(Z, (n, 3)).copy(), np.abs(h), uv, ok
        z = w @ Z
        xy = w - z[:, None] * Z
        radial, rho = _unit(xy)
        u = np.arctan2(w @ Y, w @ X)
        if self.kind == "cylinder":
            ok &= rho > 1e-12
            Q = self.O + z[:, None] * Z + self.R * radial
            d = np.abs(rho - self.R)
            return Q, radial, d, np.column_stack([u, z]), ok
        if self.kind == "cone":
            sa, ca = math.sin(self.alpha), math.cos(self.alpha)
            t = (rho - self.R) * sa + z * ca
            rf = self.R + t * sa
            ok &= (rho > 1e-12) & (rf > 1e-12)
            Q = self.O + rf[:, None] * radial + (t * ca)[:, None] * Z
            d = np.abs((rho - self.R) * ca - z * sa)
            n_g = ca * radial - sa * Z
            return Q, n_g, d, np.column_stack([u, t]), ok
        if self.kind == "sphere":
            dirn, r = _unit(w)
            ok &= r > 1e-12
            Q = self.O + self.R * dirn
            v = np.arcsin(np.clip(z / np.where(r > 0, r, 1.0), -1.0, 1.0))
            return Q, dirn, np.abs(r - self.R), np.column_stack([u, v]), ok
        # torus
        m = np.column_stack([rho - self.R, z])
        mu, mr = _unit(m)
        ok &= (rho > 1e-12) & (mr > 1e-12)
        v = np.arctan2(mu[:, 1], mu[:, 0])
        n_g = mu[:, 0:1] * radial + mu[:, 1:2] * Z
        Q = (self.O + (self.R + self.r * mu[:, 0:1]) * radial
             + (self.r * mu[:, 1:2]) * Z)
        return Q, n_g, np.abs(mr - self.r), np.column_stack([u, v]), ok

    def wrap_uv(self, uv):
        """Periodic parameters shifted into the face's own parameter box."""
        uv = uv.copy()
        u0, _u1, v0, _v1 = self.uv_bounds
        if self.u_period:
            uv[:, 0] = u0 + np.mod(uv[:, 0] - u0 + 1e-12, self.u_period) - 1e-12
        if self.v_period:
            uv[:, 1] = v0 + np.mod(uv[:, 1] - v0 + 1e-12, self.v_period) - 1e-12
        return uv

    def curvature(self, Q, n_geo):
        """`(k1, k2, d1, d2)` against the GEOMETRIC normal; the caller applies
        sigma. Principal directions are world unit tangents."""
        n = len(Q)
        Z = self.Z
        if self.kind == "plane":
            d1 = np.broadcast_to(self.X, (n, 3)).copy()
            d2 = np.broadcast_to(self.Y, (n, 3)).copy()
            z = np.zeros(n)
            return z, z.copy(), d1, d2
        if self.kind == "sphere":
            ref = np.where(np.abs(n_geo @ self.X)[:, None] < 0.9, self.X, self.Y)
            d1 = _unit(np.cross(n_geo, ref))[0]
            d2 = np.cross(n_geo, d1)
            k = np.full(n, 1.0 / self.R)
            return k, k.copy(), d1, d2
        w = Q - self.O
        xy = w - (w @ Z)[:, None] * Z
        radial, rho = _unit(xy)
        circ = np.cross(np.broadcast_to(Z, radial.shape), radial)
        if self.kind == "cylinder":
            return (np.full(n, 1.0 / self.R), np.zeros(n), circ,
                    np.broadcast_to(Z, (n, 3)).copy())
        if self.kind == "cone":
            ca = math.cos(self.alpha)
            gen = np.cross(n_geo, circ)
            return ca / np.where(rho > 0, rho, np.inf), np.zeros(n), circ, gen
        # torus: parallel k = cos v / rho, meridian k = 1 / r
        cosv = (rho - self.R) / self.r
        mer = np.cross(n_geo, circ)
        return (np.full(n, 1.0 / self.r), cosv / np.where(rho > 0, rho, np.inf),
                mer, circ)


class _Surface(object):
    """The two queries every implementation derives from `settle` and
    `curvature` the same way."""

    def height(self, P):
        """`(h, Q, N, face)` -- signed distance along the outward normal."""
        P = np.asarray(P, dtype=np.float64).reshape(-1, 3)
        Q, N, face = self.settle(P)
        return np.sum((P - Q) * N, axis=1), Q, N, face

    def normal_curvature(self, Q, N, face, T):
        """Euler's theorem: the curvature the surface shows along tangent `T`."""
        k1, k2, d1, d2 = self.curvature(Q, N, face)
        N = np.asarray(N, dtype=np.float64).reshape(-1, 3)
        T = np.asarray(T, dtype=np.float64).reshape(-1, 3)
        T = T - np.sum(T * N, axis=1)[:, None] * N
        T = _unit(T)[0]
        c1 = np.sum(T * d1, axis=1)
        c2 = np.sum(T * d2, axis=1)
        return k1 * c1 * c1 + k2 * c2 * c2


class PlaneSurface(_Surface):
    """An unbounded plane through `origin` with outward normal `normal`.

    The flat bed is `PlaneSurface()`: z = 0, out along +Z, which is what
    `DepositionFrame.to_world`'s identity map puts at h = 0.
    """

    def __init__(self, origin=(0.0, 0.0, 0.0), normal=(0.0, 0.0, 1.0)):
        self.origin = np.asarray(origin, dtype=np.float64)
        n = np.asarray(normal, dtype=np.float64)
        self.normal = n / np.linalg.norm(n)
        ref = np.array([1.0, 0.0, 0.0]) if abs(self.normal[0]) < 0.9 else \
            np.array([0.0, 1.0, 0.0])
        e1 = np.cross(self.normal, ref)
        self.e1 = e1 / np.linalg.norm(e1)
        self.e2 = np.cross(self.normal, self.e1)

    def settle(self, P):
        P = np.asarray(P, dtype=np.float64).reshape(-1, 3)
        h = (P - self.origin) @ self.normal
        n = len(P)
        return (P - h[:, None] * self.normal,
                np.broadcast_to(self.normal, (n, 3)).copy(),
                np.zeros(n, dtype=np.int64))

    def curvature(self, Q, N, face):
        n = len(np.asarray(Q).reshape(-1, 3))
        return (np.zeros(n), np.zeros(n),
                np.broadcast_to(self.e1, (n, 3)).copy(),
                np.broadcast_to(self.e2, (n, 3)).copy())


class RevolutionSurface(_Surface):
    """A `mew_frame.Profile` spun about machine X, between meridian arc
    lengths `u0` and `u1` (None is unbounded), shifted `surface_x` along the
    axis. The same surface `RevolutionFrame.to_world(u, v, 0)` draws.

    NEAREST POINT, EXACTLY. The nearest point of a surface of revolution to
    `P` lies in the plane through the axis and `P`, since turning any other
    candidate about the axis into that plane brings it closer. In that plane
    the surface is two copies of the meridian: one on `P`'s side of the axis
    and its mirror on the far side. In the profile's own (axis, signed radius)
    coordinates that is the meridian compared against `(x, +r)` and against
    `(x, -r)`, the same pair `RevolutionFrame.from_world` tries, for the reason
    its docstring gives (a dome's rho is negative).

    On each copy the candidates are:

      the profile's closed-form foot, `profile.invert`, when it lands inside
        the span. A line and a circle each have one foot, and the turn of the
        circle `invert` returns is the one nearest the span's middle, so if
        any turn of the foot is in the span, that one is;
      the ends of the span. Distance along a line or a circle has a single
        minimum, so once the foot is cut away the nearest point of the bounded
        arc is an end.

    The smallest distance wins. No iteration and no scoring rule.

    THE CURVATURES are `RevolutionFrame.curvatures_at` at h = 0: the
    meridian's `profile.curvature`, and the parallel's `x'/rho` with the pole's
    0/0 resolved to the meridian value (`RevolutionFrame._k2`). Returned
    largest first, directions swapped to match, as `CadSurface` returns them.

    A CONE APEX has no normal. A foot exactly on it raises
    `mew_frame.AxisSingularity`, as `RevolutionFrame.to_world` does.
    """

    def __init__(self, profile, u0=0.0, u1=None, surface_x=0.0):
        self.profile = profile
        self.u0 = -math.inf if u0 is None else float(u0)
        self.u1 = math.inf if u1 is None else float(u1)
        if self.u0 > self.u1:
            self.u0, self.u1 = self.u1, self.u0
        self.surface_x = float(surface_x)
        ends = [u for u in (self.u0, self.u1) if math.isfinite(u)]
        self._ends = ends
        self._hint = sum(ends) / len(ends) if ends else 0.0

    def _foot(self, P):
        """`(s, side, ry, rz)`: meridian arc length of the nearest point, which
        copy of the meridian it is on (+1 `P`'s side, -1 the mirror), and the
        unit radial direction of `P` from the axis."""
        P = np.asarray(P, dtype=np.float64).reshape(-1, 3)
        n = len(P)
        xa = P[:, 0] - self.surface_x
        r = np.hypot(P[:, 1], P[:, 2])
        on_axis = r == 0.0
        safe = np.where(on_axis, 1.0, r)
        ry = np.where(on_axis, 0.0, P[:, 1] / safe)
        rz = np.where(on_axis, 1.0, P[:, 2] / safe)
        best = np.full(n, np.inf)
        s_best = np.zeros(n)
        side = np.ones(n)
        for sgn in (1.0, -1.0):
            s, h = self.profile.invert(xa, sgn * r, s_hint=self._hint)
            s = np.broadcast_to(np.asarray(s, dtype=np.float64), (n,))
            h = np.broadcast_to(np.asarray(h, dtype=np.float64), (n,))
            d = np.where((s >= self.u0) & (s <= self.u1), np.abs(h), np.inf)
            take = d < best
            best = np.where(take, d, best)
            s_best = np.where(take, s, s_best)
            side = np.where(take, sgn, side)
            for e in self._ends:
                xe, re = self.profile.point(e)
                de = np.hypot(float(xe) - xa, float(re) - sgn * r)
                take = de < best
                best = np.where(take, de, best)
                s_best = np.where(take, e, s_best)
                side = np.where(take, sgn, side)
        return s_best, side, ry, rz

    def _meridian(self, s):
        x, rho = self.profile.point(s)
        dx, drho = self.profile.tangent(s)
        x, rho, dx, drho = (np.broadcast_to(np.asarray(a, dtype=np.float64),
                                            s.shape)
                            for a in (x, rho, dx, drho))
        if np.any((rho == 0.0) & (dx != 0.0)):
            import mew_frame
            raise mew_frame.AxisSingularity(
                "a nearest point is a CONE APEX, where the surface has no "
                "tangent plane and so no normal or curvature. Keep the design "
                "off the apex, or model the collector as a truncated cone.")
        return x, rho, dx, drho

    def settle(self, P):
        """`(Q, N, face)`, face always 0. In the meridian plane the point is
        `(x, rho)` and the outward normal `(-rho', x')`
        (`RevolutionFrame.normal`); both are spun onto `P`'s radial direction,
        mirrored when the foot is on the far copy."""
        P = np.asarray(P, dtype=np.float64).reshape(-1, 3)
        s, side, ry, rz = self._foot(P)
        x, rho, dx, drho = self._meridian(s)
        a = side * rho
        b = side * dx
        Q = np.column_stack([x + self.surface_x, a * ry, a * rz])
        N = np.column_stack([-drho, b * ry, b * rz])
        if self._ends:
            # a span END is a boundary; a sheet has no inside, so the side is
            # the end normal's
            w = P - Q
            N = _boundary_normals(
                P, Q, N, lambda i: np.where(
                    np.sum(w[i] * N[i], axis=1) < 0.0, -1.0, 1.0))
        return Q, N, np.zeros(len(s), dtype=np.int64)

    def curvature(self, Q, N, face):
        """`(k1, k2, d1, d2)`, convex-positive, k1 >= k2. The feet are found
        again from `Q`, which is on the surface, so nothing is carried over
        from `settle`."""
        Q = np.asarray(Q, dtype=np.float64).reshape(-1, 3)
        N = np.asarray(N, dtype=np.float64).reshape(-1, 3)
        s, side, ry, rz = self._foot(Q)
        _x, rho, dx, drho = self._meridian(s)
        km = np.broadcast_to(np.asarray(self.profile.curvature(s),
                                        dtype=np.float64), s.shape)
        pole = rho == 0.0
        kp = np.where(pole, km, dx / np.where(pole, 1.0, rho))
        b = side * drho
        dm = np.column_stack([dx, b * ry, b * rz])
        dp = _unit(np.cross(N, dm))[0]
        swap = kp > km
        return (np.where(swap, kp, km), np.where(swap, km, kp),
                np.where(swap[:, None], dp, dm),
                np.where(swap[:, None], dm, dp))


class CadSurface(_Surface):
    """`mew_cad.CadSolid`'s deposition surface, answered for many points at once.

    Same answers as the solid's kernel queries (`BRepExtrema_DistShapeShape`)
    -- that is the contract -- at closed-form cost wherever the
    certificate in the module docstring holds.
    """

    def __init__(self, solid):
        self.solid = solid
        occ = solid._occ
        self.faces = [_Face(fid, f, occ, self._outward_normal)
                      for fid, f in zip(solid.print_face_ids, solid._print)]
        self.stats = {"points": 0, "kernel": 0}

    def _outward_normal(self, face, u, v):
        return self.solid._normal(face, u, v)

    # -- settle ------------------------------------------------------------- #
    def settle(self, P):
        """`(Q, N, face)` -- nearest point of the deposition surface, outward
        unit normal there, and the print face's id. `(N, 3)` in.

        Every analytic face is projected for every point; that is a handful of
        vectorised array operations per face, which is why nothing here tries
        to skip faces. Per point:

          best   the nearest projection that landed INSIDE its face's trim
          bound  the smallest untrimmed distance of any face whose projection
                 landed OUTSIDE its trim, or of any face with no closed form
                 (its bounding box) -- each a lower bound on that face's true
                 distance
          open   the same, for projections the trim test could not decide or
                 the closed form could not place (on an axis, at a centre)

        `best` is the kernel's answer when `bound` and `open` both stay at or
        above it: nothing else can be nearer. Otherwise the kernel answers.
        """
        P = np.asarray(P, dtype=np.float64).reshape(-1, 3)
        n = len(P)
        self.stats["points"] += n
        best_d = np.full(n, np.inf)
        best_f = np.full(n, -1, dtype=np.int64)
        Q = np.zeros((n, 3))
        N = np.zeros((n, 3))
        bound = np.full(n, np.inf)
        opened = np.full(n, np.inf)
        outs = []                  # (face, point indices outside, their d)
        for f in self.faces:
            if f.kind is None:
                bound = np.minimum(bound, _box_gap(f.lo, f.hi, P))
                continue
            q, ng, d, uv, ok = f.project(P)
            ins = f.trim.inside(f.wrap_uv(uv))
            good = ok & (ins == 1)
            undecided = ~ok | (ins == -1)
            outside = ~good & ~undecided
            opened = np.where(undecided, np.minimum(opened, d), opened)
            oi = np.nonzero(outside)[0]
            outs.append((f, oi, d[oi]))
            better = good & (d < best_d)
            best_d = np.where(better, d, best_d)
            best_f = np.where(better, f.fid, best_f)
            Q[better] = q[better]
            N[better] = f.sigma * ng[better]
        # A FOOT OUTSIDE THE TRIM PUTS THE FACE'S NEAREST POINT ON ITS BOUNDARY,
        # for every closed form but the torus: on a plane, cylinder, cone or
        # sphere the distance from a point has ONE interior local minimum, the
        # untrimmed foot, so once that is cut away the minimum over the trimmed
        # face lies on its edges. The boundary distance is then a far tighter
        # lower bound than the untrimmed one -- on a filleted block the wall's
        # INFINITE plane passes closer to a point over the fillet than the
        # fillet does. Computed only where the untrimmed bound would break the
        # certificate, a thin strip along tangent edges. A torus can have a
        # second interior minimum and keeps the untrimmed bound.
        for f, oi, d in outs:
            lb = d
            threat = d < best_d[oi] - TIE
            if f.kind != "torus" and np.any(threat) and len(f.edge3d):
                lb = d.copy()
                ti = np.nonzero(threat)[0]
                eb = _edge_dist(P[oi[ti]], f.edge3d) - f.edge_sag - TIE
                lb[ti] = np.maximum(d[ti], eb)
            bound[oi] = np.minimum(bound[oi], lb)
        certified = ((best_f >= 0) & (bound >= best_d - TIE)
                     & (opened >= best_d - TIE))
        for j in np.nonzero(~certified)[0]:
            self.stats["kernel"] += 1
            _d, q, u, v, fid, face = self.solid._nearest_exact(P[j])
            Q[j] = q
            N[j] = self.solid._normal(face, u, v)
            best_f[j] = fid
        # An EDGE foot: inside or outside is the solid's to say, not one face's
        # normal (just past a convex rim the flat face's normal points the
        # wrong way). Only edge rows reach the classifier.
        N = _boundary_normals(P, Q, N, lambda i: self._side(P[i]),
                              lateral=KERNEL_EDGE_LATERAL)
        return Q, N, best_f

    def _side(self, P):
        """+1 outside the solid (or on it), -1 inside, by the kernel's own
        classifier."""
        from OCP.BRepClass3d import BRepClass3d_SolidClassifier
        from OCP.TopAbs import TopAbs_IN
        from OCP.gp import gp_Pnt
        cls = getattr(self, "_classifier", None)
        if cls is None:
            cls = self._classifier = BRepClass3d_SolidClassifier(
                self.solid.shape)
        out = np.ones(len(P))
        for k, p in enumerate(np.asarray(P, dtype=np.float64)):
            cls.Perform(gp_Pnt(*map(float, p)), 1e-7)
            if cls.State() == TopAbs_IN:
                out[k] = -1.0
        return out

    # -- curvature ---------------------------------------------------------- #
    def curvature(self, Q, N, face):
        """`(k1, k2, d1, d2)` convex-positive against the outward normal `N`."""
        Q = np.asarray(Q, dtype=np.float64).reshape(-1, 3)
        N = np.asarray(N, dtype=np.float64).reshape(-1, 3)
        face = np.asarray(face).reshape(-1)
        n = len(Q)
        k1 = np.zeros(n)
        k2 = np.zeros(n)
        d1 = np.zeros((n, 3))
        d2 = np.zeros((n, 3))
        done = np.zeros(n, dtype=bool)
        for f in self.faces:
            idx = np.nonzero(face == f.fid)[0]
            if not len(idx) or f.kind is None:
                continue
            a1, a2, e1, e2 = f.curvature(Q[idx], f.sigma * N[idx])
            k1[idx], k2[idx] = f.sigma * a1, f.sigma * a2
            d1[idx], d2[idx] = e1, e2
            done[idx] = True
        for j in np.nonzero(~done)[0]:
            k1[j], k2[j], d1[j], d2[j] = self._kernel_curvature(Q[j], N[j])
        return k1, k2, d1, d2

    def _kernel_curvature(self, q, nrm):
        _d, _q, u, v, _fid, face = self.solid._nearest_exact(q)
        ref = np.array([1.0, 0.0, 0.0]) if abs(nrm[0]) < 0.9 else \
            np.array([0.0, 1.0, 0.0])
        a1 = np.cross(nrm, ref)
        a1 /= np.linalg.norm(a1)
        a2 = np.cross(nrm, a1)
        L, M, Pp = self.solid.second_form_on_face(face, u, v, a1, a2)
        w, vec = np.linalg.eigh(np.array([[L, M], [M, Pp]]))
        e1 = vec[0, 1] * a1 + vec[1, 1] * a2
        e2 = vec[0, 0] * a1 + vec[1, 0] * a2
        return w[1], w[0], e1, e2


def _edge_dist(P, segs):
    """Distance from each point to the nearest of `segs` (M, 6), chunked."""
    out = np.empty(len(P))
    a, b = segs[:, :3], segs[:, 3:]
    step = max(1, 4000000 // max(1, len(segs)))
    for i in range(0, len(P), step):
        out[i:i + step] = np.min(_seg_dist2(P[i:i + step], a, b,
                                            pairwise=True), axis=1)
    return out


def _box_gap(lo, hi, P):
    d = np.maximum(np.maximum(lo - P, 0.0), P - hi)
    return np.sqrt(np.sum(d * d, axis=1))
