#!/usr/bin/env python3
"""view_cube.py -- MODERN-UI ONLY: a Fusion-style ViewCube gizmo for the VTK
viewers (gcode_player.py, stl_viewer.py, micrograph_viewer.py).

A labelled cube in the upper-right corner that co-rotates with the camera;
clicking a FACE, EDGE or CORNER snaps the view to look down that direction. Its
RGB axis triad rides the cube's back-lower-left corner, which is why there is
no separate vtkOrientationMarkerWidget.

SCENE-SIDE, NOT CHROME-SIDE. Embedded in a modern-shell page there IS no
`ViewerChrome` -- the child builds a `viewer_embed.EmbedChrome`, which draws
nothing and only exports a control spec to a ttk rail -- so a chrome-owned cube
would show in native fullscreen and vanish in the embedded panel, the DEFAULT
UI. As a renderer in the viewer child's own window it covers embedded, native
fullscreen and the classic windowed launch at once, because all three are the
same window. Only the show/hide TOGGLE is chrome-side (one `chrome.button`),
which fans it out to the ttk rail, the VTK sidebar and fullscreen for free.

THE HIT-TEST IS HAND-ROLLED; do not "simplify" it to `vtkCameraOrientationWidget`.
VTK widget pick geometry did not survive the offscreen -> native-fullscreen flip
(`ComputeInteractionState` returned Outside regardless of renderer origin), which
is why `ViewerChrome`'s sliders already hit-test by hand. The pick is analytic:
display point -> ray via the cube renderer's `DisplayToWorld` (pure matrix math,
valid offscreen), then a slab test. Deliberately NOT `renderer.GetZ` or
`vtkPicker` -- those read the GL depth buffer, which needs a rendered frame. The
geometry below is therefore pure functions of numbers, unit-testable headlessly.

Two layer scars, both still live:
* The cube renderer sits at LAYER 1, the layer the chrome overlay and the old
  orientation marker use, so `SetNumberOfLayers(2)` in `ViewerChrome` and
  `viewer_embed._teardown_fs_chrome`'s max-aware recount stay correct unedited.
* It CLEARS the depth buffer (the default above layer 0), which is safe ONLY
  because its viewport is a small upper-right box and the clear is scissored to
  it. Do NOT give this renderer a full-window viewport: a layer-1 renderer
  clearing depth across the whole window is what silently killed
  `install_cad_camera`'s orbit pivot.
"""

import math

import vtk

from vtk_helpers import fit_clipping_range, origin_marker

# The themed palette, if the modern shell is importable. Guarded exactly like
# the viewers' own _shell import so a hand-launched viewer still works.
try:
    import gui_viewer_shell as _shell
except Exception:  # noqa: BLE001
    _shell = None


# -- geometry constants --------------------------------------------------- #

# vtkAnnotatedCubeActor is a unit cube -> it spans [-0.5, 0.5] on every axis.
HALF = 0.5

# A click inside the middle |q| < EDGE_FRAC of a face reads as that FACE; the
# outer band reads as the EDGE / CORNER it borders, so the cube exposes the
# usual 6 + 12 + 8 = 26 snap directions. 0.62 leaves a comfortably large face
# target (the common case) while keeping the corners easy to hit.
EDGE_FRAC = 0.62

# The gizmo's on-screen size, in PIXELS -- deliberately not a viewport fraction.
# The old orientation marker used a fraction (0.80..1.0 of the width), so it
# ballooned in native fullscreen on a large monitor. The viewport is recomputed
# from the live window size on every sync, which also covers the embedded
# panel's offscreen resizes (they raise no ConfigureEvent).
VIEW_PX = 128
MARGIN_PX = 8

# Camera framing for the gizmo's own renderer. _SCALE is the half-height of the
# gizmo view in cube units: a cube seen corner-on has a silhouette ~0.87 units
# tall, so 1.05 fills most of the box while still clearing the triad that hangs
# off the far corner. Under parallel projection _DIST carries no image
# information -- it only has to keep the cube in front of the camera.
_SCALE = 1.05
_DIST = 10.0

# The axis triad, anchored just OUTSIDE the cube's (-,-,-) corner so its arms
# run parallel to the cube edges instead of z-fighting along them.
_AXES_ORIGIN = -HALF * 1.15
_AXES_LEN = 0.42

