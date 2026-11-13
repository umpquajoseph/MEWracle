#!/usr/bin/env python3
"""micrograph_viewer.py -- the VTK micrograph-overlay viewer.

A real VTK viewer in the SAME shape as the STL / simulation viewers: an embedded
panel in the modern shell and a native fullscreen surface, both driven by
`viewer_embed.EmbedHost`. It replaced the matplotlib "measuring workspace".

The uploaded micrograph is textured onto a plane on the **XY plane** and the
generated scaffold (the run's `.depo` centrelines, or the STL mesh) is drawn on
top of it and STAYS FIXED: every control here moves the IMAGE, not the scaffold
-- Origin X/Y/Z (sliders, typed values, arrow keys with Shift for a x10 step),
Reset to the launch placement, independent image and scaffold opacity, Scale X/Y
(mm/px) and Rotation, and Snap top-down. Precise scale is defined once in the 2D
"Scale image" window and handed in, then still adjustable.

Controls are mounted through the shared chrome, so ONE definition serves both
surfaces. Reuses stl_viewer's actor builders (`_load_centerlines`,
`_centerline_actor`, `_stl_actor`, `_render_sides`) and vtk_helpers
(`install_cad_camera`, `origin_marker`).
"""

if __name__ == "__main__":  # run as a script: put the program's folders on sys.path
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "engine"))
    import app_paths  # noqa: E402,F401

import math
import os
import sys

import vtk

from vtk_helpers import fit_clipping_range, install_cad_camera, origin_marker

# The upper-right ViewCube; guarded like the shell import so a hand-launched
# viewer still opens without it.
try:
    import view_cube as _view_cube
except Exception:  # noqa: BLE001
    _view_cube = None
import stl_viewer


# --------------------------------------------------------------------------- #
# image -> textured plane
# --------------------------------------------------------------------------- #
def pil_rgb8(im):
    """A PIL image as an (H, W, 3) uint8 RGB array.

    PIL's `convert("RGB")` CLIPS a high-bit-depth greyscale image (I;16, I, F)
    at 255 instead of scaling it, so a 16-bit SEM micrograph came out solid
    white. Those modes are stretched over the image's own min..max instead --
    the overlay is a visual alignment aid, and the stretch is what keeps its
    structure readable whatever part of the range the detector used. A flat
    image has no range to stretch and is drawn mid-grey."""
    import numpy as np
    if im.mode.startswith(("I", "F")):
        a = np.asarray(im, dtype=np.float64)
        lo, hi = float(np.nanmin(a)), float(np.nanmax(a))
        if hi > lo:
            g = np.nan_to_num((a - lo) * (255.0 / (hi - lo)))
        else:
            g = np.full(a.shape, 128.0)
        g = g.clip(0, 255).astype(np.uint8)
        return np.ascontiguousarray(np.stack([g] * 3, axis=-1))
    return np.asarray(im.convert("RGB"))


def load_image_rgb(path):
    """Read `path` into an (H, W, 3) uint8 RGB array. PIL handles JPG/TIFF/BMP;
    matplotlib reads PNG on its own. Returns None on failure."""
    try:
        import numpy as np
        try:
            from PIL import Image
            arr = pil_rgb8(Image.open(path))
        except Exception:  # noqa: BLE001 -- no PIL: PNG via matplotlib
            import matplotlib.image as mpimg
            raw = mpimg.imread(path)
            if raw.dtype.kind == "f":
                raw = (raw * 255).clip(0, 255).astype(np.uint8)
            if raw.ndim == 2:
                raw = np.stack([raw] * 3, axis=-1)
            arr = raw[:, :, :3]
        return np.ascontiguousarray(arr, dtype=np.uint8)
    except Exception:  # noqa: BLE001
        return None


def image_texture(rgb):
    """A vtkTexture from an (H, W, 3) uint8 array. Rows are flipped so the image
    reads upright on a plane whose texture origin is bottom-left."""
    import numpy as np
    from vtk.util import numpy_support
    h, w = rgb.shape[:2]
    flat = np.ascontiguousarray(rgb[::-1].reshape(-1, 3))
    img = vtk.vtkImageData()
    img.SetDimensions(int(w), int(h), 1)
    vtkarr = numpy_support.numpy_to_vtk(flat, deep=1,
                                        array_type=vtk.VTK_UNSIGNED_CHAR)
    vtkarr.SetNumberOfComponents(3)
    img.GetPointData().SetScalars(vtkarr)
    tex = vtk.vtkTexture()
    tex.SetInputData(img)
    tex.InterpolateOn()
    return tex


