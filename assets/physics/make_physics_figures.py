#!/usr/bin/env python3
"""make_physics_figures.py -- render the pictures on the Physics page's cards
(Setup -> Physics, one per law card plus Process and Visualization) into this
folder as `<card>_dark.png` and `<card>_light.png`.

What a picture is for. A user typing a value into a card should be able to see
the thing the value controls: which length "depth" is, that c scales every droop
while p decides how much faster a long span sags than a short one, that the
fusion coefficient is how far one fiber sinks into the one below it. So every
picture names the card's symbols ON the geometry, and the equation fragments in
its headings use the same symbols as the typeset law at the top of the card.

How it is drawn. The fibers, nozzles and plates are 3D tubes rendered offscreen
by VTK with the app's own studio lights and fiber material
(`vtk_helpers.studio_light_kit` / `set_fibre_material`), the look of the Viewer
and the paper's Figure 22. matplotlib then lays the dimension lines and labels
over the render, at points projected from the 3D scene, so an arrow marked
"depth" ends exactly on the fiber it measures.

What is true to the model and what is not. Every RATIO a picture shows comes
from the shipped law: doubling c doubles the depth, doubling the span at p = 2
quadruples it, a fused stack is 3d - 2*delta tall. Absolute sizes are chosen to
be legible (a 20 um fiber over a 0.3 mm pore cannot be seen next to its own
dimension line), so no picture carries a scale bar or claims one.

The app only loads the PNGs (gui_physics_diagrams.py). Run from the repo root
after changing a picture:

    .venv/Scripts/python.exe assets/physics/make_physics_figures.py [card ...]
"""

import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)

import numpy as np                                        # noqa: E402
import vtk                                                # noqa: E402
from vtk.util.numpy_support import vtk_to_numpy           # noqa: E402
from PIL import Image                                     # noqa: E402
import matplotlib                                         # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                           # noqa: E402
from matplotlib.image import AxesImage                   # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402
from matplotlib.transforms import Affine2D               # noqa: E402

import vtk_helpers                                        # noqa: E402

# --------------------------------------------------------------------------- #
# Output geometry. Pictures are authored in LOGICAL pixels (what a 96-dpi
# screen shows) and written at OUT_SCALE times that, so the app can scale them
# down to a 100-200 % display without upsampling.
# --------------------------------------------------------------------------- #
OUT_SCALE = 2
SUPERSAMPLE = 3            # VTK renders at this x the output, then box-filters

# The inks, one file per theme, and ONLY the app's own colours: `ink` is the
# modern theme's text colour (gui_theme DARK "fg" / gui_modern_theme LIGHT
# "fg"), `hint` its hint grey. The quantity a panel is about is marked by
# weight (bold label, heavier arrow), not by a colour the app does not use.
# `accent` stays as a name so a panel can say "this is the one", and it is the
# text colour. The 3D render is the same in both themes.
# A panel's box is the modern theme's `card` fill with its `hairline` edge.
THEMES = {
    "dark": dict(ink="#e0e0e0", hint="#9a9fa4", accent="#e0e0e0",
                 card="#333639", border="#3c4045", plate=(0.31, 0.33, 0.35)),
    "light": dict(ink="#1c1e21", hint="#7a8087", accent="#1c1e21",
                  card="#ffffff", border="#d9dce1", plate=(0.84, 0.86, 0.88)),
}
BLUE = (0.18, 0.42, 0.84)          # the fiber blue of the paper's Figure 22
BLUE_DEEP = (0.13, 0.32, 0.68)     # supports / lower layers, a shade darker
STEEL = (0.62, 0.64, 0.67)         # nozzle, syringe: the machine, not the melt
STEEL_DARK = (0.40, 0.42, 0.45)

TEXT_FONT = "Arial"                # what gui_fonts resolves "Helvetica" to
MATH_FONT = "Cambria"              # gui_math's first choice for the law strip

plt.rcParams.update({
    "font.family": TEXT_FONT,
    "mathtext.fontset": "custom",
    "mathtext.rm": MATH_FONT,
    "mathtext.it": MATH_FONT + ":italic",
    "mathtext.bf": MATH_FONT + ":bold",
    "mathtext.bfit": MATH_FONT + ":italic:bold",
})


# --------------------------------------------------------------------------- #
# 3D scenes
# --------------------------------------------------------------------------- #
def _polyline(points):
    pts = vtk.vtkPoints()
    for p in points:
        pts.InsertNextPoint(*map(float, p))
    line = vtk.vtkPolyLine()
    line.GetPointIds().SetNumberOfIds(len(points))
    for i in range(len(points)):
        line.GetPointIds().SetId(i, i)
    cells = vtk.vtkCellArray()
    cells.InsertNextCell(line)
    poly = vtk.vtkPolyData()
    poly.SetPoints(pts)
    poly.SetLines(cells)
    return poly


