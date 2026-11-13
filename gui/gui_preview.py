#!/usr/bin/env python3
"""gui_preview.py -- MODERN-UI ONLY: the embedded live scaffold preview.

A 3D approximation of the ACTIVE scaffold rendered inside the Scaffold-designer
page, in a DRAG-RESIZABLE right-hand pane, regenerating a 150 ms debounce after any
geometry parameter edit. The pill rail swaps each scaffold's own cached geometry +
camera; a scaffold edited while hidden rebuilds when shown. "Preview options" drives
3-D tubes, "Sagging (approximate)" (preview_sag.py), the physics path, and colour.

THE APPROXIMATION CONTRACT: the preview is approximate only in what it OMITS or
clearly labels approximate, never silently wrong in what it draws. The REAL
generator + parser produce the commanded toolpath, and with Jet lag on (the
Physics page's switch and the preview's own box) the REAL touchdown relay
(mew_deposition.make_transform, exactly
mew_export's wiring) reshapes the XY path. Drawn as swept tubes whenever the fiber
covers >= ~2.5 screen px, else as fixed-width GL centrelines. The EXACT sag solve
stays OUT (~13 s at the 30x30/0.5/x10 target against a 3 s budget); the opt-in
toggle runs the heightmap DRAPE instead. Drawn where the
print puts it: Start X/Y/Z plus the g-code-origin datum, with the stabilization
base ahead of scaffold 1 exactly as the generator emits it.

The 3D view is embedded OFFSCREEN -- vtkRenderWindow -> vtkWindowToImageFilter ->
PPM bytes -> Tk PhotoImage on a plain Tk label -- and must stay that way: a direct
HWND embed works in a bare Tk window, but inside a ttk.Notebook page the notebook's
own repaints erase the GL surface with no Tk <Expose> to hook, so the viewport
blanked after every edit. The blit costs ~15-25 ms per frame and removes that whole
failure class -- Tk owns the pixels. There is no vtk interactor; the camera comes
from Tk bindings on the image label, mirroring vtk_helpers.install_cad_camera.

GPU-DIRECT (`PreviewPanel(direct=True)`, the G-code page's full-window panel
only; Windows only; MEWRACLE_PREVIEW_BLIT=1 opts out): the blit's cost scales
with pixel area and capped that page near 35 fps. There the viewport is a Tk
FRAME whose own HWND VTK renders into onscreen (SetWindowInfo), in-process, the
same thread, so picking, the highlight and the jet keep their renderer. The
frame's background goes to "" once GL is on it, so Tk never paints over the
pixels; Tk's <Expose> on that window -- the hook the notebook case lacked --
redraws them. Overlays placed over it are sibling native windows above it, and
Tk's WS_CLIPSIBLINGS keeps GL out from under them. Measured 1600x1000,
RTX 4060: 146 fps (vsync) vs 35 fps blit.

Threading: Tk-var traces -> per-scaffold dirty flag + one 150 ms debounce -> spec
snapshot on the Tk thread (reusing gui_args._scaffold_spec verbatim, so any future
field flows to the preview automatically) -> one daemon worker -> results queue ->
an `after` poll applying them on the Tk thread, so all VTK calls stay there. A
1-slot latest-only mailbox per scaffold plus a generation id make staleness
impossible.

Failure posture: the preview may die, the designer must not. Mid-typing spec errors
keep the last good geometry, generator ValueErrors surface in the status line, and a
vtk import/embed failure degrades the panel to a text label. There is NO density
cap, so a large scaffold can stall the offscreen upload for seconds.

The classic (developer) UI never imports this module. Physics-build only
"""

import math
import os
import queue
import sys
import threading
import time
import weakref

import tkinter as tk
from tkinter import ttk

import numpy as np

# PIL gives the fast blit path, exactly as in gui_embed.py: ImageTk.PhotoImage
# .paste() updates the SAME Tk image in place (Tk_PhotoPutBlock), where a fresh
# tk.PhotoImage from PPM bytes reallocates the whole master every frame --
# measured 24.2 ms vs 5.8 ms at 700x500 and 75.3 ms vs 19.9 ms at 1400x900 on
# an RTX 4060 / 2560x1440 desktop, against a VTK render of 0.7 ms for the same
# scene. The blit, not the GPU, is what set the panel's frame rate. PIL ships
# with this install; degrade to the PPM path without it.
try:
    from PIL import Image as _PILImage
    from PIL import ImageTk as _PILImageTk
except Exception:  # noqa: BLE001 -- pragma: no cover
    _PILImage = _PILImageTk = None

import gui_fonts
import gui_modern_theme
import gui_tips
import gui_widgets
from gui_common import SESSION_DIR

# ---------------------------------------------------------------------------
# tunables
# ---------------------------------------------------------------------------
#: Coalesce keystrokes into one rebuild. Was 350 ms, which on the default 10 mm
#: scaffold was a fifth of the 1.9 s an edit took to land. A shorter wait can
#: start a build on a half-typed value ("3" on the way to "30"), but the mailbox
#: is latest-only and the half-typed value is nearly always the smaller, faster
#: scaffold, so the cost is a fraction of one extra build. The 4-axis and 5-axis
#: pages keep their own 350 ms (`gui_mandrel._MANDREL_PREVIEW_MS`).
PREVIEW_DEBOUNCE_MS = 150
# The live preview does not cap or refuse by density (no "too dense to preview"
# refusal and no display-decimation ceiling -- it renders every scaffold at full
# resolution). Note the trade-off: a multi-million-point tube can stall the offscreen
# GPU upload for a few seconds on very large/fine scaffolds.
#
# PREVIEW_MAX_PTS survives ONLY as the default cap of the standalone
# decimate_paths() helper (feature-preserving thin: near-collinear runs strided,
# CORNERS -- XY wave apexes / Z crossover cliffs -- always kept). The live
# preview does not call it unless a caller explicitly passes max_pts.
PREVIEW_MAX_PTS = 800_000
PREVIEW_TUBE_SIDES = 6          # "Tubes -- fast" facets (the STL viewer owns exact)
#: Bytes of `vtkTubeFilter` output per output point: double coordinates (24),
#: float normals (12), the strip connectivity (~16) and a carried scalar (8).
TUBE_BYTES_PER_POINT = 60


def _free_memory_bytes():
    """The memory a mesh can take now, or None (vtk_helpers.free_memory_bytes)."""
    import vtk_helpers
    return vtk_helpers.free_memory_bytes()


def tubes_fit(lines_pd, sides):
    """Whether tubing `lines_pd` with `sides` facets fits in half the free
    memory. VTK aborts the whole process when an allocation fails, so a
    print too large to tube (a 150-layer 10 mm scaffold needs ~2 GB of
    tubes) must be drawn as centrelines instead, never attempted. This does
    not thin the print: the centrelines carry every point."""
    free = _free_memory_bytes()
    if free is None:
        return True
    need = lines_pd.GetNumberOfPoints() * (int(sides) + 1) \
        * TUBE_BYTES_PER_POINT
    return need <= free // 2


def highlight_tubes_fit(n_pts):
    """Whether a highlight of `n_pts` centreline points may be drawn as
    tubes, by the rule the picture itself is sized by (`display_budget_pts`:
    `_DISPLAY_BYTES_PER_PT` a point, tube and first Render, in
    `_DISPLAY_SHARE` of the headroom), read now -- after the picture took
    its share. The highlight is a second tube mesh over the lit fiber, so a
    whole scaffold selected (Move's usual case) cost as much again as the
    picture: +1.0 GB on a 20x20 mm waved scaffold, with no budget at all,
    and VTK aborts the process when an allocation fails. One that does not
    fit is drawn as centrelines; True when the headroom cannot be read."""
    import vtk_helpers
    room = vtk_helpers.memory_headroom_bytes()
    if room is None:
        return True
    return n_pts * _DISPLAY_BYTES_PER_PT <= _DISPLAY_SHARE * room
PREVIEW_POLL_MS = 90            # result-queue poll while a build is in flight
#: A plate scaffold whose last build took at least this long (seconds) shows its
#: COMMANDED toolpath first, drawn the way the full build will be, while the jet
#: lag and sag are worked out; the full build replaces it. Not as centrelines: a
#: 1 px line is wider than a 30 mm scaffold's pores on screen and the picture
#: read as a solid slab, where the commanded path has so few points (a straight
#: pass is two) that its tubes cost next to nothing. Below it the full build lands soon
#: enough that a second picture would only flicker. A scaffold never built yet
#: counts as slow. The toolpath takes generate + parse alone: 0.5 s of the
#: ~7 s build on a 30 x 30 mm / 36 deg / 30-layer decagon.
PREVIEW_TOOLPATH_FIRST_S = 1.0
#: GIL switch interval (seconds) held for the duration of ONE preview build, and
#: only then. The preview solvers are tens of thousands of SMALL numpy calls --
#: `preview_sag.drape_sag`'s per-fiber passes especially -- so each one takes and
#: drops the GIL, and at the interpreter's 5 ms default the Tk main thread
#: reclaims it constantly. Measured on one live `ModernApp`, one spec, the 4-axis
#: *Mandrel default* preset: the same build costs 2.91 s on the worker while the
#: Tk thread sleeps and 6.37 s while its event loop runs. Raising the interval
#: recovers most of that and then saturates -- 4.80 s at 0.02 s, and no further
#: gain at 0.05 or 0.2 -- so 0.02 is the knee, not a guess.
#:
#: What it costs: while a build is in flight a Tk event may be serviced up to
#: this long later than it otherwise would. That is the window in which the panel
#: already reads "updating...", and 20 ms is under the threshold of noticing. It
#: is restored in a `finally`, so a build that raises leaves the interval alone.
PREVIEW_SWITCH_INTERVAL = 0.02
#: Mailbox key for the 4-axis (mandrel) build. The other keys are scaffold
#: indices 0..4, so a non-int key gives the tube its own latest-only slot on the
#: SAME worker without disturbing the planar ones: a tube in flight is replaced
#: by the next tube edit and never by a plate scaffold's. `_pv_worker_loop` and
#: `_pv_apply_result` dispatch on it; `gui_mandrel` is the only submitter.
PV_MANDREL = "mandrel"

#: The status line every collector's preview shows while "Disable preview" is
#: ticked, so an empty or stale picture is never mistaken for a slow build.
PREVIEW_DISABLED_STATUS = ("preview disabled. Untick ‘Disable preview’ in "
                           "Preview options to rebuild")
#: Shown in RED, in place of "updating…", when a preview refuses to build
#: because the commanded speed is below CTS (physics on). A coiled centreline
#: is what makes such a build crawl, and the Design summary already says why.
#: Both the flat plate (`_pv_kick`) and the tube (`_mandrel_below_cts`) use it.
BELOW_CTS_STATUS = "Print speed below CTS. Unable to render"
#: Status-line red for `SweepPreviewPanel.set_status(error=True)`. The light one
#: is the error red the rest of the app already uses (gui_buildplate.py:901,
#: gui_calibration.py:878, gui_sweeps.py:930); the dark one is lifted because the
#: panel's status sits on the theme background, and #c0392b on the dark ground is
#: too near it to read.
_STATUS_ERR_LIGHT = "#c0392b"
_STATUS_ERR_DARK = "#ff6b5e"
# "Show the g-code path" overlay. The colour is `gcode_player.DEPOSIT_COLOR`
# (gcode_player.py:260) written out rather than imported -- importing the player
# would pull VTK and the animator into the form's module graph, the same reason
# gui_mandrel_preview writes out PLATE_COLOR. Keep the two in step.
_GCODE_LINE_RGB = (0.96, 0.76, 0.20)
# The G-code page's highlight (`PreviewPanel.set_highlight`): a purer, brighter
# yellow than the amber overlay above, so the two never read as one.
_HIGHLIGHT_RGB = (1.0, 0.95, 0.0)
# A plain solid 1 px line, and the two obvious ways to soften it are both dead
# ends here -- do not re-attempt either. `vtkProperty.SetLineStipplePattern` is
# INERT in this build (VTK 9.6.2): 0xF0F0 and 0x0001 render the identical
# picture, so a "dashed" overlay is a solid one. Pushing it behind the fibers
# (`SetRelativeCoincidentTopologyLineOffsetParameters`, the player's fix at
# gcode_player.py:4151) changes nothing either, because a true-scale fiber is
# sub-pixel and draws as a ~1 px line too -- there is no depth for an offset to
# resolve. So on a dense scaffold with physics and sag OFF the overlay does
# cover the fibers, and that is the honest picture: the two are the SAME curve
# and only the colour differs. They separate as soon as the relay or the drape
# is on, which is when the overlay has something to say.
_REFRAME_RATIO = 1.2            # auto-reframe when the bbox diagonal moves 20 %
# The g-code zero triad's arm. Simulation's is 0.08 x the design's diagonal
# (gcode_player: `origin_marker(0.08 * diag)`); that is the ceiling here. It
# is also held to a share of the view's half-height, so it reads as a small
# marker at the zero whatever the zoom: sized off the design alone it was
# 0.15 x the largest extent, which on a mandrel is the whole rod, and the
# arms ran several rod diameters long.
ORIGIN_ARM_OF_DIAG = 0.08
ORIGIN_ARM_OF_VIEW = 0.08
#: Click-to-select (`PreviewPanel.pick_at`): a press that travels no further
#: than this is a click, not an orbit; a fiber whose drawn centreline passes
#: within `_PICK_RADIUS_PX` of the click is a candidate. The radius is what lets
#: a 1 px centreline (the "lines" display mode) be hit at all.
_CLICK_SLOP_PX = 4
_PICK_RADIUS_PX = 6.0


def _rotation_from_z(n):
    """The SHORTEST rotation taking world +Z onto the unit vector `n`, 3x3.

    `reset_view`'s default camera is an isometric expressed against world +Z,
    which is the direction a plate's and a mandrel's scaffold faces. A curved
    collector's does not (`gui_surface`'s `view_normal`), so the whole tripod --
    offset and view-up together -- is rotated onto it instead. Shortest, so the
    view only tilts: any other rotation with the same axis mapping would also
    spin the scene about it, and the user would lose the left-right sense they
    have on the other two collectors.

    None when there is nothing to do or nothing sane to do: `n` already +Z
    (the caller then keeps the exact world isometric, so a plate and a mandrel
    are byte-identical), or `n` degenerate. The antipode is the one case needing
    a chosen axis -- any perpendicular is a shortest rotation there -- and X is
    as good as any.
    """
    try:
        v = np.asarray(n, dtype=np.float64).ravel()
        if v.size != 3 or not np.all(np.isfinite(v)):
            return None
        mag = float(np.linalg.norm(v))
        if mag < 1e-9:
            return None
        v = v / mag
        c = float(v[2])                     # dot(+Z, v)
        if c > 1.0 - 1e-12:
            return None                     # already +Z
        if c < -1.0 + 1e-12:                # antipodal: turn a half-turn about X
            return np.array([[1.0, 0.0, 0.0],
                             [0.0, -1.0, 0.0],
                             [0.0, 0.0, -1.0]])
        axis = np.array([-v[1], v[0], 0.0])  # cross(+Z, v)
        kx = np.array([[0.0, -axis[2], axis[1]],
                       [axis[2], 0.0, -axis[0]],
                       [-axis[1], axis[0], 0.0]])
        # Rodrigues in the form that needs no angle: with `axis` left UNnormalised
        # its length is sin(theta), and the 1/(1+c) factor absorbs it exactly.
        return np.eye(3) + kx + kx @ kx / (1.0 + c)
    except Exception:  # noqa: BLE001
        return None
# feature-preserving thin (decimate_paths): interior vertices whose full-3D heading
# turns more than this are STRUCTURAL (wave apexes + crossover cliffs) and are never
# dropped ahead of a flatter one. Everything else is ranked by the chord error its
# removal would introduce and the flattest are dropped first (see decimate_paths).
_KEEP_ANGLE_DEG = 5.0
# Centrelines (flat GL lines) are only used when the "Draw fibers as 3-D tubes"
# checkbox is OFF -- there is no zoom-dependent auto-switch (tubes stay tubes at
# every scale).

# Which scaf_vars entries must NOT trigger a rebuild. Everything else that is a
# tk.Variable is traced -- a DENY-list, not an allow-list, so a new geometry field
# flows to the preview automatically and no geometry var can be silently
# forgotten. A hand-maintained allow-list omitted the whole array_* group and
# arrays never updated the preview. Denied here:
#   color             a pure restyle -- traced separately to _pv_on_color
#   array_travel      inter-scaffold clearance; not part of the array TILING
#   *_periods/grid/editmode  editor-only helper vars (the 2-D graph, snap grid)
# array_on/nx/ny/dx/dy/flip_* are deliberately NOT here: they now drive the
# preview's array tiling (build_preview_paths' _tile_array). Nor are
# start_x/y/z (the preview draws the scaffold where it prints) or speed /
# loop_speed (with physics on, the feed sets the jet lag the relay lays).
_NONGEOM_KEYS = frozenset({
    "color",
    "array_travel",
    "linear_periods", "linear_grid", "linear_editmode",
    "shape_grid", "shape_editmode",
})

# viewport backdrop -- the EMBEDDED designer preview deliberately does NOT
# mirror gui_viewer_shell.viewport_bg any more: this panel sits
# inside the designer page rather than behind viewer chrome, so a darker
# viewport tone read as a box cut out of the form. It now takes the shell's own
# page background in both modes, flat (no gradient), so the 3-D scene sits on
# the page instead of on a panel. The standalone viewers keep the darker
# viewport -- there the contrast is wanted. Derived from gui_modern_theme so a
# palette change carries; gui_modern_theme imports no vtk, so this module stays
# importable before/without vtk.
def _hex_rgb(h):
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (1, 3, 5))


_VIEW_BG_DARK = _hex_rgb(gui_modern_theme.DARK["bg"])
_VIEW_BG_LIGHT = _hex_rgb(gui_modern_theme.LIGHT["bg"])
_VIEW_BG_LIGHT_TOP = _VIEW_BG_LIGHT


def _rgb_hex(rgb):
    return "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in rgb)


# ---------------------------------------------------------------------------
# lazy vtk (import costs ~1 s; warmed on a daemon thread at panel build)
# ---------------------------------------------------------------------------
#: every live PreviewPanel, for the Settings window's Perspective switch
_PANELS = weakref.WeakSet()

_VTK = None
_VTK_ERR = None
_VTK_LOCK = threading.Lock()


def _import_vtk():
    """Import vtk once (thread-safe); returns the module or None (error kept
    in _VTK_ERR for the panel's degrade message)."""
    global _VTK, _VTK_ERR
    with _VTK_LOCK:
        if _VTK is None and _VTK_ERR is None:
            try:
                import vtk as _v
                _VTK = _v
            except Exception as exc:  # noqa: BLE001
                _VTK_ERR = exc
    return _VTK


# ---------------------------------------------------------------------------
# the worker-side pipeline (pure python/numpy; no Tk, no VTK -- unit-testable)
# ---------------------------------------------------------------------------
# chord tolerance for the display thin: every layer's decimated polyline stays within
# this of its full-resolution centreline. 4 um is sub-fiber for MEW (fibers are ~10-25
# um), so the thinned preview is visually indistinguishable from full resolution while
# collapsing the dense collinear fill the drape sag leaves on straight runs.
_CHORD_TOL_MM = 0.004


def _dp_mask(xy, tol):
    """Douglas-Peucker keep-mask for one XY polyline: keep the fewest vertices whose
    chord polyline stays within `tol` (mm) of every original vertex. Bounds the MAX
    chord error to `tol` by construction -- so a sharp corner (far from any spanning
    chord) is always kept and a gently curving run is kept just densely enough, with
    NO per-layer gradient. Squared-distance kernel (no sqrt) + an explicit stack, so
    it stays fast on the ~1e6-vertex layers a fine linear pattern produces."""
    n = len(xy)
    keep = np.zeros(n, dtype=bool)
    if n == 0:
        return keep
    keep[0] = keep[-1] = True
    if n <= 2:
        return keep
    x, y = xy[:, 0], xy[:, 1]
    t2 = float(tol) * float(tol)
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay = x[a], y[a]
        abx, aby = x[b] - ax, y[b] - ay
        L2 = abx * abx + aby * aby
        px = x[a + 1:b] - ax
        py = y[a + 1:b] - ay
        if L2 <= 1e-30:                       # coincident endpoints -> radial distance
            d2 = px * px + py * py
        else:
            cross = px * aby - py * abx       # 2*(triangle area); |perp| = cross/|AB|
            d2 = cross * cross / L2
        i = int(np.argmax(d2))
        if d2[i] > t2:
            keep[a + 1 + i] = True
            stack.append((a, a + 1 + i))
            stack.append((a + 1 + i, b))
    return keep


def _stride_to_cap(paths, cap):
    """Hard GPU-upload backstop: blind-stride every path down so the total is <= cap
    (+ endpoints). Only reached when the fidelity thin alone still overruns -- a
    pathological all-corner path (e.g. a random walk) where DP can drop nothing."""
    total = sum(len(p) for p in paths)
    if total <= cap:
        return paths
    stride = int(math.ceil(total / float(cap)))
    out = []
    for p in paths:
        q = p[::stride]
        if len(q) < 2 or not np.array_equal(q[-1], p[-1]):
            q = np.vstack([q, p[-1]]) if len(q) else p[[0, -1]]
        out.append(np.ascontiguousarray(q))
    return out


def decimate_paths(paths, cap=PREVIEW_MAX_PTS, tol=_CHORD_TOL_MM):
    """Feature-preserving thin of the display polylines. Returns
    (paths, was_decimated).

    NOTE: the live preview never calls this -- `build_preview_paths` renders full
    resolution unless a caller explicitly passes `max_pts`. It survives as an opt-in
    GPU-upload guard for a hypothetical huge scaffold, and as the curvature-quality
    contract the test suite pins.

    Per-path Douglas-Peucker to a chord tolerance `tol`: the max chord error is
    bounded to `tol` on EVERY layer, so structural corners (wave apexes / crossover
    cliffs) always survive and the fidelity is FLAT across layers -- no per-layer
    gradient. This fixes the "linear pattern renders blocky, worse on each higher
    layer" bug at its root: the old thin kept corners + a curvature-BLIND stride, and
    when the scaffold ran over `cap` the ceiling-stride then chorded the gentle arcs
    unequally per layer (the sag stage inflates the upper layers' point counts). DP is
    far more point-efficient than corner+stride, so a real scaffold lands well under
    `cap`; `cap` is only a hard backstop, enforced by a final blind stride for a
    pathological all-corner path DP cannot thin. Below `cap` total the paths pass
    through untouched (full resolution)."""
    total = sum(len(p) for p in paths)
    if total <= cap:
        return paths, False
    out = []
    for p in paths:
        p = np.ascontiguousarray(p)
        if len(p) <= 2:
            out.append(p)
            continue
        out.append(np.ascontiguousarray(p[_dp_mask(p[:, :2].astype(np.float64), tol)]))
    if sum(len(p) for p in out) > cap:        # pathological all-corner -> hard cap
        out = _stride_to_cap(out, cap)
    return out, True


#: the live preview's display budget comes from the memory this process can
#: still take (`vtk_helpers.memory_headroom_bytes`), read when the picture is
#: built: a machine with room draws every point, and only one that would fail
#: draws thinned. Drawing costs about 2.2 KB of memory per centreline point
#: (the 6-sided tube, then its first Render: +1.5 GB for 0.69M points),
#: so the 2.7M points of a 20x20 mm, 30-layer scaffold
#: graded to 150 um pores could not be drawn in a 3.5 GB job, and a 150-layer
#: one aborted inside Render(). The drawing may take half the headroom, the
#: rest kept for the Generate worker; the headroom is read after the drape, so
#: the drape's own budget (also half) is not added to this one.
_DISPLAY_BYTES_PER_PT = 2200
_DISPLAY_SHARE = 0.5
#: never thinned below this many points: the default 10 mm scaffold is 0.14M
_DISPLAY_FLOOR_PTS = 150_000


#: Memory one drape sample costs, by what is draped (`preview_sag.drape_sag`'s
#: own sample estimate, peak commit over it). A flat scaffold: 82 B on a
#: 20x20 mm, 10-layer, 200 um scaffold (2.2M samples), 49 B on a 30x30 mm,
#: 30-layer one (12.9M); the dear end, rounded up. A
#: mandrel tube costs far more a sample (wrapped, and drawn per point):
#: `gui_mandrel_preview._DRAPE_BYTES_PER_SAMPLE`.
PLATE_DRAPE_BYTES_PER_SAMPLE = 100
#: the share of the headroom a live drape may take; the rest is the drawing's
#: (`display_budget_pts`, read after the drape has let go of its memory, so the
#: two shares are never held at once).
_DRAPE_SHARE = 0.5


def drape_budget_samples(bytes_per_sample):
    """The drape samples a live preview may take now, or None for no limit
    (the memory free cannot be read). Sized on the memory this process can
    still take (`vtk_helpers.memory_headroom_bytes`, which counts a job's cap),
    read when the picture is built: a machine with room drapes the whole print
    (up to the drape's own time guard), and only one where the drape would
    fail draws it unsagged and says so (`drape_refusal`)."""
    import vtk_helpers
    room = vtk_helpers.memory_headroom_bytes()
    if room is None:
        return None
    return int(_DRAPE_SHARE * room / float(bytes_per_sample))


def drape_refusal(exc, budget):
    """The status-line clause for a drape that refused (`DrapeBudgetExceeded`):
    the memory free when `budget` is what stopped it, else the drape's own
    words (its time guard, or a print too wide to drape)."""
    import preview_sag
    if (getattr(exc, "samples", None) is not None and budget is not None
            and budget < preview_sag._MAX_DRAPE_SAMPLES
            and exc.samples > budget):
        return ("there is not enough memory free to sag this in the live "
                "preview (about %.1f million fiber samples)" % (exc.samples / 1e6))
    return str(exc)