def image_plane_actor(w_px, h_px, tex):
    """A textured plane spanning [0,w_px] x [0,h_px] in local (pixel) units; the
    _ImageState transform scales it to mm and places/rotates it. Unlit so the
    micrograph's own tones read true regardless of the scene lighting."""
    plane = vtk.vtkPlaneSource()
    plane.SetOrigin(0.0, 0.0, 0.0)
    plane.SetPoint1(float(w_px), 0.0, 0.0)
    plane.SetPoint2(0.0, float(h_px), 0.0)
    plane.SetXResolution(1)
    plane.SetYResolution(1)
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(plane.GetOutputPort())
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.SetTexture(tex)
    actor.GetProperty().SetLighting(False)
    actor.GetProperty().SetAmbient(1.0)
    actor.GetProperty().SetDiffuse(0.0)
    return actor


class _ImageState:
    """The image plane's live placement: per-axis scale (mm/px), rotation (deg),
    origin (mm) and Z, plus opacity. `apply()` rebuilds the actor's transform and
    renders; `reset()` restores the launch snapshot.

    Anchoring: (ox, oy) is the world XY of the image's VISUAL CENTRE, and both
    rotation and scale pivot about that centre. So dragging Rotation or Scale
    keeps the image in place -- it spins / grows about its own middle instead of
    swinging away about a pixel-space corner. Origin X/Y still translate it. The
    transform is a pure function of the state values (rebuilt from scratch every
    apply), so it never accumulates: rot 0->90->0 and scale 1->2->1 return
    EXACTLY to the start, and rotate-then-scale-then-rotate stays centred.

    `pw`, `ph` are the image's local pixel dimensions -- the half-extents
    (pw/2, ph/2) are what the pre-translate recentres onto the origin so the
    centre, not the corner, is the pivot."""

    def __init__(self, actor, window, sx, sy, rot, ox, oy, z, opacity=1.0,
                 pw=0.0, ph=0.0):
        self.actor = actor
        self.window = window
        self.pw, self.ph = float(pw), float(ph)
        self.sx, self.sy = float(sx), float(sy)
        self.rot = float(rot)
        self.ox, self.oy, self.z = float(ox), float(oy), float(z)
        self.opacity = float(opacity)
        self._home = (self.sx, self.sy, self.rot, self.ox, self.oy, self.z,
                      self.opacity)
        self.apply()

    def apply(self):
        t = vtk.vtkTransform()
        t.PostMultiply()
        # net = Translate(ox,oy,z) * RotateZ * Scale * Translate(-pw/2,-ph/2)
        # The leading recentre puts the image's pixel CENTRE at the local origin,
        # so Scale and RotateZ both pivot about the centre; the final Translate
        # then places that centre at (ox, oy, z). Corner-pivot swing is gone.
        t.Translate(-self.pw / 2.0, -self.ph / 2.0, 0.0)
        t.Scale(self.sx, self.sy, 1.0)
        t.RotateZ(self.rot)
        t.Translate(self.ox, self.oy, self.z)
        self.actor.SetUserTransform(t)
        self.actor.GetProperty().SetOpacity(self.opacity)
        self.render()

    def render(self):
        try:
            self.window.Render()
        except Exception:  # noqa: BLE001
            pass

    def reset(self):
        (self.sx, self.sy, self.rot, self.ox, self.oy, self.z,
         self.opacity) = self._home
        self.apply()

    def nudge(self, dx=0.0, dy=0.0, dz=0.0):
        self.ox += dx
        self.oy += dy
        self.z += dz
        self.apply()


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# scaffold actors (reuse stl_viewer)
# --------------------------------------------------------------------------- #
def _scaffold_actors(stl_path, render):
    """Every scaffold actor to draw + a representative diagonal for sizing the
    origin triad / move step. Prefers the `.depo` centrelines (instant, no mesh);
    falls back to the STL mesh."""
    actors = []
    centerlines = (stl_viewer._load_centerlines(stl_path)
                   if render != "exact" else None)
    if centerlines is not None:
        cl_paths, cl_pdia, _groups = centerlines
        sides = stl_viewer._render_sides(render)
        actors.append(stl_viewer._centerline_actor(
            cl_paths, cl_pdia, sides, stl_viewer._SOLID_COLOR))
    elif os.path.isfile(stl_path):
        a = stl_viewer._stl_actor(stl_path, stl_viewer._SOLID_COLOR)
        if a is not None:
            actors.append(a)
    return actors


