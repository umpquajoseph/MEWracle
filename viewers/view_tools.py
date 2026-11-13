#!/usr/bin/env python3
"""view_tools.py -- how the G-code page's picture is inspected. No Tk.

Three tools that act on the picture the G-code page already draws
(`gui_preview.PreviewPanel`, fed by `gui_gcode_editor._gce_pv_show`), none of
which rebuilds it while it is used:

  * THE PRINT-TIME WINDOW. The page's bottom play bar's layer slider comes
    down to one pair of numbers, the print times (seconds on the picture's own
    clock, `meta["ptimes"]`) between which the fiber is shown. The fiber's
    polydata carries that clock as a point array (`mew_view_t`), and a fragment
    shader on the picture's actor DISCARDS every fragment outside the window --
    the mechanism `gui_gcode_jet.JetScene` uses for Play, here with a lower
    bound as well. Dragging a slider is therefore two uniforms and a draw. The
    eyes (`_gce_hidden`) still cut geometry the old way, and the window works
    on what is left, so the two combine.
  * COLOUR BY. Scaffold (the page's own colours: today's picture), Layer or
    Speed. Each is one more point array on the same polydata (`mew_layer`,
    `mew_speed`, `mew_scaf`) mapped through a lookup table, so changing the
    choice is a mapper setting, not a rebuild. Speed is the g-code's own F
    words in force on each move (modal, from the text the picture was drawn
    from), in mm/s; F itself is mm/min. The ramps are viridis (layer) and
    inferno (speed), each cut short at the ends that would vanish into the
    page's background or collide with the selection's yellow, differently for
    the dark and the light theme.
  * MEASURE. Two clicked points, a line with its length written on it, its
    X, Y and Z legs in the axes' colours (`measure_legs`, `AXIS_RGB`), and the
    numbers (`measure_numbers`). With one point placed the line follows the
    snap under the pointer (`ViewTools.pair`). A click snaps to the CENTRELINE of the fiber under it (`PreviewPanel.pick`'s
    hit), else lands on the collector the picture draws (the plate's top face,
    the rod's cylinder: `ray_plane_z`, `ray_cylinder_x`). The marks are a
    layer-1 renderer sharing the panel's camera, so they follow every orbit,
    pan and zoom, are drawn as screen-size discs and sit over the fibers; that
    renderer clears depth, so `PreviewPanel._depth_render` hides it for every
    depth read, as it does the gimbal.

Pure functions come first (`layer_table`, `point_arrays`, `modal_feed`,
`scrub_axis`, `window_for`, the rays and `measure_numbers`) so the tests can
check them without a window.
"""

import math
import re

import numpy as np

import preview_pick

def _vtk():
    import gui_preview
    return gui_preview._VTK


def add_arrays(vtk, pd, aux):
    """Carry the per-point `aux` arrays ({name: 1-D float array, one value per
    point of `pd`}) on `pd`'s point data. A vtkTubeFilter passes them on to
    its rings, so the lines and the tubes colour and cut alike. An array of
    the wrong length is skipped, never mis-aligned."""
    from vtk.util import numpy_support as ns
    n = pd.GetNumberOfPoints()
    for name, arr in aux.items():
        a = np.asarray(arr)
        if a.ndim != 1 or len(a) != n:
            continue
        va = ns.numpy_to_vtk(np.ascontiguousarray(a, np.float32), deep=True)
        va.SetName(name)
        pd.GetPointData().AddArray(va)


#: seconds of slack at each end of a layer's print-time window: a layer's own
#: first and last fragment are never discarded for rounding
EPS_T = 1e-4

#: the clock array and the three colour arrays carried on the picture's polydata
ARRAYS = ("mew_view_t", "mew_layer", "mew_speed", "mew_scaf")

MODES = ("scaffold", "layer", "speed")
MODE_LABEL = {"scaffold": "Scaffold", "layer": "Layer", "speed": "Speed"}
#: a measurement's legs, and the sidebar's rows that name them
AXIS_RGB = {"x": (0.86, 0.22, 0.22), "y": (0.16, 0.62, 0.28),
            "z": (0.20, 0.44, 0.92)}

# -- the ramps ---------------------------------------------------------------- #
# 17 stops each, sampled from matplotlib's viridis and inferno (both are
# perceptually uniform and read in order under every common colour-blindness)
_VIRIDIS = [(68, 1, 84), (72, 24, 106), (71, 45, 123), (66, 64, 134),
            (59, 82, 139), (51, 99, 141), (44, 114, 142), (38, 130, 142),
            (33, 145, 140), (31, 160, 136), (40, 174, 128), (63, 188, 115),
            (94, 201, 98), (132, 212, 75), (173, 220, 48), (216, 226, 25),
            (253, 231, 37)]