class Scene:
    """One offscreen VTK scene, rendered to an RGBA array plus a function that
    maps a world point to its pixel in that array."""

    def __init__(self, direction, up=(0.0, 0.0, 1.0)):
        self.ren = vtk.vtkRenderer()
        self.ren.SetBackground(1.0, 1.0, 1.0)
        self.ren.SetBackgroundAlpha(0.0)
        self.kit = vtk_helpers.studio_light_kit()
        self.kit.AddLightsToRenderer(self.ren)
        self.direction = np.asarray(direction, float)
        self.up = up
        self.anchors = []        # world points that must stay inside the frame

    # -- geometry -------------------------------------------------------- #
    def _actor(self, source, color, opacity=1.0, smooth=True):
        normals = vtk.vtkPolyDataNormals()
        normals.SetInputConnection(source.GetOutputPort())
        normals.SetFeatureAngle(75.0)
        normals.SplittingOn() if not smooth else normals.SplittingOff()
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(normals.GetOutputPort())
        mapper.ScalarVisibilityOff()
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetOpacity(opacity)
        vtk_helpers.set_fibre_material(prop, True)
        self.ren.AddActor(actor)
        return actor

    def tube(self, points, radius, color=BLUE, opacity=1.0, sides=48,
             radii=None):
        """A fiber of `radius` along `points` (N, 3), capped at both ends.
        `radii`, if given, varies the radius per point (a jet, a cone)."""
        poly = _polyline(points)
        if radii is not None:
            arr = vtk.vtkDoubleArray()
            arr.SetName("r")
            for r in radii:
                arr.InsertNextValue(float(r))
            poly.GetPointData().SetScalars(arr)
        src = vtk.vtkTrivialProducer()
        src.SetOutput(poly)
        tf = vtk.vtkTubeFilter()
        tf.SetInputConnection(src.GetOutputPort())
        tf.SetNumberOfSides(sides)
        tf.CappingOn()
        if radii is not None:
            tf.SetVaryRadiusToVaryRadiusByAbsoluteScalar()
        else:
            tf.SetRadius(radius)
        return self._actor(tf, color, opacity)

    def box(self, center, size, color, opacity=1.0):
        src = vtk.vtkCubeSource()
        src.SetCenter(*center)
        src.SetXLength(size[0])
        src.SetYLength(size[1])
        src.SetZLength(size[2])
        return self._actor(src, color, opacity, smooth=False)

    def cylinder(self, base, top, radius, color, opacity=1.0, res=64):
        """A solid cylinder from `base` to `top` (vertical only)."""
        src = vtk.vtkCylinderSource()
        src.SetRadius(radius)
        h = float(top[2] - base[2])
        src.SetHeight(h)
        src.SetResolution(res)
        src.CappingOn()
        tr = vtk.vtkTransform()
        tr.Translate(base[0], base[1], base[2] + h / 2.0)
        tr.RotateX(90.0)
        tf = vtk.vtkTransformPolyDataFilter()
        tf.SetInputConnection(src.GetOutputPort())
        tf.SetTransform(tr)
        return self._actor(tf, color, opacity, smooth=False)

    def revolve(self, profile, color, opacity=1.0, res=72):
        """A solid of revolution about the z axis; `profile` is [(r, z), ...]."""
        pts = [(r, 0.0, z) for r, z in profile]
        poly = _polyline(pts)
        src = vtk.vtkTrivialProducer()
        src.SetOutput(poly)
        rf = vtk.vtkRotationalExtrusionFilter()
        rf.SetInputConnection(src.GetOutputPort())
        rf.SetResolution(res)
        rf.CappingOff()
        return self._actor(rf, color, opacity)

    # -- render ---------------------------------------------------------- #
    def _basis(self):
        """(right, up) unit vectors of the screen, for a parallel camera
        looking from +direction towards the origin (VTK's own convention:
        right = direction-of-projection x view-up)."""
        dop = -self.direction / np.linalg.norm(self.direction)
        up = np.asarray(self.up, float)
        up = up - np.dot(up, dop) * dop
        up /= np.linalg.norm(up)
        right = np.cross(dop, up)
        return right, up, dop

    def _points(self):
        out = []
        acts = self.ren.GetActors()
        acts.InitTraversal()
        for _ in range(acts.GetNumberOfItems()):
            a = acts.GetNextActor()
            a.GetMapper().Update()
            pts = a.GetMapper().GetInput().GetPoints()
            if pts is not None and pts.GetNumberOfPoints():
                out.append(vtk_to_numpy(pts.GetData()))
        if self.anchors:
            out.append(np.asarray(self.anchors, float).reshape(-1, 3))
        return np.concatenate(out)

    def scale_to_fit(self, width_px, height_px, margin=3):
        """LOGICAL pixels per world unit that fits the whole scene into the
        box. Pass it to a second scene's render() to draw both at one scale."""
        right, up, _ = self._basis()
        pts = self._points()
        du = np.ptp(pts @ right)
        dv = np.ptp(pts @ up)
        return min((width_px - 2 * margin) / du, (height_px - 2 * margin) / dv)

    def render(self, width_px, height_px, margin=3, scale=None):
        """Render the scene as tightly as it fits in `width_px` x `height_px`
        LOGICAL pixels (or at `scale` logical px per world unit). The image is
        cropped to the scene plus `margin`, so it is usually smaller than the
        box, and it is made at OUT_SCALE x that size.

        Returns (rgba uint8 (h, w, 4) in OUTPUT pixels, project, scale) where
        project(xyz) -> (px, py) in that image, y down, and `scale` is in
        logical px per world unit. The projection is computed from the camera
        basis rather than asked of VTK, so it is exact and does not depend on
        the window still being alive."""
        ss = SUPERSAMPLE
        right, up, dop = self._basis()
        pts = self._points()
        u, v = pts @ right, pts @ up
        k_logical = scale or self.scale_to_fit(width_px, height_px, margin)
        k = k_logical * OUT_SCALE
        margin = margin * OUT_SCALE
        umin, vmax = u.min(), v.max()
        w_out = int(math.ceil((u.max() - umin) * k + 2 * margin))
        h_out = int(math.ceil((vmax - v.min()) * k + 2 * margin))
        W, H = w_out * ss, h_out * ss
        # the world point at the image centre
        uc = umin + (w_out / 2.0 - margin) / k
        vc = vmax - (h_out / 2.0 - margin) / k
        depth_c = float(np.mean(pts @ dop))
        focal = uc * right + vc * up + depth_c * dop
        span = float(np.ptp(pts @ dop)) + 10.0

        win = vtk.vtkRenderWindow()
        win.SetOffScreenRendering(1)
        win.SetAlphaBitPlanes(1)
        win.SetMultiSamples(0)
        win.AddRenderer(self.ren)
        win.SetSize(W, H)
        cam = self.ren.GetActiveCamera()
        cam.ParallelProjectionOn()
        cam.SetFocalPoint(*focal)
        cam.SetPosition(*(focal - dop * span))
        cam.SetViewUp(*up)
        cam.SetParallelScale((h_out / 2.0) / k)
        self.ren.ResetCameraClippingRange()
        win.Render()
        w2i = vtk.vtkWindowToImageFilter()
        w2i.SetInput(win)
        w2i.SetInputBufferTypeToRGBA()
        w2i.ReadFrontBufferOff()
        w2i.Update()
        img = w2i.GetOutput()
        arr = vtk_to_numpy(img.GetPointData().GetScalars()).reshape(H, W, 4)
        arr = np.flipud(arr).copy()
        win.Finalize()
        # box-filter down in PREMULTIPLIED alpha, so an edge pixel does not
        # carry the white clear colour onto a dark card
        pil = Image.fromarray(arr, "RGBA").convert("RGBa")
        pil = pil.resize((w_out, h_out), Image.BOX)
        out = np.asarray(pil.convert("RGBA"))

        def project(p):
            p = np.asarray(p, float)
            return (margin + (p @ right - umin) * k,
                    margin + (vmax - p @ up) * k)

        return out, project, k_logical


# --------------------------------------------------------------------------- #
# 2D composition: one matplotlib canvas in OUTPUT pixels, y down
# --------------------------------------------------------------------------- #
class Canvas:
    def __init__(self, width, height, theme):
        """`width` x `height` in LOGICAL pixels."""
        self.s = OUT_SCALE
        self.W, self.H = width * self.s, height * self.s
        self.t = THEMES[theme]
        self.fig = plt.figure(figsize=(self.W / 100.0, self.H / 100.0),
                              dpi=100)
        self.fig.patch.set_alpha(0.0)
        ax = self.fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, self.W)
        ax.set_ylim(self.H, 0)
        ax.axis("off")
        ax.patch.set_alpha(0.0)
        self.ax = ax
        self._track = None       # a list while a panel's stage is being drawn

    def _keep(self, artist):
        if self._track is not None:
            self._track.append(artist)
        return artist

    def fit_into(self, artists, box, name=""):
        """Move `artists` (as one group) so their drawn extent is centred in
        the logical `box` (x, y, w, h). Warns when they do not fit."""
        r = self.fig.canvas.get_renderer()
        ext = []
        for a in artists:
            bb = a.get_window_extent(renderer=r)
            if bb.width > 0 or bb.height > 0:
                # display y is up from the bottom; the canvas's is down
                ext.append((bb.x0, self.H - bb.y1, bb.x1, self.H - bb.y0))
        if not ext:
            return
        ext = np.asarray(ext)
        x0, y0 = ext[:, 0].min(), ext[:, 1].min()
        x1, y1 = ext[:, 2].max(), ext[:, 3].max()
        bx, by, bw, bh = (v * self.s for v in box)
        if x1 - x0 > bw + 1 or y1 - y0 > bh + 1:
            print("  WARN stage %r: drawing is %.0f x %.0f, stage %.0f x %.0f"
                  % (name, (x1 - x0) / self.s, (y1 - y0) / self.s,
                     bw / self.s, bh / self.s))
        dx = bx + (bw - (x1 - x0)) / 2 - x0
        dy = by + (bh - (y1 - y0)) / 2 - y0
        shift = Affine2D().translate(dx, dy) + self.ax.transData
        for a in artists:
            if isinstance(a, AxesImage):
                left, right, bottom, top = a.get_extent()
                a.set_extent((left + dx, right + dx, bottom + dy, top + dy))
            else:
                a.set_transform(shift)

    def panel_box(self, x, y, w, h):
        """A panel's rounded box, in the theme's card fill and hairline."""
        X, Y = self._p((x, y))
        inset = 1.0
        self.ax.add_patch(FancyBboxPatch(
            (X + inset, Y + inset), w * self.s - 2 * inset,
            h * self.s - 2 * inset,
            boxstyle="round,pad=0,rounding_size=%g" % (6 * self.s),
            fc=self.t["card"], ec=self.t["border"], lw=1.0, zorder=0))

    # all coordinates below are LOGICAL pixels
    def _p(self, xy):
        return xy[0] * self.s, xy[1] * self.s

    def image(self, rgba, x, y):
        """Place an OUTPUT-pixel RGBA array with its top-left at logical (x, y)."""
        h, w = rgba.shape[:2]
        X, Y = x * self.s, y * self.s
        return self._keep(self.ax.imshow(rgba, extent=(X, X + w, Y + h, Y),
                                         interpolation="none", zorder=1))

    def text(self, xy, s, size=11, color=None, ha="left", va="center",
             weight="normal", zorder=5, **kw):
        X, Y = self._p(xy)
        return self._keep(self.ax.text(X, Y, s, fontsize=size * self.s * 0.82,
                                       color=color or self.t["ink"], ha=ha,
                                       va=va, weight=weight, zorder=zorder,
                                       **kw))

    def rich(self, xy, parts, size=11, ha="left", weight="normal"):
        """Text in several colours on one baseline. parts = [(text, color)] or
        [(text, color, rise, rel_size)] -- `rise` in logical px lifts a piece
        (a superscript), `rel_size` scales it. `color` may be a theme key
        ("ink", "hint", "accent") or a literal."""
        r = self.fig.canvas.get_renderer()
        widths, items = [], []
        for part in parts:
            s, c = part[0], part[1]
            rise = part[2] if len(part) > 2 else 0.0
            rel = part[3] if len(part) > 3 else 1.0
            t = self.text((0, 0), s, size=size * rel, color=self.t.get(c, c),
                          va="baseline", ha="left", weight=weight)
            bb = t.get_window_extent(renderer=r)
            widths.append(bb.width)
            items.append((t, rise))
        total = sum(widths)
        X, Y = self._p(xy)
        x = X - (total if ha == "right" else total / 2 if ha == "center"
                 else 0)
        for (t, rise), w in zip(items, widths):
            t.set_position((x, Y - rise * self.s))
            x += w
        return total / self.s

    def measure(self, parts, size):
        """Logical width of `parts` set on one line, as rich() would."""
        r = self.fig.canvas.get_renderer()
        total = 0.0
        for part in parts:
            rel = part[3] if len(part) > 3 else 1.0
            t = self.ax.text(0, 0, part[0], fontsize=size * rel * self.s * 0.82)
            total += t.get_window_extent(renderer=r).width
            t.remove()
        return total / self.s

    def wrap(self, parts, size, width):
        """Split rich `parts` into lines no wider than `width` logical px,
        breaking only between words. A math piece ($...$) never splits."""
        words = []
        for part in parts:
            text, rest = part[0], tuple(part[1:])
            if text.startswith("$"):
                words.append((text,) + rest)
                continue
            for w in re.findall(r"\S+\s*|\s+", text):
                words.append((w,) + rest)
        lines, cur = [], []
        for w in words:
            trial = cur + [(w[0].rstrip(),) + w[1:]]
            if cur and self.measure(trial, size) > width:
                lines.append(cur)
                w = (w[0].lstrip(),) + w[1:]
                cur = [w] if w[0] else []
            else:
                cur.append(w)
        if cur:
            lines.append(cur)
        for ln in lines:                    # no trailing space at a line end
            ln[-1] = (ln[-1][0].rstrip(),) + ln[-1][1:]
        return lines

    def line(self, a, b, color=None, lw=1.2, dash=None, zorder=4):
        (x0, y0), (x1, y1) = self._p(a), self._p(b)
        for ln in self.ax.plot([x0, x1], [y0, y1], color=color or self.t["ink"],
                               lw=lw * self.s, ls=(0, dash) if dash else "-",
                               solid_capstyle="butt", zorder=zorder):
            self._keep(ln)

    def polyline(self, pts, color=None, lw=1.2, dash=None, zorder=4):
        P = np.array([self._p(p) for p in pts])
        for ln in self.ax.plot(P[:, 0], P[:, 1], color=color or self.t["ink"],
                               lw=lw * self.s, ls=(0, dash) if dash else "-",
                               zorder=zorder):
            self._keep(ln)

    def arrow(self, a, b, color=None, lw=1.2, both=True, head=7, zorder=6):
        """A dimension arrow from a to b, heads touching both ends."""
        style = "<|-|>" if both else "-|>"
        self._keep(self.ax.add_patch(FancyArrowPatch(
            self._p(a), self._p(b), arrowstyle=style,
            mutation_scale=head * self.s, color=color or self.t["ink"],
            lw=lw * self.s, shrinkA=0, shrinkB=0, zorder=zorder)))

    def dot(self, xy, r=2.6, color=None, zorder=6):
        X, Y = self._p(xy)
        self._keep(self.ax.add_patch(plt.Circle((X, Y), r * self.s,
                                                color=color or self.t["ink"],
                                                zorder=zorder)))

    def save(self, path):
        self.fig.savefig(path, dpi=100, transparent=True)
        plt.close(self.fig)