# --------------------------------------------------------------------------- #
# the viewer
# --------------------------------------------------------------------------- #
def view(stl_path, image_path, gcode_path=None, scale=None,
         origin=None, rot=0.0, z=None, render="tube:12", embed=None):
    """`scale`/`origin` default to None -> the image is AUTO-FIT to the
    scaffold's XY footprint (so it's always visible on open); pass them to
    override. Every control is a slider so the embedded rail and the native
    fullscreen sidebar render identically."""
    rgb = load_image_rgb(image_path)
    if rgb is None:
        sys.stderr.write(f"micrograph_viewer: could not read image: "
                         f"{image_path}\n")
        return 1
    h_px, w_px = rgb.shape[:2]

    try:
        import gui_viewer_shell as _shell
        _bg = _shell.viewport_bg(_shell.read_dark_pref())
        _themed = True
    except Exception:  # noqa: BLE001
        _shell, _bg, _themed = None, (0.11, 0.11, 0.13), False

    renderer = vtk.vtkRenderer()
    renderer.SetBackground(*_bg)

    scaffold_actors = _scaffold_actors(stl_path, render)
    for a in scaffold_actors:
        renderer.AddActor(a)

    # scaffold XY footprint -> the image's fit placement (see _fit_placement)
    b = renderer.ComputeVisiblePropBounds()
    finite = all(map(math.isfinite, b)) and b[1] >= b[0]
    if not finite:
        b = (0.0, 1.0, 0.0, 1.0, 0.0, 0.0)
    diag = math.dist((b[0], b[2], b[4]), (b[1], b[3], b[5])) or 1.0

    def _fit_placement():
        """Scale + origin so the image spans the scaffold's XY footprint,
        centred, aspect preserved -- the always-visible default."""
        sw = max(b[1] - b[0], 1e-6)
        sh = max(b[3] - b[2], 1e-6)
        s = min(sw / w_px, sh / h_px) if (w_px and h_px) else 0.001
        cx, cy = (b[0] + b[1]) / 2.0, (b[2] + b[3]) / 2.0
        # (ox, oy) is the image's VISUAL CENTRE (the scale/rotate pivot), so it
        # is the scaffold centre directly -- no half-image corner offset.
        return s, cx, cy

    fit_s, fit_ox, fit_oy = _fit_placement()
    fit_z = b[4] - 0.002 * diag
    sx = scale[0] if scale else fit_s
    sy = scale[1] if scale else fit_s
    ox = origin[0] if origin else fit_ox
    oy = origin[1] if origin else fit_oy
    if z is None:
        z = fit_z

    renderer.AddActor(origin_marker(0.08 * diag))

    tex = image_texture(rgb)
    img_actor = image_plane_actor(w_px, h_px, tex)
    renderer.AddActor(img_actor)

    window = vtk.vtkRenderWindow()
    window.AddRenderer(renderer)
    window.SetSize(1080, 720)
    window.SetWindowName(f"Micrograph  --  {os.path.basename(image_path)}")

    state = _ImageState(img_actor, window, sx, sy, rot, ox, oy, z,
                        pw=w_px, ph=h_px)

    interactor = (embed.make_interactor() if embed is not None
                  else vtk.vtkRenderWindowInteractor())
    interactor.SetRenderWindow(window)
    interactor.SetInteractorStyle(vtk.vtkInteractorStyleTrackballCamera())
    install_cad_camera(interactor, renderer)
    if embed is not None:
        embed.attach(window, interactor, renderer)

    # Corner gizmo: the upper-right ViewCube, which also snaps the view when you
    # click a face/edge/corner. There is no separate axes marker -- the cube
    # rides its own axis triad, so a second would put two orientation gizmos
    # on one screen.
    cube = None
    if _view_cube is not None:
        try:
            cube = _view_cube.ViewCube(window, interactor, renderer,
                                       reframe=lambda: (renderer.ResetCamera(),
                                                        fit_clipping_range(renderer)))
        except Exception:  # noqa: BLE001 -- a gizmo must never sink a launch
            cube = None

    # -- live readout ---------------------------------------------------------
    hud = vtk.vtkTextActor()

    def _refresh_hud():
        # kept short so it fits the 160-px native-fullscreen sidebar too
        sc = (f"{state.sx:.3g}" if abs(state.sx - state.sy) < 1e-12
              else f"{state.sx:.3g}/{state.sy:.3g}")
        hud.SetInput(
            f" {w_px}x{h_px}px   z {state.z:.3g} mm\n"
            f" scale {sc} mm/px\n"
            f" origin {state.ox:.3g}, {state.oy:.3g}\n"
            f" rotation {state.rot:.4g} deg")

    # -- keyboard: arrow keys move the image in XY ----------------------------
    # (Z is set with the Z origin slider, so there is no keyboard Z nudge.)
    def _step():
        return 0.02 * diag

    def _on_key(obj, _evt):
        sym = (obj.GetKeySym() or "")
        big = 10.0 if obj.GetShiftKey() else 1.0
        s = _step() * big
        if sym in ("Left", "Right", "Up", "Down"):
            dx = -s if sym == "Left" else (s if sym == "Right" else 0.0)
            dy = -s if sym == "Down" else (s if sym == "Up" else 0.0)
            state.nudge(dx=dx, dy=dy)
            _refresh_hud()

    interactor.AddObserver("KeyPressEvent", _on_key, 12.0)

    # -- top-down snap: a level, straight-down view of the scaffold top -------
    def _snap_top():
        cam = renderer.GetActiveCamera()
        cx, cy = (b[0] + b[1]) / 2.0, (b[2] + b[3]) / 2.0
        cam.SetFocalPoint(cx, cy, b[5])
        cam.SetPosition(cx, cy, b[5] + max(diag, 1.0))
        cam.SetViewUp(0.0, 1.0, 0.0)
        renderer.ResetCamera()
        window.Render()

    def _fit_to_scaffold():
        s, o_x, o_y = _fit_placement()
        state.sx = state.sy = s
        state.ox, state.oy, state.z, state.rot = o_x, o_y, fit_z, 0.0
        state.apply()
        _refresh_hud()

    # -- chrome (controls) ----------------------------------------------------
    if embed is not None:
        chrome = embed.make_chrome("Micrograph", os.path.basename(image_path))
    elif _themed:
        chrome = _shell.ViewerChrome(window, interactor, renderer,
                                     title="Micrograph",
                                     subtitle=os.path.basename(image_path))
    else:
        chrome = None

    scaffold_opacity = {"v": 1.0}

    def _set_scaffold_opacity(v):
        scaffold_opacity["v"] = max(0.0, min(1.0, _as_float(v, 1.0)))
        for a in scaffold_actors:
            a.GetProperty().SetOpacity(scaffold_opacity["v"])
        window.Render()

    def _set_field(name):
        def _set(v):
            setattr(state, name, _as_float(v, getattr(state, name)))
            state.apply()
            _refresh_hud()
        return _set

    def _set_image_opacity(v):
        state.opacity = max(0.0, min(1.0, _as_float(v, state.opacity)))
        state.apply()

    if chrome is not None:
        chrome.set_hud(hud, max_lines=4)

        # Every control is a SLIDER so the embedded rail and the native
        # fullscreen sidebar render identically (a VTK overlay can't host a text
        # entry). Ranges come from the scaffold footprint; arrow keys refine.
        span_x = max(b[1] - b[0], diag * 0.2)
        span_y = max(b[3] - b[2], diag * 0.2)
        cx, cy = (b[0] + b[1]) / 2.0, (b[2] + b[3]) / 2.0
        s_hi = 4.0 * max(sx, sy, 1e-6)

        chrome.section("Opacity")
        chrome.slider("Image", lambda: state.opacity, 0.0, 1.0,
                      _set_image_opacity, fmt="%.2f")
        chrome.slider("Scaffold", lambda: scaffold_opacity["v"], 0.0, 1.0,
                      _set_scaffold_opacity, fmt="%.2f")

        chrome.section("Move image")
        chrome.slider("Origin X (mm)", lambda: state.ox, cx - span_x,
                      cx + span_x, _set_field("ox"), fmt="%.3g")
        chrome.slider("Origin Y (mm)", lambda: state.oy, cy - span_y,
                      cy + span_y, _set_field("oy"), fmt="%.3g")
        chrome.slider("Origin Z (mm)", lambda: state.z, b[4] - diag, b[5] + diag,
                      _set_field("z"), fmt="%.3g")

        chrome.section("Scale & rotate")
        chrome.slider("Scale X (mm/px)", lambda: state.sx, 0.0, s_hi,
                      _set_field("sx"), fmt="%.4g")
        chrome.slider("Scale Y (mm/px)", lambda: state.sy, 0.0, s_hi,
                      _set_field("sy"), fmt="%.4g")
        chrome.slider("Rotation (deg)", lambda: state.rot, -180.0, 180.0,
                      _set_field("rot"), fmt="%.4g")

        chrome.section("View")
        chrome.button(lambda: "Look straight down", _snap_top)
        chrome.button(lambda: "Fit to scaffold", _fit_to_scaffold)
        chrome.button(lambda: "Reset placement",
                      lambda: (state.reset(), _refresh_hud()))
        if cube is not None:
            chrome.button(cube.label, cube.toggle)

        chrome.footer_section("Keys")
        chrome.footer_label("arrows  move image\nShift  10× bigger step", lines=2)
        chrome.footer_section("Mouse")
        chrome.footer_label("Left-drag  rotate\nRight-drag  spin\nwheel  zoom",
                            lines=3)
        chrome.layout()
        chrome.install_click_handler(interactor)

    _refresh_hud()

    # CAD-style parallel camera, isometric default (matches the other viewers)
    cam = renderer.GetActiveCamera()
    cam.ParallelProjectionOn()
    cam.SetFocalPoint(0.0, 0.0, 0.0)
    cam.SetPosition(1.0, -1.0, 0.8)
    cam.SetViewUp(0.0, 0.0, 1.0)
    renderer.ResetCamera()

    if embed is not None:
        return embed.loop()
    window.Render()
    interactor.Initialize()
    interactor.Start()
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
_MICRO_FLAGS = ("--image", "--gcode", "--scale-x", "--scale-y", "--origin",
                "--rot", "--z", "--render")


