#!/usr/bin/env python3
"""gui_gcode_jet.py -- MODERN-UI ONLY: Play, the jet printing the G-code page's
text inside the page's own picture.

No machine, no second window and no player process. The page's preview
already carries a print clock for every drawn point: `meta["ptimes"]` (print
seconds per fiber point, on the player's clock) and `meta["gcode_ptimes"]` for
the commanded path, both built by `gui_preview._paths_from_gcode_file`. So
playing the print is a question of WHEN each point appears, and the answer is
one number per frame:

  * the fiber -- a copy of the picture's own polydata (lines or tubes, whichever
    the display toggle is drawing) carrying the clock as a point array, drawn
    by an actor whose fragment shader DISCARDS every fragment laid after "now".
    Nothing is rebuilt per frame; the cut falls mid-segment because the clock
    interpolates across it.
  * the jet -- no printhead is drawn, only the jet. Its top floats one
    standoff (the Machine page's printhead elevation) above the commanded
    point at "now", and it runs down to the fiber point being laid at "now", drawn
    as the player's catenary (`gcode_player._catenary_a`, imported, so the shape
    has one definition). With physics off the laid fiber IS the commanded path,
    so the jet is vertical; with physics on, its lean is the lag the picture's
    laid fiber already has.

Same renderer, so the camera is the CAD view's own and stays live while it
plays (orbit, zoom, pick). Stop puts the picture's own actor back as it was.

The page's ONE bar (`_jet_build_bar`) carries Play, the layer slider and the speed
(1x is the print's own time); `gui_gcode_view` places it and owns the layer
window the slider steps.

What it does not do: a curved collector (the tool greys and points to the
Simulation page), the field-nudged arc and the cone-jet radius profile (both
need the physics model the player builds), and acceleration (the clock is the
commanded feed rates, as the preview's is)."""

import math
import re
import time
import tkinter as tk
from tkinter import ttk

import numpy as np

import gui_form
from gui_gcode_cad import GlyphButton
from gui_tips import Tooltip, add_tip

#: the playback speeds the - / + buttons step through, as multiples of the
#: print's own time (1 is real print time: print seconds per wall second)
SPEEDS = (0.25, 0.5, 1, 2, 4, 8, 16, 32, 64)
#: the speed Play opens at, and keeps until the user steps it
DEFAULT_SPEED = 8
#: the frame tick, ms. The blit is the ceiling (gui_preview.PreviewPanel._render)
TICK_MS = 16
#: the jet never draws thinner than this many pixels
MIN_PX = 2.5

_ROTARY = re.compile(r"(?im)^\s*G[0-3]\b[^;\n(]*\b[ABC]\s*[-+.\d]")

_VS_DEC = "//VTK::Normal::Dec\nin float mewJetT;\nout float mewJetTV;"
_VS_IMPL = "//VTK::Normal::Impl\n  mewJetTV = mewJetT;"
_FS_DEC = "//VTK::Normal::Dec\nin float mewJetTV;"
# the uniform is declared by VTK from GetFragmentCustomUniforms(); declaring it
# here too is a compile error ("conflicts with previous declaration")
_FS_IMPL = "//VTK::Normal::Impl\n  if (mewJetTV > mewJetNow) discard;"


def _fmt(secs):
    secs = max(0, int(round(secs)))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_speed(speed):
    """'¼×', '½×', '8×': a playback speed as the bar writes it."""
    return {0.25: "¼×", 0.5: "½×"}.get(speed, f"{speed:g}×")