def place(cv, rgba, project, x, y):
    """Put a render on the canvas at logical (x, y) and return a function that
    maps a world point to LOGICAL canvas pixels."""
    cv.image(rgba, x, y)
    s = OUT_SCALE

    def to_canvas(p):
        px, py = project(p)
        return x + px / s, y + py / s
    return to_canvas


# --------------------------------------------------------------------------- #
# shared geometry
# --------------------------------------------------------------------------- #
def parabola_span(x0, x1, z_top, depth, n=80):
    """Centreline points of a free span from x0 to x1 resting on supports at
    height z_top, drooping `depth` at midspan (the sag law's parabola)."""
    xs = np.linspace(x0, x1, n)
    u = (xs - x0) / (x1 - x0)
    zs = z_top - depth * 4.0 * u * (1.0 - u)
    return xs, zs


def draped_fiber(supports, z_top, depths, overhang=0.9, y=0.0, n=80):
    """A fiber laid over `supports` (x positions), drooping depths[i] in span
    i, with a short overhang past each end that drapes slightly. The kink over
    each support is rounded over about one radius, as a real fiber is."""
    xs, zs = [], []
    # overhang before the first support: a gentle droop away from it
    ox = np.linspace(supports[0] - overhang, supports[0], 24, endpoint=False)
    xs.extend(ox)
    zs.extend(z_top - 0.12 * ((supports[0] - ox) / overhang) ** 2 * overhang)
    for i, (a, b) in enumerate(zip(supports[:-1], supports[1:])):
        px, pz = parabola_span(a, b, z_top, depths[i], n)
        if i:
            px, pz = px[1:], pz[1:]
        xs.extend(px)
        zs.extend(pz)
    ox = np.linspace(supports[-1], supports[-1] + overhang, 24)[1:]
    xs.extend(ox)
    zs.extend(z_top - 0.12 * ((ox - supports[-1]) / overhang) ** 2 * overhang)
    xs, zs = np.asarray(xs), np.asarray(zs)
    # round the kinks: a short moving average on z, ends held
    k = 7
    zp = np.pad(zs, (k // 2, k // 2), mode="edge")
    zs = np.convolve(zp, np.ones(k) / k, mode="valid")
    return np.column_stack([xs, np.full_like(xs, y), zs])


def support_stack(scene, x, n, r, length=1.6, y0=0.0, color=BLUE_DEEP):
    """`n` fibers stacked on top of each other, running along y, seen end-on
    from the front: the layers below that hold a span up."""
    for k in range(n):
        z = r + 2.0 * r * k
        scene.tube([(x, y0 - length / 2, z), (x, y0 + length / 2, z)], r,
                   color=color)
    return 2.0 * r * n            # top of the stack


# --------------------------------------------------------------------------- #
# The panel grid. Every picture is a row of IDENTICAL panels: a box of one size,
# its title, its equation line, its stage and its caption block always in the
# same place. A panel's drawing is centred in the stage by measuring what was
# drawn, so no figure carries hand-tuned offsets. A one-panel card, a two-panel
# card and a three-panel card share the panel size, and the app shows every
# card at one scale (gui_physics_diagrams.FULL_WIDTH is MAX_PANELS wide), so a
# panel is the same size on every card.
# --------------------------------------------------------------------------- #
PANEL_W, PANEL_H, PANEL_GAP = 360, 372, 20
MAX_PANELS = 3
PAD = 16
TITLE_Y, EQ_Y = 28, 53
STAGE_Y, STAGE_H = 70, 212
CAP_Y, CAP_GAP = 306, 18
CAP_SIZE, CAP_LINES = 10.5, 4
INNER = PANEL_W - 2 * PAD


def _check(width, what):
    if width > INNER + 0.5:
        print("  WARN %r is %.0f px wide, the panel holds %d" % (what, width,
                                                                INNER))


class _Stage:
    def __init__(self, cv, box, name):
        self.cv, self.box, self.name = cv, box, name

    def __enter__(self):
        self.cv._track = []
        return self.cv

    def __exit__(self, kind, _value, _tb):
        arts, self.cv._track = self.cv._track, None
        if kind is None:
            self.cv.fit_into(arts, self.box, self.name)


class Panels:
    def __init__(self, theme, n):
        assert 1 <= n <= MAX_PANELS
        self.t = THEMES[theme]
        self.cv = Canvas(n * PANEL_W + (n - 1) * PANEL_GAP, PANEL_H, theme)
        for i in range(n):
            self.cv.panel_box(self.x(i), 0, PANEL_W, PANEL_H)

    @staticmethod
    def x(i):
        return i * (PANEL_W + PANEL_GAP)

    def panel(self, i, title, eq=None, caption=()):
        """Draw panel i's title, equation and caption; return a context whose
        drawing is centred in the panel's stage."""
        cv, x0 = self.cv, self.x(i)
        _check(heading(cv, x0 + PAD, TITLE_Y, title), title)
        if eq:
            _check(cv.rich((x0 + PAD, EQ_Y), eq, size=12.5), eq)
        # Each caption item is a paragraph, wrapped at the panel's inner
        # width; only a new item starts a new line.
        lines = []
        for item in caption:
            parts = [(item, "hint")] if isinstance(item, str) else item
            wrapped = cv.wrap(parts, CAP_SIZE, INNER)
            if len(wrapped) > 1 and len("".join(
                    p[0] for p in wrapped[-1]).split()) < 2:
                print("  WARN %r: a caption ends on a one-word line: %r"
                      % (title, "".join(p[0] for p in wrapped[-1])))
            lines.extend(wrapped)
        if len(lines) > CAP_LINES:
            print("  WARN %r: caption wraps to %d lines, the panel holds %d"
                  % (title, len(lines), CAP_LINES))
        for k, parts in enumerate(lines):
            cv.rich((x0 + PAD, CAP_Y + k * CAP_GAP), parts, size=CAP_SIZE)
        return _Stage(cv, (x0 + PAD, STAGE_Y, INNER, STAGE_H), title)


# --------------------------------------------------------------------------- #
# shared pieces
# --------------------------------------------------------------------------- #
# Side-on and a little from above. No sideways (azimuth) component: with one,
# a level chord between two supports draws tilted, and a depth measured
# from it reads wrong.
VIEW_SIDE = (0.0, -1.0, 0.13)
VIEW_OBLIQUE = (0.85, -1.0, 0.75)


def plate(sc, t, x0, x1, y0, y1, z=0.0, thick=0.18):
    """The collector: a thin slab whose TOP is at z."""
    sc.box(((x0 + x1) / 2, (y0 + y1) / 2, z - thick / 2),
           (x1 - x0, y1 - y0, thick), t["plate"])


def bezier(p0, p1, p2, p3, n=80):
    s = np.linspace(0.0, 1.0, n)[:, None]
    p0, p1, p2, p3 = (np.asarray(q, float) for q in (p0, p1, p2, p3))
    return ((1 - s) ** 3 * p0 + 3 * (1 - s) ** 2 * s * p1
            + 3 * (1 - s) * s ** 2 * p2 + s ** 3 * p3)


def section(sc, profile, color, angle=180.0, res=48):
    """A solid of revolution about z, cut in half so its inside faces the
    camera (which looks along +y): `profile` is a closed [(r, z), ...]
    polygon, swept from +x through +y to -x."""
    pts = vtk.vtkPoints()
    for r, z in profile:
        pts.InsertNextPoint(float(r), 0.0, float(z))
    poly = vtk.vtkPolygon()
    poly.GetPointIds().SetNumberOfIds(len(profile))
    for i in range(len(profile)):
        poly.GetPointIds().SetId(i, i)
    cells = vtk.vtkCellArray()
    cells.InsertNextCell(poly)
    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetPolys(cells)
    src = vtk.vtkTrivialProducer()
    src.SetOutput(pd)
    rf = vtk.vtkRotationalExtrusionFilter()
    rf.SetInputConnection(src.GetOutputPort())
    rf.SetResolution(res)
    rf.SetAngle(angle)
    rf.CappingOn()
    tri = vtk.vtkTriangleFilter()
    tri.SetInputConnection(rf.GetOutputPort())
    return sc._actor(tri, color, smooth=False)


def taylor_cone(z_tip, r0, knee, z_end, r_jet, n=60):
    """(points, radii) of the drawn jet: a cone that necks from r0 at the
    nozzle tip to half that `knee` below it, then thins to the jet radius --
    the player's cone-jet profile, in picture units."""
    s = np.linspace(0.0, z_tip - z_end, n)
    rad = np.maximum(r_jet, r0 * 0.5 ** (s / knee))
    pts = np.column_stack([np.zeros_like(s), np.zeros_like(s), z_tip - s])
    return pts, rad


def graph_axes(cv, box, t, ylabel):
    """Bare axes (arrows, no numbers: these are shapes, not data) in a logical
    box (x, y, w, h). Returns map(u, v) for u, v in [0, 1]."""
    x, y, w, h = box
    cv.arrow((x, y + h), (x + w, y + h), color=t["hint"], lw=1.0, both=False,
             head=6)
    cv.arrow((x, y + h), (x, y), color=t["hint"], lw=1.0, both=False, head=6)
    cv.text((x + 6, y - 4), ylabel, size=10.5, color=t["hint"], ha="left",
            va="bottom")

    def m(u, v):
        return x + u * w, y + h - v * h
    return m


def heading(cv, x, y, parts, size=13.5):
    """A panel title; parts as for Canvas.rich, or a plain str."""
    if isinstance(parts, str):
        parts = [(parts, "ink")]
    return cv.rich((x, y), parts, size=size, weight="bold")


def bf(sym):
    """A symbol set bold italic, the way a panel marks the one it explains."""
    return r"$\mathbfit{%s}$" % sym


def it(sym):
    return r"$%s$" % sym


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(int(round(255 * c)) for c in rgb)


def vdim(cv, t, a, b, x, label, side="left", bold=False, size=14):
    """A vertical dimension at screen x between the heights of points a and b,
    with extension lines from them and the label beside it."""
    for q in (a, b):
        cv.line((q[0], q[1]), (x + (-5 if side == "left" else 5), q[1]),
                color=t["hint"], lw=0.8)
    cv.arrow((x, a[1]), (x, b[1]), lw=1.7 if bold else 1.3, head=6)
    mid = (a[1] + b[1]) / 2
    if side == "left":
        cv.text((x - 8, mid), label, size=size, ha="right")
    else:
        cv.text((x + 8, mid), label, size=size, ha="left",
                weight="bold" if bold else "normal")


# --------------------------------------------------------------------------- #
# Fiber sagging: depth = c * span^p
# --------------------------------------------------------------------------- #
def _sag_pair(sc, specs, r, n_support=3, length=1.2):
    """Free spans side by side in one scene: specs = [(x_left, span, depth)].
    Returns the z of the chord through the fiber TOPS over each pair."""
    chords = []
    for x, sp, dep in specs:
        tp = support_stack(sc, x, n_support, r, length=length)
        support_stack(sc, x + sp, n_support, r, length=length)
        sc.tube(draped_fiber([x, x + sp], tp + r, [dep], overhang=0.45), r,
                color=BLUE)
        chords.append(tp + 2 * r)
    return chords


def _depth_marks(cv, P, specs, chords, t):
    """Dashed chord over each span and an arrow down to the fiber top at
    midspan."""
    for (x, sp, dep), zt in zip(specs, chords):
        mid = x + sp / 2
        cv.line(P((x, 0.0, zt)), P((x + sp, 0.0, zt)), color=t["hint"],
                lw=1.0, dash=(3, 3))
        cv.arrow(P((mid, 0.0, zt)), P((mid, 0.0, zt - dep)), lw=1.4, head=6)


def fig_sag(theme):
    """Left: what span and depth ARE. Middle: what c does (same span, c
    doubled -> depth doubled). Right: what p does (same c, span doubled ->
    depth x 2^p, i.e. x 4 at the shipped p = 2)."""
    pn = Panels(theme, 3)
    t = pn.t
    r = 0.36
    sup = (r"$p$", "hint", 7, 0.72)

    with pn.panel(0, "Span and depth",
                  eq=[(r"$depth = c\,\cdot\,span$", "hint"), sup],
                  caption=[
                      [("span", "ink"), (": between the two fibers holding it "
                                         "up", "hint")],
                      [("depth", "ink"), (": how far its middle sinks below "
                                          "them", "hint")],
                      "A fiber that meets material below stops there.",
                  ]) as cv:
        sc = Scene(VIEW_SIDE)
        supports = [0.0, 8.0]
        top = support_stack(sc, supports[0], 4, r)
        support_stack(sc, supports[1], 4, r)
        zc = top + r
        depth = 2.3
        sc.tube(draped_fiber(supports, zc, [depth], overhang=0.7), r,
                color=BLUE)
        img, proj, _ = sc.render(250, 140)
        P = place(cv, img, proj, 0, 0)
        a, b = P((0.0, 0.0, zc + r)), P((8.0, 0.0, zc + r))
        m = P((4.0, 0.0, zc - depth + r))
        cv.line(a, b, color=t["hint"], lw=1.1, dash=(4, 3))
        for q in (a, b, m):
            cv.dot(q)
        cv.arrow((m[0], a[1]), m, lw=1.7)
        cv.text((m[0] + 10, (a[1] + m[1]) / 2), "depth", size=13,
                weight="bold")
        s0, s1 = P((0.0, 0.0, 0.0)), P((8.0, 0.0, 0.0))
        ys = max(s0[1], s1[1]) + 18
        for s in (s0, s1):
            cv.line((s[0], s[1] + 4), (s[0], ys + 7), color=t["hint"], lw=0.9)
        cv.arrow((s0[0], ys), (s1[0], ys), lw=1.4)
        cv.text(((s0[0] + s1[0]) / 2, ys + 15), "span", size=13, ha="center",
                weight="bold")

    # c and p at one scale, so the two panels compare directly
    span_c, gap = 4.0, 2.2
    specs_c = [(0.0, span_c, 1.0), (span_c + gap, span_c, 2.0)]
    L, p = 2.5, 2.0
    specs_p = [(0.0, L, 0.5), (L + 2.2, 2 * L, 0.5 * 2 ** p)]
    sc_c, sc_p = Scene(VIEW_SIDE), Scene(VIEW_SIDE)
    ch_c = _sag_pair(sc_c, specs_c, r)
    ch_p = _sag_pair(sc_p, specs_p, r)
    k = min(sc_c.scale_to_fit(300, 110), sc_p.scale_to_fit(300, 110))

    with pn.panel(1, [(bf("c"), "ink"), ("  scales every droop", "ink")],
                  eq=[(r"$depth = $", "hint"), (bf("c"), "ink"),
                      (r"$\,\cdot\,span$", "hint"), sup],
                  caption=[
                      [(it("c"), "hint"), (" is the depth of a 1 mm span.",
                                           "hint")],
                      [("Twice ", "hint"), (it("c"), "hint"),
                       (": every span twice as deep.", "hint")],
                  ]) as cv:
        img, proj, _ = sc_c.render(300, 110, scale=k)
        P = place(cv, img, proj, 0, 0)
        _depth_marks(cv, P, specs_c, ch_c, t)
        for (x, sp, _d), lab in zip(specs_c, ("c", "2c")):
            q = P((x + sp / 2, 0.0, 0.0))
            cv.text((q[0], q[1] + 16), it(lab), size=14, ha="center")

    with pn.panel(2, [(bf("p"), "ink"), ("  makes long spans sag more", "ink")],
                  eq=[(r"$depth = c\,\cdot\,span$", "hint"),
                      (bf("p"), "ink", 7, 0.72)],
                  caption=[
                      [("Twice the span: depth × 2", "hint"),
                       (r"$p$", "hint", 5, 0.72), (", which is × 4 at ", "hint"),
                       (it("p = 2"), "hint"), (".", "hint")],
                  ]) as cv:
        img, proj, _ = sc_p.render(300, 110, scale=k)
        P = place(cv, img, proj, 0, 0)
        _depth_marks(cv, P, specs_p, ch_p, t)
        for (x, sp, _d), lab in zip(specs_p, ("span", "2 × span")):
            s0, s1 = P((x, 0.0, 0.0)), P((x + sp, 0.0, 0.0))
            ys = max(s0[1], s1[1]) + 10
            cv.arrow((s0[0], ys), (s1[0], ys), lw=1.1, head=5)
            cv.text(((s0[0] + s1[0]) / 2, ys + 13), lab, size=11,
                    ha="center")
    return pn.cv


# --------------------------------------------------------------------------- #
# Fiber fusion: Delta = phi * d, phi <= phi_max
# --------------------------------------------------------------------------- #
# Each lower fiber's end cap sits a hair behind the one above it: two coplanar
# caps z-fight into streaks where fused fibers overlap.
RECESS = 0.03
DELTA = r"\Delta"
DELTA_BF = r"$\mathbf{\Delta}$"     # upright, as the law strip sets it


def fig_fusion(theme):
    pn = Panels(theme, 3)
    t = pn.t
    r = 0.5
    d = 2 * r
    phi = 0.35                              # the shipped junction value
    delta = phi * d
    view = (0.42, -1.0, 0.30)
    L = 1.1

    with pn.panel(0, [("How far a fiber sinks: ", "ink"), (DELTA_BF, "ink")],
                  eq=[(r"$\Delta = $", "hint"), (bf(r"\varphi"), "ink"),
                      (r"$\,d$", "hint")],
                  caption=[
                      "Dashed: where it would sit with no fusion.",
                      [(it(r"\varphi"), "hint"),
                       (": the sink as a fraction of the diameter.", "hint")],
                      "0 stacks the fibers; 0.35 sinks about a third.",
                  ]) as cv:
        sc = Scene(view)
        sc.tube([(0, -L + RECESS, r), (0, L - RECESS, r)], r, color=BLUE_DEEP)
        z_up = 3 * r - delta
        sc.tube([(0, -L, z_up), (0, L, z_up)], r, color=BLUE)
        # the unfused position is a dashed outline over the render: a
        # translucent VTK tube sorts its own faces badly
        sc.anchors = [(0, -L, 4 * r)]
        img, proj, _ = sc.render(170, 180)
        P = place(cv, img, proj, 0, 0)
        th = np.linspace(0, 2 * np.pi, 90)
        cv.polyline([P((r * np.cos(a), -L, 3 * r + r * np.sin(a))) for a in th],
                    lw=1.1, dash=(3, 3))
        xl = P((-r, -L, 0))[0] - 30
        vdim(cv, t, P((-r, -L, d)), P((-r, -L, 0.0)), xl, it("d"))
        vdim(cv, t, P((-r, -L, 4 * r)), P((-r, -L, 4 * r - delta)), xl,
             DELTA_BF, bold=True, size=15)

    with pn.panel(1, "A stack gets shorter",
                  eq=[("each contact sinks ", "hint"), (it(DELTA), "hint")],
                  caption=["Fusion lowers peaks and walls, so a tall "
                           "scaffold ends up shorter than layers × d."]) as cv:
        su, sf = Scene(view), Scene(view)
        for k in range(3):
            su.tube([(0, -L, r + 2 * r * k), (0, L, r + 2 * r * k)], r,
                    color=BLUE)
            zk = r + k * (2 * r - delta)
            rk = RECESS * (2 - k)
            sf.tube([(0, -L + rk, zk), (0, L - rk, zk)], r, color=BLUE)
        kk = su.scale_to_fit(90, 170)
        img_u, pu, _ = su.render(90, 170, scale=kk)
        img_f, pf, _ = sf.render(90, 170, scale=kk)
        foot = (0, -L, 0)
        Pu = place(cv, img_u, pu, 0, 0)
        wu = img_u.shape[1] / OUT_SCALE
        xf = wu + 100
        # the two stacks stand on one baseline
        Pf = place(cv, img_f, pf, xf, Pu(foot)[1] - pf(foot)[1] / OUT_SCALE)
        vdim(cv, t, Pu((-r, -L, 6 * r)), Pu((-r, -L, 0)),
             Pu((-r, -L, 0))[0] - 14, it("3d"))
        vdim(cv, t, Pf((-r, -L, 6 * r - 2 * delta)), Pf((-r, -L, 0)),
             Pf((-r, -L, 0))[0] - 14, r"$3d - 2\Delta$")
        yl = Pu(foot)[1] + 20
        cv.text((wu / 2, yl), "unfused", size=11, ha="center",
                color=t["hint"])
        cv.text((xf + img_f.shape[1] / OUT_SCALE / 2, yl), "fused", size=11,
                ha="center", color=t["hint"])

    with pn.panel(2, "Two kinds of contact",
                  eq=[(r"$\varphi \leq \varphi_{max}$", "hint")],
                  caption=[
                      "Junction: two fibers cross at a point.",
                      [("Straight span: fibers lie along each other. It "
                        "can take its own ", "hint"),
                       (it(r"\varphi"), "hint"), (" or reuse the junction's.",
                                               "hint")],
                      [(it(r"\varphi_{max}"), "hint"), (" caps both.", "hint")],
                  ]) as cv:
        sj, sw = Scene(VIEW_OBLIQUE), Scene(VIEW_OBLIQUE)
        sj.tube([(-1.4, 0, r), (1.4, 0, r)], r, color=BLUE_DEEP)
        sj.tube([(0, -1.4, 3 * r - delta), (0, 1.4, 3 * r - delta)], r,
                color=BLUE)
        sw.tube([(-1.4 + RECESS, 0, r), (1.4 - RECESS, 0, r)], r,
                color=BLUE_DEEP)
        sw.tube([(-1.4, 0, 3 * r - delta), (1.4, 0, 3 * r - delta)], r,
                color=BLUE)
        k = min(sj.scale_to_fit(135, 130), sw.scale_to_fit(135, 130))
        hs = []
        for i, (sc, lab) in enumerate(((sj, "junction"),
                                       (sw, "straight span"))):
            img, proj, _ = sc.render(135, 130, scale=k)
            x0 = i * 165
            place(cv, img, proj, x0, 0)
            hs.append((x0 + img.shape[1] / OUT_SCALE / 2, lab))
            yl = img.shape[0] / OUT_SCALE
        for xc, lab in hs:
            cv.text((xc, yl + 18), lab, size=12, ha="center", weight="bold")
    return pn.cv


# --------------------------------------------------------------------------- #
# Jet lag: lag = H A (1 - eps^-b), eps = v / CTS
# --------------------------------------------------------------------------- #
_LAG_A, _LAG_B = 2.8628571428571425, 0.304          # mew_physics defaults


def _lag_over_h(eps, a=_LAG_A, b=_LAG_B):
    eps = np.maximum(np.asarray(eps, float), 1.0)
    return a * (1.0 - eps ** -b)


def fig_lag(theme):
    pn = Panels(theme, 2)
    t = pn.t

    with pn.panel(0, "The fiber lands behind the nozzle",
                  eq=[(it("H"), "hint"), (": nozzle-to-collector gap", "hint")],
                  caption=["The faster the nozzle moves, the further "
                           "behind the nozzle the fiber lands."]) as cv:
        H = 4.0
        lag = H * _lag_over_h(2.0)               # at twice CTS
        sc = Scene((0.0, -1.0, 0.16))
        plate(sc, t, -lag - 3.0, 2.0, -0.9, 0.9)
        r_n = 0.32
        sc.cylinder((0, 0, H), (0, 0, H + 2.2), r_n, STEEL)
        pts, rad = taylor_cone(H, r_n * 0.9, 0.5, H - 0.9, 0.06)
        jet = bezier((0, 0, H - 0.9), (0, 0, H * 0.35),
                     (-lag * 0.45, 0, 0.06), (-lag, 0, 0.06))
        sc.tube(pts, 0, radii=rad, color=BLUE)
        sc.tube(jet, 0.06, color=BLUE)
        sc.tube([(-lag, 0, 0.06), (-lag - 2.8, 0, 0.06)], 0.06, color=BLUE)
        img, proj, _ = sc.render(250, 150)
        P = place(cv, img, proj, 0, 0)
        top = P((0, 0, H + 2.2))
        cv.arrow((top[0] - 34, top[1] - 12), (top[0] + 34, top[1] - 12),
                 lw=1.4, both=False, head=7)
        cv.text((top[0] + 42, top[1] - 12), it("v"), size=15)
        tip, foot = P((0, 0, H)), P((0, 0, 0))
        xh = tip[0] + 40
        cv.line((tip[0] + 6, tip[1]), (xh + 6, tip[1]), color=t["hint"],
                lw=0.8)
        cv.arrow((xh, tip[1]), (xh, foot[1]), lw=1.3, head=6)
        cv.text((xh + 8, (tip[1] + foot[1]) / 2), it("H"), size=15)
        land = P((-lag, 0, 0))
        yl = foot[1] + 24
        for q in (land, foot):
            cv.line((q[0], q[1] + 6), (q[0], yl + 6), color=t["hint"], lw=0.8)
        cv.arrow((land[0], yl), (foot[0], yl), lw=1.8, head=7)
        cv.text(((land[0] + foot[0]) / 2, yl + 16), "lag", size=13,
                ha="center", weight="bold")

    with pn.panel(1, "How lag grows with speed",
                  eq=[(r"$lag = H\,$", "hint"), (bf("A"), "ink"),
                      (r"$\,(1 - \varepsilon$", "hint"),
                      (r"$-$", "hint", 7, 0.72), (bf("b"), "ink", 7, 0.72),
                      (r"$\,)$", "hint"),
                      (r"$\quad \varepsilon = v\,/\,CTS$", "hint")],
                  caption=[
                      [(bf("A"), "ink"), (": the most lag, as a multiple of the "
                                          "gap.", "hint")],
                      [(bf("b"), "ink"), (": how quickly the lag climbs toward "
                                          "it.", "hint")],
                  ]) as cv:
        box = (0, 0, 280, 150)
        m = graph_axes(cv, box, t, "lag / H")
        e_max, y_max = 10.0, 3.15
        es = np.linspace(1.0, e_max, 200)

        def pt(e, v):
            return m((e - 1.0) / (e_max - 1.0), v / y_max)
        cv.line(pt(1.0, _LAG_A), pt(e_max, _LAG_A), color=t["hint"], lw=1.0,
                dash=(4, 3))
        q = pt(e_max, _LAG_A)
        cv.rich((q[0], q[1] - 8), [("limit ", "ink"), (bf("A"), "ink"),
                                   (r"$\,\cdot\,H$", "ink")], size=12,
                ha="right")
        cv.polyline([pt(e, v) for e, v in zip(es, _lag_over_h(es, b=0.7))],
                    color=t["hint"], lw=1.5, dash=(5, 3))
        cv.polyline([pt(e, v) for e, v in zip(es, _lag_over_h(es))],
                    color=_hex(BLUE), lw=2.6)
        q = pt(4.6, float(_lag_over_h(4.6, b=0.7)))
        cv.rich((q[0] + 4, q[1] + 20), [("larger ", "hint"), (bf("b"), "ink"),
                                         (": climbs sooner", "hint")],
                size=10.5)
        o = pt(1.0, 0.0)
        cv.text((o[0], o[1] + 15), "CTS", size=10.5, ha="center",
                color=t["hint"])
        cv.rich((box[2], o[1] + 19), [("speed ratio  ", "hint"),
                                      (r"$\varepsilon$", "hint")],
                size=10.5, ha="right")
    return pn.cv


# --------------------------------------------------------------------------- #
# Nozzle bore cutaway, shared by Material and Fiber diameter
# --------------------------------------------------------------------------- #
def _bore_scene(t, r_in=0.55, wall=0.35, L=4.2, melt=True):
    sc = Scene((0.0, -1.0, 0.22))
    z0, z1 = 0.0, L
    R = 1.9                                   # the reservoir above the bore
    section(sc, [(r_in, z0), (r_in + wall, z0), (r_in + wall, z1),
                 (R + wall, z1 + 0.8), (R + wall, z1 + 2.6), (R, z1 + 2.6),
                 (R, z1 + 0.8 + wall * 0.4), (r_in, z1 + wall * 0.5)], STEEL)
    if melt:
        section(sc, [(0, z0), (r_in, z0), (r_in, z1 + wall * 0.5),
                     (R, z1 + 0.8 + wall * 0.4), (R, z1 + 2.2), (0, z1 + 2.2)],
                BLUE)
        pts, rad = taylor_cone(z0 + 0.02, r_in, 0.45, z0 - 1.6, 0.09)
        sc.tube(pts, 0, radii=rad, color=BLUE)
    return sc, dict(r_in=r_in, wall=wall, L=L, R=R)


def _bore_marks(cv, P, g, t, diameter=False):
    """The needle's hole and length. `diameter`: the hole is dimensioned
    wall to wall as D, the Material card's field; otherwise axis to wall as
    r, the flow law's symbol (r = D/2)."""
    r_in, L = g["r_in"], g["L"]
    if diameter:
        a, b = P((-r_in, 0, L * 0.38)), P((r_in, 0, L * 0.38))
        cv.arrow(a, b, lw=1.4, both=True, head=5)
        cv.line((b[0] + 3, b[1]), (b[0] + 30, b[1]), color=t["hint"],
                lw=0.8)
        cv.text((b[0] + 34, b[1]), bf("D"), size=15, ha="left")
    else:
        a, b = P((0, 0, L * 0.38)), P((r_in, 0, L * 0.38))
        cv.line(P((0, 0, L * 0.38 - 0.5)), P((0, 0, L * 0.38 + 0.5)),
                lw=0.8, dash=(2, 2))
        cv.arrow(a, b, lw=1.4, both=False, head=5)
        cv.line((b[0] + 3, b[1]), (b[0] + 30, b[1]), color=t["hint"],
                lw=0.8)
        cv.text((b[0] + 34, b[1]), bf("r"), size=15, ha="left")
    a, b = P((-r_in - g["wall"], 0, L)), P((-r_in - g["wall"], 0, 0))
    vdim(cv, t, a, b, a[0] - 20, bf("L"), size=15)


# --------------------------------------------------------------------------- #
# Fiber diameter: d = sqrt(4Q / (pi v)), Q = pi r^4 dP / (8 eta L)
# --------------------------------------------------------------------------- #
def fig_diameter(theme):
    pn = Panels(theme, 2)
    t = pn.t

    with pn.panel(0, "Flow out of the nozzle",
                  eq=[(r"$Q = \pi\, r^4\, \Delta P \,/\, (8\, \eta\, L)$",
                       "hint")],
                  caption=["Pressure and temperature: Process card.",
                           "r = D/2, L and η: Material card."]) as cv:
        sc, g = _bore_scene(t)
        img, proj, _ = sc.render(150, 175)
        P = place(cv, img, proj, 0, 0)
        _bore_marks(cv, P, g, t)
        top = P((0, 0, g["L"] + 2.6))
        cv.arrow((top[0], top[1] - 26), (top[0], top[1] + 14), lw=1.8,
                 both=False, head=8)
        cv.rich((top[0] + 12, top[1] - 14), [(r"$\Delta P$", "ink"),
                                             ("  pressure", "ink")], size=12)
        out = P((0, 0, -1.6))
        cv.rich((out[0] + 14, out[1] - 6), [(it("Q"), "ink"),
                                            ("  flow", "ink")], size=12)

    with pn.panel(1, "Faster stage, thinner fiber",
                  eq=[(r"$d = $", "hint"), ("√", "hint"),
                      (r"$(4Q\,/\,\pi\, v)$", "hint")],
                  caption=["Twice the speed spreads the same melt over "
                           "twice the length: the fiber is 1/√2 as thick."]
                  ) as cv:
        sc = Scene((0.0, -0.75, 1.0))
        plate(sc, t, -0.4, 7.4, -1.0, 6.0)
        d0 = 0.9
        rows = [(1.0, "v", "d"), (2.0, "2v", "0.71 d"), (4.0, "4v", "d / 2")]
        for i, (mult, _lab, _dl) in enumerate(rows):
            dd = d0 / math.sqrt(mult)
            y = 5.0 - i * 2.4
            sc.tube([(0, y, dd / 2), (7.0, y, dd / 2)], dd / 2, color=BLUE)
        img, proj, _ = sc.render(170, 170)
        P = place(cv, img, proj, 0, 0)
        for i, (mult, lab, dl) in enumerate(rows):
            y = 5.0 - i * 2.4
            a = P((7.4, y, 0.0))
            cv.rich((a[0] + 12, a[1] - 4), [("speed ", "hint"),
                                            (it(lab), "ink"),
                                            ("  →  ", "hint"), (dl, "ink")],
                    size=12)
    return pn.cv


# --------------------------------------------------------------------------- #
# Material: the needle (D, L) and eta(T)
# --------------------------------------------------------------------------- #
_R_GAS = 8.314
_ETA_REF, _T_REF, _EA = 1040.6, 100.0, 31.3e3          # mew_physics defaults


def _eta(T, ea=_EA):
    T = np.asarray(T, float) + 273.15
    return _ETA_REF * np.exp(ea / _R_GAS * (1.0 / T - 1.0 / (_T_REF + 273.15)))


def fig_material(theme):
    pn = Panels(theme, 3)
    t = pn.t

    with pn.panel(0, "The needle",
                  eq=[(bf("D"), "ink"), (": inner diameter   ", "hint"),
                      (bf("L"), "ink"), (": length", "hint")],
                  caption=["Flow grows as D⁴ and falls as 1/L.",
                           "Measure both on your own needle."]) as cv:
        sc, g = _bore_scene(t)
        img, proj, _ = sc.render(150, 200)
        P = place(cv, img, proj, 0, 0)
        _bore_marks(cv, P, g, t, diameter=True)

    with pn.panel(1, "Viscosity and temperature",
                  eq=[(r"$\eta(T) = \eta_{ref}\, \exp[$", "hint"),
                      (bf("E_a"), "ink"),
                      (r"$/R\,(1/T - 1/T_{ref})]$", "hint")],
                  caption=["One measured point anchors the curve.",
                           [(bf("E_a"), "ink"),
                            (" sets how fast the melt thins as it heats.",
                             "hint")]]) as cv:
        box = (0, 0, 280, 150)
        m = graph_axes(cv, box, t, "viscosity  " + it(r"\eta"))
        T0, T1 = 70.0, 130.0
        Ts = np.linspace(T0, T1, 120)
        y_max = float(_eta(T0, 2 * _EA)) * 1.02

        def pt(T, e):
            return m((T - T0) / (T1 - T0), e / y_max)
        cv.polyline([pt(T, e) for T, e in zip(Ts, _eta(Ts, 2 * _EA))],
                    color=t["hint"], lw=1.5, dash=(5, 3))
        cv.polyline([pt(T, e) for T, e in zip(Ts, _eta(Ts))],
                    color=_hex(BLUE), lw=2.6)
        a = pt(_T_REF, _ETA_REF)
        cv.dot(a, r=4.5)
        cv.rich((a[0] - 6, a[1] - 14), [("anchor (", "ink"),
                                        (bf("T_{ref}"), "ink"), (", ", "ink"),
                                        (bf(r"\eta_{ref}"), "ink"),
                                        (")", "ink")], size=11.5)
        q = pt(76.0, float(_eta(76.0, 2 * _EA)))
        cv.rich((q[0] + 8, q[1] + 2), [("larger ", "hint"), (bf("E_a"), "ink"),
                                       (": more sensitive", "hint")],
                size=10.5)
        cv.rich((box[2], box[3] + 19), [("temperature  ", "hint"),
                                        (it("T"), "hint")],
                size=10.5, ha="right")
    _jet_panel(pn, 2)
    return pn.cv


def _jet_panel(pn, i):
    """The drawn jet: the Taylor cone at the nozzle, its base pinned to the
    needle's inner diameter D (JetState.cone_base_radius = D/2), necking to half that width at
    the knee height, then bending back to its touchdown -- the player's
    cone-jet profile along its jet arc."""
    t = pn.t
    with pn.panel(i, "The jet",
                  eq=[(bf("D"), "ink"),
                      (": the cone's base is the needle's inner diameter",
                       "hint")],
                  caption=[
                      [("Knee height", "ink"),
                       (": how far below the tip the cone narrows to half its "
                        "width, as a fraction of the gap ", "hint"),
                       (it("H"), "hint"), (".", "hint")],
                      "Drawing only.",
                  ]) as cv:
        sc = Scene((0.0, -1.0, 0.12))
        H = 4.0
        knee_frac = 0.12                        # mew_physics cone_knee_frac
        r0, rj, lag = 0.62, 0.05, 1.9
        plate(sc, t, -lag - 1.5, 1.3, -0.8, 0.8)
        sc.cylinder((0, 0, H), (0, 0, H + 1.4), r0 * 1.05, STEEL)
        # down through the cone, then back to the touchdown; the radius
        # follows arc length from the tip, as the player's profile does
        path = bezier((0, 0, H), (0, 0, H * 0.35), (-lag * 0.45, 0, rj),
                      (-lag, 0, rj), n=200)
        s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(
            np.diff(path, axis=0), axis=1))])
        rad = np.maximum(rj, r0 * 0.5 ** (s / (knee_frac * H)))
        sc.tube(path, 0, radii=rad, color=BLUE)
        sc.tube([(-lag, 0, rj), (-lag - 1.3, 0, rj)], rj, color=BLUE)
        img, proj, _ = sc.render(220, 200)
        P = place(cv, img, proj, 0, 0)
        # D: edge to edge of the cone at the tip, labelled on the left
        a, b = P((r0, 0, H - 0.04)), P((-r0, 0, H - 0.04))
        cv.arrow(a, b, lw=1.4, both=True, head=5)
        cv.line((b[0] - 3, b[1]), (b[0] - 22, b[1]), color=t["hint"], lw=0.8)
        cv.text((b[0] - 26, b[1]), bf("D"), size=15, ha="right")
        # knee height, beside the cone
        knee = H - knee_frac * H
        edge, kn = P((r0, 0, H)), P((r0 / 2, 0, knee))
        xk = edge[0] + 20
        cv.line((edge[0] + 3, edge[1]), (xk + 6, edge[1]), color=t["hint"],
                lw=0.8)
        cv.line((kn[0] + 3, kn[1]), (xk + 6, kn[1]), color=t["hint"], lw=0.8)
        cv.arrow((xk, edge[1]), (xk, kn[1]), lw=1.7, head=5)
        cv.text((xk + 8, (edge[1] + kn[1]) / 2), "knee height", size=12,
                weight="bold")
        cv.line(P((-r0 / 2, 0, knee)), P((r0 / 2, 0, knee)), lw=1.2)
        # the gap, at the right
        tip, foot = P((0, 0, H)), P((1.3, 0, 0))
        xh = xk + 100
        cv.line((xk + 8, tip[1]), (xh + 6, tip[1]), color=t["hint"], lw=0.8)
        cv.line((foot[0] + 3, foot[1]), (xh + 6, foot[1]), color=t["hint"],
                lw=0.8)
        cv.arrow((xh, tip[1]), (xh, foot[1]), lw=1.2, head=6)
        cv.text((xh + 8, (tip[1] + foot[1]) / 2), it("H"), size=14)


