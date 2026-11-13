"""Convex subsection volumes and closed cuts of already-built fibre meshes.

Coordinates are millimetres in the same frame as the solved print. A section
never changes a centreline or asks the sag solver to run on a smaller print.
The clipping planes point inward, as required by vtkClipClosedSurface.
"""

import math

import numpy as np


def selected_time_spans(times, layers, first, last):
    """Per fibre, return selected clock intervals; None means the whole fibre.

    A fibre entirely in one selected layer stays whole. A continuous fibre
    that crosses selected and unselected layers is sliced at those boundaries.
    """
    if not layers or any("t0" not in row or "t1" not in row for row in layers):
        raise ValueError("The print's layer clock is not ready. Update the preview.")
    windows = []
    for row in layers[first - 1:last]:
        a, b = float(row["t0"]), float(row["t1"])
        if b <= a:
            continue
        if windows and a <= windows[-1][1] + 1e-9:
            windows[-1] = (windows[-1][0], max(b, windows[-1][1]))
        else:
            windows.append((a, b))
    result = []
    for t in times:
        if len(t) < 2:
            result.append([])
            continue
        a, b = float(t[0]), float(t[-1])
        eps = max(1e-8, (b - a) * 1e-6)
        occupied = [i for i, row in enumerate(layers)
                    if min(b, float(row["t1"])) - max(a, float(row["t0"])) > eps]
        if not occupied:
            # A lead-in or an old cache without a corresponding line window:
            # attach it to the closest layer rather than refusing the cut.
            mid = (a + b) / 2
            occupied = [min(range(len(layers)), key=lambda i:
                            min(abs(mid - float(layers[i]["t0"])),
                                abs(mid - float(layers[i]["t1"])) ))]
        selected = [i for i in occupied if first <= i + 1 <= last]
        if not selected:
            result.append([])
            continue
        if len(selected) == len(occupied):
            result.append([None])
            continue
        spans = [(max(a, lo), min(b, hi)) for lo, hi in windows
                 if min(b, hi) > max(a, lo)]
        result.append(spans)
    return result


def cut_polyline_time(points, times, lo, hi, values=None):
    """Interpolate a drawn centreline and optional per-point values at a cut."""
    p = np.asarray(points, dtype=np.float64)
    t = np.asarray(times, dtype=np.float64)
    lo, hi = max(float(lo), float(t[0])), min(float(hi), float(t[-1]))
    if hi <= lo:
        return None
    inside = (t > lo) & (t < hi)
    out_t = np.r_[lo, t[inside], hi]
    out_p = np.column_stack([np.interp(out_t, t, p[:, j]) for j in range(3)])
    out_values = None if values is None else np.interp(out_t, t, np.asarray(values))
    return out_p, out_t, out_values