def jet_arc(tip, contact, segments):
    """The jet centreline from `tip` to `contact`, `segments`+1 points.

    Ported from `gcode_player.GcodePlayer._jet_arc_points` (gcode_player.py,
    the catenary branch) with no field: the same cosine-clustered drop
    fractions and the same catenary with its vertex at the touchdown, `a`
    solved by the player's own `_catenary_a`. A chord when there is no drop or
    no lag, as there."""
    from gcode_player import _catenary_a
    tip = np.asarray(tip, float)
    con = np.asarray(contact, float)
    n = int(segments)
    vt = 0.5 * (1.0 - np.cos(math.pi * np.linspace(0.0, 1.0, n + 1)))
    drop = float(tip[2] - con[2])
    lat = float(math.hypot(con[0] - tip[0], con[1] - tip[1]))
    if drop <= 1e-9 or lat < 1e-9:
        ut = vt.copy() if drop <= 1e-9 else np.zeros_like(vt)
    else:
        a = _catenary_a(lat, drop)
        z = drop * (1.0 - vt)
        ut = 1.0 - a * np.arccosh(np.clip(z / a + 1.0, 1.0, None)) / lat
        ut[0], ut[-1] = 0.0, 1.0
    return np.column_stack([tip[0] + ut * (con[0] - tip[0]),
                            tip[1] + ut * (con[1] - tip[1]),
                            tip[2] + vt * (con[2] - tip[2])])


class _Track:
    """A set of timed polylines, asked where it is at a time. `inside` says
    whether the time falls within one of the polylines rather than in a gap
    between them."""

    def __init__(self, paths, times):
        keep = [(np.asarray(p, float), np.asarray(t, float))
                for p, t in zip(paths, times) if len(p) and len(p) == len(t)]
        self.paths = [p for p, _ in keep]
        self.times = [t for _, t in keep]
        self.t0 = np.array([t[0] for t in self.times]) if keep else np.zeros(0)
        self.t1 = np.array([t[-1] for t in self.times]) if keep else np.zeros(0)

    def __bool__(self):
        return bool(self.paths)

    def at(self, t):
        """(point, inside). Before the first polyline: its first point; in a
        gap: the end of the one before; after the last: its last point."""
        k = int(np.searchsorted(self.t0, t, side="right")) - 1
        if k < 0:
            return self.paths[0][0], False
        p, tt = self.paths[k], self.times[k]
        if t > self.t1[k]:
            return p[-1], False
        i = int(np.searchsorted(tt, t, side="right")) - 1
        i = min(max(i, 0), len(tt) - 2) if len(tt) > 1 else 0
        if len(tt) < 2:
            return p[0], True
        span = tt[i + 1] - tt[i]
        f = 0.0 if span <= 0 else min(max((t - tt[i]) / span, 0.0), 1.0)
        return p[i] + f * (p[i + 1] - p[i]), True