_INFERNO = [(0, 0, 4), (11, 7, 36), (33, 12, 74), (61, 9, 101), (87, 16, 110),
            (113, 25, 110), (138, 34, 106), (163, 44, 97), (188, 55, 84),
            (210, 70, 68), (228, 90, 49), (241, 115, 29), (249, 142, 9),
            (252, 172, 17), (249, 203, 53), (242, 234, 105), (252, 255, 164)]
_STOPS = {"layer": _VIRIDIS, "speed": _INFERNO}
#: the slice of each ramp used, (from, to) in 0..1, per theme. Cut so that no
#: colour is near the page background (dark: the low end; light: the high
#: end) and none reaches the selection's yellow (1.0, 0.95, 0.0).
_SPAN = {("layer", False): (0.0, 0.56), ("layer", True): (0.40, 0.88),
         ("speed", False): (0.0, 0.64), ("speed", True): (0.54, 0.80)}
#: every colour is at least 3:1 against its page background (WCAG graphics
#: bar) and at least ~0.3 (RGB distance) from the selection yellow;
#: test_gcode_view checks both. Colour is never the only carrier: the legend
#: names the numbers.


def ends_ramp(ends, n=256):
    """`n` RGB triples running straight from `ends[0]` to `ends[1]` (each an
    RGB triple 0..1): a ramp the user chose. It is exactly what they picked;
    the contrast rule of the default ramps does not apply to it."""
    a, b = np.asarray(ends[0], np.float64), np.asarray(ends[1], np.float64)
    f = np.linspace(0.0, 1.0, n)[:, None]
    return a * (1.0 - f) + b * f


def ramp(name, dark, n=256, ends=None):
    """`n` RGB triples (0..1) of the `name` ramp ("layer" or "speed") for the
    dark or light theme, or the user's own two-colour ramp when `ends` is
    given."""
    if ends is not None:
        return ends_ramp(ends, n)
    stops = np.asarray(_STOPS[name], np.float64) / 255.0
    a, b = _SPAN[(name, bool(dark))]
    u = np.linspace(a, b, n) * (len(stops) - 1)
    i = np.clip(np.floor(u).astype(int), 0, len(stops) - 2)
    f = (u - i)[:, None]
    return stops[i] * (1.0 - f) + stops[i + 1] * f


def luminance(rgb):
    """WCAG relative luminance of an RGB triple in 0..1."""
    c = np.asarray(rgb, np.float64)
    c = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return float(0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2])


