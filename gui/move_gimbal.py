#!/usr/bin/env python3
"""move_gimbal.py -- MODERN-UI ONLY: Fusion's Move manipulator for a preview.

Three axis arrows (X red, Y green, Z blue), three plane squares (XY, YZ, ZX:
each tinted the colour of the axis it is normal to, as Fusion tints them) and
a centre ball, plus a blue RING about Z, drawn at a FIXED SIZE IN PIXELS
over the scene. Dragging an arrow slides along its axis; a square slides
freely in its plane; the ball slides in the screen's plane; the ring turns
about the vertical axis through the gimbal. The gimbal reports a WORLD delta
(dx, dy, dz, degrees about Z); what the delta moves is the caller's business
(the G-code page writes it into an offset edit, `gui_gcode_move`). The ring
can be switched off (`show(origin, ring=False)`: a 4-axis tube has no turn
about Z).

THE GEOMETRY IS PURE FUNCTIONS of numbers (`handle_shapes`, `hit_handle`,
`handle_point`), so the hit test and the drag maths are unit-tested with no
window (`test_move_gimbal.py`). The picking is analytic -- project the handles
to the screen, measure in pixels -- for the reason `view_cube.py` gives: a VTK
widget's pick geometry did not survive the offscreen -> native-window flip,
and a depth-buffer pick needs a rendered frame.

ALWAYS ON TOP. The gimbal is its own renderer at LAYER 1, which clears depth
over the whole window, so the scene can never hide it. That clear is the scar
`view_cube.py` warns about (a full-window layer-1 depth clear kills a
depth-read orbit pivot), so the panel that owns a gimbal re-renders WITHOUT
it before reading depth (`PreviewPanel._depth_render`). Its camera is a copy
of the scene's, pushed back along the view so its own clipping range always
holds the gimbal: under parallel projection that distance carries no image.
"""

import math

#: the arrows' length on screen, and how close (px) a press must land
GIMBAL_PX = 96.0
PICK_PX = 8.0
#: within this many px of an arrow the arrow wins over a square under it
ARROW_CORE_PX = 3.0
#: the shaft starts this far out (a fraction of the length), clear of the ball
ARROW_FROM = 0.24
#: each plane square spans this band of the length along both of its axes
PLANE_LO, PLANE_HI = 0.30, 0.52
#: the centre ball's radius as a fraction of the length
BALL = 0.075
#: the Z ring's radius as a fraction of the length, and its sampling
RING = 0.80
RING_N = 72

AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
#: a plane handle by name: its two in-plane axes and its normal
PLANES = {"xy": ("x", "y", "z"), "yz": ("y", "z", "x"), "zx": ("z", "x", "y")}
#: Fusion's manipulator colours
COLORS = {"x": (0.89, 0.24, 0.20), "y": (0.30, 0.71, 0.27),
          "z": (0.20, 0.46, 0.91)}