class JetScene:
    """The VTK half: the timed fiber and the jet, added to a
    `gui_preview.PreviewPanel`'s renderer and taken out again. No Tk."""

    def __init__(self, panel, standoff, mandrel=False):
        import gcode_player
        self.panel = panel
        self.vtk = vtk = _vtk()
        self.standoff = float(standoff)
        self.mandrel = bool(mandrel)
        self._collector_spin = gcode_player.Player._collector_spin
        self.segments = gcode_player.JET_ARC_SEGMENTS
        ren = panel._ren

        self.fiber_mapper = vtk.vtkPolyDataMapper()
        self.fiber = vtk.vtkActor()
        self.fiber.SetMapper(self.fiber_mapper)
        self.fiber_mapper.MapDataArrayToVertexAttribute(
            "mewJetT", "mew_jet_t", vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS,
            -1)
        sp = self.fiber.GetShaderProperty()
        sp.AddVertexShaderReplacement("//VTK::Normal::Dec", True, _VS_DEC,
                                      False)
        sp.AddVertexShaderReplacement("//VTK::Normal::Impl", True, _VS_IMPL,
                                      False)
        sp.AddFragmentShaderReplacement("//VTK::Normal::Dec", True, _FS_DEC,
                                        False)
        sp.AddFragmentShaderReplacement("//VTK::Normal::Impl", True, _FS_IMPL,
                                        False)
        self._uniforms = sp.GetFragmentCustomUniforms()
        self._uniforms.SetUniformf("mewJetNow", -1.0)
        ren.AddActor(self.fiber)

        # the jet: a polyline swept into a tube, its points moved per frame
        self.jet_pts = vtk.vtkPoints()
        self.jet_pts.SetNumberOfPoints(self.segments + 1)
        line = vtk.vtkPolyLine()
        line.GetPointIds().SetNumberOfIds(self.segments + 1)
        for i in range(self.segments + 1):
            line.GetPointIds().SetId(i, i)
        cells = vtk.vtkCellArray()
        cells.InsertNextCell(line)
        self.jet_pd = vtk.vtkPolyData()
        self.jet_pd.SetPoints(self.jet_pts)
        self.jet_pd.SetLines(cells)
        self.jet_tube = vtk.vtkTubeFilter()
        self.jet_tube.SetInputData(self.jet_pd)
        self.jet_tube.SetNumberOfSides(12)
        self.jet_tube.CappingOn()
        jm = vtk.vtkPolyDataMapper()
        jm.SetInputConnection(self.jet_tube.GetOutputPort())
        self.jet = vtk.vtkActor()
        self.jet.SetMapper(jm)
        jp = self.jet.GetProperty()
        jp.SetColor(*gcode_player.JET_COLOR)
        jp.SetAmbient(0.45)
        jp.SetDiffuse(0.6)
        jp.SetSpecular(0.3)
        ren.AddActor(self.jet)

        self.state = None
        self.mode = None
        self._src = (None, None)
        self.fiber_track = None
        self.nozzle_track = None
        self.rotary_track = None
        self._rotated = []
        self.t_start = self.t_end = 0.0
        self._t_base = 0.0
        self._hidden = []
        self._cam_saved = None

    # -- arming on the picture's current state -------------------------- #
    def arm(self, state):
        """Take the picture's shown state. False (and nothing changed) when it
        carries no clock."""
        vtk = self.vtk
        meta = state.get("meta") or {}
        paths, ptimes = state.get("paths"), meta.get("ptimes")
        lines_pd = state.get("lines_pd")
        if not paths or ptimes is None or lines_pd is None \
                or len(ptimes) != len(paths):
            return False
        T = np.concatenate([np.asarray(t, float) for t in ptimes])
        if len(T) != lines_pd.GetNumberOfPoints():
            return False
        self.fiber_track = _Track(paths, ptimes)
        gp, gt = meta.get("gcode_paths"), meta.get("gcode_ptimes")
        self.nozzle_track = (_Track(gp, gt)
                             if gp and gt is not None and len(gp) == len(gt)
                             else None) or None
        if self.mandrel:
            # This is the same parse_path clock and machine/A readings used by
            # the Simulation player, before its optional acceleration retiming.
            clock = meta.get("line_clock") or {}
            rotary = clock.get("rotary")
            if rotary is None:
                return False
            machine, angle = rotary
            points = np.column_stack((machine, angle))
            self.rotary_track = _Track([points], [clock["times"]])
            if not self.rotary_track:
                return False
            for actor in (getattr(self.panel, "_mandrel_actor", None),
                          getattr(self.panel, "_gc_actor", None)):
                if actor is not None:
                    self._rotated.append((actor, actor.GetOrientation()))
        self.t_start, self.t_end = float(T.min()), float(T.max())
        self._t_base = self.t_start
        rel = (T - self._t_base).astype(np.float32)

        mode = state.get("display_mode") or "lines"
        src = state.get("tubes_pd") if mode == "tubes" else lines_pd
        if src is None:
            return False
        import gui_preview
        sides = gui_preview.PREVIEW_TUBE_SIDES
        pd = vtk.vtkPolyData()
        if mode == "tubes" and src.GetNumberOfPoints() != len(rel) * sides:
            # the tube filter dropped a coincident point somewhere, so the
            # one-ring-per-point order is off: tube a copy that carries the
            # clock itself (the filter interpolates point data)
            lines = vtk.vtkPolyData()
            lines.ShallowCopy(lines_pd)
            lines.GetPointData().AddArray(self._clock_array(rel))
            tube = vtk.vtkTubeFilter()
            tube.SetInputData(lines)
            tube.SetRadius(max(meta.get("tube_diameter",
                                        meta["fiber_diameter"]) / 2.0, 1e-5))
            tube.SetNumberOfSides(sides)
            tube.Update()
            pd.ShallowCopy(tube.GetOutput())
        else:
            pd.ShallowCopy(src)
            pd.GetPointData().AddArray(self._clock_array(
                np.repeat(rel, sides) if mode == "tubes" else rel))
        self.fiber_mapper.SetInputData(pd)
        self._src = (meta, lines_pd)
        pm = self.panel._mapper
        self.fiber_mapper.SetScalarVisibility(pm.GetScalarVisibility())
        view = getattr(self.panel, "view_tools", None)
        if view is not None and view.paint_other(self.fiber_mapper, state):
            pass            # coloured by layer / speed / scaffold, as drawn
        elif pm.GetScalarVisibility():
            self.fiber_mapper.SetColorModeToDirectScalars()
            self.fiber_mapper.SetScalarModeToUsePointData()
        self.fiber.GetProperty().DeepCopy(self.panel._actor.GetProperty())
        self.state, self.mode = state, mode
        self.hide_picture()
        # the clipping range has to hold the jet's top at its highest,
        # wherever it goes over the print. The camera is put back on remove
        # (`fit_clipping_range` only ever pushes it out, which changes the
        # depth precision and so a few pixels of the picture)
        cam = self.panel._ren.GetActiveCamera()
        if self._cam_saved is None:
            self._cam_saved = (cam.GetPosition(), cam.GetClippingRange())
        lo, hi = meta.get("bbox") or ((0, 0, 0), (1, 1, 1))
        self._place(np.array(hi, float), np.array(hi, float), 0.02)
        self.panel._fit_clipping()
        self._cam_armed = (cam.GetPosition(), cam.GetFocalPoint(),
                           cam.GetViewUp(), cam.GetParallelScale())
        return True

    def _clock_array(self, values):
        from vtk.util import numpy_support as ns
        arr = ns.numpy_to_vtk(np.ascontiguousarray(values, np.float32),
                              deep=True)
        arr.SetName("mew_jet_t")
        return arr

    def stale(self):
        """True once the picture under the scene has changed: a new text
        landed, or the tubes toggle flipped. The page refills ONE state dict
        for every picture (`_gce_pv_show`), so the dict being the same says
        nothing: its meta and its polydata are what each new picture
        replaces."""
        st = self.panel._state
        if st is None or st is not self.state:
            return True
        meta, lines_pd = self._src
        return (st.get("meta") is not meta
                or st.get("lines_pd") is not lines_pd
                or st.get("display_mode") != self.mode)

    def hide_picture(self):
        """Hide what the scene stands in for: the picture's fiber, and the
        yellow selection, which would show a layer before it is laid."""
        p = self.panel
        hl = getattr(p, "_hl", None) or {}
        for a in [p._actor] + [hl[k][1] for k in ("fiber", "dots") if k in hl]:
            if a.GetVisibility():
                a.SetVisibility(0)
                if a not in self._hidden:
                    self._hidden.append(a)

    # -- a frame ----------------------------------------------------------- #
    def set_time(self, t):
        t = min(max(float(t), self.t_start), self.t_end)
        self._uniforms.SetUniformf("mewJetNow", float(t - self._t_base))
        contact, inside = self.fiber_track.at(t)
        if self.nozzle_track:
            nozzle, _ = self.nozzle_track.at(t)
        else:
            nozzle = contact
        if not inside:
            # between fibers (a travel the picture does not draw as fiber):
            # the jet hangs straight down to the commanded point
            contact = nozzle
        meta = self.state.get("meta") or {}
        if self.mandrel:
            reading, _ = self.rotary_track.at(t)
            # Player._collector_spin (gcode_player.py:4242) turns every
            # collector-borne actor by the positive A reading about +X.
            orientation = self._collector_spin(None, reading[3])
            self.fiber.SetOrientation(*orientation)
            for actor, _original in self._rotated:
                actor.SetOrientation(*orientation)
            contact = self.to_scene(contact)
            # The Design rod is centred at Z=0; the player's machine point is
            # measured from the surface datum (CylinderFrame.surface_z).
            frame = meta.get("frame")
            datum = float(getattr(frame, "surface_z", 0.0))
            nozzle = np.asarray(reading[:3], float) + np.array(
                [0.0, 0.0, float(meta["radius"]) - datum])
            if not inside:
                contact = nozzle
        d = float(meta.get("tube_diameter", meta.get("fiber_diameter"))
                  or 0.02)
        self._place(np.asarray(nozzle, float), np.asarray(contact, float), d)
        self.hide_picture()

    def to_scene(self, points):
        """Map collector-frame points through the displayed fibre actor."""
        matrix = self.fiber.GetMatrix()
        rotation = np.array([[matrix.GetElement(i, j) for j in range(3)]
                             for i in range(3)])
        offset = np.array([matrix.GetElement(i, 3) for i in range(3)])
        return np.asarray(points, float) @ rotation.T + offset

    def _px(self):
        """World mm per screen pixel (the panel's camera is parallel)."""
        cam = self.panel._ren.GetActiveCamera()
        h = max(self.panel._win.GetSize()[1], 1)
        return 2.0 * cam.GetParallelScale() / h

    def _place(self, nozzle, contact, fiber_d):
        px = self._px()
        tip = nozzle + np.array([0.0, 0.0, self.standoff])
        arc = jet_arc(tip, contact, self.segments)
        for i, q in enumerate(arc):
            self.jet_pts.SetPoint(i, q[0], q[1], q[2])
        self.jet_pts.Modified()
        self.jet_tube.SetRadius(max(0.5 * fiber_d, 0.5 * MIN_PX * px))

    def remove(self):
        """Take the scene out and put back what it hid."""
        ren = self.panel._ren
        for a in (self.fiber, self.jet):
            ren.RemoveActor(a)
        for a in self._hidden:
            a.SetVisibility(1)
        self._hidden = []
        for actor, orientation in self._rotated:
            actor.SetOrientation(*orientation)
        self._rotated = []
        # a selection made while playing restyled under the scene
        if getattr(self.panel, "_hl", None) is not None:
            self.panel._hl_restyle()
        cam = ren.GetActiveCamera()
        armed = getattr(self, "_cam_armed", None)
        if self._cam_saved is not None and armed == (
                cam.GetPosition(), cam.GetFocalPoint(), cam.GetViewUp(),
                cam.GetParallelScale()):
            # untouched while it played: exactly as it was
            cam.SetPosition(*self._cam_saved[0])
            cam.SetClippingRange(*self._cam_saved[1])
        else:
            self.panel._fit_clipping()