def display_budget_pts():
    """The centreline points the preview may draw now, or None for no limit
    (the headroom cannot be read, as on a machine that is not Windows)."""
    import vtk_helpers
    room = vtk_helpers.memory_headroom_bytes()
    if room is None:
        return None
    return max(_DISPLAY_FLOOR_PTS,
               int(_DISPLAY_SHARE * room / _DISPLAY_BYTES_PER_PT))
#: the first bound tried, mm, and the guess's shape: on a draped scaffold the
#: kept share fell as about (bound / 2 um) ** -0.73 from 37 % at 2 um
#: (measured on the 2.7M-point scaffold above: 25 % at 4 um, 14 % at 8 um,
#: 11 % at 10 um). The guess only saves passes; each result is checked.
_DISPLAY_STEP_MM = 0.002
_DISPLAY_KEPT_AT_STEP = 0.37
_DISPLAY_KEPT_SLOPE = 0.73


def _thin3_mask(p, tol):
    """Keep-mask for one (N,3) polyline: few vertices, found in vectorised
    passes, such that EVERY original vertex stays within `tol` (mm, in 3-D) of
    the kept polyline. Each pass offers every other kept interior vertex for
    removal (so a candidate's neighbours are fixed) and drops it only when all
    the original points between those neighbours lie within `tol` of the chord
    joining them -- checked against the originals, never the previous pass, so
    the bound does not accumulate. 3-D, unlike `_dp_mask`: a sagging span is a
    Z droop over a straight XY run, and an XY-only thin would straighten it."""
    n = len(p)
    if n <= 2:
        return np.ones(n, dtype=bool)
    t2 = float(tol) * float(tol)
    k = np.arange(n)
    parity, stall = 1, 0
    while len(k) > 2 and stall < 2:
        cand = np.arange(max(parity, 1), len(k) - 1, 2)
        parity ^= 1
        if len(cand) == 0:
            stall += 1
            continue
        a, b = k[cand - 1], k[cand + 1]
        cnt = b - a - 1                               # originals inside (a, b)
        start = np.cumsum(cnt) - cnt
        rep = np.repeat(np.arange(len(cand)), cnt)
        m = a[rep] + 1 + (np.arange(int(cnt.sum())) - start[rep])
        # distance from the chord SEGMENT, not its line (a path that doubles
        # back past a chord's end lies on the line): with u the chord's unit
        # direction and s = q.u clamped to [0, L], |q - s u|^2 =
        # |q|^2 - 2 s (q.u) + s^2. A coincident chord leaves u = 0: the
        # distance to a.
        ab = p[b] - p[a]
        L = np.sqrt(np.einsum("ij,ij->i", ab, ab))
        u = ab / np.where(L > 1e-15, L, np.inf)[:, None]
        q = p[m] - p[a][rep]
        qu = np.einsum("ij,ij->i", q, u[rep])
        s = np.clip(qu, 0.0, L[rep])
        d2 = np.einsum("ij,ij->i", q, q) - 2.0 * s * qu + s * s
        # windows are contiguous runs of `d2`: a reduceat, not ufunc.at
        worst = np.zeros(len(cand))
        has = cnt > 0
        if has.any():
            worst[has] = np.maximum.reduceat(d2, start[has])
        drop = cand[worst <= t2]
        stall = stall + 1 if len(drop) < max(1, len(cand) // 100) else 0
        if len(drop):
            mask = np.ones(len(k), dtype=bool)
            mask[drop] = False
            k = k[mask]
    out = np.zeros(n, dtype=bool)
    out[k] = True
    return out


def _thin3_fast_mask(p, tol):
    """`_thin3_mask`'s cheap first stage: the same parity passes, but a pass
    touches only the KEPT vertices. Each kept segment carries a bound `e` on how
    far the originals it replaced lie from it; dropping vertex v between a and
    b is safe when max(e_left, e_right) + |v, seg(a, b)| <= tol, since every
    point of the old segments is within |v, seg(a, b)| of the new one (distance
    to a segment is convex). The bound over-counts, so it keeps more vertices
    than `_thin3_mask` for the same `tol`, at a fraction of the cost: 0.8 s
    against 4.7 s on 2.7M draped points."""
    n = len(p)
    if n <= 2:
        return np.ones(n, dtype=bool)
    k = np.arange(n)
    e = np.zeros(n - 1)                 # bound of kept segment j: k[j]->k[j+1]
    parity, stall = 1, 0
    while len(k) > 2 and stall < 2:
        cand = np.arange(max(parity, 1), len(k) - 1, 2)
        parity ^= 1
        if len(cand) == 0:
            stall += 1
            continue
        a, v, b = p[k[cand - 1]], p[k[cand]], p[k[cand + 1]]
        ab = b - a
        L = np.sqrt(np.einsum("ij,ij->i", ab, ab))
        u = ab / np.where(L > 1e-15, L, np.inf)[:, None]
        q = v - a
        qu = np.einsum("ij,ij->i", q, u)
        s = np.clip(qu, 0.0, L)
        d = np.sqrt(np.maximum(
            np.einsum("ij,ij->i", q, q) - 2.0 * s * qu + s * s, 0.0))
        bound = np.maximum(e[cand - 1], e[cand]) + d
        ok = bound <= tol
        drop = cand[ok]
        stall = stall + 1 if len(drop) < max(1, len(cand) // 100) else 0
        if len(drop):
            e[drop - 1] = bound[ok]     # the merged segment, at its left slot
            keep = np.ones(len(k), dtype=bool)
            keep[drop] = False
            k = k[keep]
            e = e[keep[:-1]]
    out = np.zeros(n, dtype=bool)
    out[k] = True
    return out


#: the share of a thin's bound spent by its fast first stage
_DISPLAY_FAST_SHARE = 0.2


def _thin3_display_mask(p, tol):
    """The display thin of one polyline within `tol`: `_thin3_fast_mask` at a
    fifth of it, then the exact `_thin3_mask` over what is left at the rest.
    The two bounds add, so every original stays within `tol`. On 2.7M draped
    points: 375k points in 2.0 s, against 294k in 4.7 s for the exact thin
    alone."""
    p = np.asarray(p, dtype=np.float64)
    first = np.flatnonzero(_thin3_fast_mask(p, _DISPLAY_FAST_SHARE * tol))
    second = _thin3_mask(p[first], (1.0 - _DISPLAY_FAST_SHARE) * tol)
    out = np.zeros(len(p), dtype=bool)
    out[first[second]] = True
    return out


def thin_for_display(paths, ptimes, cap=None, max_bound=0.04):
    """`(paths, ptimes, bound)`: the polylines and their per-point times (None
    passes through), thinned for drawing until the total is under `cap`
    (None: `display_budget_pts()`, read now; no limit when that is None).

    Every pass thins the ORIGINALS (`_thin3_display_mask`), so `bound` (mm) is the
    true worst distance of any original point from what is drawn; None when
    nothing was thinned. The first bound is guessed from the share that must
    go (`_DISPLAY_KEPT_*`), and each pass that still overruns `cap` widens it
    by half, up to `max_bound` (pass twice the fiber diameter). Past that the
    last result is strided down to `cap` -- the same indices for points and
    times, each path's ends kept -- and `bound` is "stride"."""
    if cap is None:
        cap = display_budget_pts()
    total = sum(len(p) for p in paths)
    if cap is None or total <= cap:
        return paths, ptimes, None
    src = [np.asarray(p, dtype=np.float64) for p in paths]
    need = cap / float(total)
    tol = _DISPLAY_STEP_MM * max(1.0, (_DISPLAY_KEPT_AT_STEP / need)
                                 ** (1.0 / _DISPLAY_KEPT_SLOPE))
    tol = min(tol, max_bound)
    while True:
        masks = [_thin3_display_mask(p, tol) for p in src]
        if sum(int(m.sum()) for m in masks) <= cap or tol >= max_bound:
            break
        tol = min(tol * 1.5, max_bound)
    out = [np.ascontiguousarray(p[m]) for p, m in zip(src, masks)]
    out_t = (None if ptimes is None else
             [np.ascontiguousarray(np.asarray(t)[m])
              for t, m in zip(ptimes, masks)])
    if sum(len(p) for p in out) <= cap:
        return out, out_t, round(tol, 6)
    stride = int(math.ceil(sum(len(p) for p in out) / float(cap)))
    idx = [np.unique(np.r_[np.arange(0, len(p), stride), len(p) - 1])
           for p in out]
    return ([np.ascontiguousarray(p[i]) for p, i in zip(out, idx)],
            None if out_t is None else
            [np.ascontiguousarray(t[i]) for t, i in zip(out_t, idx)],
            "stride")


def display_thin_note(meta, lead=""):
    """The preview chip's words for a thinned drawing, or "" (`lead` comes
    first only when there is a note)."""
    if not meta.get("decimated"):
        return ""
    b = meta.get("thin_bound")
    if isinstance(b, (int, float)):
        return (f"{lead}large design: drawn simplified, within "
                f"{b * 1000:.0f} µm")
    return f"{lead}large design: drawn simplified"


def _array_copies(arr):
    """Copy count (nx*ny) for an array-options dict, clamped to >= 1."""
    if not arr:
        return 1
    return max(1, int(arr["nx"])) * max(1, int(arr["ny"]))


def tile_array(paths, ptimes, arr):
    """Replicate the deposited polylines into the scaffold's rectangular array.

    The array copies are identical scaffolds translated in XY (and stacked in Z),
    so we tile the ALREADY-laid polylines (after the physics relay + drape sag,
    which each copy would produce identically) instead of regenerating N scaffolds
    -- O(copies) array adds, no extra generation/parse/sag. Copies are spaced
    dx/dy/dz apart (XY negated on a flipped axis), the base copy at the origin,
    Z outermost so a preview stack orders like the worker's, matching
    _ScaffoldMixin._array_specs' tiling. Returns (paths, ptimes) grown by the
    copy count; ptimes are shared per copy (only the sag, already applied,
    consumes them). A 1x1x1 array returns the inputs unchanged."""
    nx = max(1, int(arr["nx"]))
    ny = max(1, int(arr["ny"]))
    nz = max(1, int(arr.get("nz", 1)))
    dx = float(arr.get("dx", 0.0)) * (-1.0 if arr.get("flip_x") else 1.0)
    dy = float(arr.get("dy", 0.0)) * (-1.0 if arr.get("flip_y") else 1.0)
    dz = float(arr.get("dz", 0.0))
    if nx == 1 and ny == 1 and nz == 1:
        return paths, ptimes
    out_p, out_t = [], []
    for k in range(nz):
        for r in range(ny):
            for c in range(nx):
                off = np.array([c * dx, r * dy, k * dz], dtype=np.float64)
                for p, t in zip(paths, ptimes):
                    out_p.append(p + off if (c or r or k) else p)
                    out_t.append(t)
    return out_p, out_t


def preview_sag(paths, ptimes, spec, options, frame=None, max_samples=None):
    """The preview-sag seam: the "Sagging (approximate)" checkbox lands here.

    The full geometric solver (`gcode_to_fibers.apply_sag`) stays excluded --
    profiled ~13 s per rebuild at the 30x30/0.5/x10 target, far over the
    preview's 3 s budget, and the cost is inherent to its exact contact
    geometry. Instead this calls the DRAPE engine (`preview_sag.drape_sag`, a
    separate pure-numpy module): the solver's own mechanism -- deposition-order
    rest-one-diameter-on-top from a rasterized heightmap of what is already
    laid, the real `sag_depth` law hung between contacts, clamped onto the
    floor -- at ~1000x the speed (0.03-0.8 s; validated 4-8 um mean RMS on a
    20 um fiber across rotated / wavy / circular scaffolds; details + honest
    limitations in that module's docstring). Fully geometry-general: contacts
    come from the painted heightmap, so any rotation / pattern / footprint /
    physics-relaid path works. The exact solve remains viewer/sim territory.

    Contract: pure, worker-thread only (no Tk / VTK); must return the same
    number of polylines (each may be resampled). `spec` is the scaffold spec,
    `options` the worker options dict -- options["sag"] carries
    {"multiplier": float, "reset_layers": int, "fuse_law": callable|None,
    "fuse_band": tuple|None} from the Export panel's sag-multiplier and
    reset-sagging fields plus the Physics page's fusion toggle. `fuse_law` is
    evaluated HERE (not in the engine) at this scaffold's own fiber diameter -- for
    BOTH contact kinds, junction and wall -- so `preview_sag` needs no physics
    import; `fuse_band` is the wall/junction blend band that goes with them (None
    when the calibration draws no distinction).

    `max_samples`: the caller's memory budget (`drape_budget_samples`),
    passed straight through; None leaves only the drape's own guards.

    `frame`: passed straight through to `preview_sag.drape_sag` -- a curved
    collector's frame, or `None` for the flat/mandrel callers.

    WHAT CURRENCY `paths` IS IN IS THE FRAME'S TO SAY, not this function's, and
    `sag_approx.holds_chart` is the one place that says it. A flat bed and the
    four-axis `CylinderFrame` take that frame's own unrolled `(u, v, h)`,
    because `parse_gcode` already unrolled them; EVERY OTHER frame -- a dome, a
    bowl, a torus AND a cone -- takes WORLD xyz in the collector's own frame,
    and `drape_sag` converts with whatever chart it picks. That is the
    currency rule, the same split
    `gcode_to_fibers.apply_sag_approx` applies at its own seam, and
    `gui_surface.build_surface_preview` is the caller that honours it here.

    Two misreadings to avoid: "`(u, v, h)` chart, not world XYZ" flatly sends a
    reader to feed a dome chart columns and get `ChartShearExcess` back, and
    DEVELOPABLE in place of "already unrolled" puts a cone's exported fiber
    0.28-0.38 mm inside its own collector while the dome beside it is exact."""
    import preview_sag as _engine
    sag = options.get("sag") or {}
    if not isinstance(sag, dict):
        sag = {}
    diameter = float(spec["fiber_diameter"])
    mult = float(sag.get("multiplier", 1.0))
    reset = int(sag.get("reset_layers", 0) or 0)
    law = sag.get("fuse_law")
    fuse = float(law(diameter)) if callable(law) else 0.0
    fuse_w = float(law(diameter, True)) if callable(law) else None
    return _engine.drape_sag(paths, ptimes, diameter,
                             sag_multiplier=mult, sag_reset_layers=reset,
                             fuse_sink=fuse, fuse_wall=fuse_w,
                             band=sag.get("fuse_band"), frame=frame,
                             max_samples=max_samples,
                             should_stop=options.get("should_stop"))


def cts_check(physics, speed_mm_min):
    """`(v_mm_s, cts_mm_s)` for a print speed against this rig's CTS, or None
    when there is no answer: physics off, a profile that cannot be evaluated,
    or a non-positive CTS (a missing anchor, not a slow speed). Both fail open.

    The ONE CTS comparison the previews make. The 4-axis refusal
    (`_mandrel_below_cts`) and the Design summary's warning both read it, so
    the two cannot disagree about where CTS is. The Print-speed field is
    **mm/min** while `cts_at` returns **mm/s**: the field is divided by 60
    here and nowhere else."""
    if physics is None:
        return None
    try:
        cts = float(physics["model"].cts_at(physics["voltage"],
                                            physics["pressure"],
                                            physics["temperature"]))
    except Exception:  # noqa: BLE001 -- an unfittable profile is not this check's call
        return None
    if not (cts > 0.0):
        return None
    return float(speed_mm_min) / 60.0, cts


def polyline_length(paths):
    """Total 3-D length (mm) of a list of polylines."""
    total = 0.0
    for p in paths:
        p = np.asarray(p, dtype=np.float64)
        if len(p) > 1:
            total += float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())
    return total


def off_plate(points, plate):
    """Whether any XY point falls outside the build plate. `plate` is
    `(x0, y0, w, h, round)` in the g-code's own frame, already shifted by the
    g-code-origin datum. A round plate is the
    circle inscribed in that square."""
    x0, y0, w, h, rnd = plate
    xy = np.asarray(points, dtype=np.float64)[:, :2]
    eps = 1e-6
    if rnd:
        r = 0.5 * w
        c = np.array([x0 + r, y0 + r])
        return bool((np.hypot(*(xy - c).T) > r + eps).any())
    return bool(((xy[:, 0] < x0 - eps) | (xy[:, 0] > x0 + w + eps)
                 | (xy[:, 1] < y0 - eps) | (xy[:, 1] > y0 + h + eps)).any())


def commanded_paths(gcode_to_fibers, paths, ptimes, travels):
    """The COMMANDED toolpath as drawable polylines -- the g-code overlay.

    Both preview builders call this, so there is ONE answer to what the overlay
    draws: every deposition move with its inbound G0 rapid spliced on, through
    the converter's own `merge_travels` rather than a second splice written
    here. Copied out, because the caller hands `paths` on to the touchdown relay
    and the drape and an aliased overlay would follow the fiber it exists to be
    compared against."""
    if travels is None:
        return [np.array(p, dtype=np.float64, copy=True) for p in paths]
    merged, _ = gcode_to_fibers.merge_travels(paths, ptimes, travels)
    return [np.array(p, dtype=np.float64, copy=True) for p in merged]


def _frame_bbox(meta):
    """The box the camera frames and pivots on: the design itself, without the
    lead-in fiber from the machine origin (see build_preview_paths' frame_bbox).
    Builders that do not report one (the curved collectors) frame on `bbox`."""
    return meta.get("frame_bbox") or meta["bbox"]


def _split_at_time(paths, times, t_split):
    """((head paths, head times), (tail paths, tail times)): every point laid
    before `t_split` in the head, the rest in the tail. A path straddling the
    split is cut in two; an empty side is dropped."""
    head_p, head_t, tail_p, tail_t = [], [], [], []
    for p, t in zip(paths, times):
        p = np.asarray(p)
        t = np.asarray(t, np.float64)
        early = t < t_split
        if early.any():
            head_p.append(p[early])
            head_t.append(t[early])
        if not early.all():
            tail_p.append(p[~early])
            tail_t.append(t[~early])
    return (head_p, head_t), (tail_p, tail_t)


def preview_gcode(spec, options=None):
    """(gcode, arc_segments): the g-code ONE scaffold's preview builds from.

    Its own function because two consumers must get the same text: the
    preview build (`build_preview_paths`) and the Design summary's quick job
    (`design_stats`), which reads the print time and fiber length off it
    without waiting for the parse, relay and drape."""
    import scaffold_gcode

    opts = options or {}
    kwargs = scaffold_gcode._scaffold_from_spec(spec)
    # G2/G3 arcs (the Export panel switch): emit true arcs and re-expand them at
    # DOUBLE the arc-segments density, exactly as the scaffold->STL chain does, so
    # the preview loops read at the same smoothness the STL will. Off -> the
    # plain G1-chord path, byte-identical.
    base_seg = int(spec.get("arc_segments", 8))
    if opts.get("g2g3"):
        kwargs["g2g3"] = True
        arc_seg = base_seg * 2
    else:
        arc_seg = base_seg
    # The stabilization base, printed FIRST and joined to the scaffold as one
    # continuous fiber -- the generator's own emission, so the jet's lag state
    # carries across the join exactly as it does in the print.
    gcode = scaffold_gcode.generate_multi_scaffold_gcode(
        [kwargs], stabilization=opts.get("stabilization"))
    return gcode, arc_seg


def design_stats(spec, options=None):
    """The Design summary's quick job: {print_time_s, fiber_length_mm} for one
    copy of this scaffold, off the preview's own g-code. The generator and one
    text walk (`scaffold_gcode.print_path_stats`) and nothing else, so it lands
    long before a preview whose relay and drape take seconds."""
    import scaffold_gcode

    gcode, _seg = preview_gcode(spec, options)
    t, length = scaffold_gcode.print_path_stats(gcode)
    return {"print_time_s": t, "fiber_length_mm": length}


def _tile_print(paths, ptimes, gcode_paths, gcode_times, arr, t_stab_end):
    """The scaffold's array copies of `paths` and of the overlay `gcode_paths`,
    or both unchanged with no array. New lists; the inputs are not touched, so
    the toolpath-first result and the full build tile the same parse."""
    if not (arr and _array_copies(arr) > 1):
        return paths, ptimes, gcode_paths
    # The stabilization base is printed ONCE, ahead of every copy, so only
    # what is laid after the scaffold's first commanded point is tiled. The
    # split is by TIME, not path index: the touchdown relay and the travel
    # merge both renumber paths, and the base's last layer runs into the
    # scaffold as one continuous fiber.
    stab_paths, stab_times, stab_g = [], [], []
    if t_stab_end is not None:
        (stab_paths, stab_times), (paths, ptimes) = _split_at_time(
            paths, ptimes, t_stab_end)
        (stab_g, _), (gcode_paths, _) = _split_at_time(
            gcode_paths, gcode_times, t_stab_end)
    paths, ptimes = tile_array(paths, ptimes, arr)
    # ...and the overlay with them, else it would sit under the first copy
    # only and read as the other copies having no g-code.
    gcode_paths, _ = tile_array(
        gcode_paths, [None] * len(gcode_paths), arr)
    return (stab_paths + list(paths), stab_times + list(ptimes),
            stab_g + list(gcode_paths))


def _design_frame(paths, ptimes, t_first, plate, t_last=None):
    """`(frame_bbox, off_plate)` of what is laid from the first commanded point
    to the last (`t_last`, None = to the end): the camera's framing box, the
    lead-in and the end-of-print exit excluded, and the Design summary's plate
    check over every copy (None with no plate). The exit runs off the plate's
    edge on purpose, so it is not a print that leaves the plate. Its other
    numbers come from `design_stats`."""
    hi = np.inf if t_last is None else float(t_last) + 1e-9
    design = []
    for p, t in zip(paths, ptimes):
        t = np.asarray(t, np.float64)
        design.append(np.asarray(p)[(t >= t_first) & (t <= hi)])
    design = [p for p in design if len(p)]
    if not design:
        return None, None
    dpts = np.concatenate(design, axis=0)
    frame_bbox = (tuple(float(x) for x in dpts.min(axis=0)),
                  tuple(float(x) for x in dpts.max(axis=0)))
    return frame_bbox, (off_plate(dpts, plate) if plate is not None else None)


def build_preview_paths(spec, gcode_path, options=None, max_pts=None,
                        on_toolpath=None):
    """spec (a gui_args._scaffold_spec dict, at its real Start X/Y/Z) ->
    (paths, meta): the deposited polylines of that ONE scaffold (plus the
    stabilization base, when options carry one) and a stats dict.
    Mirrors the fast stages of the real converter pipeline -- parse -> physics
    touchdown relay, exactly mew_export's make_transform -- so the XY geometry is
    the export's. The EXACT sag solve stays out (~13 s at the 30x30/0.5/x10
    target, 2 s even at 10x10, against a 3 s budget); the "Sagging (approximate)"
    toggle runs the DRAPE engine through the preview_sag seam instead. Runs on the
    worker thread, raising ValueError with user-facing messages. No density cap:
    `max_pts` is opt-in, the default never thins.

    options (all optional): {
      "physics": None | dict(model, pressure(Pa), voltage(kV), temperature(C),
                             zeta, coil, max_accel, square_corner_velocity),
      "sag": None | dict(multiplier=float),   # approximate drape sag
      "array": None | dict(nx, ny, dx, dy, flip_x, flip_y),  # tiled copies
      "stabilization": None | dict,  # scaffold_gcode's own stabilization dict;
                                     # printed first, never tiled
      "render_travel": bool,   # deposit the travel (G0) moves too -- the Export
                               # panel's checkbox, so the preview shows what the STL
                               # will build (with physics on, the travel is laid at
                               # the trailing touchdown and the fiber after it
                               # inherits the lag state)
      "plate": None | (x0, y0, w, h, round),  # the build plate, for the Design
                               # summary's off-plate check (see off_plate)}

    meta["gcode_paths"] is the COMMANDED toolpath (deposition moves + the G0 rapids
    between them), always built: it is the parse itself, measured at 0.2 MB and
    inside the noise of a 1.3 s build, so the Preview-options "Show the g-code path"
    box is a pure actor toggle and never a rebuild.

    `on_toolpath(paths, meta)`, when given, is called once the g-code is parsed
    and before the relay and drape: the COMMANDED fiber, tiled like the final
    one, with `meta["provisional"]` True and `off_plate` None (not yet known).
    It is the toolpath-first picture a slow build shows while it works."""
    opts = options or {}
    t0 = time.perf_counter()
    gcode, arc_seg = preview_gcode(spec, opts)
    with open(gcode_path, "w") as fh:
        fh.write(gcode)
    return _paths_from_gcode_file(
        gcode_path, opts, arc_seg=arc_seg, mode="g1",
        layers=int(spec["layers"]),
        fiber_diameter=float(spec["fiber_diameter"]),
        max_pts=max_pts, on_toolpath=on_toolpath, t0=t0)


def build_preview_paths_from_text(text, gcode_path, options=None,
                                  fiber_diameter=0.02, mode="g1",
                                  arc_segments=8, max_pts=None,
                                  on_toolpath=None):
    """The G-code page's preview: the same pipeline as `build_preview_paths`
    from the parse onward, fed a g-code TEXT instead of one scaffold's spec.

    The text is the whole file -- every scaffold, the stabilization base and
    the exit -- so `options` carries no "array" and no "stabilization": both
    are already in it. `fiber_diameter` is the tube the picture draws and the
    diameter the drape sags at (the Scaffold designer's scaffold 1 for a linked
    document, the Convert-to-STL setting for a plain file). `mode` is the
    parser's extrusion detection: "g1" for MEWracle's own continuous-flow
    output, "auto" for a file from elsewhere. Raises ValueError, as the
    designer's builder does, with a message the page shows."""
    opts = dict(options or {})
    opts.pop("array", None)
    opts.pop("stabilization", None)
    t0 = time.perf_counter()
    with open(gcode_path, "w") as fh:
        fh.write(text)
    return _paths_from_gcode_file(
        gcode_path, opts, arc_seg=int(arc_segments), mode=mode, layers=None,
        fiber_diameter=float(fiber_diameter), max_pts=max_pts,
        on_toolpath=on_toolpath, t0=t0)


def _paths_from_gcode_file(gcode_path, opts, arc_seg, mode, layers,
                           fiber_diameter, max_pts=None, on_toolpath=None,
                           t0=None):
    """Parse -> touchdown relay -> drape -> tile -> meta, for both builders
    above. `layers` is the scaffold's layer count, or None when the source is
    a whole g-code text that does not say."""
    import gcode_to_fibers

    phys = opts.get("physics")
    arr = opts.get("array")
    if t0 is None:
        t0 = time.perf_counter()
    # `on_stage(words)` (None: nobody asks) names each stage as it starts, so
    # the page can say what a long build is doing instead of a bare
    # "updating..." that cannot be told from a hang
    on_stage = opts.get("on_stage") or (lambda _words: None)
    stab = opts.get("stabilization")
    spec = {"fiber_diameter": fiber_diameter}        # all preview_sag reads
    # The Export panel's "Render travel (G0) moves as deposited fiber".
    rtravel = bool(opts.get("render_travel"))
    # The travels are parsed unconditionally now: `rtravel` deposits them, and
    # the overlay draws them. Asking for them does not change `paths`/`ptimes`
    # -- the parser reports travels alongside.
    paths, ptimes, travels = gcode_to_fibers.parse_gcode(
        gcode_path, mode=mode, with_times=True,
        arc_segments=arc_seg, with_travels=True)
    if not paths:
        raise ValueError("no deposition moves")
    # The first commanded deposition point. Everything laid before it is the
    # lead-in from the machine origin -- real fiber, drawn, but kept out of the
    # camera's framing, or a 6 mm scaffold at Start X 40 would be framed as a
    # 40 mm diagonal with the part in one corner.
    t_first = float(ptimes[0][0])
    # ...and the last. Everything laid after it is the end-of-print exit (the
    # trailing travel, `gcode_to_fibers.trailing_travel`): real fiber, drawn,
    # but it runs off the plate's edge on purpose, so the framing and the
    # plate check stop here.
    t_last = float(ptimes[-1][-1])
    # The stabilization base ends where the scaffold's first commanded layer
    # starts (one commanded path per layer, base layers first) -- the time the
    # array tiling below splits on.
    t_stab_end = (float(ptimes[int(stab["layers"])][0])
                  if stab and len(ptimes) > int(stab["layers"]) else None)

    # -- the commanded toolpath (the overlay) ------------------------------- #
    # Snapshotted HERE, before the touchdown relay and the drape: the gap
    # between it and the drawn fiber is exactly what those two stages add.
    gcode_paths = commanded_paths(gcode_to_fibers, paths, ptimes, travels)
    gcode_times = (gcode_to_fibers.merge_travels(paths, ptimes, travels)[1]
                   if t_stab_end is not None else None)
    # the overlay's own print times, so the G-code page can cut a hidden
    # part out of it as it does the fiber (untiled prints only)
    overlay_times = None if arr else (
        [np.asarray(t, np.float64).copy() for t in (
            gcode_to_fibers.merge_travels(paths, ptimes, travels)[1]
            if travels is not None else ptimes)])

    # -- the toolpath-first picture ----------------------------------------- #
    # What the print COMMANDS, with no jet lag and no sag: under a tenth of the
    # build on the 30 mm decagon, so a slow build shows its new shape at once.
    if on_toolpath is not None:
        tp, tpt = ((gcode_to_fibers.merge_travels(paths, ptimes, travels)[:2])
                   if rtravel else (paths, ptimes))
        tp, tpt, tg = _tile_print(tp, tpt, gcode_paths, gcode_times, arr,
                                  t_stab_end)
        tp_box, _off = _design_frame(tp, tpt, t_first, None, t_last)
        pts = np.concatenate(tp, axis=0)
        n_tp = int(sum(len(p) for p in tp))
        on_toolpath(tp, dict(
            n_paths=len(tp), n_pts=n_tp, n_pts_raw=n_tp,
            layers=layers,
            stab_layers=int(stab["layers"]) if stab else 0,
            fiber_diameter=fiber_diameter,
            decimated=False, physics=False, sag=False,
            array=_array_copies(arr) if arr else 1,
            bbox=(tuple(float(x) for x in pts.min(axis=0)),
                  tuple(float(x) for x in pts.max(axis=0))),
            frame_bbox=tp_box, gcode_paths=tg,
            elapsed=time.perf_counter() - t0, off_plate=None,
            provisional=True))

    # -- physics touchdown relay (exactly mew_export's make_transform) ------ #
    if phys is not None:
        on_stage("laying the jet")
        import mew_deposition
        transform = mew_deposition.make_transform(
            phys["model"], pressure=phys["pressure"], voltage=phys["voltage"],
            temperature=phys["temperature"], zeta=phys.get("zeta", 1.0),
            coil=phys.get("coil", False),
            coil_wave_amp=phys.get("coil_wave_amp", 0.0),
            coil_wave_len=phys.get("coil_wave_len", 0.3),
            max_accel=phys.get("max_accel") or None,
            square_corner_velocity=phys.get("square_corner_velocity", 6.0),
            fill_per_tau=phys.get("fill_per_tau", 6.0),
            corner_chord=phys.get("corner_chord"),
            simulate_travel=rtravel,
            # lay each repeated layer once (`mew_deposition._RepeatLays`):
            # at 0/90 deg the relay of layers 3..N is a copy of layers 1-2
            reuse_repeats=True)
        # travel-aware transforms take the 3rd arg and merge the laid travels in
        paths, ptimes = (transform(paths, ptimes, travels) if rtravel
                         else transform(paths, ptimes))
    elif rtravel:
        # physics off: the travel is still deposited, on its commanded line
        paths, ptimes = gcode_to_fibers.merge_travels(paths, ptimes, travels)

    # -- sag seam ---------------------------------------------------------- #
    # The full apply_sag stays out of the live preview (profiled ~13 s at the
    # 30x30/0.5/x10 target, far over the 3 s budget).
    # The "Sagging (approximate)" checkbox flows here as options["sag"] and runs
    # the fast drape engine instead (preview_sag() above -> preview_sag.py):
    # the same rest-on-top + droop-law mechanism on a rasterized heightmap,
    # 0.03-0.8 s where the exact solve is 1-25 s.
    # THE SAG / DRAWN-CURVE SPLIT, the export's own (`gcode_to_fibers`, where
    # `split_for_sag` is called): with the adaptive corner grid on, drape the SEED
    # grid and carry Z back onto the drawn fibre. Draping the refined grid packs
    # hundreds of crest samples into each crossing-search cell; on a 1 mm
    # zig-zag linear pattern that took the drape from 1.2 s to 29 s. `sag_grid`
    # is None with the grid off, which keeps that preview byte-identical.
    sagged = False
    sag_refused = None
    if opts.get("sag"):
        on_stage("sagging")
        import preview_sag as _engine
        sag_grid = (getattr(transform, "sag_grid", None) if phys is not None
                    else None)
        drawn, drawn_t = paths, ptimes
        drape_budget = None
        try:
            if sag_grid:
                paths, ptimes = gcode_to_fibers.split_for_sag(paths, ptimes,
                                                              sag_grid)
            sag = opts["sag"]
            if sag.get("exact"):
                paths, ptimes = gcode_to_fibers.apply_sag(
                    paths, fiber_diameter, point_times=ptimes,
                    sag_multiplier=float(sag.get("multiplier", 1.0)),
                    sag_reset_layers=int(sag.get("reset_layers", 0)))
            else:
                drape_budget = drape_budget_samples(
                    PLATE_DRAPE_BYTES_PER_SAMPLE)
                paths, ptimes = preview_sag(paths, ptimes, spec, opts,
                                            max_samples=drape_budget)
            if sag_grid:
                paths, ptimes = gcode_to_fibers.carry_sag_onto_drawn(
                    paths, ptimes, drawn, drawn_t)
            sagged = True
        except _engine.DrapeBudgetExceeded as exc:
            # a hand-edited text the drape cannot afford: drawn undraped,
            # and the status line says so instead of "updating" forever
            paths, ptimes = drawn, drawn_t
            sag_refused = drape_refusal(exc, drape_budget)

    # -- array tiling ------------------------------------------------------ #
    # The scaffold's rectangular array is replicated LAST -- each copy is the
    # same laid + sagged geometry, just translated, so we tile the finished
    # polylines rather than regenerating N scaffolds (see tile_array). The
    # copies are far enough apart (spacing >= footprint) that they sag
    # independently, so post-sag tiling matches a per-copy solve.
    on_stage("drawing")
    paths, ptimes, gcode_paths = _tile_print(paths, ptimes, gcode_paths,
                                             gcode_times, arr, t_stab_end)
    plate = opts.get("plate")
    frame_bbox, off = _design_frame(paths, ptimes, t_first, plate, t_last)

    n_raw = sum(len(p) for p in paths)
    # A caller's own cap (`decimate_paths`, XY) when it asks; otherwise the
    # design is never capped, and only its DRAWING is thinned past the display
    # budget, within a stated sub-fiber bound (`thin_for_display`), its times
    # with it, so the page still lights a line's fiber by its time.
    thin_bound = None
    if max_pts is not None:
        paths, decimated = decimate_paths(paths, max_pts)
    else:
        paths, ptimes, thin_bound = thin_for_display(
            paths, ptimes, max_bound=max(_DISPLAY_STEP_MM,
                                         2.0 * float(fiber_diameter)))
        decimated = thin_bound is not None
    pts = np.concatenate(paths, axis=0)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    meta = dict(
        n_paths=len(paths),
        n_pts=int(sum(len(p) for p in paths)),
        n_pts_raw=int(n_raw),
        layers=layers,
        stab_layers=int(stab["layers"]) if stab else 0,
        fiber_diameter=fiber_diameter,
        decimated=bool(decimated),
        thin_bound=thin_bound,
        physics=phys is not None,
        sag=bool(sagged),
        sag_exact=bool(sagged and opts["sag"].get("exact")),
        sag_refused=sag_refused,
        array=_array_copies(arr) if arr else 1,
        bbox=(tuple(float(x) for x in lo), tuple(float(x) for x in hi)),
        frame_bbox=frame_bbox,
        gcode_paths=gcode_paths,
        gcode_ptimes=overlay_times,
        # print seconds per drawn point, on the player's clock (thinned with
        # the points; None after a caller's own cap): the G-code page lights
        # a line's fiber by its time
        ptimes=(None if decimated and thin_bound is None else ptimes),
        elapsed=time.perf_counter() - t0,
        off_plate=off,
    )
    return paths, meta


# ---------------------------------------------------------------------------
# the panel (all VTK strictly on the Tk thread)
# ---------------------------------------------------------------------------
class _FlowItem:
    """One entry in an `_OptionFlow`, standing in for the widget itself.

    It exists so the collector gate can hide a toggle. `_collector_register`
    drives visibility with `grid()` / `grid_remove()` on whatever it was handed
    (`gui_scaffold._apply_collector_mode`), and a flow row cannot let it call
    those on the real widget: the reflow decides every cell, so a widget put
    back by `grid()` would land on the row and column it held before the last
    reflow -- on top of whatever now lives there. Handing the gate one of these
    instead turns show/hide into a flag the reflow reads."""

    def __init__(self, flow, widget):
        self._flow = flow
        self.widget = widget
        self.shown = True

    def grid(self, **_kw):
        self._flow.set_shown(self, True)

    def grid_remove(self):
        self._flow.set_shown(self, False)

    def __getattr__(self, name):
        """Everything else is the widget's.

        Callers that only ever read -- `cget`, `winfo_children`,
        `winfo_ismapped`, `winfo_manager` -- must see the real toggle, and the
        answers stay true because hiding really does `grid_forget` the widget:
        a hidden item reports manager "" and unmapped, exactly as a
        `grid_remove`d one does."""
        return getattr(self.widget, name)


class _OptionFlow(ttk.Frame):
    """The preview's display toggles, laid out as a ROW that wraps.

    Phase 2 made these a collapsed "Options" drawer -- a column of nine
    checkboxes, each with a grey caption under it, behind a disclosure button.
    Reported as reading like a dropdown where a row was wanted, and the reason
    the drawer gave (296 px at the narrowest) is answered by wrapping rather
    than by hiding: the row is one line where there is room for one, two where
    there is not, and the captions fold into the info-dot text that was already
    carrying the same words.

    GRID, not `place`, because `place` takes a widget out of the parent's
    geometry negotiation and the header would stop reserving height for it.
    Columns are `uniform` so the wrapped lines align instead of stepping."""

    def __init__(self, parent, hgap=None, vgap=2):
        super().__init__(parent)
        self._items = []
        self._hgap = gui_fonts.SP["m"] if hgap is None else hgap
        self._vgap = vgap
        self._cols = 0
        self.bind("<Configure>", self._on_configure)

    def add(self, widget):
        """Adopt `widget` (a child of this frame) and return its gate handle."""
        item = _FlowItem(self, widget)
        self._items.append(item)
        self.reflow()
        return item

    def add_toggle(self, text, var, tip, command=None):
        """One preview toggle: a bare checkbox whose help is its hover tooltip,
        tagged like a `gui_form` row. Returns the gate handle, as `add` does.

        THE one way a preview toggle is made, on every preview panel (the
        Scaffold designer's and Calibration's), so the rows cannot drift apart.
        No ⓘ beside the box: help is a tooltip everywhere, and a dot made each
        item a different width, which is what made the gaps in this row
        uneven."""
        import gui_form
        cb = gui_form.mark(ttk.Checkbutton(self, text=text, variable=var),
                           "check")
        if command is not None:
            cb.configure(command=command)
        gui_tips.add_tip(cb, tip)
        return self.add(cb)

    def set_shown(self, item, shown):
        if bool(item.shown) == bool(shown):
            return
        item.shown = bool(shown)
        self.reflow()

    def _on_configure(self, _e=None):
        self.reflow()

    def reflow(self):
        """Re-lay every shown toggle for the current width.

        Called on every <Configure>, so it must be cheap and must not change
        the width it is reacting to -- hence a column count derived from the
        WIDEST item rather than from a running sum, which would oscillate as
        the wrap changed the requested width."""
        vis = [it for it in self._items if it.shown]
        for it in self._items:
            if not it.shown:
                try:
                    it.widget.grid_forget()
                except tk.TclError:             # pragma: no cover -- torn down
                    pass
        if not vis:
            self._cols = 0
            return
        avail = self.winfo_width()
        if avail <= 1:                      # not laid out yet; one line for now
            avail = sum(it.widget.winfo_reqwidth() + self._hgap for it in vis)
        widest = max(it.widget.winfo_reqwidth() for it in vis) + self._hgap
        cols = max(1, min(len(vis), int(avail // max(widest, 1))))
        for c in range(max(cols, self._cols)):
            self.columnconfigure(c, weight=0,
                                 uniform=("opt" if c < cols else ""))
        self._cols = cols
        for n, it in enumerate(vis):
            try:
                it.widget.grid(row=n // cols, column=n % cols, sticky="w",
                               padx=(0, self._hgap), pady=(0, self._vgap))
            except tk.TclError:                 # pragma: no cover -- torn down
                pass
        # The REQUEST is one column wide at any allocation; only the height
        # follows the wrap. Left to grid, the request is `cols x widest`, so a
        # narrower allocation lowers it -- and in the Calibration page's
        # PanedWindow a pane's request moves the sash, which widens the row
        # again. That cycle never settled: `root.update()` spun forever
        # (test_gui_calibration.option_row_request_ignores_its_width).
        rows = -(-len(vis) // cols)
        row_h = max(it.widget.winfo_reqheight() for it in vis) + self._vgap
        self.grid_propagate(False)
        self.configure(width=widest, height=rows * row_h)


class PreviewPanel(ttk.Frame):
    """The right-hand preview column: header + subtitle, the VTK host frame,
    and a one-line status readout. Owns ONE render window shared across the
    scaffolds; the mixin passes per-scaffold state (tube polydata, bbox,
    camera) in and out through show_state()/apply_state()."""

    MIN_W = 296
    # Centreline wire width in PIXELS, for the thin-fiber ("lines") display
    # mode. 1.0 for every panel here -- see `_update_display_mode` for why that
    # number is load-bearing on a zoomed-out scaffold -- and raised only by a
    # subclass whose scene is a few layers framed large.
    _LINE_WIDTH_PX = 1.0

    def __init__(self, parent, app, direct=False):
        super().__init__(parent, padding=(gui_fonts.SP["l"],
                                          gui_fonts.PAGE_MARGIN,
                                          gui_fonts.PAGE_MARGIN,
                                          gui_fonts.SP["m"]))
        _PANELS.add(self)
        self.app = app
        # GPU-direct: VTK draws onscreen into the viewport's OWN window (see
        # `_DIRECT_NOTE`). Windows only; MEWRACLE_PREVIEW_BLIT=1 opts out.
        self._direct = bool(direct and sys.platform == "win32"
                            and os.environ.get("MEWRACLE_PREVIEW_BLIT") != "1")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        head = ttk.Frame(self)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        # the title sits in its own row frame so a subclass can hang something
        # beside it (the Calibration preview's section ⓘ) without re-gridding
        self.title_row = ttk.Frame(head)
        self.title_row.grid(row=0, column=0, sticky="w")
        import gui_form
        # The panel's own chrome -- its heading, the subtitle, the status line
        # and the viewport -- is exempt from the parameter-row guard ONE LABEL AT
        # A TIME, never as a whole panel: the toggles live inside it, and they
        # are parameter rows the guard must still see.
        gui_form.exempt(
            gui_widgets.section_header(self.title_row, "Preview"),
            "preview panel heading").pack(side="left")
        # Options are not on the form: they change THIS picture and
        # nothing else, so they belong on it rather than in a tile among the
        # modules that decide what gets printed. They are a wrapping ROW under
        # the header from here on, not a drawer behind a button -- see
        # `_OptionFlow` for why the drawer went.
        self._reset_btn = ttk.Button(head, text="Reset view", width=10,
                                     command=self.reset_view)
        self._reset_btn.grid(row=0, column=1, sticky="e")

        self._sub_var = tk.StringVar(value="")
        gui_form.exempt(
            ttk.Label(self, textvariable=self._sub_var, style="Hint.TLabel",
                      justify="left", wraplength=330),
            "preview subtitle: which design, and how it is drawn").grid(
            row=1, column=0, sticky="w", pady=(2, gui_fonts.SP["s"]))

        # filled by _pv_fill_option_sections once the sim vars exist; empty and
        # hidden until then (an empty strip of padding above the picture reads
        # as a layout fault), shown by `show_options` on the first fill.
        self.options_host = _OptionFlow(self)
        self.options_host.grid(row=2, column=0, sticky="ew",
                               pady=(0, gui_fonts.SP["s"]))
        self.options_host.grid_remove()

        dark = self._is_dark()
        # the viewport is a plain Tk label showing the offscreen render (see
        # module docstring); it doubles as the placeholder-text surface.
        # width/height/border/pad are pinned so this label's REQUESTED size never
        # depends on the image it is showing. Load-bearing, not cosmetic: `_render`
        # paints a PhotoImage sized to the host's current area, and left to itself
        # a Label requests (image size + 2*border + 2*pad), the grid honours it,
        # `<Configure>` fires and `_on_configure` renders again -- a feedback loop
        # with no fixed point, the image requested always one layout pass STALE.
        # Measured on X11 it either grew +2 px per render without bound or
        # oscillated 376<->356, and `root.update()` never returned. With an image
        # present Tk reads `width`/`height` in PIXELS, so 1x1 makes the request
        # constant while `sticky="nsew"` still stretches the widget.
        bg = _rgb_hex(_VIEW_BG_DARK if dark else _VIEW_BG_LIGHT)
        self._placeholder = None
        if self._direct:
            # a Frame, not a Label: once the GL context is on it, its
            # background goes to "" so Tk never paints over the GL pixels
            self._host = tk.Frame(self, background=bg, takefocus=0,
                                  cursor="crosshair", borderwidth=0,
                                  highlightthickness=0, width=1, height=1)
            self._placeholder = tk.Label(self._host, text="", background=bg,
                                         font="MewHint", foreground="#888888",
                                         justify="center")
            # Expose: Windows asks for the pixels again (an overlay moved
            # off them, the window came back from behind another)
            self._host.bind("<Expose>", lambda _e: self._render_soon())
        else:
            self._host = tk.Label(self, background=bg,
                text="", font="MewHint", foreground="#888888",
                anchor="center", takefocus=0, cursor="crosshair",
                borderwidth=0, highlightthickness=0, padx=0, pady=0,
                width=1, height=1)
        gui_form.exempt(self._host, "the preview viewport")
        self._host.grid(row=3, column=0, sticky="nsew")
        self._photo = None              # live PhotoImage (must stay referenced)
        self._photo_size = None         # its (w, h); a match means paste in place
        self._drag_render_id = None     # pending coalesced interactive render

        self._status_var = tk.StringVar(value="")
        # Kept as an attribute, not discarded into `.grid()`, because `set_status`
        # recolours it: a refusal to render has to look different from progress.
        self._status_lbl = ttk.Label(self, textvariable=self._status_var,
                                     style="Hint.TLabel", justify="left")
        gui_form.exempt(self._status_lbl, "preview status readout")
        self._status_lbl.grid(row=4, column=0, sticky="w",
                              pady=(gui_fonts.SP["s"], 0))
        self._status_error = False
        # the subtitle and the status line wrap at the panel's own width: a
        # narrow window gives the panel less than the subtitle's 330 px, and
        # a label wider than its cell is cut, not wrapped
        self._wrap_w = None
        self.bind("<Configure>", self._rewrap, add="+")

        # -- VTK scene handles (None until first map + vtk import) ---------- #
        self._win = None
        self._ren = None
        self._w2i = None
        self._light_kit = None          # the studio kit; built on first use
        self._mapper = None
        self._actor = None
        self._gc_mapper = None          # the g-code overlay (Preview options)
        self._gc_actor = None
        self._hl = None                 # the highlight layer (set_highlight)
        self._dead = False              # embed failed; stay degraded
        self._mesh_held = False         # page out of sight: no tubes (hold_mesh)
        self._state = None              # the state dict currently shown
        self._drag = None               # (mode, last_x, last_y)
        # Click-to-select (`pick_at`). `on_pick(hit)` is called on a left
        # click that did not drag; None (every panel but the G-code page's)
        # leaves Button-1 exactly the orbit it always was. `pick_outline` is
        # the page's `() -> nodes` or `() -> (nodes, text)`: the document's
        # outline for the text drawn, and that text, so a stale picture is
        # never resolved against new line numbers.
        self.on_pick = None
        self.pick_outline = None
        self._click0 = None             # Button-1 press point while it may click
        self._clicks = 1                # 2 while the press is a double-click's
        self._click_mods = 0            # Tk state bits of that press
        # The ViewCube (view_cube.py) in the viewport's upper right, for a
        # panel that asks for it before its scene exists (the G-code page).
        # It is the viewers' own cube; this panel has no interactor, so its
        # clicks are routed from `_press`.
        self.want_view_cube = False
        self._cube = None
        # The Move manipulator (move_gimbal.py), built on first `show_gimbal`.
        # `on_gimbal(delta, done)` hears a drag: the WORLD delta since the
        # press, `done` on release. The highlight rides the drag, so what is
        # moved is seen moving before any rebuild.
        self.on_gimbal = None
        self._gimbal = None
        self._gdrag = None              # see _overlay_press
        # the highlight's pose: drawn turned `_hl_rz` degrees about Z
        # through `_hl_pivot`, then shifted by `_hl_offset` (world mm)
        self._hl_offset = (0.0, 0.0, 0.0)
        self._hl_rz = 0.0
        self._hl_pivot = (0.0, 0.0, 0.0)
        # The page's inspection tools (view_tools.py): the print-time window,
        # colour by layer / speed, Measure. Built on first use, and only for
        # a panel whose page asks (the G-code page); `_frames` counts the
        # frames drawn, so a depth buffer read for a hover is reused until
        # something has been drawn since.
        self.want_view_tools = False
        self.view_tools = None
        self._frames = 0
        # Tubes for a NEW picture built off the Tk thread, for a panel that
        # asks (the G-code page): see `_tubes_async`.
        self.async_tubes = False

        self._host.bind("<Map>", self._on_map)
        self._host.bind("<Configure>", self._on_configure)
        # A queued frame must not outlive the tree: see _render_soon for what a
        # pending callback costs at teardown.
        self._host.bind("<Destroy>", self._on_host_destroy)
        self._host.bind("<Button-1>", lambda e: self._press("orbit", e))
        # the second press of a double-click: `on_pick` hears clicks=2 (the
        # G-code page widens a picked move to its layer)
        self._host.bind("<Double-Button-1>",
                        lambda e: self._press("orbit", e, clicks=2))
        self._host.bind("<B1-Motion>", self._motion)
        self._host.bind("<ButtonRelease-1>", self._release)
        self._host.bind("<Button-3>", lambda e: self._press("zorbit", e))
        self._host.bind("<B3-Motion>", self._motion)
        self._host.bind("<ButtonRelease-3>", self._release)
        self._host.bind("<Button-2>", lambda e: self._press("pan", e))
        self._host.bind("<B2-Motion>", self._motion)
        self._host.bind("<ButtonRelease-2>", self._release)
        self._host.bind("<MouseWheel>", self._on_wheel)
        # a handle under the pointer lights up (the gimbal only)
        self._host.bind("<Motion>", self._hover, add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")

    # -- the options row --------------------------------------------------- #
    def show_options(self):
        """Reveal the options row, once something has been built into it."""
        self.options_host.grid()
        self.options_host.reflow()

    # -- small readouts ---------------------------------------------------- #
    def _is_dark(self):
        try:
            return bool(self.app.dark_mode.get())
        except (AttributeError, tk.TclError):
            return False

    def set_subtitle(self, text):
        self._sub_var.set(text)

    def set_status(self, text, error=False):
        """The line under the picture. `error=True` draws it RED.

        Red means the preview REFUSED to build -- the picture above is stale and
        no build is in flight -- as against "updating…", which promises one is.
        The default is False so every ordinary caller clears the colour again
        without having to know it exists; only an empty `foreground` restores the
        style's own colour, which is why it is set rather than deleted."""
        self._status_var.set(text)
        error = bool(error)
        if error == self._status_error:
            return
        self._status_error = error
        try:
            self._status_lbl.configure(
                foreground=(_STATUS_ERR_DARK if self._is_dark()
                            else _STATUS_ERR_LIGHT) if error else "")
        except tk.TclError:                      # pragma: no cover -- torn down
            pass

    def _say_placeholder(self, text):
        if self._direct:
            ph = self._placeholder
            if text:
                ph.configure(text=text)
                ph.place(relx=0.5, rely=0.5, anchor="center")
            else:
                ph.place_forget()
            return
        if text:
            self._host.configure(text=text, image="")
            self._photo = None
            self._photo_size = None
        else:
            self._host.configure(text="")

    # -- scene lifecycle ----------------------------------------------------#
    def _ensure_scene(self):
        """Create the render window inside the host on first use. False while
        vtk is still importing / the host isn't mapped; degrades permanently
        (self._dead) on a real embed failure."""
        if self._win is not None:
            return True
        if self._dead or not self._host.winfo_ismapped():
            return False
        vtk = _import_vtk()
        if vtk is None:
            if _VTK_ERR is not None:
                self._dead = True
                self._say_placeholder(f"3D preview unavailable\n({_VTK_ERR})")
            return False
        if self._direct:
            # settle the host BEFORE the window exists: an idle callback run
            # here may render, and a render of a window not yet bound to the
            # host opens VTK's own top-level window ("Visualization Toolkit -
            # Win32OpenGL"), seen on opening the G-code page
            if getattr(self, "_scene_settling", False):
                return False
            self._scene_settling = True
            try:
                self._host.update_idletasks()
            finally:
                self._scene_settling = False
            if self._win is not None:
                return True
        try:
            self._ren = vtk.vtkRenderer()
            self._apply_viewport_bg()
            self._win = vtk.vtkRenderWindow()
            if self._direct:
                self._win.SetWindowInfo(str(self._host.winfo_id()))
            else:
                self._win.SetOffScreenRendering(1)
            self._win.SetSize(max(self._host.winfo_width(), 32),
                              max(self._host.winfo_height(), 32))
            self._win.AddRenderer(self._ren)
            if self._direct:
                self._host.configure(background="")
            else:
                self._w2i = vtk.vtkWindowToImageFilter()
                self._w2i.SetInput(self._win)
                self._w2i.ReadFrontBufferOff()
                # `_render` has just rendered the window; left on, this filter
                # renders it a SECOND time before it reads the pixels (a frame
                # cost 2x its single render: 723 vs 359 ms on a plate, 1415 vs
                # 682 ms on a tube, software GL; identical pixels, measured).
                self._w2i.ShouldRerenderOff()
            self._mapper = vtk.vtkPolyDataMapper()
            self._actor = vtk.vtkActor()
            self._actor.SetMapper(self._mapper)
            self._ren.AddActor(self._actor)
            # The g-code overlay: its OWN additive actor, always present and
            # hidden when the box is off -- the same shape as the mandrel rod,
            # one actor whose visibility follows one checkbox. Opaque, because
            # an opacity below 1 would put VTK's translucent pass over the whole
            # scene; see _GCODE_LINE_RGB for the two softening tricks that do
            # not work here.
            self._gc_mapper = vtk.vtkPolyDataMapper()
            self._gc_actor = vtk.vtkActor()
            self._gc_actor.SetMapper(self._gc_mapper)
            gp = self._gc_actor.GetProperty()
            gp.SetColor(*_GCODE_LINE_RGB)
            gp.SetLineWidth(1.0)
            gp.SetLighting(False)       # a wire, not a lit surface
            self._gc_actor.SetVisibility(0)
            self._ren.AddActor(self._gc_actor)
            # g-code zero, the XYZ triad Simulation draws
            # (`_update_origin_marker` places and sizes it)
            import vtk_helpers
            self._origin_actor = vtk_helpers.origin_marker(1.0)
            self._origin_actor.SetVisibility(0)
            self._ren.AddActor(self._origin_actor)
            cam = self._ren.GetActiveCamera()
            cam.SetParallelProjection(True)
            self._apply_studio_light()
            if self.want_view_cube:
                self._make_view_cube()
            self._say_placeholder("")
            if self._direct:
                # Paint the empty viewport now. The host stopped painting its
                # background above, so until the first design lands (seconds
                # for a large one, longer after Restore at launch) Windows
                # showed the bare host: a white rectangle under "updating…
                # drawing", which read as a broken page.
                self._win.Render()
        except Exception as exc:  # noqa: BLE001 -- degrade, never crash the GUI
            self._dead = True
            self._win = None
            self._say_placeholder(f"3D preview unavailable\n({exc})")
            return False
        return True

    def _studio_light_on(self):
        """The Preview-options "Studio lighting" switch (default on). Read
        through the app so a missing var (an isolated panel test) still lights
        the scene the way the page does."""
        var = getattr(self.app, "pv_studio_light", None)
        try:
            return bool(var.get()) if var is not None else True
        except tk.TclError:
            return True

    def _apply_studio_light(self):
        """Key/fill/back/head kit in, or VTK's automatic single headlight.

        PORTED from gcode_player._apply_studio_light, including the reason it
        clears FIRST: vtkRenderer creates its automatic headlight during any
        Render that finds the light list empty, and that light is an ordinary
        member of the list afterwards -- so Add/RemoveLightsFromRenderer alone
        leaves it in place and studio mode ends up with a stray sixth light the
        player does not have. Clearing to empty is what makes the A/B honest in
        both directions, because an empty list is what VTK re-fills.

        The headlight lights every tube from exactly the view direction, so the
        shading is symmetric across a fibre and it reads flat; the kit's
        off-axis key gives the highlight that makes it read round. Ambient is
        deliberately NOT raised -- the kit adds DIRECTIONAL light, so the
        away-facing facet still falls dark and the pores stay open when zoomed
        out, which is exactly what the old ambient 0.55 floor got wrong (see the
        material note in the tubes branch of _apply_display_mode).

        vtk_helpers imports vtk at module scope, so the import is LAZY here:
        this module must stay importable on a machine with no working vtk (the
        panel degrades to a placeholder instead of taking the GUI down).
        """
        if self._ren is None:
            return
        if self._light_kit is None:
            try:
                from vtk_helpers import studio_light_kit
                self._light_kit = studio_light_kit()
            except Exception:  # noqa: BLE001 -- no kit -> stock lighting, no crash
                return
        self._ren.RemoveAllLights()          # clear FIRST, always
        if self._studio_light_on():
            self._light_kit.AddLightsToRenderer(self._ren)
        # else: left empty, and the next Render re-creates the auto headlight

    def on_studio_light_changed(self):
        """The checkbox's command: re-light, then redraw. Every panel on the
        page owns one, so the 3-, 4- and 5-axis previews follow one switch."""
        self._apply_studio_light()
        self._render_soon()

    def _apply_viewport_bg(self):
        if self._ren is None:
            return
        self._ren.SetBackground(*(_VIEW_BG_DARK if self._is_dark()
                                  else _VIEW_BG_LIGHT))
        self._ren.GradientBackgroundOff()

    def retheme(self):
        """Dark-mode toggle: host + viewport backdrop, then re-render."""
        bg = _rgb_hex(_VIEW_BG_DARK if self._is_dark() else _VIEW_BG_LIGHT)
        if not self._direct:
            self._host.configure(background=bg)
        else:
            self._placeholder.configure(background=bg)
            if self._win is None:       # GL owns the pixels once it is on
                self._host.configure(background=bg)
        self._apply_viewport_bg()
        view = self.view_tools
        if view is not None:
            view.theme_changed()
        self._render_soon()

    def _on_destroy(self, _e=None):
        if self._win is not None:
            try:
                self._win.Finalize()
            except Exception:  # noqa: BLE001
                pass
            self._win = None

    def _rewrap(self, _e=None):
        try:
            pad = self.cget("padding")
            if not isinstance(pad, (tuple, list)):
                pad = self.tk.splitlist(str(pad))
            pad = [int(float(str(x))) for x in pad] or [0]
        except (tk.TclError, ValueError):
            pad = [0]
        lr = pad[0] + (pad[2] if len(pad) > 2 else pad[0])
        w = min(330, max(120, self.winfo_width() - lr - 4))
        if w == self._wrap_w:
            return
        self._wrap_w = w
        for lbl in self.grid_slaves(row=1, column=0) + [self._status_lbl]:
            try:
                lbl.configure(wraplength=w)
            except tk.TclError:
                pass

    def _on_map(self, _e=None):
        # re-sync the theme on every map: the dark_mode var does not exist yet
        # when the panel is constructed (the theme mixin creates it later), so
        # construction-time colours can be stale
        self.retheme()
        self.app._pv_on_panel_mapped()

    def _on_configure(self, _e=None):
        if self._win is not None:
            self._win.SetSize(max(self._host.winfo_width(), 32),
                              max(self._host.winfo_height(), 32))
            self._render()

    def _render(self):
        """Offscreen render -> the host label's pixels, NOW. Tk owns the screen
        pixels, so a single render is always enough (nothing can paint over it
        -- the fix for the notebook-repaint blanking).

        The BLIT is this panel's frame-rate ceiling, not the render: the VTK
        side costs ~0.7 ms even for a 3-million-triangle five-axis scene, while
        handing the pixels to Tk costs tens of milliseconds and scales with the
        panel's pixel AREA. So the size-unchanged case pastes into the image Tk
        already holds and reallocates nothing -- see the PIL import above for
        the measured numbers."""
        self._cancel_pending_render()
        if self._win is None or not self._host.winfo_ismapped():
            return
        self._frames += 1
        self._size_origin_marker()
        if self._direct:
            try:
                self._win.Render()      # onscreen: the swap IS the frame
            except Exception:  # noqa: BLE001
                pass
            return
        if self._page_hidden():
            # The shell's PageStack keeps every page MAPPED, so the test above
            # is true for a page nobody can see, and its picture was drawn in
            # full (and blitted) for each edit made elsewhere. Owe the frame
            # instead; `_page_arrived` pays it when the page comes up.
            self._render_owed = True
            self._watch_page()
            return
        self._render_owed = False
        try:
            from vtk.util import numpy_support as ns
            self._win.Render()
            self._w2i.Modified()
            self._w2i.Update()
            img = self._w2i.GetOutput()
            w, h, _d = img.GetDimensions()
            if w < 2 or h < 2:
                return
            arr = ns.vtk_to_numpy(img.GetPointData().GetScalars())
            arr = arr.reshape(h, w, -1)[::-1, :, :3]   # vtk rows are bottom-up
            buf = np.ascontiguousarray(arr)            # the [::-1] view is not
            if _PILImageTk is not None:
                # frombuffer over the ARRAY, not a tobytes() copy of it: the
                # bytes intermediate is a second full-frame copy for nothing.
                pil = _PILImage.frombuffer("RGB", (w, h), buf, "raw", "RGB", 0, 1)
                if self._photo is None or self._photo_size != (w, h):
                    self._photo = _PILImageTk.PhotoImage(pil, master=self._host)
                    self._photo_size = (w, h)
                    self._host.configure(image=self._photo, text="")
                else:
                    self._photo.paste(pil)             # in-place: the fast path
            else:
                ppm = b"P6 %d %d 255\n" % (w, h) + buf.tobytes()
                self._photo = tk.PhotoImage(master=self._host, data=ppm)
                self._photo_size = (w, h)
                self._host.configure(image=self._photo, text="")
        except Exception:  # noqa: BLE001
            pass

    # -- pages the shell keeps mapped but hides ------------------------------#
    def _page_stack(self):
        """`(page, stack)` when this panel lives on a page of the modern
        shell's PageStack, else `(None, None)` (the classic notebook unmaps
        its hidden pages, so `winfo_ismapped` already answers there)."""
        import gui_pagestack
        w = self._host
        try:
            while w is not None and w.master is not None:
                if isinstance(w.master, gui_pagestack.PageStack):
                    return w, w.master
                w = w.master
        except tk.TclError:                          # pragma: no cover
            pass
        return None, None

    def _page_hidden(self):
        """True when this panel's page is under another in the PageStack."""
        page, stack = self._page_stack()
        if page is None:
            return False
        try:
            return str(stack.select()) != str(page)
        except tk.TclError:                          # pragma: no cover
            return False

    def _watch_page(self):
        """Once: ask the stack to tell this panel when a page comes up."""
        if getattr(self, "_page_watched", False):
            return
        _page, stack = self._page_stack()
        if stack is None:
            return
        stack.bind("<<NotebookTabChanged>>", self._page_arrived, add="+")
        self._page_watched = True

    def _page_arrived(self, _e=None):
        if getattr(self, "_render_owed", False) and not self._page_hidden():
            # behind the stack's own `_fit_current`, which resizes the page
            # this handler runs before: one frame at the final size
            try:
                self.winfo_toplevel().after_idle(self._render_soon)
            except tk.TclError:                      # pragma: no cover
                pass

    # -- coalesced drawing ---------------------------------------------------#
    # Tk delivers <B1-Motion> at the mouse's report rate (125-1000 Hz) and does
    # NOT collapse a backlog the way Windows collapses WM_MOUSEMOVE for a native
    # window, so drawing inline meant one FULL frame per queued event, and the
    # image rubber-banded behind the cursor on a big panel. Moving the camera
    # stays inline -- it is microseconds, and dropping one would lose the drag's
    # travel -- while the DRAW goes on Tk's idle queue, which by definition runs
    # only once the event backlog is empty. A burst of N moves therefore costs
    # ONE frame, at the newest camera. The embedded viewers solve the same
    # problem one process away, in viewer_embed._coalesce_moves.
    def _render_soon(self):
        """Draw once the event backlog clears. Every caller is a drag or a state
        tweak that can wait a few ms; `_render` is the immediate one, and any
        immediate render cancels a deferred one that is still pending.

        Scheduled on the TOPLEVEL, like every other `after` in this app, and not
        on the host label. The Tcl command behind an `after` belongs to the
        widget it was scheduled through, but a teardown that flushes pending
        callbacks does it from the root -- `for aid in root.tk.call("after",
        "info"): root.after_cancel(aid)`, which is what test_ui_shell._destroy
        does. `after_cancel` deletes the script command through the widget it is
        called ON, so cancelling a LABEL's callback from the root drops the
        command out of Tcl while leaving its name in the label's `_tclCommands`;
        `Misc.destroy` (which does not guard that call) then deletes it a second
        time and raises `can't delete Tcl command` out of the ROOT's destroy --
        the whole app's teardown, not just this panel's. Measured: test_ui_shell
        exited 1 with all 51 cases passed and two stale `_render_deferred`
        commands, one on the preview panel's label and one on the plate
        editor's."""
        if self._drag_render_id is not None:
            return                                   # already queued
        try:
            self._drag_render_id = self.winfo_toplevel().after_idle(
                self._render_deferred)
        except tk.TclError:                          # pragma: no cover -- gone
            self._drag_render_id = None

    def _render_deferred(self):
        self._drag_render_id = None
        try:
            if not self._host.winfo_exists():        # pragma: no cover
                return
        except tk.TclError:                          # pragma: no cover
            return
        self._render()

    def _on_host_destroy(self, _e=None):
        # Unconditional: during teardown Tk hands the handler a <Destroy> whose
        # `widget` is the path STRING, not this object, so an identity guard
        # here never fires. The host label has no children to confuse it with.
        self._cancel_pending_render()
        if self._direct:
            # the GL context lives on the host's window: let it go while
            # that window still exists (Tk fires <Destroy> before it goes)
            self._on_destroy()

    def _cancel_pending_render(self):
        if self._drag_render_id is None:
            return
        pending, self._drag_render_id = self._drag_render_id, None
        try:
            self.winfo_toplevel().after_cancel(pending)
        except tk.TclError:                          # pragma: no cover
            pass

    # -- geometry in --------------------------------------------------------#
    def apply_state(self, state, subtitle):
        """Build/refresh the shown scene from a mixin state dict:
        {paths, meta, color, tube_pd, camera, framed_diag, status}. Builds the
        tube polydata on first show of a given generation, reuses it after."""
        self.set_subtitle(subtitle)
        if not self._ensure_scene():
            self.set_status(state.get("status", ""),
                            state.get("status_error", False))
            return False
        vtk = _VTK
        if state.get("paths") is None:
            # The waiting posture: the last good geometry stays on screen, so
            # its overlay does too -- `self._state` is deliberately not moved.
            self.set_status(state.get("status", ""),
                            state.get("status_error", False))
            return True
        try:
            if state.get("lines_pd") is None:
                state["lines_pd"] = self._lines_polydata(vtk, state)
                state["tubes_pd"] = None
                state["display_mode"] = None
            self._actor.SetVisibility(1)            # undo a `clear`
            self._actor.GetProperty().SetColor(
                *state.get("color", (0.8, 0.25, 0.2)))
            # Scalars only exist when the caller asked for per-path colours;
            # OFF is the default and is what every other page keeps, so the
            # actor's single colour above still decides what they draw.
            self._set_path_color_mode(state)
            # reframe on first show / big footprint change; else restore the
            # scaffold's own saved camera so a tweak never steals the view.
            # A Start X/Y move is a tweak: the design moves under the user's
            # camera, zoomed out or not (a caller that must re-aim clears
            # framed_diag, as the curved collector does)
            diag = state["diag"]
            framed = state.get("framed_diag")
            self._state = state
            if (framed is None
                    or diag > framed * _REFRAME_RATIO
                    or diag < framed / _REFRAME_RATIO):
                self.reset_view(state)
                state["framed_diag"] = diag
            elif state.get("camera") is not None:
                self._restore_camera(state["camera"])
            self._update_display_mode(force=True)
            self._update_gcode_overlay()
            self._update_origin_marker()
            view = self._view()
            if view is not None:
                view.sync(state)
                view.sync_overlay(state)
            self._fit_clipping()
            self._render_soon()
            self._refresh_status()
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"preview render failed ({exc})")
        return True

    def _view(self):
        """The inspection tools (`view_tools.ViewTools`) when the page asked
        for them (`want_view_tools`), built on first use."""
        if self.view_tools is None and self.want_view_tools \
                and self._win is not None:
            import view_tools
            self.view_tools = view_tools.ViewTools(self)
        return self.view_tools

    def clear(self):
        """Blank the picture until the next `apply_state` that carries geometry.

        The waiting posture above keeps the last good geometry on screen, which
        is right for an edit to the SAME design and wrong for a handover of the
        shared panel to a different one: switching the collector left the other
        mode's scaffold on screen for the whole of the new build. The status
        line is left alone -- the build the switch kicks writes its own."""
        self._state = None
        for actor in (self._actor, self._gc_actor):
            if actor is not None:
                actor.SetVisibility(0)
        self.set_highlight()
        if self._win is not None:
            self._render_soon()

    def set_highlight(self, polylines=(), points=()):
        """Recolour part of the picture yellow: `polylines` (a list of (N,3)
        fiber pieces) drawn the way the fibers are -- a tube just wide enough
        to cover the drawn fiber's facets, with its material, or a wire as wide as its wire --
        so the piece reads as the scaffold turned yellow; `points` as spheres
        1.5 fiber diameters across. No arguments clears it. Follows the
        tubes switch (`_update_display_mode` restyles it)."""
        polylines = [p for p in polylines if len(p) >= 2]
        points = list(points)
        if self._win is None or self._ren is None:
            return
        vtk = _VTK
        if self._hl is None:
            if not polylines and not points:
                return
            hl = {}
            for k in ("fiber", "dots"):
                m = vtk.vtkPolyDataMapper()
                a = vtk.vtkActor()
                a.SetMapper(m)
                a.SetVisibility(0)
                self._ren.AddActor(a)
                hl[k] = (m, a)
            self._hl = hl
        # clearing what is already clear changes no pixel: skip the restyle
        # and its frame (every regeneration of the text asks for it)
        if not polylines and not points and not any(self._hl.get("data", ())):
            return
        # the same pieces again (the print window's debounce re-lights what
        # the landing just lit) change no pixel either; a restyle from a new
        # picture or a tubes switch goes through `_update_display_mode`
        old = self._hl.get("data")
        if (old is not None and len(old[0]) == len(polylines)
                and len(old[1]) == len(points)
                and all(np.array_equal(a, b)
                        for a, b in zip(old[0], polylines))
                and np.array_equal(np.asarray(old[1], dtype=np.float64),
                                   np.asarray(points, dtype=np.float64))):
            return
        self._hl["data"] = (polylines, points)
        self._hl_restyle()

    def _hl_restyle(self):
        """Build the highlight for the shown picture's fiber size and mode."""
        hl = self._hl
        if hl is None or self._actor is None or self._mesh_held:
            return              # held: rebuilt by hold_mesh(False)
        vtk = _VTK
        polylines, points = hl["data"]
        meta = (self._state or {}).get("meta") or {}
        sides = meta.get("tube_sides") or PREVIEW_TUBE_SIDES
        d = float(meta.get("tube_diameter", meta.get("fiber_diameter"))
                  or 0.02)
        tubes = self._tubes_enabled()
        (fm, fa), (dm, da) = hl["fiber"], hl["dots"]
        for a in (fa, da):
            a.GetProperty().DeepCopy(self._actor.GetProperty())
            a.GetProperty().SetColor(*_HIGHLIGHT_RGB)
        if polylines:
            pd = self._polylines_polydata(vtk, polylines)
            if tubes and not highlight_tubes_fit(pd.GetNumberOfPoints()):
                # too large to tube in the memory free now: centrelines,
                # drawn the way the picture's own are
                tubes = False
                fa.GetProperty().SetLineWidth(self._LINE_WIDTH_PX)
                fa.GetProperty().SetLighting(False)
            if tubes:
                tube = vtk.vtkTubeFilter()
                tube.SetInputData(pd)
                # wide enough that its flat faces clear the fiber's
                # corners: both are PREVIEW_TUBE_SIDES-gons, twisted
                # differently, and a face sits at cos(pi/n) of a corner
                # (1.04x left blue stripes along a 6-sided tube)
                tube.SetRadius(max(d / 2.0 * 1.02 / math.cos(
                    math.pi / sides), 1e-5))
                tube.SetNumberOfSides(sides)
                tube.Update()
                pd = tube.GetOutput()
            fm.SetInputData(pd)
        else:
            self._hl_release(fm, fa)
        if points:
            from vtk.util import numpy_support as ns
            src = vtk.vtkPoints()
            src.SetData(ns.numpy_to_vtk(np.ascontiguousarray(
                np.asarray(points, dtype=np.float64)), deep=True))
            cloud = vtk.vtkPolyData()
            cloud.SetPoints(src)
            ball = vtk.vtkSphereSource()
            ball.SetRadius(max(0.75 * d, 1e-5))
            ball.SetThetaResolution(16)
            ball.SetPhiResolution(12)
            glyph = vtk.vtkGlyph3D()
            glyph.SetSourceConnection(ball.GetOutputPort())
            glyph.SetInputData(cloud)
            glyph.ScalingOff()
            glyph.Update()
            dm.SetInputData(glyph.GetOutput())
            da.GetProperty().SetLighting(True)
        else:
            self._hl_release(dm, da)
        fa.SetVisibility(1 if polylines else 0)
        da.SetVisibility(1 if points else 0)
        for a in (fa, da):
            self._hl_pose(a)
        self._render_soon()

    def _hl_release(self, mapper, actor):
        """Give back a highlight layer's mesh and the graphics buffers drawn
        from it, as `hold_mesh` does the picture's: a hidden actor kept
        both, so a whole scaffold lit once held a second tube mesh (298 MB,
        about 1 GB with its buffers, on a 20x20 mm waved scaffold) after
        the selection was gone."""
        if mapper.GetInput() is None or not mapper.GetInput().GetNumberOfPoints():
            return
        mapper.SetInputData(_VTK.vtkPolyData())
        if self._win is not None:
            try:
                actor.ReleaseGraphicsResources(self._win)
            except Exception:  # noqa: BLE001 -- the memory is a courtesy
                pass

    def _hl_pose(self, a):
        """VTK draws an actor at T(position) T(origin) R T(-origin): the
        highlight turned about the pivot, then shifted."""
        a.SetOrigin(*self._hl_pivot)
        a.SetOrientation(0.0, 0.0, self._hl_rz)
        a.SetPosition(*self._hl_offset)

    def set_highlight_offset(self, offset=(0.0, 0.0, 0.0), rz=0.0,
                             pivot=(0.0, 0.0, 0.0)):
        """Draw the highlight turned `rz` degrees about the vertical axis
        through `pivot`, then shifted by `offset` (world mm): the parts a
        Move is moving, seen at their new place while the picture of the
        moved print is still being built."""
        pose = (tuple(float(v) for v in offset), float(rz),
                tuple(float(v) for v in pivot))
        # Unchanged is a no-op: every edit that finishes and every panel that
        # opens (`_gce_mv_end`) puts the zero pose back, and a full frame for
        # it cost 0.7 s on a plate and 1.4 s on a tube (software GL).
        if pose == (self._hl_offset, self._hl_rz, self._hl_pivot):
            return
        self._hl_offset, self._hl_rz, self._hl_pivot = pose
        hl = self._hl
        if hl is None:
            return
        for k in ("fiber", "dots"):
            self._hl_pose(hl[k][1])
        self._render_soon()

    def highlight_offset(self):
        return self._hl_offset

    def highlight_pose(self):
        """(offset, rz degrees, pivot) of the highlight."""
        return self._hl_offset, self._hl_rz, self._hl_pivot

    def _set_path_color_mode(self, state):
        """Paint from the polydata's own RGB scalars, or from the actor's single
        colour. Called on every `apply_state`, so a page that stops supplying
        `path_colors` goes back to one colour without a rebuild."""
        if self._mapper is None:
            return
        view = self._view()
        if view is not None and view.paint(state):
            return                      # colour by layer / speed / scaffold
        on = (state.get("path_colors") is not None
              and len(state["path_colors"]) == len(state.get("paths") or []))
        self._mapper.SetScalarVisibility(1 if on else 0)
        if on:
            self._mapper.SetColorModeToDirectScalars()
            self._mapper.SetScalarModeToUsePointData()

    def _refresh_status(self):
        state = self._state
        if state is None:
            return
        if state.get("display_mode") != "lines":
            note = ""
        elif state.get("tubes_too_big"):
            note = (" · drawn as centerlines: this print is too large to "
                    "draw as tubes in the memory free now")
        else:
            note = " · centerlines (tick 'Draw fibers as 3-D tubes' for tubes)"
        self.set_status(state.get("status", "") + note,
                        state.get("status_error", False))

    def _lines_polydata(self, vtk, state):
        """numpy polylines -> one centreline vtkPolyData (the tube filter's
        input AND the thin-fiber display geometry)."""
        pd = self._polylines_polydata(vtk, state["paths"],
                                      state.get("path_colors"))
        radii = (state.get("meta") or {}).get("point_radii")
        if radii is not None and len(radii) == len(state["paths"]) and all(
                len(r) == len(p) for r, p in zip(radii, state["paths"])):
            from vtk.util import numpy_support as ns
            arr = ns.numpy_to_vtk(np.ascontiguousarray(np.concatenate(radii),
                                                       dtype=np.float64), deep=True)
            arr.SetName("fiber_radius")
            pd.GetPointData().AddArray(arr)
        if state.get("aux"):
            import view_tools
            view_tools.add_arrays(vtk, pd, state["aux"])
        return pd

    @staticmethod
    def _polylines_polydata(vtk, paths, path_colors=None):
        """A list of (N,3) polylines -> one vtkPolyData of lines. Shared by the
        fiber centrelines and the g-code overlay so both are built one way.

        `path_colors`, when given, is one RGB triple PER PATH and paints each
        polyline separately; without it the polydata carries no scalars at all
        and every caller gets exactly the geometry it always got, drawn in the
        actor's single `state["color"]`.

        DIRECT RGB POINT SCALARS, not a lookup table, for two reasons: there is
        no scalar field here to map (the colour IS the datum -- which layer a
        point belongs to), and point data survives `vtkTubeFilter`, so the
        thin-line and 3-D-tube display modes colour identically with no second
        code path. A curved collector drawn several LAYERS at once in one colour
        makes a crossed grid unreadable -- two readers looking at the same render
        concluded a layer was missing from it.
        """
        from vtk.util import numpy_support as ns
        pts_np = np.ascontiguousarray(
            np.concatenate(paths, axis=0), dtype=np.float64)
        counts = np.array([len(p) for p in paths], dtype=np.int64)
        offsets = np.zeros(len(paths) + 1, dtype=np.int64)
        np.cumsum(counts, out=offsets[1:])
        conn = np.arange(len(pts_np), dtype=np.int64)
        cells = vtk.vtkCellArray()
        cells.SetData(ns.numpy_to_vtkIdTypeArray(offsets, deep=True),
                      ns.numpy_to_vtkIdTypeArray(conn, deep=True))
        points = vtk.vtkPoints()
        points.SetData(ns.numpy_to_vtk(pts_np, deep=True))
        pd = vtk.vtkPolyData()
        pd.SetPoints(points)
        pd.SetLines(cells)
        if path_colors is not None and len(path_colors) == len(paths):
            rgb = np.repeat(
                np.clip(np.asarray(path_colors, dtype=np.float64), 0.0, 1.0)
                * 255.0 + 0.5, counts, axis=0).astype(np.uint8)
            arr = ns.numpy_to_vtk(np.ascontiguousarray(rgb), deep=True,
                                  array_type=vtk.VTK_UNSIGNED_CHAR)
            arr.SetName("path_rgb")
            pd.GetPointData().SetScalars(arr)
        return pd

    def _gcode_enabled(self):
        """The Preview-options "Show the g-code path" switch (default OFF).
        Read through the app so a missing var (an isolated panel test) simply
        means no overlay."""
        var = getattr(self.app, "pv_show_gcode", None)
        try:
            return bool(var.get()) if var is not None else False
        except tk.TclError:
            return False

    def _update_gcode_overlay(self):
        """Show or hide the commanded-toolpath overlay for the shown state.

        Pure restyle: `meta["gcode_paths"]` rides every build, so the checkbox
        never triggers one. The polydata is built on first show and cached on
        the state beside the fiber polydata, invalidated with it."""
        actor = self._gc_actor
        if actor is None:
            return
        state = self._state
        gpaths = ((state.get("meta") or {}).get("gcode_paths")
                  if state is not None else None)
        if not self._gcode_enabled() or not gpaths:
            actor.SetVisibility(0)
            return
        if state.get("gcode_pd") is None:
            state["gcode_pd"] = self._polylines_polydata(_VTK, gpaths)
            gt = state.get("gcode_t")
            if gt is not None and len(gt) == state["gcode_pd"] \
                    .GetNumberOfPoints():
                import view_tools
                view_tools.add_arrays(_VTK, state["gcode_pd"],
                                      {"mew_view_t": gt})
        self._gc_mapper.SetInputData(state["gcode_pd"])
        actor.SetVisibility(1)
        view = self._view()
        if view is not None:
            view.sync_overlay(state)

    def hold_mesh(self, held):
        """Give back the drawn mesh while the panel's page is out of sight, and
        rebuild it when the page returns.

        The tube mesh and the graphics buffers drawn from it are the panel's
        largest cost: 294 MB of tubes and about 350 MB of process memory in
        all, on a plate of four 8 x 8 mm, 15-layer scaffolds. Simulation draws
        the same print in its own process and sizes its fibers to the memory
        that is free when it opens (`gcode_player._tube_sides_that_fit`), so a
        hidden Design page holding its copy pushed Simulation down to Lines
        on an ordinary machine. While held, the centrelines and the paths
        stay (the picture comes back with no rebuild of the design); only
        the tubes and the buffers go. The rebuild on return goes through
        `_update_display_mode`, so it is held to the same free-memory rule
        (`tubes_fit`) as any first draw."""
        held = bool(held)
        if held == self._mesh_held:
            return
        self._mesh_held = held
        state = self._state
        if held:
            if state is not None:
                state["tubes_pd"] = None
                state["display_mode"] = None
            if self._mapper is not None:
                self._mapper.SetInputData(_VTK.vtkPolyData())
                if self._win is not None:
                    try:
                        self._actor.ReleaseGraphicsResources(self._win)
                    except Exception:  # noqa: BLE001 -- the memory is a courtesy
                        pass
            if self._hl is not None:
                # the highlight is a tube mesh too (a whole scaffold lit is
                # as large as the picture); `_update_display_mode` rebuilds it
                for k in ("fiber", "dots"):
                    self._hl_release(*self._hl[k])
        elif state is not None and state.get("lines_pd") is not None:
            self._update_display_mode(force=True)
            self._render_soon()

    def _tubes_enabled(self):
        """The Preview-options "Draw fibers as 3-D tubes" switch (default on).
        Read through the app so a missing var (e.g. an isolated panel test)
        degrades to the auto tube/line behaviour."""
        var = getattr(self.app, "pv_show_tubes", None)
        try:
            return bool(var.get()) if var is not None else True
        except tk.TclError:
            return True

    def _update_display_mode(self, force=False):
        """Tubes vs centrelines is driven ONLY by the Preview-options "Draw
        fibers as 3-D tubes" checkbox -- tubes when it is on (the default),
        flat centrelines when it is off. There is no zoom-dependent auto-switch:
        the tubes stay tubes at every scale. Called after apply / a checkbox
        toggle; zoom/orbit/pan don't change the mode so they skip it."""
        state = self._state
        if state is None or self._ren is None or state.get("lines_pd") is None:
            return
        if self._mesh_held:
            return                      # rebuilt by hold_mesh(False)
        vtk = _VTK
        mode = "tubes" if self._tubes_enabled() else "lines"
        if mode == "tubes" and state.get("tubes_pd") is None:
            meta = state["meta"]
            state["tubes_too_big"] = not tubes_fit(
                state["lines_pd"], meta.get("tube_sides") or PREVIEW_TUBE_SIDES)
            if state["tubes_too_big"]:
                mode = "lines"
        if mode == state.get("display_mode") and not force:
            return
        prop = self._actor.GetProperty()
        if mode == "tubes":
            if state.get("tubes_pd") is None:
                if self._tubes_async(vtk, state):
                    return          # the last picture stays up meanwhile
                tube = self._tube_filter(vtk, state)
                tube.Update()
                state["tubes_pd"] = tube.GetOutput()
            self._mapper.SetInputData(state["tubes_pd"])
            prop.SetLineWidth(1.0)
            # Matte diffuse shading, matching the STL viewer's solid fibers
            # (ambient ~0, diffuse 1, a little specular): the round tube reads
            # from light->dark across its width, so neighbouring fibers stay
            # visually SEPARATE and the pores between them stay dark. The old
            # strong ambient floor (0.55) made the fibers self-lit ("luminous"),
            # which flooded small pores with brightness when zoomed out. A small
            # ambient keeps the darkest facet off pure black without glowing.
            prop.SetLighting(True)
            prop.SetAmbient(0.12)
            prop.SetDiffuse(0.9)
            prop.SetSpecular(0.25)
            prop.SetSpecularPower(20)
            try:
                prop.SetRenderLinesAsTubes(False)
            except AttributeError:
                pass
        else:
            # plain GL lines (RenderLinesAsTubes proved driver-fragile here:
            # some GPUs draw nothing at all with it on). Unlit -> flat wires,
            # matching stl_viewer / the player's wires. Kept at 1 px (was 1.6):
            # a fixed-pixel-width wire that is wider than the on-screen pore
            # spacing merges neighbouring fibers into a solid block when zoomed
            # out -- 1 px keeps the pores open as long as the GPU can resolve them.
            # `_LINE_WIDTH_PX` is that 1 px for every panel here; a SUBCLASS may
            # raise it when it draws a handful of layers framed large rather than
            # a whole scaffold zoomed out, since the pore argument above does not apply there.
            self._mapper.SetInputData(state["lines_pd"])
            prop.SetLineWidth(self._LINE_WIDTH_PX)
            prop.SetLighting(False)
            try:
                prop.SetRenderLinesAsTubes(False)
            except AttributeError:
                pass
        state["display_mode"] = mode
        self._hl_restyle()
        self._refresh_status()

    @staticmethod
    def _tube_filter(vtk, state):
        """The tube filter for `state`'s centrelines, set up, not yet run."""
        tube = vtk.vtkTubeFilter()
        tube.SetInputData(state["lines_pd"])
        meta = state["meta"]
        tube.SetRadius(max(meta.get(
            "tube_diameter", meta["fiber_diameter"]) / 2.0, 1e-5))
        if state["lines_pd"].GetPointData().HasArray("fiber_radius"):
            tube.SetInputArrayToProcess(
                0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS,
                "fiber_radius")
            tube.SetVaryRadiusToVaryRadiusByAbsoluteScalar()
        tube.SetNumberOfSides(meta.get("tube_sides") or PREVIEW_TUBE_SIDES)
        # a subsection's cut fibres end flat at the box, not open
        tube.SetCapping(1 if meta.get("cap_tubes") else 0)
        return tube

    #: centreline points from which `_tubes_async` builds off the Tk thread
    #: (below it the tubes take a few milliseconds)
    ASYNC_TUBE_MIN_POINTS = 20_000

    def _tubes_async(self, vtk, state):
        """True when `state`'s tubes are being built on a thread instead
        (`async_tubes` panels, a picture already on screen, a large print):
        the same filter, the same output, and the window keeps working.

        The tube filter held the window 0.2-1.2 s on every new G-code page
        picture. VTK lets go of the GIL while it runs, so on a
        thread it costs the window nothing. The last picture stays up until
        the tubes land (`_tubes_async_poll`), then they go up as they would
        have; a newer picture in the meantime drops them."""
        if not self.async_tubes or self._mapper is None \
                or self._mapper.GetInput() is None:
            return False            # the first picture: nothing to keep up
        lines = state["lines_pd"]
        if lines.GetNumberOfPoints() < self.ASYNC_TUBE_MIN_POINTS:
            return False
        job = state.get("tube_job")
        if job is not None and job["lines"] is lines:
            return True             # already on its way
        tube = self._tube_filter(vtk, state)
        job = {"lines": lines, "out": None, "done": threading.Event()}

        def run():
            try:
                tube.Update()
                job["out"] = tube.GetOutput()
            except Exception:  # noqa: BLE001 -- built here instead, below
                job["out"] = None
            finally:
                job["done"].set()
        state["tube_job"] = job
        threading.Thread(target=run, daemon=True).start()
        self.after(15, lambda: self._tubes_async_poll(state, job))
        return True

    def _tubes_async_poll(self, state, job):
        if not job["done"].is_set():
            try:
                self.after(15, lambda: self._tubes_async_poll(state, job))
            except tk.TclError:
                pass
            return
        if state.get("tube_job") is job:
            state.pop("tube_job", None)
        if self._state is not state or state.get("lines_pd") is not job["lines"] \
                or state.get("tubes_pd") is not None:
            return                  # a newer picture replaced this one
        if job["out"] is None:
            # the thread failed: build it here, as it always was
            tube = self._tube_filter(_VTK, state)
            tube.Update()
            job["out"] = tube.GetOutput()
        state["tubes_pd"] = job["out"]
        try:
            self._update_display_mode(force=True)
            view = self._view()
            if view is not None:
                view.sync(state)
            self._fit_clipping()
            self._render_soon()
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"preview render failed ({exc})")

    def show_state(self, state, subtitle):
        """Pill-rail switch: save the outgoing camera, show `state`."""
        self._save_camera()
        self.apply_state(state, subtitle)

    # -- camera -------------------------------------------------------------#
    def _pivot(self):
        if self._state is None:
            return (0.0, 0.0, 0.0)
        (x0, y0, z0), (x1, y1, z1) = _frame_bbox(self._state["meta"])
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0)

    def reset_view(self, state=None):
        """Frame the isometric default on the design (never the plate)."""
        state = state or self._state
        if self._ren is None or state is None or state.get("meta") is None:
            return
        (x0, y0, z0), (x1, y1, z1) = _frame_bbox(state["meta"])
        c = ((x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0)
        # Z IS IN THE MAX SINCE THE CURVED COLLECTOR. It was X and
        # Y only, which is right for the two collectors that existed: a flat
        # scaffold's Z span is layer height (0.17 mm against 50.9 in X, measured
        # off `shots.json`), and a tube's Y and Z spans are BOTH the diameter,
        # because `build_mandrel_preview`'s bbox pads to +/-radius on each, so
        # taking the larger of the two changes nothing. A dome is the first
        # geometry whose Z span is its LARGEST -- 5.01 mm against 3.97 in Y on
        # the shipped defaults -- and framing on 3.97 cropped it off every edge.
        ext = max(x1 - x0, y1 - y0, z1 - z0, 1e-3)
        cam = self._ren.GetActiveCamera()
        d = 4.0 * ext
        # A FLAT LATTICE OPENS 60 DEGREES ABOVE THE PLATE, not at the 29-degree
        # isometric. From the isometric a stack of layers hides the pore behind
        # it once the stack is taller than ~0.6 of the pore, so a 20-layer
        # scaffold with 0.3 mm pores drew as a solid sheet. Simulation opens at
        # the same angle (gcode_player.OPENING_DIR_FLAT), so the two views of
        # one design agree. A tube or dome (Z span comparable to its width)
        # keeps the isometric: looking down on a tube hides the tube.
        flat = (z1 - z0) < 0.1 * max(x1 - x0, y1 - y0, 1e-3)
        hz = 1.212 if flat else 0.55        # 0.7*sqrt(2)*tan(60 deg) = 1.212
        k = 1.0 / math.sqrt(0.7 ** 2 + 0.7 ** 2 + hz ** 2)
        off = np.array([-0.7 * k * d, -0.7 * k * d, hz * k * d])
        up = np.array([0.0, 0.0, 1.0])
        # THE ISOMETRIC IS RELATIVE TO WHICH WAY THE SCAFFOLD FACES, not to
        # world +Z. On a plate and a mandrel those are the same thing and the
        # rotation below is the identity, byte for byte. On a surface of
        # revolution they are not: the shipped dome's design faces
        # (0.825, 0, -0.565), and the world isometric sat at dot -0.78 to that
        # -- the camera inside the collector, the scaffold behind it. Rotating
        # the whole tripod (offset AND up) keeps the framing everyone already
        # knows and only re-aims it. `view_normal` is set by
        # `gui_surface.build_surface_preview`, and is absent everywhere else.
        n = (state.get("meta") or {}).get("view_normal")
        if n is not None:
            rot = _rotation_from_z(n)
            if rot is not None:
                off, up = rot @ off, rot @ up
            off, up = self._clear_sight_line(state, c, off, up, n)
        cam.SetFocalPoint(*c)
        cam.SetPosition(c[0] + off[0], c[1] + off[1], c[2] + off[2])
        cam.SetViewUp(*up)
        # from 60 degrees the footprint's diamond stands 1.22*ext tall on
        # screen, against 0.68*ext from the isometric: give it the room
        cam.SetParallelScale((0.72 if flat else 0.62) * ext)
        self._persp_sync()
        if state is self._state:
            self._update_display_mode()
            self._fit_clipping()
            self._render()
        self._save_camera()

    def _clear_sight_line(self, state, c, off, up, n):
        """`(off, up)`, swung toward the facing normal `n` until the drawn
        collector no longer stands between the camera and the scaffold.

        The isometric sits about 60 degrees off `n`. A convex cap falls away
        from that line and keeps it (returned unchanged, so a dome's view is
        what it was); inside a CUP the near wall stands in it, and the page
        showed a grey wall with the scaffold behind it -- Bowl, even after
        Reset view. Straight down `n` always clears a concave patch, because
        that line leaves through the mouth. The target is the drawn path point
        nearest the framed centre, lifted a hair along `n` so the surface the
        fibre lies on is not counted as in the way."""
        actor = getattr(self, "_surface_actor", None)
        paths = state.get("paths")
        if actor is None or not actor.GetVisibility() or not paths:
            return off, up
        import vtk
        try:
            n = np.asarray(n, dtype=np.float64)
            n = n / np.linalg.norm(n)
            pts = np.concatenate([np.asarray(p, dtype=np.float64)
                                  for p in paths], axis=0)
            cc = np.asarray(c, dtype=np.float64)
            target = pts[np.argmin(np.linalg.norm(pts - cc, axis=1))]
            target = tuple(target + 0.01 * float(np.linalg.norm(off)) * n)
            tree = vtk.vtkOBBTree()
            tree.SetDataSet(actor.GetMapper().GetInput())
            tree.BuildLocator()
        except Exception:  # noqa: BLE001  (no sane test -> keep the view)
            return off, up
        dist = float(np.linalg.norm(off))
        d0 = off / dist
        for a in (0.0, 0.25, 0.5, 0.75, 1.0):
            d = (1.0 - a) * d0 + a * n
            d = d / np.linalg.norm(d)
            hits = vtk.vtkPoints()
            tree.IntersectWithLine(tuple(cc + d * dist), target, hits, None)
            if hits.GetNumberOfPoints() == 0:
                u = up - float(up @ d) * d        # keep the view-up square
                if np.linalg.norm(u) < 1e-6:
                    u = np.cross(d, [1.0, 0.0, 0.0])
                    if np.linalg.norm(u) < 1e-6:
                        u = np.cross(d, [0.0, 1.0, 0.0])
                return d * dist, u / np.linalg.norm(u)
        return off, up

    def look_at(self, points):
        """Frame `points` ((N,3), the parts to look at) from where the camera
        already looks: the direction and up stay, the centre moves to them and
        the zoom fits them, never tighter than a fiftieth of the design.
        Returns False when there is nothing to frame."""
        if self._ren is None or self._state is None:
            return False
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        pts = pts[np.isfinite(pts).all(axis=1)]
        if not len(pts):
            return False
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        c = (lo + hi) / 2.0
        (x0, y0, z0), (x1, y1, z1) = _frame_bbox(self._state["meta"])
        whole = max(x1 - x0, y1 - y0, z1 - z0, 1e-3)
        ext = max(float((hi - lo).max()), whole / 50.0)
        cam = self._ren.GetActiveCamera()
        fp = np.array(cam.GetFocalPoint())
        pos = np.array(cam.GetPosition())
        d = pos - fp
        cam.SetFocalPoint(*c)
        cam.SetPosition(*(c + d))
        cam.SetParallelScale(0.62 * ext)
        self._persp_sync()
        self._fit_clipping()
        self._render()
        self._save_camera()
        return True

    def _save_camera(self):
        if self._ren is None or self._state is None:
            return
        cam = self._ren.GetActiveCamera()
        self._state["camera"] = (cam.GetPosition(), cam.GetFocalPoint(),
                                 cam.GetViewUp(), cam.GetParallelScale())

    def _restore_camera(self, saved):
        cam = self._ren.GetActiveCamera()
        pos, fp, up, scale = saved
        cam.SetPosition(*pos)
        cam.SetFocalPoint(*fp)
        cam.SetViewUp(*up)
        cam.SetParallelScale(scale)
        self._persp_sync()

    def _persp_sync(self):
        """The Settings window's Perspective switch. Every fit, zoom and pan
        here works in the orthographic SCALE (the half-height, in mm, the
        view shows); in perspective the camera's distance follows it, so the
        focal plane shows exactly that half-height and all of that maths
        holds there unchanged."""
        if self._ren is None:
            return
        import vtk_helpers
        cam = self._ren.GetActiveCamera()
        if not vtk_helpers.prefer_perspective():
            cam.ParallelProjectionOn()
            return
        fp = np.array(cam.GetFocalPoint())
        v = np.array(cam.GetPosition()) - fp
        n = float(np.linalg.norm(v))
        t = math.tan(math.radians(cam.GetViewAngle()) / 2.0)
        if n > 1e-12 and t > 0.0:
            cam.SetPosition(*(fp + v / n * (cam.GetParallelScale() / t)))
        cam.ParallelProjectionOff()

    def _update_origin_marker(self):
        """Put the XYZ triad on g-code zero (`_pv_origin_point`), or hide it:
        the Settings switch is off, or the collector has no zero to draw.
        Every frame sizes it (`_size_origin_marker`)."""
        a = getattr(self, "_origin_actor", None)
        if a is None:
            return
        import vtk_helpers
        st = self._state
        fn = getattr(self.app, "_pv_origin_point", None)
        p = (fn(st["meta"]) if callable(fn) and st is not None and st.get("meta")
             and vtk_helpers.prefer_origin() else None)
        if p is None:
            a.SetVisibility(0)
            return
        (x0, y0, z0), (x1, y1, z1) = _frame_bbox(st["meta"])
        self._origin_diag = max(math.dist((x0, y0, z0), (x1, y1, z1)), 1e-3)
        a.SetPosition(*p)
        a.SetVisibility(1)
        self._size_origin_marker()

    def _size_origin_marker(self):
        """The triad's arm for the camera now: Simulation's share of the
        design's diagonal, never more than `ORIGIN_ARM_OF_VIEW` of the
        view's half-height (the parallel scale, which the perspective
        camera keeps too, `_persp_sync`)."""
        a = getattr(self, "_origin_actor", None)
        diag = getattr(self, "_origin_diag", None)
        if a is None or not diag or not a.GetVisibility() or self._ren is None:
            return
        arm = ORIGIN_ARM_OF_DIAG * diag
        scale = self._ren.GetActiveCamera().GetParallelScale()
        if scale > 0.0:
            arm = min(arm, ORIGIN_ARM_OF_VIEW * scale)
        if abs(a.GetScale()[0] - arm) > 1e-6 * arm:
            a.SetScale(arm)

    def set_origin_visible(self, _on):
        """The Settings switch, live."""
        if self._ren is None:
            return
        self._update_origin_marker()
        self._fit_clipping()
        self._render_soon()

    def set_perspective(self, on):
        """Flip the picture on show (the switch's live path)."""
        if self._ren is None:
            return
        self._persp_sync()
        self._fit_clipping()
        self._render_soon()

    def _fit_clipping(self):
        try:
            import vtk_helpers
            vtk_helpers.fit_clipping_range(self._ren)
        except Exception:  # noqa: BLE001
            pass

    # the exact rigid rotation install_cad_camera uses, pivoted at p
    def _rotate(self, axis, angle, p):
        vtk = _VTK
        cam = self._ren.GetActiveCamera()
        t = vtk.vtkTransform()
        t.Translate(p[0], p[1], p[2])
        t.RotateWXYZ(angle, axis[0], axis[1], axis[2])
        t.Translate(-p[0], -p[1], -p[2])
        cam.SetPosition(t.TransformPoint(cam.GetPosition()))
        cam.SetFocalPoint(t.TransformPoint(cam.GetFocalPoint()))
        r = vtk.vtkTransform()
        r.RotateWXYZ(angle, axis[0], axis[1], axis[2])
        cam.SetViewUp(r.TransformVector(cam.GetViewUp()))

    def _cursor_pivot(self, x_tk, y_tk):
        """The model point UNDER THE CURSOR -- the orbit anchor, matching the
        windowed/embedded viewers (vtk_helpers.install_cad_camera._pivot):
        read the depth buffer at the pixel; if it hit geometry (0<z<1) use
        that world point, else fall back to the cursor's focal-plane point.
        Falls back to the scaffold-centre pivot if the read fails. VTK display
        coords are y-up, so flip the Tk y. (GetZ works on the offscreen
        window; the preview has a single renderer -- no overlay depth wipe.)"""
        ren = self._ren
        if ren is None:
            return self._pivot()
        h = max(self._host.winfo_height(), 1)
        # the depth buffer at the camera shown now, as `pick` has it: the
        # on-screen window's buffers are not kept across a focus loss (Alt-
        # Tab), so a read without a fresh frame can find no fiber
        if not self._depth_render():
            try:
                self._win.Render()
            except Exception:  # noqa: BLE001 -- the read below degrades
                pass
        try:
            z = ren.GetZ(int(x_tk), int(h - y_tk))
        except Exception:  # noqa: BLE001
            z = 1.0
        if 0.0 < z < 1.0:
            ren.SetDisplayPoint(int(x_tk), int(h - y_tk), z)
            ren.DisplayToWorld()
            w = ren.GetWorldPoint()
            if w[3]:
                return (w[0] / w[3], w[1] / w[3], w[2] / w[3])
        return self._focal_plane_world(x_tk, y_tk)

    def _press(self, mode, e, clicks=1):
        if self._win is None:
            return
        if mode == "orbit" and self._overlay_press(e):
            return
        self._clicks = clicks
        self._click_mods = int(getattr(e, "state", 0) or 0)
        view = self.view_tools
        self._click0 = ((e.x, e.y) if mode == "orbit"
                        and (self.on_pick is not None
                             or (view is not None and view.measuring))
                        else None)
        # anchor the orbit/turntable pivot on the point under the cursor NOW
        # (fixed for the whole drag, like the viewers); pan doesn't pivot
        piv = (self._cursor_pivot(e.x, e.y)
               if mode in ("orbit", "zorbit") else None)
        self._drag = (mode, e.x, e.y, piv)

    def _release(self, _e=None):
        if self._gdrag is not None:
            self._gimbal_drag(_e, done=True)
            return
        click, self._click0 = self._click0, None
        if click is not None and _e is not None and self._drag is not None:
            self._drag = None
            self._fire_pick(_e.x, _e.y, self._clicks)
            return
        self._drag = None
        if self._drag_render_id is not None:
            self._render()      # a drag's LAST frame is drawn, never left queued
        self._save_camera()

    def _motion(self, e):
        if self._gdrag is not None:
            self._gimbal_drag(e)
            return
        if self._drag is None or self._win is None:
            return
        mode, lx, ly, piv = self._drag
        if self._click0 is not None:
            # a click until it travels: the camera holds still under a
            # click, so the picked fiber is the one the user aimed at
            cx, cy = self._click0
            if (e.x - cx) ** 2 + (e.y - cy) ** 2 <= _CLICK_SLOP_PX ** 2:
                return
            self._click0 = None
        dx, dy = e.x - lx, e.y - ly
        self._drag = (mode, e.x, e.y, piv)
        w = max(self._host.winfo_width(), 1)
        h = max(self._host.winfo_height(), 1)
        cam = self._ren.GetActiveCamera()
        p = piv if piv is not None else self._pivot()
        if mode == "orbit":
            # trackball rates (200 deg / window), Tk's y-down flips elevation
            self._rotate(cam.GetViewUp(), -200.0 * dx / w, p)
            pos, fp, up = (cam.GetPosition(), cam.GetFocalPoint(),
                           cam.GetViewUp())
            nvd = (pos[0] - fp[0], pos[1] - fp[1], pos[2] - fp[2])
            right = (nvd[1] * up[2] - nvd[2] * up[1],
                     nvd[2] * up[0] - nvd[0] * up[2],
                     nvd[0] * up[1] - nvd[1] * up[0])
            self._rotate(right, 200.0 * dy / h, p)
            cam.OrthogonalizeViewUp()
        elif mode == "zorbit":
            # turntable: azimuth about the WORLD +Z axis through the pivot
            self._rotate((0.0, 0.0, 1.0), -200.0 * dx / w, p)
        else:  # pan
            scale = cam.GetParallelScale()
            wpp = 2.0 * scale / h                     # world units per pixel
            pos = np.array(cam.GetPosition())
            fp = np.array(cam.GetFocalPoint())
            up = np.array(cam.GetViewUp())
            fwd = fp - pos
            fwd /= max(np.linalg.norm(fwd), 1e-12)
            up = up / max(np.linalg.norm(up), 1e-12)
            right = np.cross(fwd, up)
            delta = right * (-dx * wpp) + up * (dy * wpp)
            cam.SetPosition(*(pos + delta))
            cam.SetFocalPoint(*(fp + delta))
        self._fit_clipping()
        self._render_soon()

    def _focal_plane_world(self, x_tk, y_tk):
        """Cursor pixel -> world point at the focal-plane depth (the viewers'
        zoom-to-cursor anchor). VTK display coords are y-up."""
        ren = self._ren
        h = max(self._host.winfo_height(), 1)
        fx, fy, fz = ren.GetActiveCamera().GetFocalPoint()
        ren.SetWorldPoint(fx, fy, fz, 1.0)
        ren.WorldToDisplay()
        z = ren.GetDisplayPoint()[2]
        ren.SetDisplayPoint(x_tk, h - y_tk, z)
        ren.DisplayToWorld()
        wpt = ren.GetWorldPoint()
        if wpt[3]:
            return (wpt[0] / wpt[3], wpt[1] / wpt[3], wpt[2] / wpt[3])
        return wpt[:3]

    # -- the ViewCube and the Move gimbal (overlays with their own clicks) -- #
    def _make_view_cube(self):
        """The viewers' ViewCube in this panel's upper right. A snap keeps
        the zoom and centre, as the cube does in the Viewer."""
        try:
            import view_cube

            def reframe():
                self._fit_clipping()
                self._render_soon()
                self._save_camera()
            self._cube = view_cube.ViewCube(
                self._win, None, self._ren, reframe=reframe,
                dark=self._is_dark())
        except Exception:  # noqa: BLE001 -- no cube, the picture still works
            self._cube = None

    def _vtk_xy(self, e):
        """A Tk event's point in VTK display coordinates (y up)."""
        return e.x, max(self._host.winfo_height(), 1) - e.y

    def _overlay_press(self, e):
        """A left press on the ViewCube snaps the view; on a gimbal handle
        starts a move. True when the press was theirs (no orbit, no pick)."""
        x, y = self._vtk_xy(e)
        cube = self._cube
        if cube is not None:
            try:
                hit = cube.pick(x, y)
            except Exception:  # noqa: BLE001
                hit = None
            if hit is not None:
                self._drag = self._click0 = None
                cube.snap(hit)
                return True
        g = self._gimbal
        if g is not None and g.visible() and self.on_gimbal is not None:
            import move_gimbal
            handle = g.hit(x, y)
            p0 = g.point(handle, x, y) if handle else None
            if p0 is not None:
                self._drag = self._click0 = None
                # (handle, press point, highlight pose, gimbal origin,
                #  [turn so far, last angle] for the ring)
                self._gdrag = (handle, p0, self.highlight_pose(), g.origin,
                               [0.0, move_gimbal.ring_angle(g.origin, p0)])
                g.set_active(handle)
                self._render_soon()
                return True
        return False

    def _gimbal_drag(self, e, done=False):
        """Follow a gimbal drag: the gimbal and the highlight move with it,
        and `on_gimbal` hears the delta (dx, dy, dz, degrees about Z)."""
        import move_gimbal
        handle, p0, hl0, o0, turn = self._gdrag
        g = self._gimbal
        if handle == "rz":
            return self._gimbal_turn(e, done)
        delta = (0.0, 0.0, 0.0)
        if e is not None:
            x, y = self._vtk_xy(e)
            # the constraint is the gimbal's at the press, not where it has
            # since been dragged to
            now = g.origin
            g.origin = o0
            p = g.point(handle, x, y)
            g.origin = now
            if p is not None:
                delta = move_gimbal.constrain(
                    handle, (p[0] - p0[0], p[1] - p0[1], p[2] - p0[2]))
        g.show((o0[0] + delta[0], o0[1] + delta[1], o0[2] + delta[2]))
        off0, rz0, piv0 = hl0
        self.set_highlight_offset((off0[0] + delta[0], off0[1] + delta[1],
                                   off0[2] + delta[2]), rz0, piv0)
        self._gimbal_end(tuple(delta) + (0.0,), done)

    def _gimbal_turn(self, e, done):
        """Follow a drag of the Z ring: the turn is the angle swept about
        the gimbal's vertical axis, added up event by event so it can wind
        past a half turn. The highlight turns about the gimbal."""
        import move_gimbal
        _h, _p0, hl0, o0, turn = self._gdrag
        g = self._gimbal
        if e is not None:
            p = g.point("rz", *self._vtk_xy(e))
            if p is not None:
                a = move_gimbal.ring_angle(o0, p)
                turn[0] += move_gimbal.turn_delta(turn[1], a)
                turn[1] = a
        ang = turn[0]
        # the highlight was drawn R0 about q, then shifted t0; turning that
        # by `ang` about the gimbal g keeps q and gives R0+ang and
        # R(ang) (q + t0 - g) + g - q
        off0, rz0, q = hl0
        c, s = math.cos(math.radians(ang)), math.sin(math.radians(ang))
        ux, uy = q[0] + off0[0] - o0[0], q[1] + off0[1] - o0[1]
        t = (c * ux - s * uy + o0[0] - q[0], s * ux + c * uy + o0[1] - q[1],
             off0[2])
        self.set_highlight_offset(t, rz0 + ang, q)
        self._gimbal_end((0.0, 0.0, 0.0, ang), done)

    def _gimbal_end(self, delta, done):
        g = self._gimbal
        if done:
            self._gdrag = None
            g.set_active(None)
        cb = self.on_gimbal
        if cb is not None:
            cb(delta, done)
        if done:
            self._render()

    def show_gimbal(self, origin, ring=None):
        """Put the Move manipulator at `origin` (world), or hide it (None);
        `ring`: offer the turn about Z (None: as it was). Never during a
        drag of it: the drag owns where it is."""
        if self._gdrag is not None:
            return
        if origin is None:
            if self._gimbal is not None and self._gimbal.visible():
                self._gimbal.show(None)
                self._render_soon()
            return
        if self._win is None:
            return
        if self._gimbal is None:
            try:
                import move_gimbal
                self._gimbal = move_gimbal.MoveGimbal(self._win, self._ren)
            except Exception:  # noqa: BLE001 -- no gimbal; the box still moves
                return
        self._gimbal.show(origin, ring=ring)
        self._render_soon()

    def gimbal_origin(self):
        g = self._gimbal
        return g.origin if g is not None and g.visible() else None

    def _hover(self, e):
        view = self.view_tools
        if view is not None and view.measuring and self._drag is None \
                and self._gdrag is None and self._win is not None:
            view.hover(e.x, e.y)
        g = self._gimbal
        if g is None or not g.visible() or self._gdrag is not None \
                or self._drag is not None:
            return
        x, y = self._vtk_xy(e)
        h = g.hit(x, y)
        if h != g._active:
            g.set_active(h)
            try:
                self._host.configure(cursor="fleur" if h else "crosshair")
            except tk.TclError:
                pass
            self._render_soon()

    def _depth_render(self):
        """Render the scene WITHOUT the gimbal, and without showing it, so
        a depth read finds the fibers: the gimbal's layer clears depth over
        the whole window (move_gimbal's docstring). False when there is no
        gimbal on screen (the caller's own render is enough)."""
        g = self._gimbal
        if g is not None and not g.visible():
            g = None
        view = self.view_tools
        marks = view.overlay_ren() if view is not None else None
        if (g is None and marks is None) or self._win is None:
            return False
        win = self._win
        rens = [r for r in (g.ren if g is not None else None, marks)
                if r is not None]
        for r in rens:
            r.SetDraw(0)
        try:
            win.SwapBuffersOff()
            win.Render()
        finally:
            win.SwapBuffersOn()
            for r in rens:
                r.SetDraw(1)
        self._render_soon()
        return True

    # -- click-to-select ---------------------------------------------------- #
    def _fire_pick(self, x, y, clicks=1):
        view = self.view_tools
        if view is not None and view.measuring:
            # Measure owns the click: it places a point and changes no
            # selection and runs no pick
            view.click(x, y)
            return
        cb = self.on_pick
        if cb is None:
            return
        try:
            hit = self.pick_at(x, y)
        except Exception:  # noqa: BLE001 -- a failed pick is a miss
            hit = None
        if hit is not None:
            hit["clicks"] = int(clicks)
            hit["state"] = self._click_mods
        cb(hit)

    def pick(self, x, y, radius_px=_PICK_RADIUS_PX, zread=None):
        """The drawn fiber under viewport pixel (x, y) -- Tk coords on
        `_host`, an event's e.x, e.y -- as `preview_pick.pick_segment`'s
        dict(path, seg, t, point, dist_px) plus `time` (its print time, or
        None), else None. Geometry only: any page can use it."""
        import preview_pick
        state = self._state
        if self._win is None or state is None or not state.get("paths"):
            return None
        paths = state["paths"]
        cached = state.get("_pick_index")
        if cached is None or cached[0] is not paths:
            cached = (paths, preview_pick.segment_index(paths))
            state["_pick_index"] = cached
        view = self._view()
        index = view.pick_index(state, cached[1]) if view is not None \
            else cached[1]
        ren, win = self._ren, self._win
        # the depth buffer at the camera shown now (a caller that holds one
        # for this very frame passes its own `zread`)
        if zread is None and not self._depth_render():
            win.Render()
        w, h = win.GetSize()
        cam = ren.GetActiveCamera()
        m = cam.GetCompositeProjectionTransformMatrix(
            ren.GetTiledAspectRatio(), 0, 1)
        M = np.array([[m.GetElement(i, j) for j in range(4)]
                      for i in range(4)])
        near, far = cam.GetClippingRange()
        meta = state.get("meta") or {}
        d = float(meta.get("tube_diameter", meta.get("fiber_diameter"))
                  or 0.02)
        # a centreline lies one radius behind its tube's front face; slack of
        # two pixels' worth of world for the "lines" mode's rounding
        wpp = 2.0 * cam.GetParallelScale() / max(h, 1)
        eps = (0.75 * d + 2.0 * wpp) / max(far - near, 1e-12)
        # zoomed in, a tube is many pixels wide and its edge lies further
        # from the centreline than the fixed radius: reach the whole tube
        radius_px = max(float(radius_px), 0.6 * d / max(wpp, 1e-12) + 2.0)
        vtk = _VTK

        if zread is None:
            def zread(x0, y0, x1, y1):
                from vtk.util import numpy_support as ns
                fa = vtk.vtkFloatArray()
                win.GetZbufferData(x0, y0, x1, y1, fa)
                return ns.vtk_to_numpy(fa)

        # the drawn surface's furthest reach from its centreline: a tube's
        # corner (a PREVIEW_TUBE_SIDES-gon), or half a 1 px wire
        surf = 0.5 * d / math.cos(math.pi / PREVIEW_TUBE_SIDES) + 1.5 * wpp
        hit = preview_pick.pick_segment(index, M, w, h, float(x),
                                        float(h - y), zread, radius_px, eps,
                                        surface_tol=surf)
        if hit is not None:
            hit["time"] = preview_pick.time_at(meta.get("ptimes"), hit)
        return hit

    def _pick_outline_now(self):
        """(nodes, clock) for resolving a pick to lines, or None when the
        picture's line clock is missing or was built from another text."""
        clock = ((self._state or {}).get("meta") or {}).get("line_clock")
        src = self.pick_outline
        if clock is None or src is None:
            return None
        got = src()
        nodes, text = got if isinstance(got, tuple) else (got, None)
        if text is not None and clock.get("text") != text:
            return None
        return nodes, clock

    def pick_at(self, x, y):
        """Click-to-select: the fiber under viewport pixel (x, y), as
        dict(scaffold, layer, first, last, line, key, label) -- 1-based, the
        lines inclusive, in the text this panel drew. `line` is the exact
        move that laid the clicked fiber (by its print time on the line
        clock, never nearest-in-XY; a crossing gives the fiber nearest the
        camera); first..last is the layer holding it. Fiber outside every
        layer (the lead-in) gives first == last == line, key and layer None.
        None for empty space, or no `pick_outline` / line clock."""
        import preview_pick
        hit = self.pick(x, y)
        if hit is None or hit.get("time") is None:
            return None
        got = self._pick_outline_now()
        if got is None:
            return None
        nodes, clock = got
        line = preview_pick.line_at_time(clock, hit["time"])
        if line is None:
            return None
        return preview_pick.layer_at_line(nodes, line) or dict(
            scaffold=None, layer=None, first=line, last=line, line=line,
            key=None, label="line %d" % line)

    def set_layer_highlight(self, first=None, last=None):
        """Light the fiber laid by lines `first`..`last` (1-based, inclusive:
        a `pick_at` result's range) in the highlight yellow; None clears.
        Shares `set_highlight`'s layer, so the latest call wins."""
        import preview_pick
        state = self._state or {}
        meta = state.get("meta") or {}
        clock = meta.get("line_clock")
        if first is None or last is None or clock is None \
                or not state.get("paths"):
            self.set_highlight()
            return
        win = preview_pick.line_window(clock, int(first), int(last))
        if win is None:
            self.set_highlight()
            return
        self.set_highlight(preview_pick.layer_polylines(
            state["paths"], meta.get("ptimes"), *win))

    def _on_wheel(self, e):
        if self._win is None:
            return "break"
        factor = 1.15 if e.delta > 0 else 1.0 / 1.15
        cam = self._ren.GetActiveCamera()
        before = self._focal_plane_world(e.x, e.y)
        cam.SetParallelScale(cam.GetParallelScale() / factor)
        self._persp_sync()
        after = self._focal_plane_world(e.x, e.y)
        s = (before[0] - after[0], before[1] - after[1], before[2] - after[2])
        fp, pos = cam.GetFocalPoint(), cam.GetPosition()
        cam.SetFocalPoint(fp[0] + s[0], fp[1] + s[1], fp[2] + s[2])
        cam.SetPosition(pos[0] + s[0], pos[1] + s[1], pos[2] + s[2])
        self._fit_clipping()
        self._render_soon()
        self._save_camera()
        return "break"      # never bubble to the page scroll


# ---------------------------------------------------------------------------
# the App mixin (wiring: traces, debounce, worker, results)
# ---------------------------------------------------------------------------
class _PreviewMixin:
    """Sits before _ModernPagesMixin in ModernApp's MRO (so the
    _show_scaffold_frame override chains). gui_shell calls
    _build_scaffold_preview(page) after the scaffold form is built."""

    # -- construction -------------------------------------------------------#
    def _build_scaffold_preview(self, parent):
        # The worker/mailbox/poll plumbing is created FIRST: since the tab merge
        # the 4-axis half submits to the same worker, and its traces are
        # installed a few lines below, so `_pv_cv` must already exist by then.
        self._pv_results = queue.Queue()
        self._pv_mailbox = {}                  # key -> (gen, spec, options),
        self._pv_cv = threading.Condition()    # latest-only per key
        self._pv_worker = None
        self._pv_pending = 0
        self._pv_poll_id = None
        # ONE panel for both collector modes. It is
        # the mandrel-aware subclass in both: the translucent collector rod is an
        # ADDITIVE actor, so 3-axis simply hides it, and every other behaviour --
        # camera, offscreen blit, tube/line display mode, colour -- is the plain
        # PreviewPanel's. Sharing the panel is what makes the 4-axis preview
        # inherit the viewer colour and the Preview-options toggles.
        from gui_mandrel_preview import MandrelPreviewPanel
        holder = MandrelPreviewPanel(parent, self)
        panel = holder.widget
        # the shell now hands us the scaffold page's ttk.PanedWindow so the user
        # can drag the form|preview divider (issue: the fixed column clipped the
        # form); fall back to the old fixed column if some caller still grids.
        if isinstance(parent, ttk.PanedWindow):
            parent.add(panel, weight=3)
        else:
            panel.grid(row=0, column=2, sticky="nsew")
        self._pv_panel = panel
        # The panel exists -> fill its options row. Here, not in the page
        # builder, because the row is the panel's: the shell runs
        # _build_scaffold_container (which makes sim_physics_deposit) before this,
        # so the physics checkbox has its var by now.
        self._pv_fill_option_sections()
        self._mandrel_preview = holder
        self._mandrel_preview_state = self._mandrel_new_preview_state()
        self._mandrel_preview_after = None
        # See _pv_rod_wanted: the rod is withheld from the moment the collector
        # is switched to the mandrel until that switch's tube actually lands.
        self._mandrel_rod_pending = False
        self._install_mandrel_preview_traces()
        self.pv_show_collector.trace_add("write", self._pv_on_show_collector)
        holder.set_collector_visible(self._pv_rod_wanted())
        self.pv_show_plate.trace_add("write", self._pv_on_show_plate)
        holder.set_plate_visible(self._pv_plate_wanted())
        self._pv_states = [self._pv_new_state()
                           for _ in range(len(self.scaf_vars))]
        self._pv_after = None
        self._pv_suppress = False
        self._pv_dark_traced = False
        self._install_preview_traces()
        self._pv_trace_dark()
        threading.Thread(target=_import_vtk, daemon=True).start()
        return panel

    def _preview_persist_entries(self):
        """The scaffold designer's "Preview options" display toggles, folded into
        the preset persist map (gui_presets._persist_map). They carry NO explicit
        preset-kind prefix, so they ride the SCAFFOLD kind (the catch-all) -- a
        saved scaffold preset restores whether the preview drew 3-D tubes and the
        approximate sag, matching every other scaffold-designer field. Global
        (not per-scaffold) toggles, so a single pv.* key each. Guarded with getattr
        so an isolated/partial build (no preview panel) simply persists nothing."""
        entries = {}
        if hasattr(self, "pv_disabled"):
            entries["pv.disabled"] = self.pv_disabled
        if hasattr(self, "pv_show_tubes"):
            entries["pv.show_tubes"] = self.pv_show_tubes
        if hasattr(self, "pv_show_sag"):
            entries["pv.show_sag"] = self.pv_show_sag
        if hasattr(self, "pv_show_fusion"):
            entries["pv.show_fusion"] = self.pv_show_fusion
        if hasattr(self, "pv_show_lag"):
            entries["pv.show_lag"] = self.pv_show_lag
        if hasattr(self, "pv_studio_light"):
            entries["pv.studio_light"] = self.pv_studio_light
        if hasattr(self, "pv_curve_quality"):
            entries["pv.curve_quality"] = self.pv_curve_quality
        if hasattr(self, "pv_fill_per_tau"):
            entries["pv.fill_per_tau"] = self.pv_fill_per_tau
        if hasattr(self, "pv_tube_sides"):
            entries["pv.tube_sides"] = self.pv_tube_sides
        if hasattr(self, "pv_auto_corner_grid"):
            entries["pv.auto_corner_grid"] = self.pv_auto_corner_grid
        if hasattr(self, "pv_show_collector"):
            entries["pv.show_collector"] = self.pv_show_collector
        if hasattr(self, "pv_show_gcode"):
            entries["pv.show_gcode"] = self.pv_show_gcode
        if hasattr(self, "pv_show_plate"):
            entries["pv.show_plate_on"] = self.pv_show_plate
        return entries

    # -- Preview-options expandable (built by the scaffold-section builder) --#
    def _pv_ensure_option_vars(self):
        """Create the global preview-display vars once. Called from the scaffold
        section builder (gui_pages) BEFORE this mixin's _build_scaffold_preview
        runs, so both the checkboxes and _install_preview_traces find them.

        JET LAG, SAGGING and FUSION are each the preview's OWN display toggle
        (`pv_show_lag` / `pv_show_sag` / `pv_show_fusion`), not a mirror of the
        Physics page's law switch (`sim_physics_deposit` / `sim_sag_law` /
        `sim_physical_fusion`). That keeps them on the SCAFFOLD design preset with
        the other pv.* toggles (the law switches ride the Physics preset), and
        unticking one changes nothing that is generated or exported. A law that is
        off on its Physics card stays off here too (`_pv_lag_shown` & co.)."""
        # ON by default: the preview shows tubes + approximate
        # sag + fusion out of the box, matching the physics-deposited defaults on the
        # Physics page. Untick any to lighten the picture.
        # THE PREVIEW'S OWN OFF SWITCH, for every collector. Off by default.
        # The preview builds on a thread inside this process, and on a large
        # imported collector that build runs for minutes and makes the whole
        # window lag (12 x 12 mm on `test collector.step`: ~110 s
        # headless). Ticked, no build is queued at all, so the app runs at the
        # speed of Generate alone.
        if not hasattr(self, "pv_disabled"):
            self.pv_disabled = tk.BooleanVar(value=False)
        if not hasattr(self, "pv_show_tubes"):
            self.pv_show_tubes = tk.BooleanVar(value=True)
        if not hasattr(self, "pv_show_sag"):
            self.pv_show_sag = tk.BooleanVar(value=True)
        if not hasattr(self, "pv_show_fusion"):
            self.pv_show_fusion = tk.BooleanVar(value=True)
        # Jet lag, the preview's OWN switch like the two above: it hides the
        # touchdown reshape from the picture without touching the export. The
        # Physics page's Jet lag switch still has to be on for it to draw.
        if not hasattr(self, "pv_show_lag"):
            self.pv_show_lag = tk.BooleanVar(value=True)
        # The PREVIEW'S OWN curve-quality knobs, apart from the export's
        # (`s_curve_quality` / `s_fill_per_tau` / `s_auto_corner_grid`): what
        # the live picture spends on a turnaround is a different question from
        # what the STL and the simulation spend, and each is set on its own in
        # Settings > Quality. Defaults are the tier the engine documents as the
        # preview's (mew_deposition.CURVE_QUALITY: "draft") and the engine's
        # own corner grid.
        if not hasattr(self, "pv_curve_quality"):
            self.pv_curve_quality = tk.StringVar(value="draft")
            self._pv_curve_quality_label = tk.StringVar(value="")
            self._sync_pv_curve_quality_label()
        if not hasattr(self, "pv_fill_per_tau"):
            self.pv_fill_per_tau = tk.StringVar(value="6")
        if not hasattr(self, "pv_auto_corner_grid"):
            self.pv_auto_corner_grid = tk.BooleanVar(value=True)
        # How many flat sides the live picture's tubes get. The CAD page's
        # picture does not take the EXPORT's Tube facets; the export's value is
        # what the STL spends and is no business of the picture's.
        if not hasattr(self, "pv_tube_sides"):
            self.pv_tube_sides = tk.StringVar(value="12")
        # Studio lighting, ON by default and in every axis mode. The stock VTK
        # headlight lights a tube from exactly the view direction, so the
        # shading is symmetric across the fibre and it reads flat; the kit's
        # off-axis key puts a highlight along each strand, which is what
        # separates the strands inside a bundle. Same kit the sim player uses.
        if not hasattr(self, "pv_studio_light"):
            self.pv_studio_light = tk.BooleanVar(value=True)
        # The collector rod, 4-axis only. Its checkbox is registered "mandrel"
        # so it is not even shown in 3-axis mode, but the VAR lives here with
        # the other pv.* toggles so it persists the same way.
        if not hasattr(self, "pv_show_collector"):
            self.pv_show_collector = tk.BooleanVar(value=True)
        # The build plate, 3-axis only -- the flat collector. ON by default,
        # like the rod, so the Design tab opens with the plate the print sits
        # on. Persisted as `pv.show_plate_on` so a False saved by an older
        # version, which defaulted it off, does not pin it hidden.
        if not hasattr(self, "pv_show_plate"):
            self.pv_show_plate = tk.BooleanVar(value=True)
        # The commanded-toolpath overlay. OFF by default (unlike every toggle
        # above): a diagnostic layer over the picture, not part of it. Shown in
        # BOTH collector modes, so unlike the rod it is not registered "mandrel".
        if not hasattr(self, "pv_show_gcode"):
            self.pv_show_gcode = tk.BooleanVar(value=False)

    def _pv_fill_option_sections(self):
        """Fill the preview panel's own options row.

        Called from _build_scaffold_preview, which the shell runs AFTER
        _build_scaffold_container -- so sim_physics_deposit already exists and the
        physics checkbox can bind to it. The options are a wrapping row
        (`_OptionFlow`), filled in that order."""
        panel = getattr(self, "_pv_panel", None)
        if panel is None or getattr(panel, "options_host", None) is None:
            return
        self._build_scaffold_preview_options(panel.options_host)
        panel.show_options()
        # The gate has already run for the boot collector (it runs while the
        # form is built, before this panel exists), so these boxes would sit in
        # whatever state `_OptionFlow` gave them -- all three collectors'
        # showing at once. Re-run it now that they are registered.
        self._apply_collector_mode()

    def _build_scaffold_preview_options(self, body, v=None):
        """The preview's display options, built ONCE into the panel's row.

        Every toggle here is global -- one picture, one set of switches -- which
        is why it does not take a scaffold. `body` is the panel's `_OptionFlow`;
        each toggle is one small frame (checkbox + info dot) handed to
        `body.add`, and the returned handle is what the collector gate shows and
        hides. `v` is accepted and ignored so the classic developer UI's call
        site still works.

        NO CAPTION LINES. The drawer this replaced put a grey sentence under
        most boxes ("Off = flat lines; on = round fibers.", "4-axis only."),
        which is what made it a column tall enough to need a disclosure. Every
        one of them said something the box's own info dot already said, or
        something the collector gate now says by simply not showing the box."""

        def toggle(text, var, tip, title=None, command=None):
            # `title` named the ⓘ popup; the tooltip that replaced it has none
            return body.add_toggle(text, var, tip, command=command)

        # DISABLE PREVIEW leads the row, and is the one switch here that is not
        # about what the picture shows: it stops the picture being BUILT, for
        # every collector. It reads first because someone reaching for it is
        # waiting on a rebuild, not styling one.
        toggle("Disable preview", self.pv_disabled,
               "Stops the live preview from rebuilding, for every collector "
               "and every scaffold.\n\n"
               "A large design can take minutes to preview and slows the whole "
               "window while it builds; with this ticked the app runs at the "
               "speed of Generate alone. A build already running finishes in "
               "the background and is thrown away.",
               title="Disable preview")

        toggle("3-D tubes", self.pv_show_tubes,
               "Draw fibers as round 3-D tubes instead of flat center lines.\n\n"
               "A true-scale fiber is sub-pixel when zoomed out, so even with "
               "tubes on, the roundness may not be visible until you zoom in.",
               title="3-D tubes")

        toggle("Studio light", self.pv_studio_light,
               "Light the preview with a key/fill/back kit instead of the stock "
               "single headlight.\n\n"
               "The headlight sits on the camera, so it lights every fiber "
               "head-on and the tube reads flat. The kit's off-axis key puts a "
               "highlight along each strand, so neighboring strands stay "
               "separate. Appearance only: it changes no geometry. The "
               "simulation viewer has the same switch.",
               title="Studio lighting")

        # The three law toggles -- Jet lag, Sagging, Fusion -- are PREVIEW-ONLY.
        # Each draws only while its Physics-page law switch is on too, because
        # that switch is what generation and export follow
        # (`_pv_lag_shown` / `_pv_sag_shown` / `_pv_fusion_shown`).
        toggle("Jet lag", self.pv_show_lag,
               "Draw the fiber where the jet lands: it trails the nozzle and "
               "rounds every corner. Preview only: it changes nothing that is "
               "generated or exported.\n\n"
               "Needs Jet lag on in its Physics-page card, which is what the "
               "export follows.",
               title="Jet lag")

        toggle("Sagging", self.pv_show_sag,
               "Fast approximate sag: fibers rest on what is already laid and "
               "sag between supports. It is close to the exact solve but not the "
               "same. The STL viewer and the simulation show the fully solved "
               "sag.\n\n"
               "Preview only. Needs Sagging on in its Physics-page card, "
               "which is what the export follows.",
               title="Approximate sag")

        # Fusion is the preview's OWN toggle (pv_show_fusion), not a mirror of the
        # Physics page's var -- so it rides the SCAFFOLD design preset with the other
        # pv.* options, and the preview's box is the on/off for what the preview
        # draws (the same rule as the sag toggle above). It only bites while sagging
        # is what draws it: fusion is a rest-height change, so with sag off there is
        # nothing to lower (the drape passes the commanded path straight through).
        toggle("Fusion", self.pv_show_fusion,
               "Where fibers cross, the upper one settles INTO the one below "
               "instead of resting a full diameter on top, so stacked crossover "
               "peaks come down while the pore sag between them is unchanged.\n\n"
               "Needs \u2018Sagging\u2019 on. Fusion lowers rest heights, and "
               "without sag there are no rest heights to lower.\n\n"
               "A preview-display toggle, saved with this scaffold's preset. The "
               "STL and simulation get fusion from the Fusion card on the "
               "Physics page, and this draws only while that card's switch is "
               "on too. The amount comes from that card\u2019s junction and "
               "straight-span sinks either way.\n\n"
               "\u03c6 is a fraction of ONE fiber diameter, so on a mm-scale scaffold "
               "the effect is sub-pixel until you zoom in.",
               title="Fusion")

        toggle("G-code path", self.pv_show_gcode,
               "Draw the COMMANDED toolpath as a thin gold line over the fibers: "
               "the moves in the g-code file, rapids (G0) included. It is the "
               "same color the simulation viewer draws the head\u2019s path in.\n\n"
               "The fibers are where the material ends up. This is where the "
               "nozzle was told to go. The gap between them is what the physics "
               "adds: jet lag behind the head, rounded corners, and the sag of "
               "the approximate sag. With physics and sagging both off the two "
               "coincide.\n\n"
               "Off by default. A preview-display toggle, saved with this "
               "scaffold's preset. It is never part of the exported STL.",
               title="G-code path")

        # THE COLLECTOR, one box per collector and exactly one of them showing.
        # Each is registered to its own mode, so the gate does the explaining
        # that "4-axis only." / "5-axis only." captions would otherwise do.
        self._collector_register(
            toggle("Collector", self.pv_show_plate,
                   "Draw the plate the scaffold is printed on. It is the "
                   "3-axis collector, at the size and thickness set on the "
                   "Printer & collector page\u2019s Collector card.\n\n"
                   "OFF by default, unlike the other two collectors: the plate "
                   "is much larger than a scaffold, so it fills the view behind "
                   "one rather than framing it the way a mandrel or a dome does.\n\n"
                   "The scaffold sits where its Start X/Y puts it on the "
                   "plate.\n\n"
                   "A preview-display toggle: the plate is never part of the "
                   "exported STL.",
                   title="Collector",
                   command=self._pv_on_show_plate), "plate")

        self._collector_register(
            toggle("Collector", self.pv_show_collector,
                   "Draw the tube the fibers are wound onto. It is solid, so "
                   "fibers on the far side are hidden behind it. Untick to see "
                   "the whole winding at once.\n\n"
                   "A preview-display toggle: the collector is never part of the "
                   "exported STL either way.",
                   title="Collector"), "mandrel")

        # The per-scaffold VIEWER COLOUR is not here. It is in the Geometry
        # module's own grid (`gui_pages._build_scaffold_color_row`), which is
        # per scaffold and so does not have to be re-pointed at a different
        # scaffold on every pill switch the way a control on this shared panel
        # would.

    def _pv_refresh_display(self):
        """A Preview-options DISPLAY toggle flipped (3-D tubes, the g-code
        overlay): re-style the shown scaffold and re-render. Both collector
        modes share the one panel, so this serves the tube designer too. No
        geometry rebuild -- every build carries what either switch draws."""
        panel = getattr(self, "_pv_panel", None)
        if panel is None:
            return
        panel._update_display_mode(force=True)
        panel._update_gcode_overlay()
        panel._render_soon()

    def _pv_relight(self):
        """The Studio-light box flipped: swap the renderer's lights and redraw.
        Cheaper than its neighbours above -- the lights are a renderer property,
        so nothing rebuilds and nothing re-styles. One panel serves every axis
        mode (both collector modes share it), so this is the whole fan-out."""
        panel = getattr(self, "_pv_panel", None)
        if panel is None:
            return
        panel.on_studio_light_changed()

    def _pv_trace_dark(self):
        """Retheme the panel when dark mode flips. The dark_mode var may not
        exist yet at panel build (the theme mixin creates it later), so this
        retries from _pv_on_panel_mapped until it lands."""
        if self._pv_dark_traced:
            return
        var = getattr(self, "dark_mode", None)
        if var is None:
            return
        try:
            var.trace_add("write", lambda *_a: self._pv_panel.retheme())
            self._pv_dark_traced = True
        except tk.TclError:
            pass

    @staticmethod
    def _pv_new_state():
        return dict(gen_sub=0, gen_done=0, dirty=True, paths=None, meta=None,
                    lines_pd=None, tubes_pd=None, gcode_pd=None,
                    display_mode=None,
                    camera=None, framed_diag=None, diag=0.0,
                    color=(0.8, 0.25, 0.2), status="")

    def _install_preview_traces(self):
        for i, v in enumerate(self.scaf_vars):
            # trace EVERY geometry var (deny-list): a scaf_vars entry that is a
            # tk.Variable and not in _NONGEOM_KEYS rebuilds the preview. Widget
            # refs (rotation combo, the tables/canvases stashed in the same dict)
            # are skipped by the isinstance guard. This auto-covers array_* and
            # any field added later, so nothing is silently forgotten.
            for key, var in v.items():
                if key in _NONGEOM_KEYS or not isinstance(var, tk.Variable):
                    continue
                var.trace_add(
                    "write", lambda *_a, i=i: self._pv_on_edit(i))
            v["color"].trace_add(
                "write", lambda *_a, i=i: self._pv_on_color(i))
            combo = v.get("rotation")
            if combo is not None:                    # a widget, not a var
                combo.bind("<<ComboboxSelected>>",
                           lambda _e, i=i: self._pv_on_edit(i), add="+")
                combo.bind("<KeyRelease>",
                           lambda _e, i=i: self._pv_on_edit(i), add="+")
        # global physics knobs (the Printhead-sim panel): a change re-lays
        # EVERY scaffold's touchdown path, so dirty them all. The sim panel's
        # own P/T traces also rewrite each scaffold's diameter var when the
        # diameter is physics-driven; the debounce coalesces the burst.
        # the travel checkboxes and `pv_fill_per_tau` (Settings > Quality) ride here rather
        # than with the sag knobs below: depositing the travels adds real fiber, and
        # the corner-resolution grid re-samples the touchdown path, whether or not
        # sag is being shown -- so both must dirty every scaffold, not just the
        # sagged draw. A knob that reaches the physics dict but not this list is
        # picked up only on the NEXT unrelated edit, which reads as the preview
        # ignoring it.
        # The coil-wave knobs and the Calibration fields are read by
        # _pv_snapshot_options too, so they ride here -- the list the 4-axis
        # preview already traces (gui_mandrel._install_mandrel_preview_traces).
        # So do the plate size and g-code-origin anchor: the exported coordinates
        # and the plate drawing follow those settings.
        for name in ("sim_physics_deposit", "pv_show_lag", "sim_sag_law",
                     "sim_standoff",
                     "sim_voltage", "sim_pressure", "sim_temperature",
                     "sim_kinematics", "sim_max_accel", "sim_scv",
                     "sim_coil_wave", "sim_coil_wave_amp", "sim_coil_wave_len",
                     "s_travel_in_stl", "s_travel_gcode_line", "s_g2g3",
                     "pv_fill_per_tau", "pv_curve_quality",
                     "pv_auto_corner_grid",
                     "plate_shape", "plate_x", "plate_y", "plate_diameter",
                     "plate_thickness", "origin_anchor", "origin_custom",
                     "origin_face"):
            var = getattr(self, name, None)
            if var is not None:
                var.trace_add("write", lambda *_a: self._pv_all_dirty())
        for var in (getattr(self, "cal_vars", None) or {}).values():
            if isinstance(var, tk.Variable):
                var.trace_add("write", lambda *_a: self._pv_all_dirty())
        # The stabilization base is drawn with scaffold 1 only (see
        # _pv_snapshot_stabilization), so its fields dirty that one.
        for name in ("stab_on", "stab_width", "stab_height", "stab_stepover",
                     "stab_layers", "stab_arc_radius_mult", "stab_start_x",
                     "stab_start_y"):
            var = getattr(self, name, None)
            if var is not None:
                var.trace_add("write", lambda *_a: self._pv_on_edit(0))
        # Preview-options display toggles: tubes on/off is a pure restyle (swap
        # the mapper input, no rebuild); sag on/off changes geometry, so dirty
        # every scaffold like a global edit -- as do the Export panel's sag knobs
        # (the multiplier the drape droops by, and the periodic reset it bands by).
        # once, however many scaffold bodies this builds
        dvar = getattr(self, "pv_disabled", None)
        if dvar is not None and not getattr(self, "_pv_disabled_traced", False):
            dvar.trace_add("write", lambda *_a: self._pv_disabled_changed())
            self._pv_disabled_traced = True
        tvar = getattr(self, "pv_show_tubes", None)
        if tvar is not None:
            tvar.trace_add("write", lambda *_a: self._pv_refresh_display())
        svar = getattr(self, "pv_show_sag", None)
        if svar is not None:
            svar.trace_add("write", lambda *_a: self._pv_all_dirty())
        gvar = getattr(self, "pv_show_gcode", None)
        if gvar is not None:            # the overlay is a restyle, like tubes
            gvar.trace_add("write", lambda *_a: self._pv_refresh_display())
        lvar = getattr(self, "pv_studio_light", None)
        if lvar is not None:            # lights only: no rebuild, no restyle
            lvar.trace_add("write", lambda *_a: self._pv_relight())
        for name in ("s_sag_mult", "s_sag_reset", "s_sag_reset_layers",
                     "pv_show_fusion", "sim_physical_fusion"):
            mvar = getattr(self, name, None)
            if mvar is not None:            # rebuild only while sag is shown
                mvar.trace_add("write", lambda *_a: self._pv_sag_edit())

    def _pv_preview_disabled(self):
        """Is "Disable preview" ticked? False in a shell without the toggle."""
        var = getattr(self, "pv_disabled", None)
        return bool(var is not None and var.get())

    def _pv_drop_queued(self, key):
        """Take a not-yet-started build for `key` out of the worker mailbox.

        A build already RUNNING cannot be stopped (it is a thread); its result
        is discarded on arrival by the generation id the caller bumps."""
        cv = getattr(self, "_pv_cv", None)
        if cv is None:
            return
        with cv:
            if self._pv_mailbox.pop(key, None) is not None:
                self._pv_pending = max(0, self._pv_pending - 1)

    def _pv_disabled_changed(self):
        """Ticking shows the disabled status on whichever collector is live;
        unticking rebuilds it. Both go through each collector's own submit
        path, which is where the gate sits."""
        self._pv_all_dirty()
        fn = getattr(self, "_schedule_mandrel_preview", None)
        if callable(fn):
            fn()
        self._pv_summary_changed()

    def _pv_sag_edit(self):
        """A sag knob changed -- the Export panel's multiplier or periodic reset, or
        the preview's fusion toggle. All three only move geometry the SAGGED drape
        draws, so skip the rebuild entirely while the sag toggle is off."""
        if self._pv_sag_shown():
            self._pv_all_dirty()

    # -- what the preview draws: its own toggle AND the Physics-page law ------- #
    # The preview's Jet lag / Sagging / Fusion boxes only HIDE a law from the
    # picture; a law switched off on its Physics-page card is off for generation
    # and export, so the preview cannot draw it either. Missing vars (a partial
    # build) read as on, the shipped default of both halves.
    def _pv_law_and(self, preview_attr, law_attr):
        out = True
        for attr in (preview_attr, law_attr):
            var = getattr(self, attr, None)
            if var is not None and not var.get():
                out = False
        return out

    def _pv_lag_shown(self):
        return self._pv_law_and("pv_show_lag", "sim_physics_deposit")

    def _pv_sag_shown(self):
        return self._pv_law_and("pv_show_sag", "sim_sag_law")

    def _pv_fusion_shown(self):
        return self._pv_law_and("pv_show_fusion", "sim_physical_fusion")

    def _pv_all_dirty(self):
        """A global (whole-print / physics) input changed: every scaffold's
        preview is stale. The active one rebuilds now (debounced); the hidden
        ones rebuild when the pill rail shows them.
"""
        if self._pv_suppress:
            return
        for state in self._pv_states:
            state["dirty"] = True
        self._pv_on_edit(self._active_scaffold_index())

    # -- edit plumbing ------------------------------------------------------#
    def _pv_on_edit(self, i):
        if self._pv_suppress:
            return
        self._pv_states[i]["dirty"] = True
        if i == self._active_scaffold_index():
            if self._pv_after is not None:
                try:
                    self.root.after_cancel(self._pv_after)
                except (ValueError, tk.TclError):
                    pass
            self._pv_after = self.root.after(
                PREVIEW_DEBOUNCE_MS, self._pv_kick)

    def _pv_on_color(self, i):
        state = self._pv_states[i]
        state["color"] = self._pv_parse_color(self.scaf_vars[i]["color"])
        if self._pv_curved_active():
            return          # _mandrel_on_color restyles the tube instead
        if i == self._active_scaffold_index():
            panel = self._pv_panel
            if panel._actor is not None and state.get("lines_pd") is not None:
                panel._actor.GetProperty().SetColor(*state["color"])
                panel._render_soon()

    @staticmethod
    def _pv_parse_color(var):
        try:
            h = var.get().strip().lstrip("#")
            if len(h) == 6:
                return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
                        int(h[4:6], 16) / 255.0)
        except (ValueError, tk.TclError):
            pass
        return (0.8, 0.25, 0.2)

    def _pv_subtitle(self, i):
        meta = self._pv_states[i].get("meta")
        phys = meta is not None and meta.get("physics")
        sag = meta is not None and meta.get("sag")
        # the words the display toggles use: "G-code path", "Sagging"
        path_word = "physics path" if phys else "g-code path"
        if sag:
            tail = "approximate sag. Simulation shows the exact sag."
        elif meta is not None and meta.get("sag_refused"):
            tail = f"no sag: {meta['sag_refused']}."
        elif phys:
            tail = "no sag. Simulation shows the sagging."
        else:
            tail = "no sag. Simulation shows the physics and the sagging."
        return f"Scaffold {i + 1} · {path_word} · {tail}"

    def _pv_curved_active(self):
        """True while the 4- or 5-axis half owns the shared panel.

        Every guard that keeps the flat half off the panel asks this. They used
        to ask `_mandrel_is_active` alone, which predates the curved collector,
        so a flat build still in flight when the collector went to 5-axis
        painted its plate scaffold over the 5-axis tab."""
        return bool(self._mandrel_is_active())

    # -- rebuild ------------------------------------------------------------#
    def _pv_kick(self):
        self._pv_after = None
        if getattr(self, "_pv_panel", None) is None:
            return
        # The 4- or 5-axis half owns the shared panel while its collector is
        # selected; a planar rebuild would draw a plate scaffold over it. The
        # dirty flags stay set, so switching back rebuilds immediately.
        if self._pv_curved_active():
            return
        i = self._active_scaffold_index()
        state = self._pv_states[i]
        if self._pv_preview_disabled():
            # left dirty, so unticking rebuilds; gen bumped, so a build still
            # in flight cannot paint over the status
            self._pv_drop_queued(i)
            state["gen_sub"] += 1
            state["status"] = PREVIEW_DISABLED_STATUS
            self._pv_panel.set_status(state["status"])
            # the Design summary does not need the preview, so it keeps up
            self._ds_quick(i)
            self._ds_submit(i)
            return
        if not state["dirty"]:
            return
        try:
            spec = self._pv_snapshot_spec(i)
            options = self._pv_snapshot_options(i)
        except Exception:  # noqa: BLE001 -- mid-typing values; keep last good
            state["status"] = "waiting for valid parameters…"
            self._pv_panel.set_status(state["status"])
            return
        self._ds_quick(i, spec, options)
        cts = state.get("cts_now")
        below = cts is not None and cts[0] < cts[1]
        if below:
            # REFUSED, as the tube's preview refuses: below CTS the physics relay
            # coils the centreline and the build crawls. `gen_sub` is bumped so a
            # build already in flight cannot paint over this, and the state stays
            # dirty so raising the speed rebuilds. The last good picture stays.
            self._pv_drop_queued(i)
            state["gen_sub"] += 1
            state["status"] = BELOW_CTS_STATUS
            self._pv_panel.set_status(state["status"], error=True)
            self._ds_submit(i)
            return
        self._ds_submit(i)
        state["dirty"] = False
        state["gen_sub"] += 1
        state["color"] = self._pv_parse_color(self.scaf_vars[i]["color"])
        state["status"] = "updating…"
        self._pv_panel.set_status(state["status"])
        last = (state.get("meta") or {})
        options["toolpath_first"] = (
            last.get("provisional")
            or float(last.get("elapsed", math.inf)) >= PREVIEW_TOOLPATH_FIRST_S)
        with self._pv_cv:
            self._pv_mailbox[i] = (state["gen_sub"], spec, options)
            self._pv_cv.notify()
        self._pv_pending += 1
        self._pv_ensure_worker()
        self._pv_arm_poll()

    # -- the Design summary's quick job ------------------------------------ #
    # The summary must not wait for the preview: a large scaffold's relay and
    # drape take seconds to minutes, and a print speed below CTS is exactly the
    # edit a user needs to hear about at once. So the CTS check runs HERE, on
    # the snapshot the preview was just given (microseconds), and the print
    # time and fiber length run on a thread of their own (`design_stats`: the
    # generator plus one text walk, ~30 ms at 10x10 mm and 3-4 s only at
    # 50x50 mm / 0.1 mm / 50 layers), never queued behind a preview build.
    def _ds_quick(self, i, spec=None, options=None):
        state = self._pv_states[i]
        if spec is None:
            try:
                spec = self._pv_snapshot_spec(i)
                options = self._pv_snapshot_options(i)
            except Exception:  # noqa: BLE001 -- mid-typing; keep the last numbers
                return
        state["cts_now"] = cts_check(options.get("physics"), spec["speed"])
        state["ds_gen"] = state.get("ds_gen", 0) + 1
        state["ds_job"] = (state["ds_gen"], spec, options)
        self._pv_summary_changed()

    def _ds_submit(self, i):
        """Start the quick job `_ds_quick` staged. Called LAST in the kick, after
        a forced paint: every Tk call gives up the GIL and must win it back,
        and against a thread busy in the generator that cost 760 ms of Tk
        calls on a 30x30 mm / 0.2 mm / 30-layer scaffold, all of it before
        the CTS warning was on screen.

        The paint is skipped while the window is not yet shown: main() builds
        it withdrawn, so at boot there is nothing to paint, and the call would
        instead lay out the whole half-built page tree, which can run three
        times per boot and cost 10.7 s of a 32 s build under load."""
        state = self._pv_states[i]
        job = state.pop("ds_job", None)
        if job is None:
            return
        try:
            if self.root.winfo_viewable():
                self.root.update_idletasks()
        except tk.TclError:
            pass
        if getattr(self, "_ds_cv", None) is None:
            self._ds_cv = threading.Condition()
            self._ds_mailbox = {}            # latest-only, like the preview's
            self._ds_results = queue.Queue()
            self._ds_outstanding = 0
            self._ds_poll_id = None
            threading.Thread(target=self._ds_worker_loop, daemon=True).start()
        with self._ds_cv:
            if self._ds_mailbox.pop(i, None) is not None:
                self._ds_outstanding -= 1
            self._ds_mailbox[i] = job
            self._ds_outstanding += 1
            self._ds_cv.notify()
        self._ds_arm_poll()

    def _ds_worker_loop(self):
        while True:
            with self._ds_cv:
                while not self._ds_mailbox:
                    self._ds_cv.wait()
                i, (gen, spec, options) = self._ds_mailbox.popitem()
            try:
                stats = design_stats(spec, options)
            except Exception:  # noqa: BLE001 -- no numbers, not a crash
                stats = None
            self._ds_results.put((i, gen, stats))

    def _ds_arm_poll(self):
        if self._ds_poll_id is None:
            self._ds_poll_id = self.root.after(PREVIEW_POLL_MS, self._ds_poll)

    def _ds_poll(self):
        self._ds_poll_id = None
        landed = False
        try:
            while True:
                i, gen, stats = self._ds_results.get_nowait()
                self._ds_outstanding -= 1
                state = self._pv_states[i]
                if gen == state.get("ds_gen"):     # not overtaken by an edit
                    state["ds_stats"] = stats
                    landed = True
        except queue.Empty:
            pass
        if landed:
            self._pv_summary_changed()
        if self._ds_outstanding > 0:
            self._ds_arm_poll()

    def _pv_snapshot_spec(self, i):
        """The run pipeline's own spec builder, with traces suppressed (its
        _sync_* helpers rewrite the canonical pattern vars). Start X/Y/Z and
        the g-code-origin datum are kept, so the preview draws the scaffold
        where it prints, beside the stabilization base and on the plate."""
        self._pv_suppress = True
        try:
            spec = self._scaffold_spec(i)
        finally:
            self._pv_suppress = False
        return spec

    def _pv_snapshot_stabilization(self, i):
        """The stabilization base as scaffold_gcode's own dict, or None.

        Drawn with scaffold 1 only: the generator prints it once, first, as
        scaffold 1's lead-in. The same values `_build_args_scaffold` sends --
        scaffold 1's fiber diameter (the base has no field of its own), the
        origin datum added to its Start X/Y -- so the preview and the print
        agree on where it sits. A garbage field raises -> the waiting posture."""
        if i != 0 or not bool(getattr(self, "stab_on", None)
                              and self.stab_on.get()):
            return None
        cdx, cdy = self._scaffold_origin_xy()
        stepover = float(self.stab_stepover.get())
        mult = self.stab_arc_radius_mult.get().strip()
        return dict(
            width=float(self.stab_width.get()),
            height=float(self.stab_height.get()),
            stepover=stepover,
            fiber_diameter=float(self.scaf_vars[0]["diameter"].get()),
            layers=int(float(self.stab_layers.get())),
            arc_radius=float(mult) * stepover if mult else None,
            start_x=float(self.stab_start_x.get()) + cdx,
            start_y=float(self.stab_start_y.get()) + cdy)

    def _pv_snapshot_options(self, i):
        """The worker options for scaffold `i`: physics on iff the Physics page's
        Jet lag switch AND the preview's own Jet lag box are ticked AND a model
        builds (`_pv_lag_shown`). Mirrors
        mew_export: the model comes from the Calibration tab's rig profile
        (the _calibration_models hook) at the sim standoff/knobs, zeta from
        the calibration, kinematics per the sim checkbox. A blank/garbage sim
        field raises -- same "waiting for valid parameters" posture as the
        spec (physics ON but unusable must not silently fall back to a plain
        preview). Tk-thread only; the returned dict is handed to the worker.
        Carries the Preview-options "Sagging" state (AND the Physics page's
        Fiber sagging switch, `_pv_sag_shown`) through to the worker's sag
        seam (preview_sag) as None | {"multiplier": float}, independent of the
        jet-lag switches. The multiplier is the Export panel's own sag-multiplier
        field (s_sag_mult) so the preview droops like the STL will; a garbage
        value raises -> the waiting posture (same rule as the sim fields). The
        Export "No sag" checkbox is deliberately ignored -- the preview toggle
        itself is the on/off here.

        Also carries scaffold `i`'s rectangular array (nx/ny/dx/dy/flip) as
        None | dict so the worker tiles the copies (build_preview_paths'
        tile_array) -- the preview then shows the whole array, not one unit. A
        garbage array field raises -> the same waiting posture."""
        sag = None
        if self._pv_sag_shown():
            mult = 1.0
            mvar = getattr(self, "s_sag_mult", None)
            if mvar is not None:
                mult = float(mvar.get())
                if mult <= 0:
                    raise ValueError("sag multiplier must be > 0")
            # the Export panel's periodic sag reset, so the preview bands where the
            # STL will; unticked (or absent, in a partial build) -> 0 = never reset
            reset = 0
            if bool(getattr(self, "s_sag_reset", None) and self.s_sag_reset.get()):
                reset = int(self.s_sag_reset_layers.get())
                if reset < 1:
                    raise ValueError("reset sagging every N layers must be >= 1")
            fuse_law, fuse_band = self._pv_fusion_law()
            sag = {"multiplier": mult, "reset_layers": reset,
                   "fuse_law": fuse_law, "fuse_band": fuse_band}
        array = self._pv_snapshot_array(i)
        stab = self._pv_snapshot_stabilization(i)
        plate = self._pv_snapshot_plate()
        # The jet lays material while travelling, always -- so the preview always
        # shows it, exactly as the run always passes --render-travel (see
        # gui_args). Independent of the jet-lag switches: they only decide
        # whether that travel is lag-shaped. The Export panel's own travel
        # checkbox is now about the exported STL, which the preview is not.
        rtravel = True
        # the Export panel's G2/G3-arcs switch: the preview then emits true arcs
        # and re-expands them at DOUBLE the arc-segments density, mirroring the
        # STL pipeline so the preview and STL loops read at the same smoothness.
        g2g3 = bool(getattr(self, "s_g2g3", None) and self.s_g2g3.get())
        if not (hasattr(self, "sim_physics_deposit") and self._pv_lag_shown()):
            return {"physics": None, "sag": sag, "array": array,
                    "render_travel": rtravel, "g2g3": g2g3,
                    "stabilization": stab, "plate": plate}
        try:
            from mew_physics import MEWModel, Nozzle
        except Exception:  # noqa: BLE001 -- physics modules absent
            return {"physics": None, "sag": sag, "array": array,
                    "render_travel": rtravel, "g2g3": g2g3,
                    "stabilization": stab, "plate": plate}
        gap = float(self.sim_standoff.get())
        pressure = float(self.sim_pressure.get()) * 6894.76   # psi -> Pa
        voltage = float(self.sim_voltage.get())
        temperature = float(self.sim_temperature.get())
        models = self._calibration_models()
        if models is not None:
            material, nozzle, calibration = models
            model = MEWModel(material=material, nozzle=nozzle,
                             working_distance=gap, calibration=calibration)
        else:
            model = MEWModel(nozzle=Nozzle(), working_distance=gap)
        # The mandrel per-layer registration hook. It is NOT a calibration field
        # (mew_calibration.py:339 excludes it from the profile on purpose), so it
        # cannot arrive through `_calibration_models` the way every cal.* value
        # does -- it has to be written onto the model here, which is the preview's
        # equivalent of `mew_export` passing `--mandrel-per-layer` to
        # `build_model`. Inert in 3-axis: with no ContactPlacement there is
        # nothing to re-evaluate, and `_reshape` never reads the flag.
        if bool(getattr(self, "sim_mandrel_per_layer", None)
                and self.sim_mandrel_per_layer.get()):
            import dataclasses as _dc
            model.calibration = _dc.replace(
                model.calibration, mandrel_per_layer_registration=True)
        kin_on = bool(getattr(self, "sim_kinematics", None)
                      and self.sim_kinematics.get())
        max_accel = float(self.sim_max_accel.get()) if kin_on else 0.0
        scv = float(self.sim_scv.get()) if kin_on else 6.0
        # the mandrel's rotary limit; only the 4-axis preview reads it
        try:
            rot_accel = (float(self.sim_rotary_accel.get() or 0.0)
                         if kin_on else 0.0)
        except (ValueError, AttributeError, tk.TclError):
            rot_accel = 0.0
        # below-CTS coiling-WAVE indicator: read the Physics-page toggle + amp/len so
        # the preview draws the same wave the locked sim / export bake (mew_coil_wave).
        # Unticked -> amp 0 (the straight passthrough), mirroring _sim_play_args.
        cw_on = bool(getattr(self, "sim_coil_wave", None)
                     and self.sim_coil_wave.get())
        try:
            cw_amp = float(self.sim_coil_wave_amp.get()) if cw_on else 0.0
        except (ValueError, AttributeError):
            cw_amp = 0.0
        try:
            cw_len = float(self.sim_coil_wave_len.get())
        except (ValueError, AttributeError):
            cw_len = 0.3
        # Corner resolution (Export panel). The preview reshapes through the SAME
        # make_transform the export does, so reading the field here is what keeps
        # the picture and the exported STL on one grid -- and the preview is where
        # a faceted turnaround is actually noticed. Absent field / garbage -> the
        # engine default, never a crash mid-typing.
        try:
            fill_per_tau = float(self.pv_fill_per_tau.get())
            if not fill_per_tau > 0:
                fill_per_tau = 6.0
        except (ValueError, AttributeError):
            fill_per_tau = 6.0
        cq = getattr(self, "pv_curve_quality", None)
        curve_quality = (cq.get().strip().lower() if cq is not None
                         else "draft") or "draft"
        # "Auto corner resolution": the plate relay's adaptive corner grid, built
        # by the same `auto_corner_grid` from the same diameter the export's
        # `--auto-corner-grid` carries, but at the DRAFT tier whatever Curve
        # quality says -- `mew_deposition.CURVE_QUALITY`'s "draft the live
        # preview's". On a 1 mm zig-zag linear pattern Draft took 2.4 s against
        # 4.7 s at Standard (1.6 s off). What it gives up: 4.2 kinks per mm over
        # 20 deg against 0.035, slight corners at the crests that show only in
        # a ~0.6 mm close-up. The export keeps the chosen tier.
        corner_chord = None
        d_auto = getattr(self, "_flat_auto_corner_diameter",
                         lambda box=None: None)(
            getattr(self, "pv_auto_corner_grid", None))
        if d_auto is not None:
            import mew_deposition
            try:
                corner_chord = mew_deposition.auto_corner_grid(
                    d_auto, curve_quality)
            except ValueError:
                corner_chord = None
        return {"sag": sag, "array": array, "render_travel": rtravel,
                "g2g3": g2g3, "stabilization": stab, "plate": plate,
                "physics": dict(
                    model=model, pressure=pressure, voltage=voltage,
                    temperature=temperature,
                    zeta=model.calibration.contact_damping_zeta,
                    coil=False,                       # physical sewing-machine coil (dormant)
                    coil_wave_amp=cw_amp, coil_wave_len=cw_len,
                    max_accel=max_accel, square_corner_velocity=scv,
                    rotary_accel=rot_accel,
                    fill_per_tau=fill_per_tau,
                    # The Curve-quality tier, as its ENGINE TOKEN. Read here so
                    # the preview and the export answer to the same field; the
                    # 4-axis preview reads it directly; the flat relay reads it
                    # only through `corner_chord` below.
                    curve_quality=curve_quality,
                    # None unless "Auto corner resolution" is ticked, which keeps
                    # the flat relay on the fixed grid, byte-identical.
                    corner_chord=corner_chord)}

    def _pv_fusion_law(self):
        """`(sink law, wall band)` for the preview drape, or `(None, None)` when
        fusion is off / the physics modules are absent. The law is
        `(diameter, wall=False) -> mm`; the band is the (sin_lo, sin_hi) the drape
        blends the wall sink into the junction sink over, None when this calibration
        uses one magnitude for every contact.

        Gated by the preview's OWN `pv_show_fusion` (a scaffold-preset display
        toggle) AND the Physics page's Fiber fusion switch (`_pv_fusion_shown`).
        Deliberately NOT part of the physics block below: fusion is independent of
        Jet lag (the export toggles `--physical-fusion` and `--physics-deposit`
        separately), so fusion alone must band the preview without forcing the
        touchdown relay on.
        It needs only the calibration -- `junction_sink` reads `cal.fusion_*` -- plus
        the print temperature for the optional thermal-viscosity term, so the AMOUNT
        still tracks the rig calibration exactly as the export's does. Called on the
        Tk thread; the returned closure is pure and safe to hand to the worker."""
        if not (hasattr(self, "pv_show_fusion") and self._pv_fusion_shown()):
            return None, None
        try:
            from mew_physics import MEWModel, Nozzle
        except Exception:  # noqa: BLE001 -- physics modules absent
            return None, None
        try:
            gap = float(self.sim_standoff.get())
        except Exception:  # noqa: BLE001 -- irrelevant to fusion; keep the default
            gap = 4.0
        models = self._calibration_models()
        if models is not None:
            material, nozzle, calibration = models
            model = MEWModel(material=material, nozzle=nozzle,
                             working_distance=gap, calibration=calibration)
        else:
            model = MEWModel(nozzle=Nozzle(), working_distance=gap)
        temp = None
        tvar = getattr(self, "sim_temperature", None)
        if tvar is not None:
            try:
                temp = float(tvar.get())
            except Exception:  # noqa: BLE001 -- thermal term only; ratio 1 without it
                temp = None
        return model.make_fusion_law(temperature=temp), model.fusion_wall_band()

    def _pv_snapshot_array(self, i):
        """Scaffold `i`'s rectangular array as a worker dict, or None when its
        box is off. Reads the same per-scaffold array_* vars _array_specs uses
        (an array is of ONE scaffold's own copies). Garbage counts/spacings
        raise -> the spec builder's waiting posture. Counts clamp to >= 1;
        blank spacing reads as 0 (a degenerate stack, but not an error)."""
        v = self.scaf_vars[i]
        av = v.get("array_on")
        if av is None or not av.get():
            return None
        nx = max(1, int(float(v["array_nx"].get())))
        ny = max(1, int(float(v["array_ny"].get())))
        nz = max(1, int(float(v["array_nz"].get()))) if "array_nz" in v else 1
        dx = float(v["array_dx"].get() or 0.0)
        dy = float(v["array_dy"].get() or 0.0)
        dz = float(v["array_dz"].get() or 0.0) if "array_dz" in v else 0.0
        return dict(nx=nx, ny=ny, nz=nz, dx=dx, dy=dy, dz=dz,
                    flip_x=bool(v["array_flip_x"].get()),
                    flip_y=bool(v["array_flip_y"].get()))

    def _pv_ensure_worker(self):
        if self._pv_worker is None or not self._pv_worker.is_alive():
            self._pv_worker = threading.Thread(
                target=self._pv_worker_loop, daemon=True)
            self._pv_worker.start()

    def _pv_worker_loop(self):
        """The ONE preview build thread, for both collectors.

        `PV_MANDREL` is the 4-axis slot: its `spec` is the plain-values kwargs
        dict `gui_mandrel._mandrel_snapshot` took on the Tk thread (its own
        g-code path included), so nothing here reads a tk.Variable. Every key
        else is a scaffold index and takes the planar pipeline unchanged."""
        while True:
            with self._pv_cv:
                while not self._pv_mailbox:
                    self._pv_cv.wait()
                i, (gen, spec, options) = self._pv_mailbox.popitem()
            prev_switch = sys.getswitchinterval()
            sys.setswitchinterval(PREVIEW_SWITCH_INTERVAL)
            try:
                # a newer edit for this slot is waiting: this build's picture
                # would be discarded on arrival, so it may stop early
                stop = (lambda i=i: i in self._pv_mailbox)
                if i == PV_MANDREL:
                    from gui_mandrel_preview import build_mandrel_preview
                    paths, meta = build_mandrel_preview(
                        **spec,
                        on_toolpath=(self._pv_toolpath_sink(i, gen)
                                     if (options or {}).get("toolpath_first")
                                     else None),
                        should_stop=stop)
                else:
                    gpath = os.path.join(SESSION_DIR,
                                         f"preview_s{i + 1}.gcode")
                    paths, meta = build_preview_paths(
                        spec, gpath, dict(options, should_stop=stop),
                        on_toolpath=(self._pv_toolpath_sink(i, gen)
                                     if options.get("toolpath_first")
                                     else None))
                self._pv_results.put((i, gen, paths, meta, None))
            except Exception as exc:  # noqa: BLE001
                self._pv_results.put((i, gen, None, None, exc))
            finally:
                sys.setswitchinterval(prev_switch)

    def _pv_toolpath_sink(self, i, gen):
        """`on_toolpath` for one build (worker thread): queue the commanded
        toolpath as a provisional result, unless a newer edit is already
        waiting, whose own build will replace this one before it shows."""
        def sink(paths, meta):
            with self._pv_cv:
                if i in self._pv_mailbox:
                    return
            self._pv_results.put((i, gen, paths, meta, None))
        return sink

    # -- results (Tk thread) --------------------------------------------------#
    def _pv_arm_poll(self):
        if self._pv_poll_id is None:
            self._pv_poll_id = self.root.after(
                PREVIEW_POLL_MS, self._pv_poll)

    def _pv_poll(self):
        self._pv_poll_id = None
        try:
            while True:
                i, gen, paths, meta, err = self._pv_results.get_nowait()
                if not (meta or {}).get("provisional"):
                    # the toolpath-first picture is extra; its build still owes one
                    self._pv_pending = max(0, self._pv_pending - 1)
                self._pv_apply_result(i, gen, paths, meta, err)
        except queue.Empty:
            pass
        if self._pv_pending > 0:
            self._pv_arm_poll()

    def _pv_apply_result(self, i, gen, paths, meta, err):
        if i == PV_MANDREL:             # the 4-axis half owns its own state
            self._mandrel_apply_result(gen, paths, meta, err)
            return
        state = self._pv_states[i]
        if gen < state["gen_sub"]:
            return                                  # a newer edit is in flight
        if (meta or {}).get("provisional"):
            # The toolpath-first picture: drawn, labelled, and NOT the build's
            # result -- `gen_done` stays put and the status keeps "updating", so
            # everything that waits for the build still waits, and the Design
            # summary is not told (its plate check is not known yet).
            if gen <= state.get("gen_done", -1):
                return                              # the full build beat it
            state.update(paths=paths, meta=meta, lines_pd=None, tubes_pd=None,
                         gcode_pd=None, display_mode=None)
            state["diag"] = math.dist(*_frame_bbox(meta))
            state["status"] = (f"toolpath only · {meta['layers']} layers · "
                               "updating…")
            if (i == self._active_scaffold_index()
                    and not self._pv_curved_active()):
                self._pv_panel.apply_state(state, self._pv_subtitle(i))
            return
        if err is not None:
            state["status"] = (str(err) if isinstance(err, ValueError)
                               else f"preview failed ({err})")
        else:
            state.update(gen_done=gen, paths=paths, meta=meta,
                         lines_pd=None, tubes_pd=None, gcode_pd=None,
                         display_mode=None)
            state["diag"] = math.dist(*_frame_bbox(meta))
            # one form with the mandrel's line (gui_mandrel): what it is,
            # then how it was drawn
            state["status"] = (
                (f"array {meta['array']}× · " if meta.get("array", 1) > 1
                 else "")
                + f"{meta['layers']} layers · {meta['n_pts']:,} points · "
                + ("physics on · " if meta.get("physics") else "")
                + ("approximate sag · " if meta.get("sag")
                   else "no sag: too much to drape · "
                   if meta.get("sag_refused") else "sag off · ")
                + f"drawn in {meta['elapsed']:.2f} s"
                + display_thin_note(meta, " · "))
        # The state is always updated, so switching collectors back shows this
        # result -- but the PANEL is shared with the 4-axis half since the tab
        # merge, and a planar job still in flight when the toggle flips would
        # otherwise land its plate scaffold on top of the tube (the plate mesh
        # drawn over the collector rod).
        if i == self._active_scaffold_index() and not self._pv_curved_active():
            self._pv_panel.apply_state(state, self._pv_subtitle(i))
        self._pv_summary_changed()

    def _pv_summary_changed(self):
        """Tell the Design summary (modern shell only) a build landed."""
        fn = getattr(self, "_ds_refresh", None)
        if fn is not None:
            fn()

    # -- page / rail hooks ----------------------------------------------------#
    def _show_scaffold_frame(self, i):
        super()._show_scaffold_frame(i)
        if getattr(self, "_pv_panel", None) is None:
            return
        # Same rule as _pv_apply_result: the pill rail is hidden in 4-axis, but
        # _apply_collector_mode pins s_count to 1, which reaches here.
        if self._pv_curved_active():
            return
        state = self._pv_states[i]
        self._pv_panel.show_state(state, self._pv_subtitle(i))
        if state["dirty"] or state["paths"] is None:
            self._pv_kick_now()

    def _pv_kick_now(self):
        if self._pv_after is not None:
            try:
                self.root.after_cancel(self._pv_after)
            except (ValueError, tk.TclError):
                pass
            self._pv_after = None
        self._pv_kick()

    def _pv_rod_wanted(self):
        """Whether the preview should draw the collector rod.

        THREE conditions, and the checkbox is only one of them.

        The rod belongs to the 4-axis half of a SHARED panel, so a ticked box
        must still draw nothing while the toggle is on the flat plate.

        And a switch TO the mandrel withholds it until that switch's tube has
        been drawn (`_mandrel_rod_pending`, cleared in `_mandrel_apply_result`).
        The rebuild is asynchronous and the panel keeps showing the previous
        geometry meanwhile, so revealing the rod on the switch itself wrapped it
        around a plate scaffold -- or around a stale tube -- for the length of
        the build. Ordinary field edits do NOT re-arm it: the rod would then
        blink out on every keystroke, and it is already around the right tube.

        Both call sites of `set_collector_visible` go through here so they
        cannot disagree."""
        if not self._mandrel_is_active():
            # A CURVED collector is not a rod. The dome/bowl/cone/torus body is
            # put in the scene elsewhere; drawing a cylinder for it here
            # would be a second, wrong definition of the same collector.
            return False
        if getattr(self, "_mandrel_rod_pending", False):
            return False
        var = getattr(self, "pv_show_collector", None)
        return True if var is None else bool(var.get())

    def _pv_on_show_collector(self, *_a):
        holder = getattr(self, "_mandrel_preview", None)
        if holder is not None:
            holder.set_collector_visible(self._pv_rod_wanted())

    def _pv_snapshot_plate(self):
        """The build plate as `off_plate` takes it, or None (no check).

        The SAME rectangle the preview draws (`MandrelPreviewPanel._size_plate`):
        size from the Machine page's plate fields, centre from
        `gui_buildplate._plate_center_xy`, in the g-code frame the preview
        builds in. A circular plate is the circle inscribed in its square.
        Unlike the slab, no fallback centre: a plate that cannot be placed is
        not checked."""
        fn = getattr(self, "_plate_center_xy", None)
        try:
            shape = str(self.plate_shape.get()).strip()
            if shape == "circular":
                sx = sy = float(self.plate_diameter.get())
            else:
                sx = float(self.plate_x.get())
                sy = float(self.plate_y.get())
            cx, cy = fn() if callable(fn) else (None, None)
        except (AttributeError, ValueError, TypeError, tk.TclError):
            return None
        if cx is None or not (sx > 0.0 and sy > 0.0):
            return None
        return (cx - 0.5 * sx, cy - 0.5 * sy, sx, sy, shape == "circular")

    def _pv_origin_point(self, meta=None):
        """Where g-code zero sits in the picture's frame, or None (a curved
        collector draws none). Plate: the point Setup's Collector card names,
        with the picture's Z 0 on the plate top (it draws fiber at the
        commanded Z). Mandrel: on the rod at its zero end, on the axis or on
        the top of the surface, as the radial zero says."""
        try:
            var = getattr(self, "s_collector", None)
            col = str(var.get()) if var is not None else "plate"
            if col == "mandrel":
                # a picture drawn from a re-based text says where its X 0
                # went (the G-code page's `preview_shift`): that page's
                # tubes, not the designer's one, set the rod's zero
                shift = (meta or {}).get("preview_shift")
                ox = (-float(shift[0]) if shift is not None
                      else self._mandrel_origin_offset()[0])
                _axial, radial = self._rotary_origin_keys()
                r = (float((meta or {}).get("radius") or 0.0)
                     if radial == "surface" else 0.0)
                return (-ox, 0.0, r)
            if col != "plate":
                return None
            px, py, pz = self._origin_datum()
            return (px, py, pz - self._plate_top_z())
        except Exception:  # noqa: BLE001 -- a half-typed field: no triad
            return None

    def _pv_plate_wanted(self):
        """Whether the preview should draw the build plate.

        The 3-axis twin of `_pv_rod_wanted`: the plate belongs to the flat half
        of a shared panel, so a ticked box must draw nothing while the toggle
        is on a rod or a dome. No "build pending" deferral like the rod's --
        the plate is sized from the Machine page's own fields, not from the
        build that is in flight, so there is no stale-geometry window to hide.
        """
        if self._mandrel_is_active():
            return False
        var = getattr(self, "pv_show_plate", None)
        return False if var is None else bool(var.get())

    def _pv_on_show_plate(self, *_a):
        holder = getattr(self, "_mandrel_preview", None)
        if holder is not None:
            holder.set_plate_visible(self._pv_plate_wanted())

    def _collector_preview_switch(self):
        """The collector toggle flipped: hand the shared panel to the other half.

        Called by `gui_scaffold._apply_collector_mode`. Guarded so the toggle can
        settle before the preview exists (the shell builds the form first)."""
        holder = getattr(self, "_mandrel_preview", None)
        if holder is None:
            return
        # A real change of mode blanks the panel first, so the other mode's
        # scaffold never stands in for this one while it builds; the show-last
        # calls below then put back this mode's own last good preview, if any.
        # Not on a re-write of the same mode (a preset load, the form builder's
        # first call): that would blank a preview that is still the right one.
        mode = self.s_collector.get()
        if getattr(self, "_pv_shown_collector", mode) != mode:
            holder.clear()
        self._pv_shown_collector = mode
        # Arm the deferral BEFORE the first visibility call, so the switch to
        # 4-axis hides the rod and the tube's arrival is what shows it.
        self._mandrel_rod_pending = bool(self._mandrel_is_active())
        holder.set_collector_visible(self._pv_rod_wanted())
        holder.set_plate_visible(self._pv_plate_wanted())
        if self._mandrel_is_active():
            # The 4-axis rebuild is asynchronous now, so put the last good tube
            # back on the shared panel at once; without it the plate scaffold
            # would stay on screen for the length of the build.
            self._mandrel_show_last()
            self._refresh_mandrel_preview()
        else:
            self._pv_all_dirty()
            self._pv_kick_now()

    def _pv_on_panel_mapped(self):
        """First (and every) map of the host: create the scene and show the
        active scaffold; build it if it never built."""
        if getattr(self, "_pv_panel", None) is None:
            return
        self._pv_trace_dark()
        i = self._active_scaffold_index()
        state = self._pv_states[i]
        self._pv_panel.apply_state(state, self._pv_subtitle(i))
        if state["dirty"] or state["paths"] is None:
            self._pv_kick_now()