# --------------------------------------------------------------------------- #
# Critical translation speed
# --------------------------------------------------------------------------- #
def fig_cts(theme):
    pn = Panels(theme, 1)
    t = pn.t
    with pn.panel(0, "Below and above CTS",
                  caption=[
                      [(bf("CTS"), "ink"), (": the slowest speed that lays a "
                                            "straight fiber.", "hint")],
                      "Measure it on your rig: the model does not predict it.",
                      "Below it, the app draws the fiber as this wave.",
                  ]) as cv:
        rf = 0.09
        sc = Scene((0.0, -0.75, 1.0))
        py0, py1 = -0.55, 2.95
        plate(sc, t, -0.5, 9.5, py0, py1)
        # below CTS the app draws the fiber as mew_coil_wave's lateral sine
        # (the shipped 0.1 mm amplitude over a 0.3 mm wavelength, scaled up),
        # its offset tapered to zero over the first and last half wavelength
        amp, wl = 0.42, 1.26
        xs = np.linspace(0.0, 9.0, 900)
        taper = np.clip(np.minimum(xs, 9.0 - xs) / (0.5 * wl), 0.0, 1.0)
        ys = 2.2 + amp * np.sin(2 * np.pi * xs / wl) * taper
        sc.tube(np.column_stack([xs, ys, np.full_like(xs, rf)]), rf,
                color=BLUE, sides=20)
        sc.tube([(0, 0.0, rf), (9.0, 0.0, rf)], rf, color=BLUE, sides=20)
        img, proj, _ = sc.render(310, 120)
        P = place(cv, img, proj, 0, 0)
        a = P((-0.5, py1, 0))
        cv.rich((a[0], a[1] - 10), [("slower than ", "ink"), (it("CTS"), "ink"),
                                    (": coils, drawn as a wave", "ink")],
                size=12)
        b = P((-0.5, py0, -0.18))
        cv.rich((b[0], b[1] + 20), [("at ", "ink"), (it("CTS"), "ink"),
                                    (" or faster: straight", "ink")], size=12)
    return pn.cv