ACTIVE_RGB = (1.0, 0.82, 0.10)
BALL_RGB = (0.92, 0.92, 0.92)
HANDLES = ("center", "x", "y", "z", "xy", "yz", "zx", "rz")


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _mul(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def handle_shapes(origin, length):
    """The handles in WORLD coordinates: {"x": (shaft start, tip), ...,
    "xy": [four corners], ..., "center": origin}."""
    out = {"center": tuple(origin)}
    for k, a in AXES.items():
        out[k] = (_add(origin, _mul(a, ARROW_FROM * length)),
                  _add(origin, _mul(a, length)))
    r = RING * length
    out["rz"] = [_add(origin, (r * math.cos(2 * math.pi * i / RING_N),
                               r * math.sin(2 * math.pi * i / RING_N), 0.0))
                 for i in range(RING_N)]
    lo, hi = PLANE_LO * length, PLANE_HI * length
    for k, (u, v, _n) in PLANES.items():
        U, V = AXES[u], AXES[v]
        out[k] = [_add(origin, _add(_mul(U, a), _mul(V, b)))
                  for a, b in ((lo, lo), (hi, lo), (hi, hi), (lo, hi))]
    return out


def _seg_dist(p, a, b):
    """Pixel distance from 2-D point p to the segment a..b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, (
        (p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _inside(p, poly):
    """Even-odd point-in-polygon, 2-D."""
    hit = False
    n = len(poly)
    for i in range(n):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % n]
        if (y0 > p[1]) != (y1 > p[1]):
            x = x0 + (p[1] - y0) * (x1 - x0) / (y1 - y0)
            if x > p[0]:
                hit = not hit
    return hit


def _area(poly):
    a = 0.0
    for i in range(len(poly)):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % len(poly)]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2.0


def hit_handle(project, origin, length, x, y, pick_px=PICK_PX, ring=True):
    """The handle under display point (x, y), or None. `project` maps a
    world point to display pixels (any y direction, as long as x, y use the
    same one). The ball wins, then an arrow within `ARROW_CORE_PX`, then a
    plane square the point is inside, then the nearest arrow or ring
    (`ring`: drawn) within `pick_px`. An arrow, a square or the ring seen
    edge-on (shorter or thinner than a few pixels on screen) cannot be
    grabbed: dragging it would have no direction to follow."""
    shapes = handle_shapes(origin, length)
    p = (float(x), float(y))
    c = project(shapes["center"])
    tip_px = {}
    for k in AXES:
        q = project(shapes[k][1])
        tip_px[k] = math.hypot(q[0] - c[0], q[1] - c[1])
    ball_px = max(BALL * max(tip_px.values() or [0.0]), pick_px)
    if math.hypot(p[0] - c[0], p[1] - c[1]) <= ball_px:
        return "center"
    best = None
    for k in AXES:
        if tip_px[k] < 12.0:
            continue
        a, b = project(shapes[k][0]), project(shapes[k][1])
        d = _seg_dist(p, a, b)
        if d <= pick_px and (best is None or d < best[0]):
            best = (d, k)
    # an arrow drawn across a square (an oblique view) keeps only its own
    # few pixels: around it, the square under the point is meant
    if best is not None and best[0] <= ARROW_CORE_PX:
        return best[1]
    for k in PLANES:
        poly = [project(q) for q in shapes[k]]
        if _area(poly) < 30.0:
            continue
        if _inside(p, poly):
            return k
    if ring:
        poly = [project(q) for q in shapes["rz"]]
        if _area(poly) >= 400.0:
            d = min(_seg_dist(p, poly[i], poly[(i + 1) % len(poly)])
                    for i in range(len(poly)))
            if d <= pick_px and (best is None or d < best[0]):
                best = (d, "rz")
    return best[1] if best is not None else None


def handle_point(handle, origin, ray_o, ray_d, view_dir=None):
    """Where the ray (ray_o + s*ray_d) meets `handle`'s constraint, as a
    world point, or None when it cannot (an axis along the line of sight,
    a plane seen edge-on). The drag's delta is the difference of two of
    these: under an axis it lies along the axis, under a plane in the
    plane, under the ball in the screen's plane (normal `view_dir`)."""
    if handle in AXES:
        a = AXES[handle]
        w0 = _sub(origin, ray_o)
        b = _dot(a, ray_d)
        c = _dot(ray_d, ray_d)
        den = c - b * b                     # |a| = 1
        if den < 1e-9 * max(c, 1e-12):
            return None
        t = (b * _dot(ray_d, w0) - c * _dot(a, w0)) / den
        return _add(origin, _mul(a, t))
    if handle in PLANES:
        n = AXES[PLANES[handle][2]]
    elif handle == "rz":
        n = AXES["z"]
    elif handle == "center":
        if view_dir is None:
            return None
        n = view_dir
    else:
        return None
    nd = _dot(n, ray_d)
    if abs(nd) < 1e-6 * math.sqrt(max(_dot(ray_d, ray_d), 1e-24)):
        return None
    s = _dot(n, _sub(origin, ray_o)) / nd
    return _add(ray_o, _mul(ray_d, s))


def ring_angle(origin, p):
    """The angle (degrees, counter-clockwise from +X, seen from +Z) of
    world point `p` about the vertical axis through `origin`."""
    return math.degrees(math.atan2(p[1] - origin[1], p[0] - origin[0]))


def turn_delta(a0, a1):
    """The turn from angle a0 to a1 (degrees), the short way round:
    (-180, 180]. A drag adds these up event by event, so it can wind past
    a half turn."""
    d = (a1 - a0) % 360.0
    return d - 360.0 if d > 180.0 else d


def constrain(handle, delta):
    """A delta held to the handle's axis or plane (rounding noise off the
    plane removed); the ball's free delta is returned as it is."""
    if handle in AXES:
        a = AXES[handle]
        return _mul(a, _dot(delta, a))
    if handle in PLANES:
        n = AXES[PLANES[handle][2]]
        return _sub(delta, _mul(n, _dot(delta, n)))
    return tuple(delta)


# --------------------------------------------------------------------------- #
# the VTK side
# --------------------------------------------------------------------------- #
class MoveGimbal:
    """The manipulator's actors in their own layer-1 renderer over
    `scene` (the panel's renderer) in `window`. Hidden until `show(origin)`.
    `px` is its arrow length on screen, re-fitted on every scene render."""

    def __init__(self, window, scene, px=GIMBAL_PX):
        import vtk
        self._vtk = vtk
        self.window = window
        self.scene = scene
        self.px = float(px)
        self.origin = (0.0, 0.0, 0.0)
        self.length = 1.0
        self._on = False
        self._ring = True
        self._active = None
        self.ren = vtk.vtkRenderer()
        self.ren.SetLayer(1)
        self.ren.InteractiveOff()
        self.ren.SetActiveCamera(vtk.vtkCamera())
        self.ren.GetActiveCamera().ParallelProjectionOn()
        window.SetNumberOfLayers(max(2, window.GetNumberOfLayers()))
        window.AddRenderer(self.ren)
        self.actors = {}
        self._build()
        self._apply()
        self._tag = scene.AddObserver("StartEvent", self._sync)

    # -- construction --------------------------------------------------- #
    def _actor(self, src, rgb):
        vtk = self._vtk
        m = vtk.vtkPolyDataMapper()
        m.SetInputConnection(src.GetOutputPort())
        a = vtk.vtkActor()
        a.SetMapper(m)
        pr = a.GetProperty()
        pr.SetColor(*rgb)
        pr.SetAmbient(0.55)
        pr.SetDiffuse(0.5)
        pr.SetSpecular(0.0)
        self.ren.AddActor(a)
        return a

    def _build(self):
        """Each handle in UNIT space at the origin; `_place` scales and
        moves it. An arrow is VTK's +X arrow turned onto its axis."""
        vtk = self._vtk
        for k in AXES:
            src = vtk.vtkArrowSource()
            src.SetTipLength(0.22)
            src.SetTipRadius(0.055)
            src.SetShaftRadius(0.018)
            src.SetTipResolution(20)
            src.SetShaftResolution(12)
            xf = vtk.vtkTransform()
            if k == "y":
                xf.RotateZ(90.0)
            elif k == "z":
                xf.RotateY(-90.0)
            # the shaft starts at ARROW_FROM: squeeze the unit arrow onto
            # [ARROW_FROM, 1] along its own axis
            xf.Translate(ARROW_FROM, 0.0, 0.0)
            xf.Scale(1.0 - ARROW_FROM, 1.0, 1.0)
            tf = vtk.vtkTransformPolyDataFilter()
            tf.SetInputConnection(src.GetOutputPort())
            tf.SetTransform(xf)
            self.actors[k] = self._actor(tf, COLORS[k])
        for k, (u, v, n) in PLANES.items():
            src = vtk.vtkPlaneSource()
            U, V = AXES[u], AXES[v]
            src.SetOrigin(*_add(_mul(U, PLANE_LO), _mul(V, PLANE_LO)))
            src.SetPoint1(*_add(_mul(U, PLANE_HI), _mul(V, PLANE_LO)))
            src.SetPoint2(*_add(_mul(U, PLANE_LO), _mul(V, PLANE_HI)))
            a = self._actor(src, COLORS[n])
            a.GetProperty().SetLighting(False)
            self.actors[k] = a
        tor = vtk.vtkParametricTorus()
        tor.SetRingRadius(RING)
        tor.SetCrossSectionRadius(0.024)
        src = vtk.vtkParametricFunctionSource()
        src.SetParametricFunction(tor)
        src.SetUResolution(RING_N * 2)
        src.SetVResolution(8)
        self.actors["rz"] = self._actor(src, COLORS["z"])
        ball = vtk.vtkSphereSource()
        ball.SetRadius(BALL)
        ball.SetThetaResolution(20)
        ball.SetPhiResolution(14)
        self.actors["center"] = self._actor(ball, BALL_RGB)

    # -- state ---------------------------------------------------------- #
    def visible(self):
        return self._on

    def show(self, origin, ring=None):
        """Show it at `origin` (world), or hide it with None. `ring`: draw
        (and offer) the Z ring; None keeps it as it was."""
        if ring is not None:
            self._ring = bool(ring)
        if origin is None:
            self._on = False
        else:
            self._on = True
            self.origin = tuple(float(v) for v in origin)
        self._apply()
        self._place()

    def set_active(self, handle):
        """Light `handle` (the one being dragged) in yellow; None: none."""
        self._active = handle
        for k, a in self.actors.items():
            rgb = ACTIVE_RGB if k == handle else (
                BALL_RGB if k == "center" else
                COLORS[PLANES[k][2]] if k in PLANES else
                COLORS["z"] if k == "rz" else COLORS[k])
            a.GetProperty().SetColor(*rgb)

    def _apply(self):
        # A hidden gimbal must not be DRAWN at all: its layer-1 renderer
        # clears depth over the whole window even with every actor invisible,
        # so a click-to-select that reads depth after the Move or Subsection
        # tool closed found nothing under the pointer.
        self.ren.SetDraw(1 if self._on else 0)
        for k, a in self.actors.items():
            a.SetVisibility(1 if self._on and (self._ring or k != "rz")
                            else 0)

    # -- per-render fit --------------------------------------------------- #
    def _world_per_px(self):
        cam = self.scene.GetActiveCamera()
        h = max(self.window.GetSize()[1], 1)
        if not cam.GetParallelProjection():
            # perspective: the size of a pixel at the gimbal's own depth
            pos, fp = cam.GetPosition(), cam.GetFocalPoint()
            d = _sub(fp, pos)
            n = math.sqrt(_dot(d, d)) or 1.0
            depth = abs(_dot(_sub(self.origin, pos), d)) / n or n
            return 2.0 * depth * math.tan(
                math.radians(cam.GetViewAngle()) / 2.0) / h
        return 2.0 * cam.GetParallelScale() / h

    def _place(self):
        self.length = self.px * self._world_per_px()
        for a in self.actors.values():
            a.SetPosition(*self.origin)
            a.SetScale(self.length)

    def _sync(self, *_a):
        """Follow the scene's camera, at the scene's render (layer 0 draws
        first): the same direction, up and zoom, the gimbal kept its size in
        pixels, the camera backed off until the gimbal sits inside its own
        clipping range."""
        if not self._on:
            return
        self._place()
        src = self.scene.GetActiveCamera()
        dst = self.ren.GetActiveCamera()
        pos, fp = src.GetPosition(), src.GetFocalPoint()
        if not src.GetParallelProjection():
            # perspective (the Settings switch): the scene's very camera, or
            # the gimbal drifts off its parts away from the focal plane
            dst.ParallelProjectionOff()
            dst.SetViewAngle(src.GetViewAngle())
            dst.SetFocalPoint(*fp)
            dst.SetPosition(*pos)
            dst.SetViewUp(*src.GetViewUp())
            self.ren.ResetCameraClippingRange()
            return
        dst.ParallelProjectionOn()
        d = _sub(pos, fp)
        n = math.sqrt(_dot(d, d)) or 1.0
        far = max(n, 50.0 * self.length + math.sqrt(_dot(
            _sub(self.origin, fp), _sub(self.origin, fp))))
        dst.SetFocalPoint(*fp)
        dst.SetPosition(*_add(fp, _mul(d, far / n)))
        dst.SetViewUp(*src.GetViewUp())
        dst.SetParallelScale(src.GetParallelScale())
        self.ren.ResetCameraClippingRange()

    # -- picking (display coordinates, VTK's: y up) ----------------------- #
    def _project(self, p):
        r = self.scene
        r.SetWorldPoint(p[0], p[1], p[2], 1.0)
        r.WorldToDisplay()
        q = r.GetDisplayPoint()
        return (q[0], q[1])

    def ray(self, x, y):
        """Display point -> (origin, direction) of the line of sight, world."""
        r = self.scene

        def at(z):
            r.SetDisplayPoint(float(x), float(y), z)
            r.DisplayToWorld()
            w = r.GetWorldPoint()
            return (w[0] / w[3], w[1] / w[3], w[2] / w[3]) if w[3] \
                else tuple(w[:3])
        p0, p1 = at(0.0), at(1.0)
        return p0, _sub(p1, p0)

    def view_dir(self):
        cam = self.scene.GetActiveCamera()
        d = _sub(cam.GetFocalPoint(), cam.GetPosition())
        n = math.sqrt(_dot(d, d)) or 1.0
        return _mul(d, 1.0 / n)

    def hit(self, x, y):
        """The handle under display point (x, y), y up, or None."""
        if not self._on:
            return None
        self._place()
        return hit_handle(self._project, self.origin, self.length, x, y,
                          ring=self._ring)

    def point(self, handle, x, y):
        o, d = self.ray(x, y)
        return handle_point(handle, self.origin, o, d, self.view_dir())