def _vtk():
    import gui_preview
    return gui_preview._VTK


class PlayButton(GlyphButton):
    """The bar's Play / Pause glyph. Greyed, with the reason in its tooltip,
    while Play cannot act (`set_why`)."""

    TIP = ("Play: watch the jet print this text in the picture, carrying "
           "on from the layer the slider is on (layer 0 is the empty "
           "collector). Press again to pause (Space). Drag the slider to go "
           "back to the picture.")

    def __init__(self, parent, command):
        super().__init__(parent, "▶", command, size=13)
        self._why = None
        self._tip = Tooltip(self, self.TIP)

    def set_why(self, why):
        self._why = why
        self.configure(state="disabled" if why else "normal")
        self.retheme()
        self._tip.text = self.TIP + ("\n\n" + why if why else "")

    def set_playing(self, on):
        self.configure(text="❚❚" if on else "▶")


class _GcodeJetMixin:
    """The page half: the one bar along the picture's foot (Play, the layer
    slider, the speed), Space / Esc, and the frame loop. Sits before
    `_GcodeEditorMixin` in the MRO; the bar's own place, grip and the layer
    window it steps belong to `gui_gcode_view._GcodeViewMixin`, which sits in
    front of this one."""

    def _build_gcode_editor_page(self, page):
        super()._build_gcode_editor_page(page)
        self._jet = None
        self._jet_after = None
        self._jet_playing = False
        self._jet_t = 0.0
        self._jet_speed = DEFAULT_SPEED
        self._jet_wall = None
        self._jet_by_scrub = False      # a stop the slider asked for
        self._jet_clicked = None
        self.jet_startup_s = None       # click -> first moving frame, seconds
        self.jet_frame_ms = []          # the last frames' tick-to-tick cost
        if getattr(self, "_gce_inspect", None) is None or \
                getattr(self, "_gce_pv_panel", None) is None:
            self._jet_btn = None
            self._jet_bar = None
            return
        # a child of the PAGE placed over the picture, as the tree box is: a
        # child of the picture label is painted over by every frame's blit
        self._jet_bar = self._jet_build_bar(self._gce_browser.master)
        self.root.bind_all("<KeyPress-Escape>", self._jet_key_esc, add="+")
        self.root.bind_all("<KeyPress-space>", self._jet_key_space, add="+")
        self._jet_refresh_why()

    # -- the bar ------------------------------------------------------------- #
    def _jet_build_bar(self, parent):
        """Play, the layer slider, 'Layer 7 / 20' with the print time, and the
        speed. The view mixin adds the grip and places it."""
        bar = ttk.Frame(parent, style="Bar.TFrame", padding=(3, 3, 8, 3),
                        borderwidth=1, relief="solid")
        self._jet_btn = self._jet_play_btn = PlayButton(bar,
                                                        self._jet_play_pause)
        self._jet_btn.pack(side="left")
        from gui_gcode_view import MoveScrub
        self._jet_scale = MoveScrub(bar, self._jet_on_layer)
        self._jet_scale.pack(side="left", fill="x", expand=True, padx=8)
        add_tip(self._jet_scale,
                "The layers shown, one step a layer. Drag it, or click it and "
                "use the arrow keys (Shift for ten at a time). The left end is "
                "layer 0, the empty collector; the right end shows the whole "
                "print. Play carries on from the layer it is on.")
        self._jet_time_var = tk.StringVar(value="")
        lab = ttk.Label(bar, textvariable=self._jet_time_var,
                        style="BarHint.TLabel", width=24, anchor="center")
        gui_form.mark(lab, "live")
        lab.pack(side="left")
        self._jet_slower = GlyphButton(bar, "−",
                                       lambda: self._jet_step_speed(-1),
                                       size=13)
        self._jet_slower.pack(side="left", padx=(10, 0))
        add_tip(self._jet_slower, "Slower. 1× is the print's own time.")
        self._jet_speed_var = tk.StringVar(value=fmt_speed(self._jet_speed))
        ttk.Label(bar, textvariable=self._jet_speed_var, width=5,
                  anchor="center", style="Bar.TLabel").pack(side="left")
        self._jet_faster = GlyphButton(bar, "+",
                                       lambda: self._jet_step_speed(1),
                                       size=13)
        self._jet_faster.pack(side="left")
        add_tip(self._jet_faster, "Faster. 1× is the print's own time.")
        return bar

    # -- whether the tool can act ------------------------------------------ #
    def _jet_why(self):
        """None when Play can act on the picture, else what stands in the
        way, in the user's words."""
        panel = getattr(self, "_gce_pv_panel", None)
        if panel is None or panel._win is None:
            return "The picture is not drawn yet."
        st = panel._state
        meta = (st or {}).get("meta") or {}
        if not st or not st.get("paths"):
            return "Nothing in the picture to play yet."
        if meta.get("ptimes") is None:
            return "This picture was thinned, so it has no print clock."
        if self._jet_is_mandrel() and not (meta.get("line_clock") or {}).get("rotary"):
            return "The mandrel's rotation clock is not ready yet."
        text = self.gce.get_text() if getattr(self, "gce", None) else ""
        if _ROTARY.search(text) and not self._jet_is_mandrel():
            return ("This text turns a rotary axis. Play needs a designed "
                    "mandrel: use the Simulation page for this file.")
        return None

    def _jet_is_mandrel(self):
        return self._gce_pv_is_tube()

    def _jet_refresh_why(self):
        b = getattr(self, "_jet_btn", None)
        if b is not None:
            b.set_why(None if self._jet is not None else self._jet_why())

    def _gce_pv_apply(self, paths, meta, err):
        super()._gce_pv_apply(paths, meta, err)
        self._jet_refresh_why()

    def _gce_pv_show(self):
        super()._gce_pv_show()
        scene = getattr(self, "_jet", None)
        if scene is not None and not self._jet_playing and scene.stale():
            # a new picture under a PAUSED jet (a new text, a preset, an eye):
            # no frame is queued to take it, so the old design's timed fiber
            # stayed over the new one. One frame re-arms it or stops.
            self._jet_frame()

    def _gce_on_show(self):
        super()._gce_on_show()
        self._jet_refresh_why()

    # -- start / stop ------------------------------------------------------ #
    def _jet_toggle(self):
        if self._jet is not None:
            self._jet_stop()
        else:
            self._jet_start()

    def _jet_start(self):
        why = self._jet_why()
        if why:
            self._jet_refresh_why()
            return
        self._jet_clicked = time.perf_counter()
        self.jet_startup_s = None
        self.jet_frame_ms = []
        panel = self._gce_pv_panel
        try:
            standoff = float(self._elevation_value())
        except Exception:  # noqa: BLE001 -- a half-typed field: the default
            standoff = 0.0
        if not standoff > 0.0:
            standoff = 2.0
        scene = JetScene(panel, standoff, mandrel=self._jet_is_mandrel())
        if not scene.arm(panel._state):
            scene.remove()
            self._jet_refresh_why()
            return
        self._jet = scene
        self._jet_t = min(max(self._jet_start_time(scene), scene.t_start),
                          scene.t_end)
        self._jet_btn.set_why(None)
        self._jet_show_speed()
        self._jet_set_playing(True)
        self._jet_frame()

    def _jet_start_time(self, scene):
        """Where Play begins on the print clock. The view mixin makes it the
        start of the layer the slider is on; here, the start of the print."""
        return scene.t_start

    def _jet_stop(self):
        scene, self._jet = self._jet, None
        if self._jet_after is not None:
            try:
                self.root.after_cancel(self._jet_after)
            except tk.TclError:
                pass
            self._jet_after = None
        self._jet_playing = False
        if scene is None:
            return
        scene.remove()
        self._jet_play_btn.set_playing(False)
        self._jet_refresh_why()
        scene.panel._render()

    def _jet_set_playing(self, on):
        scene = self._jet
        if on and scene is not None and self._jet_t >= scene.t_end:
            self._jet_t = scene.t_start          # play again from the start
        self._jet_playing = bool(on)
        self._jet_wall = time.perf_counter()
        self._jet_play_btn.set_playing(on)

    def _jet_play_pause(self):
        if self._jet is None:
            self._jet_start()
        else:
            self._jet_set_playing(not self._jet_playing)
            self._jet_frame()

    def _jet_step_speed(self, step):
        i = SPEEDS.index(self._jet_speed) if self._jet_speed in SPEEDS else 0
        self._jet_speed = SPEEDS[min(max(i + step, 0), len(SPEEDS) - 1)]
        self._jet_show_speed()

    def _jet_show_speed(self):
        self._jet_speed_var.set(fmt_speed(self._jet_speed))

    def _jet_on_layer(self, k):
        """The slider was moved to `k` layers laid (0 is the empty
        collector): pause, drop the jet and show the picture through that
        layer."""
        if self._jet is not None:
            self._jet_by_scrub = True
            try:
                self._jet_stop()
            finally:
                self._jet_by_scrub = False
        hook = getattr(self, "_view_set_layer", None)
        if hook is not None:
            hook(k)

    def _jet_readout(self, scene):
        """The bar while the jet runs: the slider follows the layer being laid
        and the label gives 'Layer 7 / 20' with the print time at the jet."""
        t = self._jet_t - scene.t_start
        total = scene.t_end - scene.t_start
        at = getattr(self, "_view_layer_at", None)
        n = len(getattr(self, "_view_layers", ()) or ())
        if at is None or not n:
            self._jet_time_var.set(f"{_fmt(t)} / {_fmt(total)}")
            return
        k = at(t)
        row = self._view_layers[k]
        span = max(row["t1"] - row["t0"], 1e-9)
        frac = min(max((t - row["t0"]) / span, 0.0), 1.0)
        self._jet_scale.set_progress(k + frac)
        self._jet_time_var.set(f"Layer {k + 1} / {n}  ·  {_fmt(t)}")

    # -- the loop ----------------------------------------------------------- #
    def _jet_frame(self):
        """One frame, and the next one scheduled while playing."""
        if self._jet_after is not None:
            try:
                self.root.after_cancel(self._jet_after)
            except tk.TclError:
                pass
            self._jet_after = None
        scene = self._jet
        if scene is None:
            return
        if not self._gce_page_shown():
            self._jet_stop()
            return
        if scene.stale():
            # the text changed under it (or the tubes toggle): take the new
            # picture and carry on at the same moment
            st = scene.panel._state
            if self._jet_why():
                self._jet_stop()
                return
            scene.remove()
            scene = JetScene(scene.panel, scene.standoff,
                             mandrel=self._jet_is_mandrel())
            if st is None or not scene.arm(st):
                scene.remove()
                self._jet = scene     # so _jet_stop finds the scene to drop
                self._jet_stop()
                return
            self._jet = scene
        now = time.perf_counter()
        if self._jet_playing:
            self._jet_t += (now - self._jet_wall) * self._jet_speed
            if self._jet_t >= scene.t_end:
                self._jet_t = scene.t_end
                self._jet_set_playing(False)
        self._jet_wall = now
        scene.set_time(self._jet_t)
        self._jet_readout(scene)
        scene.panel._render()
        done = time.perf_counter()
        if self.jet_startup_s is None and self._jet_clicked is not None:
            self.jet_startup_s = done - self._jet_clicked
        self.jet_frame_ms.append((done - now) * 1e3)
        del self.jet_frame_ms[:-120]
        if self._jet_playing:
            self._jet_after = self.root.after(TICK_MS, self._jet_frame)

    # -- keys ---------------------------------------------------------------- #
    def _jet_typing(self, e):
        try:
            return e.widget.winfo_class() in self._GCE_TYPING
        except (tk.TclError, AttributeError):
            return True

    def _jet_key_esc(self, e):
        if self._jet is None or not self._gce_page_shown():
            return None
        self._jet_stop()
        return "break"

    def _jet_key_space(self, e):
        if not self._gce_page_shown() or self._jet_typing(e):
            return None
        if self._jet is None:
            # a press on a focused button is its own; start only from the
            # picture or the slider
            focus = getattr(self, "_view_has_focus", None)
            if focus is None or not focus(e) or self._jet_why():
                return None
        self._jet_play_pause()
        return "break"

    def _on_tab_changed(self, _event=None):
        if getattr(self, "_jet", None) is not None \
                and not self._gce_page_shown():
            self._jet_stop()
        super()._on_tab_changed(_event)