# --------------------------------------------------------------------------- #
# Process: the rig
# --------------------------------------------------------------------------- #
def fig_process(theme):
    pn = Panels(theme, 1)
    t = pn.t
    with pn.panel(0, "What you set for each print",
                  caption=[
                      [(it(r"\Delta P"), "ink"), (" pushes the melt out of the "
                                                  "nozzle.", "hint")],
                      [(it("T"), "ink"), (" sets how runny the melt is.", "hint")],
                      [(it("V"), "ink"), (" is applied from nozzle to collector.",
                                          "hint")],
                      [(it("H"), "ink"), (" is the nozzle tip's height above it.",
                                          "hint")],
                  ]) as cv:
        sc = Scene((0.42, -1.0, 0.28))
        H = 4.6
        plate(sc, t, -3.4, 3.4, -2.2, 2.2)
        r_n, z_n = 0.24, H
        sc.cylinder((0, 0, z_n), (0, 0, z_n + 1.6), r_n, STEEL)
        sc.cylinder((0, 0, z_n + 1.6), (0, 0, z_n + 2.0), 0.9, STEEL)
        sc.cylinder((0, 0, z_n + 2.0), (0, 0, z_n + 5.2), 1.05, STEEL)
        sc.cylinder((0, 0, z_n + 2.5), (0, 0, z_n + 3.6), 1.18, STEEL_DARK)
        pts, rad = taylor_cone(z_n, r_n * 0.9, 0.5, 0.05, 0.05)
        sc.tube(pts, 0, radii=rad, color=BLUE)
        sc.tube([(0, 0, 0.05), (-2.8, 0, 0.05)], 0.05, color=BLUE)
        img, proj, _ = sc.render(150, 186)
        P = place(cv, img, proj, 0, 0)
        x_lab = P((1.18, 0, z_n + 3.05))[0] + 34

        def leader(world, parts):
            a = P(world)
            cv.line(a, (x_lab - 6, a[1]), color=t["hint"], lw=0.9)
            cv.dot(a, r=2.2, color=t["hint"])
            cv.rich((x_lab, a[1] + 5), parts, size=12)

        top = P((0, 0, z_n + 5.2))
        cv.arrow((top[0], top[1] - 26), (top[0], top[1] + 4), lw=1.8,
                 both=False, head=8)
        cv.rich((top[0] + 12, top[1] - 12), [("pressure ", "ink"),
                                             (it(r"\Delta P"), "ink")],
                size=12)
        leader((1.18, 0, z_n + 3.05), [("temperature ", "ink"),
                                       (it("T"), "ink")])
        leader((r_n, 0, z_n + 0.8), [("voltage ", "ink"), (it("V"), "ink")])
        tip, foot = P((0, 0, z_n)), P((0, 0, 0))
        xh = x_lab + 8
        cv.line((tip[0] + 8, tip[1]), (xh + 6, tip[1]), color=t["hint"],
                lw=0.8)
        cv.line((foot[0] + 8, foot[1]), (xh + 6, foot[1]), color=t["hint"],
                lw=0.8)
        cv.arrow((xh, tip[1]), (xh, foot[1]), lw=1.4, head=7)
        cv.rich((xh + 10, (tip[1] + foot[1]) / 2 + 5),
                [("gap ", "ink"), (it("H"), "ink")], size=12)
    return pn.cv