def parse_micro_argv(args):
    """Split the argv (WITHOUT the program name) into (positionals, options).
    Every flag accepts BOTH '--opt value' and '--opt=value' (so a negative
    origin never reads as another flag). Pure -- unit-testable."""
    opts = {flag: None for flag in _MICRO_FLAGS}
    rest = []
    i = 0
    while i < len(args):
        key, eq, val = args[i].partition("=")
        if key in opts:
            if eq:
                opts[key] = val
                i += 1
            elif i + 1 < len(args):
                opts[key] = args[i + 1]
                i += 2
            else:
                i += 1
            continue
        rest.append(args[i])
        i += 1
    return rest, opts


def _parse_origin(text):
    if not text:
        return (0.0, 0.0)
    parts = str(text).replace(";", ",").split(",")
    try:
        return (float(parts[0]), float(parts[1]))
    except (IndexError, ValueError):
        return (0.0, 0.0)


def main(argv):
    from viewer_embed import pop_embed_arg
    args, embed_spec = pop_embed_arg(argv[1:])
    rest, opts = parse_micro_argv(args)
    if len(rest) != 1 or not opts["--image"]:
        sys.stderr.write(
            "usage: micrograph_viewer.py <file.stl|.depo> --image PATH "
            "[--gcode g] [--scale-x mm/px] [--scale-y mm/px] [--origin X,Y] "
            "[--rot deg] [--z mm] [--render exact|wireframe|tube:N] "
            "[--embed name:WxH]\n")
        return 2
    stl_path = rest[0]
    if stl_path.endswith(".depo.npz"):
        stl_path = stl_path[:-len(".depo.npz")] + ".stl"
    render = opts["--render"] or "tube:12"
    if render != "exact" and render != "wireframe" \
            and not render.startswith("tube:"):
        render = "tube:12"
    # scale / origin are OPTIONAL: absent -> the viewer auto-fits the image to
    # the scaffold footprint (always visible). Present -> exact override.
    scale = None
    if opts["--scale-x"] not in (None, ""):
        sx = _as_float(opts["--scale-x"], 0.001)
        sy = _as_float(opts["--scale-y"], sx)
        scale = (sx, sy)
    origin = _parse_origin(opts["--origin"]) if opts["--origin"] else None
    rot = _as_float(opts["--rot"], 0.0)
    z = opts["--z"]
    z = float(z) if z not in (None, "") else None
    embed = None
    if embed_spec:
        from viewer_embed import EmbedHost
        embed = EmbedHost(embed_spec)
    return view(stl_path, opts["--image"], gcode_path=opts["--gcode"],
                scale=scale, origin=origin, rot=rot, z=z, render=render,
                embed=embed)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