# The six faces, as (label, outward normal, view-up when looking AT that face).
# The side faces stand upright on world +Z (a CAD cube never tips the horizon);
# TOP/BOTTOM take +Y, which is what makes a TOP click land on exactly the view
# gcode_player._snap_top produces. See _face_matrix for the third axis.
_FACES = (
    ("RIGHT",  (1, 0, 0),  (0, 0, 1)),
    ("LEFT",   (-1, 0, 0), (0, 0, 1)),
    ("BACK",   (0, 1, 0),  (0, 0, 1)),
    ("FRONT",  (0, -1, 0), (0, 0, 1)),
    ("TOP",    (0, 0, 1),  (0, 1, 0)),
    ("BOTTOM", (0, 0, -1), (0, 1, 0)),
)

# Label rendering. The font size is only the TEXTURE resolution vtkTextActor3D
# rasterizes at (the on-screen size comes from _fit_scale), so it is set high
# enough that the labels stay crisp when the gizmo is drawn large in native
# fullscreen on a hi-dpi monitor.
_LABEL_FONT_PX = 48
_LABEL_W_FRAC = 0.74      # the widest label spans this much of a face
_LABEL_H_FRAC = 0.34
# How far proud of its face each label floats. This is NOT cosmetic and must not
# be trimmed back: at 0.004 the cube's own top face WON the depth test against
# the "TOP" label and swallowed it whole (snapping to any of the four top
# corners showed a blank top face). It was never a culling,
# minification or foreshortening problem: drawn alone the label inked fine, and
# adding the faces took it to zero. The failure is size-dependent (gone by
# ~160 px, hit at the real 128 px), so an "it looks fine here" eyeball at any one
# size proves nothing; check the real VIEW_PX at every direction instead.
# 0.012 sits mid-plateau (0.008
# through 0.05 all render identically) and costs well under a pixel of parallax
# at gizmo scale.
_LABEL_LIFT = 0.012


def _mix(a, b, t):
    """Blend two rgb triples: t=0 -> a, t=1 -> b."""
    return tuple(a[i] * (1.0 - t) + b[i] * t for i in range(3))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _text_extent(actor):
    """The (width, height) of a vtkTextActor3D's text in its own local units."""
    b = actor.GetBounds()
    if b is None or b[0] > b[1]:
        return (0.0, 0.0)
    return (b[1] - b[0], b[3] - b[2])


def _fit_scale(actors, w_frac=_LABEL_W_FRAC, h_frac=_LABEL_H_FRAC):
    """ONE scale for every label, sized so the WIDEST ("BOTTOM") still fits.

    Uniform by construction: scaling each label to its own face independently
    would render "TOP" in bigger type than "BOTTOM", which no real view cube
    does. Falls back to a harmless 1.0 if the text extents aren't available."""
    face = 2 * HALF
    best = math.inf
    for a in actors:
        w, h = _text_extent(a)
        if w > 1e-9:
            best = min(best, w_frac * face / w)
        if h > 1e-9:
            best = min(best, h_frac * face / h)
    return best if best < math.inf else 1.0


def _face_matrix(actor, normal, up, s):
    """Place a label flat on `normal`'s face, upright along `up`, scaled by `s`.

    The label's local frame becomes (right, up, normal) with `normal` pointing
    OUT of the cube -- vtkTextActor3D draws text in its local XY readable from
    +Z, so an outward normal is exactly what makes each face read right-way-up
    from outside. right = up x normal keeps that frame right-handed."""
    right = _cross(up, normal)
    b = actor.GetBounds()
    # the text's own local centre, so a label centres on its face regardless of
    # where vtkTextActor3D happened to anchor the quad
    cx = (b[0] + b[1]) * 0.5 if b and b[0] <= b[1] else 0.0
    cy = (b[2] + b[3]) * 0.5 if b and b[2] <= b[3] else 0.0

    m = vtk.vtkMatrix4x4()
    m.Identity()
    for i in range(3):
        m.SetElement(i, 0, right[i] * s)
        m.SetElement(i, 1, up[i] * s)
        m.SetElement(i, 2, normal[i] * s)
        m.SetElement(i, 3, normal[i] * (HALF + _LABEL_LIFT)
                     - (right[i] * s * cx + up[i] * s * cy))
    return m


# -- pure geometry (no vtk -- unit-tested headlessly) --------------------- #