def contrast(a, b):
    """WCAG contrast ratio of two RGB triples (1 .. 21)."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# -- the print's layers and moves ---------------------------------------------- #
def layer_table(nodes, clock):
    """The print's layers in print order, for the slider: a list of dicts
    ``scaffold`` (1-based, by group key in order of first appearance), ``name``
    (the group's label), ``layer`` (1-based within it), ``first`` / ``last``
    (1-based lines, inclusive) and ``t0`` / ``t1`` (the print-time window the
    layer runs in, `preview_pick.line_window`). A layer with no move in it is
    left out, so every row has a window."""
    rows, order = [], {}
    for nd in nodes:
        if nd.kind != "group":
            continue
        s = order.setdefault(tuple(nd.key), len(order) + 1)
        for ch in nd.children:
            win = preview_pick.line_window(clock, ch.first + 1, ch.last + 1)
            if win is None:
                continue
            rows.append(dict(scaffold=s, name=nd.label, layer=int(ch.key[2]) + 1,
                             first=ch.first + 1, last=ch.last + 1,
                             t0=win[0], t1=win[1]))
    rows.sort(key=lambda r: r["t0"])
    return rows


_FEED = re.compile(r"(?i)(?<![A-Za-z])F\s*([-+]?(?:\d+\.?\d*|\.\d+))")


def modal_feed(text):
    """The feed in force on each line of `text`, mm/min, as an array indexed
    by 1-based line number (index 0 unused): the last F word on a G line at or
    before it. NaN before the first. Comments (after ``;``) are not read."""
    lines = text.split("\n")
    out = np.full(len(lines) + 2, np.nan)
    cur = np.nan
    for n, ln in enumerate(lines, start=1):
        s = ln.split(";", 1)[0].lstrip()
        if s[:1] in ("G", "g") and "F" in s.upper():
            m = _FEED.search(s)
            if m:
                try:
                    cur = float(m.group(1))
                except ValueError:
                    pass
        out[n] = cur
    out[len(lines) + 1] = cur
    return out


def point_arrays(T, layers, clock, feed):
    """The per-point arrays for drawn points at print times `T` (1-D float):
    ``mew_layer`` (1-based index into `layers`), ``mew_scaf`` (the scaffold
    ordinal of that layer) and ``mew_speed`` (mm/s: the modal feed of the
    clock's segment the point falls in, over 60). Points before the first
    layer or after the last take the nearest layer's."""
    T = np.asarray(T, np.float64)
    out = {}
    if layers:
        t0 = np.array([r["t0"] for r in layers])
        idx = np.clip(np.searchsorted(t0, T, side="right") - 1, 0,
                      len(layers) - 1)
        out["mew_layer"] = (idx + 1).astype(np.float32)
        out["mew_scaf"] = np.array([r["scaffold"] for r in layers],
                                   np.float32)[idx]
    times = np.asarray(clock["times"], np.float64)
    sl = np.asarray(clock["seg_line"], np.int64)
    if len(sl) and feed is not None and np.isfinite(feed).any():
        j = np.clip(np.searchsorted(times, T, side="right") - 1, 0,
                    len(sl) - 1)
        f = feed[np.clip(sl[j], 0, len(feed) - 1)]
        lo = np.nanmin(f) if np.isfinite(f).any() else 0.0
        out["mew_speed"] = (np.where(np.isfinite(f), f, lo) / 60.0
                            ).astype(np.float32)
    return out


def window_for(layers, a, b):
    """(lo, hi): the print-time window of layers `a`..`b` (0-based, inclusive
    indexes into `layers`), with `EPS_T` of slack."""
    return layers[a]["t0"] - EPS_T, layers[b]["t1"] + EPS_T


def scrub_axis(clock, first, last):
    """The moves between 1-based lines `first`..`last`, for the scrubber: the
    clock's segment indexes that take time (a zero-length marker is not a
    move), in print order."""
    times = np.asarray(clock["times"], np.float64)
    sl = np.asarray(clock["seg_line"])
    j0 = int(np.searchsorted(sl, first, side="left"))
    j1 = int(np.searchsorted(sl, last, side="right"))
    dt = times[1:] - times[:-1]
    return j0 + np.flatnonzero(dt[j0:j1] > 0)


def move_number(clock, seg):
    """(n, total): the 1-based number of clock segment `seg` among every move
    of the file (segments that take time), and how many moves there are."""
    times = np.asarray(clock["times"], np.float64)
    moving = (times[1:] - times[:-1]) > 0
    cum = np.cumsum(moving)
    return int(cum[seg]), int(cum[-1]) if len(cum) else 0


# -- rays and collectors -------------------------------------------------------- #
def ray_from_pixel(M, w, h, x, y):
    """(origin, unit direction) of the camera ray through display pixel
    (x, y), from the composite matrix `M`: the near and far points."""
    a = preview_pick.unproject(M, w, h, x, y, 0.0)
    b = preview_pick.unproject(M, w, h, x, y, 1.0)
    d = b - a
    n = float(np.linalg.norm(d))
    return a, (d / n if n > 0 else d)


def ray_plane_z(o, d, z0, xlim, ylim):
    """The point where the ray meets the horizontal plane z = z0 inside the
    rectangle `xlim` x `ylim`, or None."""
    if abs(d[2]) < 1e-12:
        return None
    s = (z0 - o[2]) / d[2]
    p = o + s * d
    if xlim[0] <= p[0] <= xlim[1] and ylim[0] <= p[1] <= ylim[1]:
        return p
    return None


def ray_cylinder_x(o, d, radius, xlim):
    """The first point (nearest the camera) where the ray meets the cylinder
    of `radius` about the X axis, between x = xlim[0] and xlim[1], or None."""
    a = d[1] ** 2 + d[2] ** 2
    if a < 1e-12:
        return None
    b = 2.0 * (o[1] * d[1] + o[2] * d[2])
    c = o[1] ** 2 + o[2] ** 2 - radius ** 2
    disc = b * b - 4.0 * a * c
    if disc < 0.0:
        return None
    r = math.sqrt(disc)
    for s in sorted(((-b - r) / (2.0 * a), (-b + r) / (2.0 * a))):
        p = o + s * d
        if xlim[0] <= p[0] <= xlim[1]:
            return p
    return None


def measure_legs(p1, p2):
    """The X, Y and Z legs from `p1` to `p2`: {"x": (a, b), ...}."""
    a, b = np.asarray(p1, float), np.asarray(p2, float)
    q = np.array((b[0], a[1], a[2]))
    r = np.array((b[0], b[1], a[2]))
    return {"x": (a, q), "y": (q, r), "z": (r, b)}


def measure_numbers(p1, p2, rod_radius=None):
    """dict(dx, dy, dz, dist) between two world points (mm)."""
    p1, p2 = np.asarray(p1, float), np.asarray(p2, float)
    d = p2 - p1
    return dict(dx=float(d[0]), dy=float(d[1]), dz=float(d[2]),
                dist=float(np.linalg.norm(d)))


# -- the shader ------------------------------------------------------------------ #
_VS_DEC = "//VTK::Normal::Dec\nin float mewViewT;\nout float mewViewTV;"
_VS_IMPL = "//VTK::Normal::Impl\n  mewViewTV = mewViewT;"
_FS_DEC = "//VTK::Normal::Dec\nin float mewViewTV;"
# the uniforms are declared by VTK from GetFragmentCustomUniforms()
_FS_IMPL = ("//VTK::Normal::Impl\n"
            "  if (mewViewTV < mewViewLo || mewViewTV > mewViewHi) discard;")

#: a window wide enough to hold any print: the shader's "show everything"
_ALL = 3.0e38


class ViewTools:
    """The VTK half of the three tools on one `PreviewPanel`. Built lazily
    by the panel when the page asks for it (`want_view_tools`); every method
    is cheap and none rebuilds geometry."""

    def __init__(self, panel):
        self.panel = panel
        self.mode = "scaffold"
        self.custom = {}            # mode -> (low RGB, high RGB), the user's
        self.window = None          # (lo, hi) in print seconds, or None
        self._base = 0.0            # the clock's zero on the polydata
        self._armed = {}            # actor id -> shader installed
        self.measuring = False
        self.pts = []               # placed measure points (world)
        self.probes = []            # what each was: probe() dicts
        self.snap = None            # the hover's dict, or None
        self.on_measure = None      # callback(event: "hover"|"place", info)
        self._ren2 = None
        self._last_hover = 0.0
        self._zcache = None
        self._gen = 0

    # -- the clock array and the shader ---------------------------------------- #
    def _shader(self, actor, mapper, on):
        """Install (or take out) the window's shader on `actor`/`mapper`. Only
        a polydata that carries `mew_view_t` may have it: a vertex attribute
        mapped to a missing array is an error at draw time."""
        key = id(actor)
        if bool(self._armed.get(key)) == bool(on):
            return
        sp = actor.GetShaderProperty()
        if on:
            mapper.MapDataArrayToVertexAttribute(
                "mewViewT", "mew_view_t",
                _vtk().vtkDataObject.FIELD_ASSOCIATION_POINTS, -1)
            sp.AddVertexShaderReplacement("//VTK::Normal::Dec", True,
                                          _VS_DEC, False)
            sp.AddVertexShaderReplacement("//VTK::Normal::Impl", True,
                                          _VS_IMPL, False)
            sp.AddFragmentShaderReplacement("//VTK::Normal::Dec", True,
                                            _FS_DEC, False)
            sp.AddFragmentShaderReplacement("//VTK::Normal::Impl", True,
                                            _FS_IMPL, False)
            u = sp.GetFragmentCustomUniforms()
            u.SetUniformf("mewViewLo", -_ALL)
            u.SetUniformf("mewViewHi", _ALL)
        else:
            sp.ClearAllVertexShaderReplacements()
            sp.ClearAllFragmentShaderReplacements()
            mapper.RemoveAllVertexAttributeMappings()
        self._armed[key] = bool(on)

    def sync(self, state):
        """After `apply_state` has put `state` up: arm the shader on the fiber
        (and the commanded-path overlay) when its data carry the clock, set
        the window and the colours."""
        p = self.panel
        has = bool(state and (state.get("aux") or {}).get("mew_view_t")
                   is not None)
        self._base = float((state or {}).get("aux_base", 0.0))
        if p._actor is not None:
            self._shader(p._actor, p._mapper, has)
        self._gen += 1
        self._apply_window()

    def sync_overlay(self, state):
        """The commanded-path overlay's own clock (`state["gcode_aux"]`)."""
        p = self.panel
        if p._gc_actor is None:
            return
        has = bool(state and state.get("gcode_t") is not None)
        self._shader(p._gc_actor, p._gc_mapper, has)
        self._apply_window()

    def _apply_window(self):
        lo, hi = ((-_ALL, _ALL) if self.window is None else
                  (self.window[0] - self._base, self.window[1] - self._base))
        for actor in (self.panel._actor, self.panel._gc_actor):
            if actor is not None and self._armed.get(id(actor)):
                u = actor.GetShaderProperty().GetFragmentCustomUniforms()
                u.SetUniformf("mewViewLo", float(lo))
                u.SetUniformf("mewViewHi", float(hi))

    def set_window(self, lo=None, hi=None):
        """Show only fiber laid between print times `lo` and `hi` (absolute
        seconds); no arguments shows everything. Two uniforms and a draw."""
        self.window = None if lo is None else (float(lo), float(hi))
        self._gen += 1
        self._apply_window()
        self.panel._render_soon()

    def windowed(self):
        return self.window is not None

    def pick_index(self, state, index):
        """`index` (`preview_pick.segment_index`) cut to the segments inside
        the window, so a click never lands on fiber the window discards (its
        centreline lies nearer than the buffer behind it, and would pass the
        visibility test). The index itself when nothing is cut."""
        if self.window is None:
            return index
        meta = state.get("meta") or {}
        pt = meta.get("ptimes")
        paths = state.get("paths")
        if pt is None or paths is None or len(pt) != len(paths):
            return index
        cache = state.get("_pick_T")
        if cache is None or cache[0] is not pt:
            T = np.concatenate([np.asarray(t, np.float64) for t in pt])
            cache = (pt, T)
            state["_pick_T"] = cache
        T = cache[1]
        seg = index["seg"]
        if len(T) != len(index["P"]) or not len(seg):
            return index
        lo, hi = self.window
        keep = (T[seg] >= lo - EPS_T) & (T[seg + 1] <= hi + EPS_T)
        return dict(index, seg=seg[keep])

    # -- colour -------------------------------------------------------------- #
    def set_mode(self, mode):
        self.mode = mode if mode in MODES else "scaffold"
        self._gen += 1
        self.panel._set_path_color_mode(self.panel._state or {})
        self.panel._hl_restyle()
        self.panel._render_soon()

    def theme_changed(self):
        self.panel._set_path_color_mode(self.panel._state or {})

    def scale(self, state, mode=None):
        """(lo, hi) of the colour scale for `mode` on `state`'s data, or None
        when the picture carries no such array."""
        a = (state or {}).get("aux") or {}
        key = {"layer": "mew_layer", "speed": "mew_speed",
               "scaffold": "mew_scaf"}[mode or self.mode]
        v = a.get(key)
        if v is None or not len(v):
            return None
        return float(np.nanmin(v)), float(np.nanmax(v))

    def table(self, mode=None):
        """The ramp now in use for a "layer" / "speed" mode: the user's two
        colours when set, else the theme's default."""
        mode = mode or self.mode
        return ramp(mode, self.panel._is_dark(), ends=self.custom.get(mode))

    def set_custom(self, mode, ends):
        """Use the two colours `ends` (RGB 0..1) for `mode`'s ramp, or the
        default again when None; repaint at once."""
        if ends is None:
            self.custom.pop(mode, None)
        else:
            self.custom[mode] = (tuple(ends[0]), tuple(ends[1]))
        st = self.panel._state
        if st is not None and mode == self.mode:
            self.paint(st)
        self.panel._render_soon()

    def lookup(self, state, mode=None):
        """The vtkLookupTable for `mode` on `state`, built for the current
        theme, or None for a mode that is one flat colour."""
        mode = mode or self.mode
        sc = self.scale(state, mode)
        if sc is None:
            return None
        vtk = _vtk()
        lut = vtk.vtkLookupTable()
        dark = self.panel._is_dark()
        if mode == "scaffold":
            cols = (state.get("aux_meta") or {}).get("scaffold_rgb") or []
            n = int(sc[1])
            if n < 2 or len(cols) < n:
                return None
            lut.SetNumberOfTableValues(n)
            for i in range(n):
                lut.SetTableValue(i, *cols[i], 1.0)
            lut.SetTableRange(0.5, n + 0.5)
            return lut
        lo, hi = sc
        if mode == "layer":
            hi = max(hi, lo + 1.0)
        elif not hi > lo:
            # every move at one feed: the ramp's middle, so the legend's
            # single chip is the colour the fibres wear
            lo, hi = lo - 0.5, lo + 0.5
        table = self.table(mode)
        lut.SetNumberOfTableValues(len(table))
        for i, c in enumerate(table):
            lut.SetTableValue(i, float(c[0]), float(c[1]), float(c[2]), 1.0)
        lut.SetTableRange(lo, hi)
        return lut

    def paint(self, state):
        """Set the fiber mapper for the chosen colour mode. True when the
        mapper maps a scalar array through a table (the panel's own
        single-colour / per-path paths then stand down)."""
        p = self.panel
        m = p._mapper
        if m is None or state is None:
            return False
        lut = self.lookup(state)
        if lut is None:
            return False
        name = {"layer": "mew_layer", "speed": "mew_speed",
                "scaffold": "mew_scaf"}[self.mode]
        m.SetLookupTable(lut)
        m.SetScalarRange(*lut.GetTableRange())
        m.SetScalarModeToUsePointFieldData()
        m.SelectColorArray(name)
        m.SetColorModeToMapScalars()
        m.SetInterpolateScalarsBeforeMapping(0)
        m.UseLookupTableScalarRangeOn()
        m.SetScalarVisibility(1)
        return True

    def paint_other(self, mapper, state):
        """The same colouring on another mapper over the same data (Play's
        fiber). True when it was painted."""
        lut = self.lookup(state)
        if lut is None:
            return False
        name = {"layer": "mew_layer", "speed": "mew_speed",
                "scaffold": "mew_scaf"}[self.mode]
        mapper.SetLookupTable(lut)
        mapper.SetScalarModeToUsePointFieldData()
        mapper.SelectColorArray(name)
        mapper.SetColorModeToMapScalars()
        mapper.SetInterpolateScalarsBeforeMapping(0)
        mapper.UseLookupTableScalarRangeOn()
        mapper.SetScalarVisibility(1)
        return True

    # -- measure --------------------------------------------------------------- #
    def _overlay(self):
        """The marks' renderer (layer 1, the panel's camera), built on first use."""
        if self._ren2 is not None:
            return self._ren2
        p = self.panel
        vtk = _vtk()
        ren = vtk.vtkRenderer()
        ren.SetLayer(1)
        ren.InteractiveOff()
        ren.SetActiveCamera(p._ren.GetActiveCamera())
        p._win.SetNumberOfLayers(max(2, p._win.GetNumberOfLayers()))
        p._win.AddRenderer(ren)
        self._marks = {}
        for key, size in (("a", 12.0), ("snap", 17.0)):
            pts = vtk.vtkPoints()
            cells = vtk.vtkCellArray()
            pd = vtk.vtkPolyData()
            pd.SetPoints(pts)
            pd.SetVerts(cells)
            m = vtk.vtkPolyDataMapper()
            m.SetInputData(pd)
            a = vtk.vtkActor()
            a.SetMapper(m)
            pr = a.GetProperty()
            pr.SetPointSize(size)
            pr.SetRenderPointsAsSpheres(True)
            pr.SetLighting(False)
            a.SetVisibility(0)
            ren.AddActor(a)
            self._marks[key] = (pd, a)
        for key, width in (("x", 1.5), ("y", 1.5), ("z", 1.5), ("line", 2.5)):
            ld = vtk.vtkPolyData()
            ld.SetPoints(vtk.vtkPoints())
            ld.SetLines(vtk.vtkCellArray())
            lm = vtk.vtkPolyDataMapper()
            lm.SetInputData(ld)
            la = vtk.vtkActor()
            la.SetMapper(lm)
            la.GetProperty().SetLineWidth(width)
            la.GetProperty().SetLighting(False)
            la.SetVisibility(0)
            ren.AddActor(la)
            self._marks[key] = (ld, la)
        # the length on the line: a 2-D text at a world point, so it follows
        # every orbit, pan and zoom and stays upright and one size
        tx = vtk.vtkTextActor()
        tx.GetPositionCoordinate().SetCoordinateSystemToWorld()
        tp = tx.GetTextProperty()
        from gui_viewer_shell import apply_font
        apply_font(tp)
        tp.SetJustificationToCentered()
        tp.SetVerticalJustificationToCentered()
        dpi = p._host.winfo_fpixels("1i") / 96.0
        tp.SetFontSize(int(round(14 * dpi)))
        tp.SetBackgroundOpacity(0.92)
        tp.FrameOn()
        tx.SetVisibility(0)
        ren.AddActor2D(tx)
        self._marks["text"] = (None, tx)
        self._ren2 = ren
        self._colour_marks()
        return ren

    def _colour_marks(self):
        if self._ren2 is None:
            return
        dark = self.panel._is_dark()
        # placed points and the line: a strong blue/orange pair that holds on
        # both grounds; the snap preview is the page's own contrast (white on
        # dark, near-black on light) so it is told apart by SIZE and colour
        placed = (1.0, 0.45, 0.10) if not dark else (1.0, 0.62, 0.25)
        snap = (1.0, 1.0, 1.0) if dark else (0.05, 0.05, 0.10)
        self._marks["a"][1].GetProperty().SetColor(*placed)
        self._marks["line"][1].GetProperty().SetColor(*placed)
        self._marks["snap"][1].GetProperty().SetColor(*snap)
        for ax, rgb in AXIS_RGB.items():
            self._marks[ax][1].GetProperty().SetColor(*rgb)
        tp = self._marks["text"][1].GetTextProperty()
        tp.SetColor(*((0.95, 0.95, 0.97) if dark else (0.08, 0.09, 0.12)))
        tp.SetBackgroundColor(*((0.12, 0.13, 0.15) if dark
                                else (1.0, 1.0, 1.0)))
        tp.SetFrameColor(*placed)

    def overlay_ren(self):
        """The marks' renderer while anything is drawn in it (else None):
        `PreviewPanel._depth_render` hides it for a depth read."""
        r = self._ren2
        if r is None:
            return None
        if any(a.GetVisibility() for _pd, a in self._marks.values()):
            return r
        return None

    def set_measuring(self, on):
        self.measuring = bool(on)
        if not on:
            self.pts, self.probes = [], []
            self.snap = None
            if self._ren2 is not None:
                self._draw()
        self.panel._render_soon()

    def _set_points(self, key, pts):
        pd, a = self._marks[key]
        vtk = _vtk()
        vp = vtk.vtkPoints()
        cells = vtk.vtkCellArray()
        for i, q in enumerate(pts):
            vp.InsertNextPoint(*[float(c) for c in q])
            cells.InsertNextCell(1)
            cells.InsertCellPoint(i)
        pd.SetPoints(vp)
        pd.SetVerts(cells)
        pd.Modified()
        a.SetVisibility(1 if len(pts) else 0)

    def _set_line(self, key, ends):
        """Mark `key` as the segment `ends` (two points), or hidden (None)."""
        ld, la = self._marks[key]
        if ends is None:
            la.SetVisibility(0)
            return
        vtk = _vtk()
        vp = vtk.vtkPoints()
        for q in ends:
            vp.InsertNextPoint(*[float(c) for c in q])
        cells = vtk.vtkCellArray()
        cells.InsertNextCell(2)
        cells.InsertCellPoint(0)
        cells.InsertCellPoint(1)
        ld.SetPoints(vp)
        ld.SetLines(cells)
        ld.Modified()
        la.SetVisibility(1)

    def pair(self):
        """The two ends being read: both points placed, or the first and the
        snap under the pointer, or None."""
        if len(self.pts) >= 2:
            return self.pts[0], self.pts[1]
        if self.pts and self.snap:
            return self.pts[0], self.snap["point"]
        return None

    def _draw(self):
        self._overlay()
        self._colour_marks()
        self._set_points("a", self.pts)
        self._set_points("snap", [self.snap["point"]] if self.snap else [])
        pair = self.pair()
        self._set_line("line", pair)
        legs = measure_legs(*pair) if pair else {}
        for ax in AXIS_RGB:
            self._set_line(ax, legs.get(ax))
        tx = self._marks["text"][1]
        if pair:
            a, b = np.asarray(pair[0], float), np.asarray(pair[1], float)
            tx.SetInput(f"{np.linalg.norm(b - a):.3f} mm")
            tx.GetPositionCoordinate().SetValue(*((a + b) / 2.0))
        tx.SetVisibility(1 if pair else 0)

    def clear_points(self):
        self.pts, self.probes = [], []
        self.snap = None
        if self._ren2 is not None:
            self._draw()
        self.panel._render_soon()

    # -- what a pixel is, for the tool ------------------------------------------ #
    def _matrix(self, win_size):
        p = self.panel
        cam = p._ren.GetActiveCamera()
        m = cam.GetCompositeProjectionTransformMatrix(
            p._ren.GetTiledAspectRatio(), 0, 1)
        return np.array([[m.GetElement(i, j) for j in range(4)]
                         for i in range(4)])

    def collector_hit(self, x, y):
        """The collector under Tk pixel (x, y): dict(point, kind) with kind
        "plate" or "rod", or None. Only a collector the picture draws counts
        (the plate / rod actor is visible)."""
        p = self.panel
        if p._win is None:
            return None
        w, h = p._win.GetSize()
        M = self._matrix((w, h))
        o, d = ray_from_pixel(M, w, h, float(x), float(h - y))
        hits = []
        rod = getattr(p, "_mandrel_actor", None)
        src = getattr(p, "_mandrel_src", None)
        if rod is not None and src is not None and rod.GetVisibility():
            cx = src.GetCenter()[1]
            half = 0.5 * src.GetHeight()
            q = ray_cylinder_x(o, d, float(src.GetRadius()),
                               (cx - half, cx + half))
            if q is not None:
                hits.append((float(np.dot(q - o, d)), q, "rod"))
        plate = getattr(p, "_plate_actor", None)
        psrc = getattr(p, "_plate_src", None)
        if plate is not None and psrc is not None and plate.GetVisibility():
            c = psrc.GetCenter()
            top = c[2] + 0.5 * psrc.GetZLength()
            q = ray_plane_z(
                o, d, top,
                (c[0] - 0.5 * psrc.GetXLength(), c[0] + 0.5 * psrc.GetXLength()),
                (c[1] - 0.5 * psrc.GetYLength(), c[1] + 0.5 * psrc.GetYLength()))
            if q is not None:
                hits.append((float(np.dot(q - o, d)), q, "plate"))
        if not hits:
            return None
        hits.sort(key=lambda t: t[0])
        return dict(point=hits[0][1], kind=hits[0][2])

    def rod_radius(self):
        """The drawn rod's radius while it is shown, else None."""
        p = self.panel
        rod = getattr(p, "_mandrel_actor", None)
        src = getattr(p, "_mandrel_src", None)
        if rod is not None and src is not None and rod.GetVisibility():
            return float(src.GetRadius())
        return None

    def probe(self, x, y, fresh=True):
        """What a click at Tk pixel (x, y) would land on: the fiber's
        centreline there (kind "fiber", with the pick's hit under `hit`), else
        the collector, else None. `fresh=False` (a hover) may reuse the last
        depth buffer while nothing has been drawn since."""
        p = self.panel
        hit = p.pick(x, y, zread=None if fresh else self._zread())
        if hit is not None:
            return dict(point=np.asarray(hit["point"], float), kind="fiber",
                        hit=hit)
        return self.collector_hit(x, y)

    def _zread(self):
        """A zread over a cached whole-window depth buffer, refreshed when the
        panel has drawn since (`PreviewPanel._frames`)."""
        p = self.panel
        w, h = p._win.GetSize()
        key = (p._frames, w, h, self._gen)
        if self._zcache is None or self._zcache[0] != key:
            if not p._depth_render():
                p._win.Render()
            vtk = _vtk()
            from vtk.util import numpy_support as ns
            fa = vtk.vtkFloatArray()
            p._win.GetZbufferData(0, 0, w - 1, h - 1, fa)
            arr = ns.vtk_to_numpy(fa).reshape(h, w).copy()
            self._zcache = (key, arr)
        arr = self._zcache[1]
        return lambda x0, y0, x1, y1: arr[y0:y1 + 1, x0:x1 + 1]

    def hover(self, x, y, min_gap=0.05):
        """Show the snap under the pointer. Throttled to one probe per
        `min_gap` seconds; a probe is one depth read and numpy."""
        import time
        now = time.perf_counter()
        if now - self._last_hover < min_gap:
            return
        self._last_hover = now
        self.snap = self.probe(x, y, fresh=False)
        self._draw()
        self.panel._render_soon()
        if self.on_measure:
            self.on_measure("hover", self.snap)

    def click(self, x, y):
        """A click: place the next point (a third click starts over)."""
        got = self.probe(x, y, fresh=True)
        if got is None:
            if self.on_measure:
                self.on_measure("miss", None)
            return None
        if len(self.pts) >= 2:
            self.pts, self.probes = [], []
        self.pts.append(np.asarray(got["point"], float))
        self.probes.append(got)
        self.snap = None
        self._draw()
        self.panel._render_soon()
        if self.on_measure:
            self.on_measure("place", got)
        return got