# --------------------------------------------------------------------------- #
# Export / Visualization: the coiling wave
# --------------------------------------------------------------------------- #
def fig_visual(theme):
    pn = Panels(theme, 1)
    t = pn.t
    with pn.panel(0, "Coiling wave below CTS",
                  caption=["Below CTS the fiber is drawn as this wave, to "
                           "show where CTS was crossed.",
                           "It is a marker, not a model of real coiling. It "
                           "needs Jet lag on."]
                  ) as cv:
        sc = Scene((0.0, -0.55, 1.0))
        plate(sc, t, -0.5, 8.9, -1.9, 1.9)
        amp, wl = 0.62, 1.86                # the shipped 0.1 / 0.3 mm, scaled
        xs = np.linspace(0, 8.4, 700)
        ys = amp * np.sin(2 * np.pi * xs / wl)
        rf = 0.08
        sc.tube(np.column_stack([xs, ys, np.full_like(xs, rf)]), rf,
                color=BLUE, sides=20)
        img, proj, _ = sc.render(300, 150)
        P = place(cv, img, proj, 0, 0)
        x_c = wl * 3.75                     # a trough
        c0, c1 = P((x_c, 0, 0)), P((x_c, -amp, 0))
        cv.line(P((x_c - 1.1, 0, 0)), P((x_c + 1.1, 0, 0)), lw=0.9,
                dash=(3, 3))
        cv.arrow(c0, c1, lw=1.5, head=5)
        lab = P((x_c, -1.45, 0))
        cv.text(lab, "amplitude", size=12.5, ha="center", weight="bold")
        w0, w1 = P((wl * 0.25, amp, 0)), P((wl * 1.25, amp, 0))
        yw = min(w0[1], w1[1]) - 14
        for q in (w0, w1):
            cv.line((q[0], q[1] - 4), (q[0], yw - 5), lw=0.8)
        cv.arrow((w0[0], yw), (w1[0], yw), lw=1.5, head=5)
        cv.text((w1[0] + 10, yw), "wavelength", size=12.5, weight="bold")
    return pn.cv


FIGURES = {
    "sag": fig_sag,
    "fusion": fig_fusion,
    "lag": fig_lag,
    "diameter": fig_diameter,
    "material": fig_material,
    "cts": fig_cts,
    "process": fig_process,
    "visual": fig_visual,
}


def main(argv):
    want = set(argv) or set(FIGURES)
    for key in FIGURES:
        if key not in want:
            continue
        for theme in THEMES:
            cv = FIGURES[key](theme)
            path = os.path.join(HERE, "%s_%s.png" % (key, theme))
            cv.save(path)
            print("wrote", os.path.relpath(path, REPO))


if __name__ == "__main__":
    main(sys.argv[1:])