def ray_box(origin, direction, half=HALF):
    """Slab-test a ray against the axis-aligned cube [-half, half]^3.

    Returns the parameter t of the point where the ray FIRST meets the cube
    (the entry point, or the exit when the origin is already inside), or None
    when it misses / the cube lies entirely behind the origin."""
    tmin, tmax = -math.inf, math.inf
    for i in range(3):
        o, d = origin[i], direction[i]
        if abs(d) < 1e-12:
            # parallel to this slab: a miss unless the origin is between planes
            if abs(o) > half:
                return None
            continue
        t1 = (-half - o) / d
        t2 = (half - o) / d
        if t1 > t2:
            t1, t2 = t2, t1
        tmin = max(tmin, t1)
        tmax = min(tmax, t2)
        if tmin > tmax:
            return None
    if tmin > 0.0:
        return tmin
    return tmax if tmax > 0.0 else None


def cube_region(point, half=HALF, edge_frac=EDGE_FRAC):
    """A point ON the cube surface -> the snap direction it selects, as a unit-ish
    (dx, dy, dz) of components in {-1, 0, 1}.

    Exactly one component is non-zero for a FACE, two for an EDGE, three for a
    CORNER. The dominant axis (the face actually struck) always contributes, so
    a hit is never classified as the zero vector."""
    q = [point[i] / half for i in range(3)]
    face = max(range(3), key=lambda i: abs(q[i]))     # the face we struck
    out = [0, 0, 0]
    for i in range(3):
        if i == face or abs(q[i]) >= edge_frac:
            out[i] = 1 if q[i] >= 0.0 else -1
    return tuple(out)


def snap_view_up(direction):
    """The view-up to pair with a snap along `direction`.

    World +Z is up in these scenes, so it is the natural choice for every view
    that is not straight down/up the Z axis. For the pure top/bottom views Z is
    degenerate (parallel to the view), so fall back to +Y -- which also makes a
    TOP-face click reproduce gcode_player._snap_top exactly."""
    if direction[0] == 0 and direction[1] == 0:
        return (0.0, 1.0, 0.0)
    return (0.0, 0.0, 1.0)


def _normalize(v):
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / n, v[1] / n, v[2] / n) if n > 1e-12 else (0.0, 0.0, 1.0)


def viewport_rect(width, height, size_px=VIEW_PX, margin_px=MARGIN_PX):
    """The gizmo's upper-right viewport as VTK normalized coords
    (xmin, ymin, xmax, ymax). VTK's origin is bottom-left, so "upper" is the
    high-y end. Degenerate window sizes clamp instead of dividing by zero."""
    w = max(1, int(width))
    h = max(1, int(height))
    size = max(1, min(int(size_px), w, h))
    m = max(0, int(margin_px))
    # the margin has to yield to the window, not the other way round: on a
    # window narrower than VIEW_PX + MARGIN_PX a raw (w - m) went NEGATIVE and
    # handed VTK an inverted viewport
    x1 = min(w, max(size, w - m))
    y1 = min(h, max(size, h - m))
    return ((x1 - size) / w, (y1 - size) / h, x1 / w, y1 / h)


# -- the gizmo ------------------------------------------------------------ #