def cut_mesh_time(vertices, triangles, ring_times, sides, lo, hi):
    """Cut the already-tubed full fibre at two print times and close its ends."""
    import vtk
    from vtk.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy

    vertices = np.asarray(vertices, dtype=np.float64)
    triangles = np.asarray(triangles, dtype=np.int64)
    times = np.asarray(ring_times, dtype=np.float64)
    scalars = np.r_[np.repeat(times, sides), times[0], times[-1]]
    if len(scalars) != len(vertices):
        raise ValueError("Tube ring times do not match its mesh.")
    points = vtk.vtkPoints()
    points.SetData(numpy_to_vtk(np.ascontiguousarray(vertices), deep=True))
    cells = vtk.vtkCellArray()
    offsets = np.arange(len(triangles) + 1, dtype=np.int64) * 3
    cells.SetData(numpy_to_vtkIdTypeArray(offsets, deep=True),
                  numpy_to_vtkIdTypeArray(np.ascontiguousarray(triangles.ravel()), deep=True))
    poly = vtk.vtkPolyData()
    poly.SetPoints(points)
    poly.SetPolys(cells)
    arr = numpy_to_vtk(scalars, deep=True)
    arr.SetName("subsection_time")
    poly.GetPointData().SetScalars(arr)
    for value, inside_out in ((float(lo), False), (float(hi), True)):
        clip = vtk.vtkClipPolyData()
        clip.SetInputData(poly)
        clip.SetValue(value)
        clip.SetInsideOut(inside_out)
        clip.Update()
        poly = clip.GetOutput()
        if not poly.GetNumberOfPolys():
            return np.empty((0, 3)), np.empty((0, 3), dtype=np.int64)
    tri = vtk.vtkTriangleFilter()
    tri.SetInputData(poly)
    tri.Update()
    result = tri.GetOutput()
    if not result.GetNumberOfPoints() or not result.GetNumberOfPolys():
        return np.empty((0, 3)), np.empty((0, 3), dtype=np.int64)
    out_v = vtk_to_numpy(result.GetPoints().GetData()).astype(np.float64, copy=True)
    out_t = vtk_to_numpy(result.GetPolys().GetConnectivityArray()).reshape(-1, 3).astype(np.int64, copy=True)
    # vtkFillHolesFilter drops collinear points when it triangulates a cut.
    # Those points still belong to the side triangles, leaving T-junctions
    # and open edges in the STL. Fan each boundary component to its centre
    # instead, preserving every directed side edge and reversing its winding.
    directed = np.concatenate((out_t[:, [0, 1]], out_t[:, [1, 2]], out_t[:, [2, 0]]))
    undirected = np.sort(directed, axis=1)
    _, inverse, counts = np.unique(undirected, axis=0, return_inverse=True, return_counts=True)
    boundary = directed[counts[inverse] == 1]
    if not len(boundary):
        return out_v, out_t
    parent = {}
    def root(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in boundary:
        ra, rb = root(int(a)), root(int(b))
        if ra != rb:
            parent[rb] = ra
    groups = {}
    for a, b in boundary:
        groups.setdefault(root(int(a)), []).append((int(a), int(b)))
    centres, caps = [], []
    for edges in groups.values():
        ids = sorted({x for edge in edges for x in edge})
        centre = len(out_v) + len(centres)
        centres.append(out_v[ids].mean(axis=0))
        caps.extend((b, a, centre) for a, b in edges)
    return np.concatenate((out_v, np.asarray(centres))), np.concatenate(
        (out_t, np.asarray(caps, dtype=np.int64)))


def outline_points(section):
    """The drawn outline as an (n, 2) array in millimetres about the section's
    centre. `outline_mm` is what the editor writes (the same millimetre
    graph as the Footprint tool's custom outline). A section saved before it
    existed carries `outline` in units of its X/Y size; that is scaled here,
    so an old preset or Print History entry still cuts the same shape."""
    if section.get("outline_mm") is not None:
        p = np.asarray(section["outline_mm"], dtype=np.float64)
    else:
        sx, sy = map(float, section["size"][:2])
        p = np.asarray(section.get("outline", ()), dtype=np.float64)
        if p.ndim == 2 and p.shape[1:] == (2,):
            p = p * np.array([sx, sy])
    if p.ndim != 2 or p.shape[1:] != (2,) or len(p) < 3:
        raise ValueError("Draw an outline with at least three points.")
    if not np.isfinite(p).all():
        raise ValueError("Outline points must be finite.")
    return p


def _outline(section):
    """Return a counter-clockwise convex outline in local XY coordinates."""
    shape = section["shape"]
    sx, sy = map(float, section["size"][:2])
    if shape == "box":
        return np.array([[-sx / 2, -sy / 2], [sx / 2, -sy / 2],
                         [sx / 2, sy / 2], [-sx / 2, sy / 2]])
    if shape == "circle":
        a = np.arange(96) * (2 * math.pi / 96)
        return np.column_stack((np.cos(a) * sx / 2, np.sin(a) * sy / 2))
    if shape != "outline":
        raise ValueError("Unknown subsection shape: %s" % shape)
    p = outline_points(section)
    edges = np.roll(p, -1, axis=0) - p
    cross = edges[:, 0] * np.roll(edges[:, 1], -1) - edges[:, 1] * np.roll(edges[:, 0], -1)
    if np.any(np.linalg.norm(edges, axis=1) < 1e-12) or not (
            np.all(cross > 1e-12) or np.all(cross < -1e-12)):
        raise ValueError("The outline must be a convex polygon without repeated points.")
    p = p if np.all(cross > 0) else p[::-1]
    for a, b in zip(p, np.roll(p, -1, axis=0)):
        e, delta = b - a, p - a
        if np.any(e[0] * delta[:, 1] - e[1] * delta[:, 0] < -1e-12):
            raise ValueError("The outline must be a convex polygon without repeated points.")
    return p


#: a snapped section may reach this fraction of the way to the far side of the
#: tube (half the circumference): past it the patch meets its own far edge
_SNAP_REACH = 0.9


def is_snapped(section):
    """Whether the section is wrapped onto the mandrel's surface (the
    designer's "Snap to surface"). It needs the tube's radius, which only a
    4-axis design supplies, so a snapped section carried onto a flat print
    is simply the free box it was."""
    return (bool(section.get("snap_surface"))
            and bool(section.get("mandrel_radius"))
            and section.get("shape") != "sphere")


def snap_frame(section):
    """`(radius, phi0, flat)` for a snapped section.

    The patch is the section's footprint laid on the surface of the mandrel:
    its X runs along the rod and its Y around it, in millimetres of arc at
    the mandrel's radius, centred where the box's centre points (`phi0`, the
    angle of the centre about the rod, from +Z towards +Y). `flat` is the
    same section in the UNROLLED frame (x along the rod, s = radius * angle,
    w = distance from the rod's axis), where it is the ordinary convex prism:
    its Z is the radial depth about the centre's own distance from the axis,
    or every layer when a layer selection replaces the height."""
    radius = float(section["mandrel_radius"])
    cx, cy, cz = (float(v) for v in section["center"])
    flat = dict(section, center=[cx, 0.0, math.hypot(cy, cz)],
                snap_surface=False)
    flat.pop("mandrel_radius", None)
    return radius, math.atan2(cy, cz), flat


def clipping_planes(section):
    """Return inward normals and points defining a closed convex subsection.

    A snapped section (`is_snapped`) returns the planes of its UNROLLED frame
    (`snap_frame`), and only validates it: nothing clips a mesh with them.

    An outline uses points in units of the requested X/Y size, centred on the
    origin. A circle and sphere are polygonal at the clipping resolution.
    Layer selection omits flat Z planes; a sphere retains its curved boundary.
    """
    center = np.asarray(section["center"], dtype=np.float64)
    size = np.asarray(section["size"], dtype=np.float64)
    if center.shape != (3,) or size.shape != (3,) or not (
            np.isfinite(center).all() and np.isfinite(size).all()
            and np.all(size > 0)):
        raise ValueError("Subsection centre and positive size must be finite.")
    angle = math.radians(float(section.get("rotation", 0)))
    if not math.isfinite(angle):
        raise ValueError("Rotation must be finite.")
    c, s = math.cos(angle), math.sin(angle)
    rotation = np.array([[c, -s], [s, c]])
    if is_snapped(section):
        radius, _phi0, flat = snap_frame(section)
        around = float(np.abs((_outline(section) @ rotation.T)[:, 1]).max())
        if around > _SNAP_REACH * math.pi * radius:
            raise ValueError(
                "A snapped subsection can reach at most %.0f%% of the way "
                "round the tube from its centre (%.1f mm of arc). Make it "
                "narrower, or turn off Snap to surface."
                % (100 * _SNAP_REACH, _SNAP_REACH * math.pi * radius))
        return clipping_planes(flat)
    normals, points = [], []
    if section["shape"] == "sphere":
        if not np.allclose(size, size[0], rtol=1e-9, atol=1e-12):
            raise ValueError("A sphere needs the same X, Y and Z diameter.")
        # Evenly distributed tangent planes; the result is a closed faceted
        # sphere, with the same geometric meaning at every orientation.
        n = 162
        k = np.arange(n, dtype=np.float64)
        z = 1.0 - 2.0 * (k + 0.5) / n
        a = math.pi * (3.0 - math.sqrt(5.0)) * k
        radial = np.sqrt(1.0 - z * z)
        outward = np.column_stack((radial * np.cos(a), radial * np.sin(a), z))
        normals.extend(-outward)
        points.extend(center + outward * (size[0] / 2.0))
    else:
        polygon = _outline(section) @ rotation.T + center[:2]
        for p, q in zip(polygon, np.roll(polygon, -1, axis=0)):
            edge = q - p
            normal = np.array([-edge[1], edge[0], 0.0])
            normal /= np.linalg.norm(normal)
            normals.append(normal)
            points.append(np.array([p[0], p[1], center[2]]))
        if section.get("z_mode", "height") == "height":
            normals += [np.array([0., 0., 1.]), np.array([0., 0., -1.])]
            points += [center - [0., 0., size[2] / 2],
                       center + [0., 0., size[2] / 2]]
        elif section.get("mandrel_radius"):
            # A layer-selected XY prism otherwise selects both the near and
            # far sides of a tube. Keep the side on which the user placed a
            # narrow section; the plane through the axis misses the surface
            # throughout that prism, so it does not make a height cut in a ply.
            radial = np.array([0., center[1], center[2]], dtype=np.float64)
            projected_y = abs(s) * size[0] + abs(c) * size[1]
            if np.linalg.norm(radial) > 1e-12 and projected_y < 2 * float(section["mandrel_radius"]):
                normals.append(radial / np.linalg.norm(radial))
                points.append(np.array([center[0], 0., 0.]))
    return np.asarray(normals), np.asarray(points)


class ClosedClipper:
    """Clip each closed fibre separately so overlapping fibres stay separate."""

    def __init__(self, section):
        import vtk
        if is_snapped(section):
            raise ValueError("A snapped subsection is cut by time spans "
                             "(`snapped_spans`), not by planes.")
        self.vtk = vtk
        self.normals, self.points = clipping_planes(section)
        self.planes = vtk.vtkPlaneCollection()
        for normal, point in zip(self.normals, self.points):
            plane = vtk.vtkPlane()
            plane.SetNormal(*normal)
            plane.SetOrigin(*point)
            self.planes.AddItem(plane)

    def intersects_bounds(self, lo, hi):
        """Whether a bounding box can meet the section's half-spaces."""
        far = np.where(self.normals >= 0, hi, lo)
        return not np.any(np.sum(self.normals * (far - self.points), axis=1) < 0)

    def clip(self, vertices, triangles):
        from vtk.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy

        vertices = np.asarray(vertices, dtype=np.float64)
        triangles = np.asarray(triangles, dtype=np.int64)
        if not len(vertices) or not len(triangles):
            return vertices, triangles
        # Bounding-box support points cheaply reject whole fibres without a VTK
        # conversion. A fully enclosed fibre remains byte-for-byte unchanged.
        lo, hi = vertices.min(axis=0), vertices.max(axis=0)
        near = np.where(self.normals >= 0, lo, hi)
        far = np.where(self.normals >= 0, hi, lo)
        d = np.sum(self.normals * self.points, axis=1)
        if np.any(np.sum(self.normals * far, axis=1) < d):
            return np.empty((0, 3)), np.empty((0, 3), dtype=np.int64)
        if np.all(np.sum(self.normals * near, axis=1) >= d):
            return vertices, triangles
        vtk = self.vtk
        points = vtk.vtkPoints()
        points.SetData(numpy_to_vtk(np.ascontiguousarray(vertices), deep=True))
        cells = vtk.vtkCellArray()
        offsets = np.arange(len(triangles) + 1, dtype=np.int64) * 3
        cells.SetData(numpy_to_vtkIdTypeArray(offsets, deep=True),
                      numpy_to_vtkIdTypeArray(np.ascontiguousarray(triangles.ravel()), deep=True))
        poly = vtk.vtkPolyData()
        poly.SetPoints(points)
        poly.SetPolys(cells)
        clip = vtk.vtkClipClosedSurface()
        clip.SetInputData(poly)
        clip.SetClippingPlanes(self.planes)
        clip.GenerateFacesOn()
        clip.Update()
        tri = vtk.vtkTriangleFilter()
        tri.SetInputConnection(clip.GetOutputPort())
        tri.Update()
        result = tri.GetOutput()
        if not result.GetNumberOfPoints() or not result.GetNumberOfPolys():
            return np.empty((0, 3)), np.empty((0, 3), dtype=np.int64)
        out_v = vtk_to_numpy(result.GetPoints().GetData()).astype(np.float64, copy=True)
        out_t = vtk_to_numpy(result.GetPolys().GetConnectivityArray()).reshape(-1, 3).astype(np.int64, copy=True)
        return out_v, out_t


# --------------------------------------------------------------------------- #
# The preview's cut: fibre CENTRELINES, clipped in numpy.
#
# The Design view never clips a mesh. It clips the drawn centrelines (the
# preview's `paths`, with their print clock and radii) against the same convex
# volume the export clips the tubes with, then draws what is left as ordinary
# fibres: so a clipped picture takes colours, clicks, the line clock and the
# tube toggle exactly as the whole one does, and a section of a million-point
# print costs a few array passes instead of one mesh clip per plane.
# A sphere is clipped by its own equation, not by its 162 facets.
# --------------------------------------------------------------------------- #
_EPS = 1e-9


def _check_section(section):
    """The finite, positive centre/size and rotation every cut needs."""
    center = np.asarray(section["center"], dtype=np.float64)
    size = np.asarray(section["size"], dtype=np.float64)
    if center.shape != (3,) or size.shape != (3,) or not (
            np.isfinite(center).all() and np.isfinite(size).all()
            and np.all(size > 0)):
        raise ValueError("Subsection centre and positive size must be finite.")
    if not math.isfinite(float(section.get("rotation", 0))):
        raise ValueError("Rotation must be finite.")
    return center, size


def section_aabb(section):
    """A box (lo, hi) holding the section, infinite along an axis the shape
    does not bound (a layer selection has no flat Z limits). Conservative:
    it only ever lets a segment through to the exact test."""
    center, size = _check_section(section)
    if section["shape"] == "sphere":
        r = size[0] / 2.0
        return center - r, center + r
    a = math.radians(float(section.get("rotation", 0)))
    c, s = math.cos(a), math.sin(a)
    poly = _outline(section) @ np.array([[c, -s], [s, c]]).T + center[:2]
    lo = np.r_[poly.min(axis=0), -np.inf]
    hi = np.r_[poly.max(axis=0), np.inf]
    if section.get("z_mode", "height") == "height":
        lo[2], hi[2] = center[2] - size[2] / 2, center[2] + size[2] / 2
    return lo, hi


def _segment_span(a, b, section, aabb=None):
    """(lo, hi) per segment a->b: the stretch t in [0, 1] inside the section's
    convex volume; hi <= lo where the segment misses it. Segments clear of
    the section's bounding box are dropped before the exact test."""
    n = len(a)
    if aabb is not None and n:
        box_lo, box_hi = aabb
        keep = np.flatnonzero(
            np.all(np.maximum(a, b) >= box_lo - 1e-7, axis=1)
            & np.all(np.minimum(a, b) <= box_hi + 1e-7, axis=1))
        lo, hi = np.zeros(n), np.full(n, -1.0)
        if len(keep):
            lo[keep], hi[keep] = _segment_span(a[keep], b[keep], section)
        return lo, hi
    lo, hi = np.zeros(n), np.ones(n)
    if section["shape"] == "sphere":
        center, size = _check_section(section)
        if not np.allclose(size, size[0], rtol=1e-9, atol=1e-12):
            raise ValueError("A sphere needs the same X, Y and Z diameter.")
        r = size[0] / 2.0
        d = b - a
        f = a - center
        qa = np.einsum("ij,ij->i", d, d)
        qb = 2.0 * np.einsum("ij,ij->i", f, d)
        qc = np.einsum("ij,ij->i", f, f) - r * r
        flat = qa < 1e-24                      # a point: inside or out whole
        disc = qb * qb - 4.0 * qa * qc
        miss = np.where(flat, qc > 0, disc < 0)
        root = np.sqrt(np.maximum(disc, 0.0))
        with np.errstate(divide="ignore", invalid="ignore"):
            t0 = (-qb - root) / (2.0 * qa)
            t1 = (-qb + root) / (2.0 * qa)
        t0 = np.where(flat, 0.0, t0)
        t1 = np.where(flat, 1.0, t1)
        lo, hi = np.maximum(lo, t0), np.minimum(hi, t1)
        hi = np.where(miss, -1.0, hi)
        return lo, hi
    normals, points = clipping_planes(section)
    c = np.sum(normals * points, axis=1)
    da = a @ normals.T - c                     # (n, planes), >= 0 is inside
    db = b @ normals.T - c
    ina, inb = da >= -_EPS, db >= -_EPS
    out = ~ina & ~inb
    with np.errstate(divide="ignore", invalid="ignore"):
        enter = np.where(~ina & inb, -da / (db - da), 0.0)
        leave = np.where(ina & ~inb, da / (da - db), 1.0)
    lo = np.maximum(lo, enter.max(axis=1))
    hi = np.minimum(hi, leave.min(axis=1))
    hi = np.where(out.any(axis=1), -1.0, hi)
    return lo, hi


def clip_polyline(points, section, times=None, values=None, aabb=None):
    """The pieces of one drawn centreline inside the section, each as
    (points, times, values) with `times` and `values` interpolated at a cut
    (None when not given). A fibre wholly inside comes back as itself."""
    if is_snapped(section):
        return _clip_snapped(points, section, times, values)
    p = np.asarray(points, dtype=np.float64)
    if len(p) < 2:
        return []
    lo, hi = _segment_span(p[:-1], p[1:], section, aabb)
    ok = hi - lo > 1e-12
    if not ok.any():
        return []
    idx = np.flatnonzero(ok)
    # a run of kept segments continues while each one starts where the last
    # ended: the next index, the last one cut at its far end only by a plane
    # it did not cross, and this one cut at its near end by none
    brk = np.flatnonzero((np.diff(idx) != 1) | (hi[idx[:-1]] < 1 - 1e-12)
                         | (lo[idx[1:]] > 1e-12)) + 1
    t = None if times is None else np.asarray(times, dtype=np.float64)
    v = None if values is None else np.asarray(values, dtype=np.float64)

    def at(arr, seg, u):
        return arr[seg] + u * (arr[seg + 1] - arr[seg])

    pieces = []
    for run in np.split(idx, brk):
        s, e = int(run[0]), int(run[-1])
        head = at(p, s, lo[s])
        tail = at(p, e, hi[e])
        pts = np.vstack((head[None], p[s + 1:e + 1], tail[None]))
        pt = None if t is None else np.concatenate(
            ([at(t, s, lo[s])], t[s + 1:e + 1], [at(t, e, hi[e])]))
        pv = None if v is None else np.concatenate(
            ([at(v, s, lo[s])], v[s + 1:e + 1], [at(v, e, hi[e])]))
        pieces.append((pts, pt, pv))
    return pieces


def unroll(points, section):
    """Points (n, 3) in the rod's frame -> the snapped section's UNROLLED
    frame (`snap_frame`): x along the rod, s = radius * the angle from the
    section's centre (in (-pi, pi] of arc), w = distance from the axis."""
    radius, phi0, _flat = snap_frame(section)
    p = np.asarray(points, dtype=np.float64)
    dphi = (np.arctan2(p[:, 1], p[:, 2]) - phi0 + math.pi) % (2 * math.pi) - math.pi
    return np.column_stack((p[:, 0], radius * dphi, np.hypot(p[:, 1], p[:, 2])))


def roll(points, section):
    """The inverse of `unroll`."""
    radius, phi0, _flat = snap_frame(section)
    q = np.asarray(points, dtype=np.float64)
    phi = q[:, 1] / radius + phi0
    return np.column_stack((q[:, 0], q[:, 2] * np.sin(phi), q[:, 2] * np.cos(phi)))


def _clip_snapped(points, section, times=None, values=None):
    """`clip_polyline` for a snapped section: the centreline is unrolled, cut
    by the flat prism, and its pieces rolled back onto the rod. The unrolled
    angle jumps by a whole turn where the fibre crosses the far side of the
    tube; that one segment is dropped, which is why a snapped section may not
    reach there (`clipping_planes`)."""
    p = np.asarray(points, dtype=np.float64)
    if len(p) < 2:
        return []
    radius, _phi0, flat = snap_frame(section)
    u = unroll(p, section)
    t = None if times is None else np.asarray(times, dtype=np.float64)
    v = None if values is None else np.asarray(values, dtype=np.float64)
    breaks = np.flatnonzero(np.abs(np.diff(u[:, 1])) > math.pi * radius) + 1
    aabb = section_aabb(flat)
    out = []
    for run in np.split(np.arange(len(p)), breaks):
        if len(run) < 2:
            continue
        for q, qt, qv in clip_polyline(
                u[run], flat, None if t is None else t[run],
                None if v is None else v[run], aabb):
            out.append((roll(q, section), qt, qv))
    return out


def snapped_spans(section, path, times, windows=(None,)):
    """The print-clock intervals of one fibre that lie inside a snapped
    section: the export cuts the fibre's tube at these (`cut_mesh_time`), the
    same way it cuts a layer selection. `windows` are the layer selection's
    clock windows for this fibre (None = the whole fibre)."""
    spans = []
    for window in windows:
        if window is None:
            pts, t = np.asarray(path, np.float64), np.asarray(times, np.float64)
        else:
            cut = cut_polyline_time(path, times, *window)
            if cut is None:
                continue
            pts, t = cut[0], cut[1]
        for _q, qt, _v in _clip_snapped(pts, section, t):
            if qt[-1] - qt[0] > 1e-12:
                spans.append((float(qt[0]), float(qt[-1])))
    return spans


def section_pieces(section, paths, ptimes=None, radii=None, layers=None,
                   command_times=None):
    """Cut a whole picture to one section: (paths, ptimes, radii) lists of the
    pieces of every fibre inside it, in print order. `layers` (the print-order
    layer table with its t0/t1 clock) is read only for a layer selection,
    which keeps whole the fibres of the chosen layers and cuts a continuous
    fibre at the boundaries of their clock (`selected_time_spans`)."""
    n = len(paths)
    have_t = ptimes is not None and len(ptimes) == n
    have_r = radii is not None and len(radii) == n
    spans = None
    if section.get("z_mode") == "layers":
        if not have_t:
            raise ValueError("Layer selection needs a current print clock.")
        clock = (command_times if command_times is not None
                 and len(command_times) == n else ptimes)
        spans = selected_time_spans(clock, layers, int(section["layer_start"]),
                                    int(section["layer_end"]))
    out_p, out_t, out_r = [], [], []
    aabb = section_aabb(section)
    for i, path in enumerate(paths):
        t = ptimes[i] if have_t else None
        r = radii[i] if have_r else None
        if spans is None:
            parts = [(np.asarray(path, np.float64), t, r)]
        else:
            parts = []
            for window in spans[i]:
                if window is None:
                    parts.append((np.asarray(path, np.float64), t, r))
                    continue
                cut = cut_polyline_time(path, t, *window, values=r)
                if cut is not None:
                    parts.append((cut[0], cut[1], cut[2]))
        for pts, pt, pr in parts:
            for q, qt, qr in clip_polyline(pts, section, pt, pr, aabb):
                out_p.append(q)
                if have_t:
                    out_t.append(qt)
                if have_r:
                    out_r.append(qr)
    return out_p, (out_t if have_t else None), (out_r if have_r else None)