class ViewCube:
    """The upper-right ViewCube. Construct it once, after the interactor exists
    and install_cad_camera has run (priority ordering does the rest).

    `reframe` is the viewer's own camera framing, run after a snap sets the
    direction -- the same division of labour gcode_player._snap_top already
    used (it only sets direction + view-up and lets _frame_camera re-fit the
    zoom and clipping). Defaults to a plain ResetCamera + fit_clipping_range."""

    def __init__(self, window, interactor, scene_renderer, reframe=None,
                 dark=None, visible=True):
        self.window = window
        self.interactor = interactor
        self.scene = scene_renderer
        self._reframe = reframe
        self._on = bool(visible)

        pal = _shell.palette(dark) if _shell is not None else None
        self.ren = vtk.vtkRenderer()
        self.ren.SetLayer(1)          # over the scene; see the module docstring
        self.ren.InteractiveOff()     # never routes interaction (like the chrome)
        self.ren.SetActiveCamera(vtk.vtkCamera())
        self.ren.GetActiveCamera().ParallelProjectionOn()

        self.props = self._build_cube(pal)
        self.axes = origin_marker(_AXES_LEN)
        self.axes.SetPosition(_AXES_ORIGIN, _AXES_ORIGIN, _AXES_ORIGIN)
        self.props.append(self.axes)
        for p in self.props:
            self.ren.AddViewProp(p)

        # a layer-1 renderer needs the window to HAVE a layer 1; the viewers
        # that build no ViewerChrome (the embedded panel) leave it at 1, and a
        # renderer above the count is silently not rendered. Never shrink it --
        # ViewerChrome / the fullscreen teardown own the upper bound.
        window.SetNumberOfLayers(max(2, window.GetNumberOfLayers()))
        window.AddRenderer(self.ren)

        self._apply_visibility()
        # ride the SCENE's StartEvent: it renders first (layer 0), so the cube
        # camera is already in step by the time layer 1 draws. This is also what
        # keeps the viewport honest across resizes without a ConfigureEvent hook.
        self._sync_tag = self.scene.AddObserver("StartEvent", self._sync)
        # No interactor: a panel that routes its own clicks (the G-code page's
        # Tk-driven preview, gui_preview.PreviewPanel) asks `pick` / `snap`
        # itself, in VTK display coordinates.
        self._press_tag = None if interactor is None else \
            interactor.AddObserver(
                # 9.0: under the chrome's button/slider handlers (10) so a
                # sidebar click always wins, over install_cad_camera's nav
                # (0.3) so a cube click never also orbits.
                "LeftButtonPressEvent", self._on_press, 9.0)

    # -- construction ----------------------------------------------------- #
    def _build_cube(self, pal):
        """The labelled cube, as [faces, edges, 6 x label]. Faces read in the
        machine's frame: +Z is TOP, -Y (toward the operator) is FRONT, +X is
        RIGHT -- the standard CAD convention, and the one the scene's own origin
        triad already implies.

        Deliberately NOT vtkAnnotatedCubeActor, which looks like the obvious
        fit: it labels faces with vtkVectorText, which (a) is not Helvetica, so
        it would break the sidebar theme's one-typeface invariant, and (b) is
        laid out at a fixed aspect with no fitting, so word labels overflow the
        cube badly ("FRONT" ran ~1.5x the face width at any readable scale).
        vtkTextActor3D renders real Helvetica through apply_font, and fitting is
        ours to control -- see _fit_scale."""
        if pal is not None:
            face = _shell._hex_to_rgb(pal["card"])
            text = _shell._hex_to_rgb(pal["fg"])
            # NOT pal["border"]: that tone is tuned for a hairline against a
            # flat page, and on the cube it vanished -- a white card edged in
            # #d9dce1, floating on the near-white 3D backdrop, had no silhouette
            # at all. Mixing the foreground in gives the gizmo a readable
            # outline in both palettes without hardcoding either.
            edge = _mix(face, text, 0.45)
        else:
            face, text = (0.85, 0.86, 0.88), (0.1, 0.1, 0.1)
            edge = _mix(face, text, 0.45)

        src = vtk.vtkCubeSource()
        src.SetXLength(2 * HALF)
        src.SetYLength(2 * HALF)
        src.SetZLength(2 * HALF)
        src.Update()

        fm = vtk.vtkPolyDataMapper()
        fm.SetInputConnection(src.GetOutputPort())
        faces = vtk.vtkActor()
        faces.SetMapper(fm)
        faces.GetProperty().SetColor(*face)
        # flat-ish shading: enough to read the three visible faces apart, not so
        # much that a face goes dark and swallows its label
        faces.GetProperty().SetAmbient(0.55)
        faces.GetProperty().SetDiffuse(0.45)
        faces.GetProperty().SetSpecular(0.0)

        ed = vtk.vtkExtractEdges()
        ed.SetInputConnection(src.GetOutputPort())
        em = vtk.vtkPolyDataMapper()
        em.SetInputConnection(ed.GetOutputPort())
        edges = vtk.vtkActor()
        edges.SetMapper(em)
        edges.GetProperty().SetColor(*edge)
        edges.GetProperty().SetLineWidth(1.5)
        edges.GetProperty().SetLighting(False)

        labels = [self._label_actor(name, text) for name, _n, _u in _FACES]
        s = _fit_scale(labels)
        for (_name, normal, up), act in zip(_FACES, labels):
            act.SetUserMatrix(_face_matrix(act, normal, up, s))
        return [faces, edges] + labels

    def _label_actor(self, text, rgb):
        a = vtk.vtkTextActor3D()
        a.SetInput(text)
        tp = a.GetTextProperty()
        tp.SetFontSize(_LABEL_FONT_PX)
        tp.SetColor(*rgb)
        tp.SetJustificationToCentered()
        tp.SetVerticalJustificationToCentered()
        if _shell is not None:
            _shell.apply_font(tp, bold=True)
        else:
            tp.SetFontFamilyToArial()
            tp.BoldOn()
        return a

    # -- visibility ------------------------------------------------------- #
    def visible(self):
        return self._on

    def label(self):
        """The chrome button's label provider."""
        return "Hide view cube" if self._on else "Show view cube"

    def toggle(self):
        self.set_visible(not self._on)

    def set_visible(self, on):
        self._on = bool(on)
        self._apply_visibility()

    def _apply_visibility(self):
        # hide the PROPS, never remove the renderer: the layer-1 renderer is
        # what the fullscreen teardown's layer recount sees, and churning it
        # would reintroduce the orphaned-layer popup.
        for p in self.props:
            p.SetVisibility(self._on)

    # -- per-render camera sync ------------------------------------------- #
    def _sync(self, *_args):
        """Point the gizmo camera the same way as the scene camera, and re-seat
        the fixed-pixel viewport for the live window size. Cheap enough to run
        every frame (a GetSize + a handful of floats), which is why there is no
        separate resize hook to fall out of step."""
        w, h = self.window.GetSize()
        self.ren.SetViewport(*viewport_rect(w, h))

        src = self.scene.GetActiveCamera()
        pos, fp = src.GetPosition(), src.GetFocalPoint()
        d = _normalize((pos[0] - fp[0], pos[1] - fp[1], pos[2] - fp[2]))
        dst = self.ren.GetActiveCamera()
        dst.SetFocalPoint(0.0, 0.0, 0.0)
        dst.SetPosition(d[0] * _DIST, d[1] * _DIST, d[2] * _DIST)
        dst.SetViewUp(*src.GetViewUp())
        dst.SetParallelScale(_SCALE)
        self.ren.ResetCameraClippingRange()

    # -- picking ---------------------------------------------------------- #
    def _in_viewport(self, x, y):
        w, h = self.window.GetSize()
        x0, y0, x1, y1 = viewport_rect(w, h)
        return (x0 * w) <= x <= (x1 * w) and (y0 * h) <= y <= (y1 * h)

    def _ray(self, x, y):
        """Display point -> (origin, direction) in the gizmo renderer's world,
        which IS cube space (the cube sits at the origin, untransformed).

        DisplayToWorld is pure matrix math -- no depth-buffer read -- so this
        works offscreen and in native fullscreen alike."""
        r = self.ren

        def _at(z):
            r.SetDisplayPoint(float(x), float(y), z)
            r.DisplayToWorld()
            p = r.GetWorldPoint()
            return [p[i] / p[3] for i in range(3)] if p[3] else list(p[:3])

        p0, p1 = _at(0.0), _at(1.0)
        return p0, _normalize((p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]))

    def pick(self, x, y):
        """The snap direction under the display point, or None for a miss."""
        if not self._on or not self._in_viewport(x, y):
            return None
        origin, direction = self._ray(x, y)
        t = ray_box(origin, direction)
        if t is None:
            return None
        hit = [origin[i] + direction[i] * t for i in range(3)]
        return cube_region(hit)

    # -- snapping --------------------------------------------------------- #
    def snap(self, direction):
        """Look down `direction` at the current focal point, then let the viewer
        re-frame. Only the DIRECTION and view-up are set here: under the parallel
        projection these scenes use, the camera's distance carries no image
        information (zoom is the parallel scale) and fit_clipping_range slides it
        back anyway -- the same contract _snap_top has always honoured."""
        cam = self.scene.GetActiveCamera()
        fx, fy, fz = cam.GetFocalPoint()
        d = _normalize((float(direction[0]), float(direction[1]),
                        float(direction[2])))
        cam.SetPosition(fx + d[0], fy + d[1], fz + d[2])
        cam.SetViewUp(*snap_view_up(direction))
        cam.OrthogonalizeViewUp()
        if self._reframe is not None:
            self._reframe()
        else:
            self.scene.ResetCamera()
            fit_clipping_range(self.scene)
        self.window.Render()

    def _on_press(self, obj, _evt):
        hit = self.pick(*obj.GetEventPosition())
        if hit is None:
            return                       # fall through to the orbit style
        self.snap(hit)
        try:                             # consumed: never also orbit
            obj.GetCommand(self._press_tag).SetAbortFlag(1)
        except Exception:  # noqa: BLE001
            pass
