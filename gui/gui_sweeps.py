#!/usr/bin/env python3
"""gui_sweeps.py -- MODERN-UI ONLY (physics): the calibration PRINT engine.

This module owns no page and no picture. Calibrate is a mode of the CAD window
(`gui_gcode_calibrate`), the forms are `gui_calibration`'s, and this module keeps
what neither of those is:

  * `_SweepsMixin` -- the ENGINE wiring: the sw.* vars, the debounced worker
    (snapshot -> mew_sweeps.generate -> per-slide move extraction -> `_sw_panel`,
    the mode's stand-in for a preview), the protocol field builders the cards
    mount, the bottom-bar Generate/Save, and the "sweep" preset kind.
    "calibration" is a genuine INPUT mode, so **Generate** builds the slide on
    screen's sagged STL + .depo and feeds last_gcode/last_stl/last_depo, and
    **Save g-code as…** routes to _sw_export_gcode_dialog, because a sweep is
    N files.

Three protocols are designed here: the CTS line sweep (`kind` "cts"), the lag
circles (`kind` "circle") and the melt TEMPERATURE LADDER (`kind` "diam"). The ladder
is one print design serving TWO tabs -- `viscosity` and `activation_energy` -- because
the runbook's viscosity anchor is not a separate print but the hottest rung of the
temperature sweep (the calibration runbook written with the accompanying paper). Selecting
either tab points the shared bottom bar at the same generator and the same fields. The jet-lag SQUARES are hidden from the UI -- the `"lag"`
engine path is untouched, but no GUI field reaches it.
NOTHING the app already owns is duplicated here. The gap is the Physics page's
standoff, CTS its calibration anchor, the plate size and origin the Build-plate page's,
and the fiber diameter the scaffold designer's -- itself the physics-predicted value
whenever "Physics-deposited scaffold" is on, so a calibration print cannot be sized by
a diameter the same page's lag prediction disagrees with. Read at snapshot time.
Each is read at snapshot time, never copied into a field.
Robustness posture: NEVER erase -- adding speed ratios auto-splits onto more slides,
and a specimen too big for the plate is flagged explicitly
(mew_sweeps.SweepResult.oversize) rather than silently dropped. Mid-typing garbage
keeps the last good geometry, and engine ValueErrors and warnings surface in a
dedicated readout. A vtk failure degrades the preview to a text label; the form and
Save still work.
  * a vtk failure degrades the preview to a text label; the form + Save still work.
The engine (mew_sweeps.py) is a byte-identical port of the data-folder scripts, so
the emitted g-code + CSV match what the operator printed before.
"""
import inspect
import gui_prefs
import gui_units
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import types

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np

import gui_fonts
import gui_form
import gui_tips
import gui_widgets
import gui_preview
import mew_sweeps
import mew_circle_fit
from gui_common import (SESSION_DIR, DEPO_CACHE_EXT, HERE, IS_WINDOWS, WORKER_PIPE_UTF8,
                        SWEEP_PRESETS_DIR, SWEEP_KIND_PREFIX,
                        SAG_SOLVER_CHOICES, MESH_DETAIL_CHOICES,
                        SAG_RESOLUTION_CHOICES, SAG_RESOLUTION_VAL2LBL,
                        SAG_RESOLUTION_TIP)

SWEEP_DEBOUNCE_MS = 350
SWEEP_POLL_MS = 90
PSI_TO_PA = 6894.76

# preview colours (read on both viewport backdrops)
_DEFAULT_DEPO_HEX = "#cc4033"             # deposited test geometry
_TRAVEL_COLOR = (0.95, 0.62, 0.15)        # G0 hop strands
# the collector -- plate or rod -- in the Viewer's and the Scaffold designer's
# material (`gcode_player.PLATE_COLOR`, `gui_mandrel_preview`'s plate and rod),
# the same in both themes, as it is there
_COLLECTOR_RGB = (0.38, 0.45, 0.56)       # == gcode_player.PLATE_COLOR
_PLATE_EDGE_LIGHT = (0.35, 0.40, 0.48)
_PLATE_EDGE_DARK = (0.55, 0.58, 0.62)
_PLATE_SEL_COLOR = (0.18, 0.44, 0.84)     # accent edge for the targeted plate
_LABEL_LIGHT = (0.12, 0.13, 0.15)
_LABEL_DARK = (0.92, 0.93, 0.95)




def _finite(text):
    """float(text) for a number typed on this page, refusing "nan" / "inf".

    `float` parses both, and NaN passes every `> 0` / `<= 0` guard downstream,
    so a field left at "nan" raised out of a live readout's Tk callback or
    reached a generator. Raises ValueError like any other non-number, so each
    caller keeps its own fallback or its own named message."""
    val = float(text)
    if not math.isfinite(val):
        raise ValueError("%r is not a usable number" % text)
    return val

class _Fixed:
    """A stand-in for one Tk variable that reads a fixed value and ignores
    writes and traces -- one field of a preset, seen without loading it."""
    __slots__ = ("_v",)

    def __init__(self, value):
        self._v = value

    def get(self):
        return self._v

    def set(self, _value):
        pass

    def trace_add(self, *_a, **_k):
        return ""

    def trace_remove(self, *_a, **_k):
        pass


class _RigView:
    """The app, with some of its Tk variables reading fixed values and some of
    its methods answered by the caller -- a machine PRESET seen through the
    page's own code, without loading it into the page.

    WHY NOT LOAD IT AND PUT THE PAGE BACK. Setting the Machine page's fields
    fires their traces, and the calibration preview is rebuilt from traces: a
    rebuild that loads a preset schedules the next rebuild. Reading through
    this view runs the SAME methods (`_rotary_rod`, `_rotary_surface_z`,
    `_machine_args`, ...) on the preset's values and writes nothing to the
    page. A method is looked up on the app's class and bound to the VIEW, so
    whatever it calls next reads the view's values too."""

    def __init__(self, app, fixed, methods):
        self.__dict__.update(_app=app, _fixed=fixed, _methods=methods)

    def __getattr__(self, name):
        d = self.__dict__
        if name in d["_methods"]:
            return d["_methods"][name]
        if name in d["_fixed"]:
            return d["_fixed"][name]
        app = d["_app"]
        fn = inspect.getattr_static(type(app), name, None)
        if isinstance(fn, types.FunctionType):
            return types.MethodType(fn, self)
        return getattr(app, name)


def view_with(app, values, methods=None):
    """A `_RigView` of `app` with every Tk var whose id() is a key of `values`
    reading that value instead -- including the vars the page keeps inside
    dicts and tuples (the motion limits, the custom chain's axes). Nothing is
    written to the page."""

    def sub(obj, depth=0):
        """`obj` with every var in `values` swapped, and whether any was."""
        if id(obj) in values:
            return _Fixed(values[id(obj)]), True
        if depth < 2 and isinstance(obj, (tuple, list)):
            parts = [sub(x, depth + 1) for x in obj]
            if any(c for _p, c in parts):
                return type(obj)(p for p, _c in parts), True
        elif depth < 2 and isinstance(obj, dict):
            parts = {k: sub(v, depth + 1) for k, v in obj.items()}
            if any(c for _p, c in parts.values()):
                return {k: p for k, (p, _c) in parts.items()}, True
        return obj, False

    fixed = {}
    for attr, obj in list(vars(app).items()):
        new, changed = sub(obj)
        if changed:
            fixed[attr] = new
    return _RigView(app, fixed, methods or {})


class _SweepsMixin:
    """The engine wiring behind Calibrate, and the STL/sim actions. Mounted by
    `gui_gcode_calibrate` when the CAD window builds (physics)."""

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    def _sw_mount(self, panel):
        """Start the engine plumbing, reporting to `panel`: the CAD window's
        stand-in for a preview panel (`gui_gcode_calibrate._Picture`). The
        FORM is gui_calibration's; this owns only the debounced
        generate->extract->apply pipeline behind it."""
        self._sw_vars_make()
        self._sw_panel = panel

        # engine plumbing (the scaffold preview's proven shape, single-state)
        self._sw_state = self._sw_new_state()
        self._sw_results = queue.Queue()
        self._sw_cv = threading.Condition()
        self._sw_mailbox = None            # (gen, kind, params, physics) latest-only
        self._sw_worker = None
        self._sw_pending = 0
        self._sw_poll_id = None
        self._sw_after = None
        self._sw_result = None             # last good mew_sweeps.SweepResult
        self._sw_prints = []               # per-print {depo, commanded, travels}
        self._sw_plate = None              # (ox, oy, w, h) of the last result
        self._sw_diam = 0.015
        self._sw_build_busy = False
        self._sw_dark_traced = False
        self._sw_install_traces()
        return panel

    def _sw_trace_dark(self):
        """Retheme the panel when dark mode flips (the dark_mode var may not
        exist at build time -- the theme mixin creates it later, so retry from
        _sw_on_panel_mapped until it lands, exactly like the scaffold preview)."""
        if self._sw_dark_traced:
            return
        var = getattr(self, "dark_mode", None)
        if var is None:
            return
        try:
            var.trace_add("write", lambda *_a: self._sw_panel.retheme())
            self._sw_dark_traced = True
        except tk.TclError:
            pass

    @staticmethod
    def _sw_new_state():
        s = gui_preview._PreviewMixin._pv_new_state()
        s.update(travels=None, travel_pd=None, plates=None, plate_sel=0,
                 labels=None)
        return s

    # ------------------------------------------------------------------ #
    # vars + persistence
    # ------------------------------------------------------------------ #
    def _sw_vars_make(self):
        if hasattr(self, "sw_vars"):
            return
        v = {}

        def S(key, val):
            v[key] = tk.StringVar(value=val)

        def B(key, val):
            v[key] = tk.BooleanVar(value=val)

        S("kind", "cts")
        # Which Calibration-page TAB is showing (a calibration_procedures key).
        # It lives here, in the sw.* key group, so a Print History reproduce
        # reopens the page on the tab its print came from. No per-tab preset
        # carries it (`_sweep_tab_keys`). It selects no geometry -- see
        # _SW_NONGEOM, which keeps a tab switch from re-running the print build.
        S("cal_tab", "cts")
        # CTS line sweep
        S("cts_start", "600");   S("cts_stop", "240");  S("cts_step", "60")
        S("cts_reps", "3");      S("cts_line_len", "");  S("cts_pitch", "1.5")
        S("cts_col_gap", "5.0"); S("cts_base_speed", ""); S("cts_ref", "450")
        B("cts_bottom_up", False)
        B("cts_slowest_first", True)
        # linking loops between the lines: the Scaffold designer's Turnaround
        # radius and Loop speed, with its defaults, behind a switch that is on
        # as Design's linking loops are (gui_scaffold `link_loops_on`). Off is
        # the straight hop, the engine's byte-identical default.
        B("cts_loops_on", True)
        S("cts_loop_arc", "1.5"); S("cts_loop_speed", "")
        # the CTS ladder on the MANDREL -- the "CTS (4-axis)" tab's print: the
        # same ladder and the same reading, laid as lines along the mandrel axis
        # (`mew_sweeps.generate_cts_mandrel`, paper section 2.4.6). Its own keys,
        # like every other tab's, so designing one ladder never moves the other.
        # Line length and gap default to a ladder that fits the default 4-axis
        # rod (7 speeds x 5 mm + 7 x 1.5 mm = 45.5 mm of its 48) without the
        # shrink-to-fit. `gui_presets._migrate_legacy_cts_collector` maps the
        # old one-tab keys (`cts_collector`, `cts_m_line_len`, `cts_m_gap`).
        S("cts4_start", "600");  S("cts4_stop", "240");  S("cts4_step", "60")
        S("cts4_reps", "3");     S("cts4_pitch", "1.5")
        S("cts4_line_len", "5"); S("cts4_gap", "1.5")
        # (`cts_lines_dx/dy` and `cts_base_dx/dy` were the Positioning page's manual
        #  CTS-block offsets. That page is gone, so nothing could write
        #  them -- and a persisted knob no UI can reach is worse than no knob: an old
        #  preset carrying a non-zero value would silently shift a calibration print
        #  with nothing on screen to explain it. Removed. The ENGINE keeps
        #  `CtsParams.lines_offset` / `base_offset` at (0, 0) (mew_sweeps.py:175-176),
        #  emitted bytes are unchanged.)
        # the melt TEMPERATURE LADDER -- the ACTIVATION ENERGY tab's print. The
        # keys stay `diam_*` rather than `ea_*`: they are read by users' saved
        # calibration presets, and the Ea fit's own measurement rows already own
        # the `ea_` prefix. Pressure, gap, plate, origin and fiber diameter are NOT fields
        # here: each is a condition the app already owns and is read at snapshot
        # time (`sim_pressure`, `_sw_gap`, `_sw_plate_size`, `_sw_origin`,
        # `_sw_diam_mm`). The ladder, the one fixed feed and the replicate count
        # are genuinely new -- the app holds a single `sim_temperature`, never a
        # ladder, and no other page owns a calibration print feed.
        S("diam_temps", "100 92.5 85 77.5 70")
        S("diam_speed", "700");   S("diam_reps", "5")
        S("diam_line_len", "");   S("diam_pitch", "1.5")
        S("diam_col_gap", "5.0"); S("diam_base_speed", "")
        B("diam_bottom_up", False)
        # the VISCOSITY plate -- ONE slide at ONE temperature, and its own keys.
        # It shares the ladder's generator (a one-rung ladder IS this plate) but
        # NOT its variables: the two quantities are recalibrated on different
        # clocks -- viscosity weekly, because the melt ages, and the activation
        # energy once per polymer batch -- so a viscosity run must not disturb
        # the ladder the Ea tab has designed, or be disturbed by it.
        # Defaults are the ladder's anchor rung.
        S("visc_temp", "100")
        S("visc_speed", "700");   S("visc_reps", "5")
        S("visc_line_len", "");   S("visc_pitch", "1.5")
        S("visc_col_gap", "5.0"); S("visc_base_speed", "")
        B("visc_bottom_up", False)
        # the SAG LADDER -- a row of walls whose spacing steps wider along X,
        # crossed by two bridging fibres that droop into every gap at once.
        # Its own keys, like every other protocol's: the ladder is the only
        # print on the page whose design variable is a LENGTH list rather than
        # a speed or a temperature, and nothing else on the page owns one.
        # Defaults are the tentative sag calibration procedure's (drafted
        # for the accompanying paper) and are expected to
        # move once the first ladder has been printed and looked at.
        S("sag_spans", "0.25 0.35 0.5 0.7 1.0 1.4 2.0")
        B("sag_mirror", True)
        S("sag_wall_len", "3.0"); S("sag_layers", "20")
        S("sag_lane", "middle");  S("sag_visit", "2")
        S("sag_inset", "0.75");   S("sag_lead", "5.0")
        S("sag_speed", "700");    S("sag_col_gap", "5.0")
        S("sag_base_speed", "")
        # the mandrel LAG TUBE (4-axis) -- the "Lag radii (4-axis)" tab's print:
        # one meander band per winding angle. NO CTS ladder: the mandrel's CTS
        # belongs on the CTS tab behind a 3/4-axis switch (planned, not built).
        # The mandrel diameter, the rod and its datums, the gap and the fibre
        # diameter are NOT fields here: each is read at snapshot time from
        # where it lives (the Scaffold designer's 4-axis mandrel, the DEFAULT
        # 4-axis machine's rotary group via `_sw_rig_view`, the Physics page),
        # exactly as the plate prints read theirs. Defaults are the engine's
        # (`mew_sweeps.DEFAULT_TUBE_ANGLES`, `LagTubeParams`): 20/45/70 plus the
        # 90 check, 13 mm bands 1 mm apart, which is 48 mm of tube and fits the
        # shipped 4-axis machine's 50 mm rod. The paper's section 2.4.6 set
        # (0/20/45/70/90, 20 mm bands) needs about 80 mm. The spacing is 1 mm,
        # not the paper's 2. `tube_lines` blank is
        # "as many as fit once around". Every band is `tube_band_len` long:
        # the separate check-band length (`tube_null_len`) is gone, and a
        # preset still carrying it drops it, which is the right failure.
        S("tube_angles", "20 45 70 90")
        S("tube_spacing", "1.0"); S("tube_lines", "")
        S("tube_band_len", "13"); S("tube_band_gap", "1.0")
        S("tube_speed", "900")
        S("tube_settle", "2")
        # WHERE ON THE ROD a mandrel calibration print sits (the lag tube and the
        # 4-axis CTS ladder alike): which end the offset is measured from, and
        # how far. This page's own, not the Scaffold designer's -- a calibration
        # print does not move when a scaffold design does. The rod itself is the
        # default 4-axis machine's (`_sw_rig_view`), and the print shrinks to
        # the room these leave on it (`mew_sweeps._fit_to_rod`).
        S("rod_place", "left"); S("rod_offset", "2")
        # lag CIRCLES -- the flat lag-AMPLITUDE calibration. CTS, gap, plate size, plate
        # origin and fiber diameter are NOT fields here: each is a condition the app
        # already owns, so there is exactly one source of truth for each.
        S("circ_eps", "2 3 4 6")
        S("circ_reps", "2");      S("circ_reps_eps", "")
        # THE SPECIMEN TABLE, as one string (mew_sweeps.specs_to_str). BLANK is the
        # default and means "no table": the engine takes the circ_eps / circ_reps /
        # circ_reps_eps path above and lays a byte-identical plate. Non-blank, it is the
        # WHOLE truth and those three are ignored.
        #
        # ONE key rather than a key per row, because the row count is a user edit: a
        # per-row key set could not grow, and an old preset with a different row count
        # would leave orphans. Blank -> `specimens=None`, never an empty list.
        S("circ_specimens", "")
        S("circ_lr", str(mew_circle_fit.DEFAULT_LR))
        S("circ_layout", "nested")
        # The KEY changed with the meaning (it was `circ_k_prior`, a multiplier
        # defaulting to 1). A preset written before that carries the old key,
        # which the persist map now does not know and drops, so the sizing falls
        # back to this default -- the right failure. Keeping the key would have
        # read a stored 1.0 as an amplitude and sized every circle 2.9x small.
        S("circ_amp_prior", "%g" % mew_circle_fit.A_DEFAULT)
        S("circ_fixed_radius", "0"); S("circ_floor", "0")
        S("circ_spacing", "3.0"); S("circ_travel_offset", "1.0")
        S("circ_layers", "1");    S("circ_stab_eps", "1.5")
        S("circ_chord_tol", "0.001")
        S("circ_lead_mult", "2.0"); S("circ_lead_min", "2.0")
        # plate layout knobs with no home elsewhere. `margin` is a sweep-layout
        # inset, and `max_feed` is a machine ceiling the app genuinely does not
        # hold: machine_model carries no velocity limit at all (only the
        # accel-planner's max_accel / square-corner velocity, gui_sim.py:290),
        # so this is an original field, not a duplicate.
        S("margin", "4.0")
        S("max_feed", "")        # machine max translation feed (mm/min); blank = none
        # fiber + stabilization scaffold
        B("stab_on", True)
        S("stab_w", "10.0");     S("stab_h", "10.0");   S("stab_layers", "5")
        S("stab_stepover", "1.7"); S("stab_arc", "1.5")
        # output: only the CSV toggle is page-local; the destination folder +
        # basename come from the shared "Save g-code as…" dialog (bottom bar),
        # which batch-writes every plate -- no in-form Folder/Basename fields.
        B("write_csv", False)
        # EXPORT and QUALITY: this page's own copies of the Scaffold designer's
        # Export / Quality rows that mean something for a calibration print
        # (`_sw_build_output`, `_sw_build_quality`). Own keys, not the
        # designer's vars, for the reason the fiber colour and diameter
        # multiplier are this page's: a calibration print does not change when a
        # scaffold export setting does, so none is read from the designer with
        # nothing on this page showing it (the tube's exit move, the
        # travel-in-STL box). Defaults are the designer's, except the sag
        # solver, which stays "exact" -- what this page always ran.
        B("exit_move", True);       S("exit_margin", "5")
        B("g2g3", False)
        B("travel_as_g1", True);    S("gcode_convention", "GX")
        B("travel_in_stl", False);  B("travel_gcode_line", False)
        S("faces", "12");           S("sag_mode", "exact")
        B("sag_reset", False);      S("sag_reset_layers", "20")
        S("sag_resolution", "1")
        S("mesh_tol", "1.0")
        self.sw_vars = v
        # display toggles + colour (panel + traces read these through the app)
        self.sw_show_plate = tk.BooleanVar(value=True)
        # two independent path sets. `sw_show_gcode` = the commanded toolpath
        # overlay (_gc_actor), still a toggle. `sw_show_depo` drove the fiber
        # actor until its "Deposited" checkbox was dropped (see
        # `_sw_build_preview_options`); it survives ONLY as a persisted key, so
        # presets on disk and the persist-map parity guard keep working, and
        # nothing reads it any more.
        self.sw_show_depo = tk.BooleanVar(value=True)
        self.sw_show_gcode = tk.BooleanVar(value=False)
        self.sw_show_travel = tk.BooleanVar(value=True)
        self.sw_show_labels = tk.BooleanVar(value=True)
        self.sw_show_tubes = tk.BooleanVar(value=False)
        # the preview's OWN Jet lag box: hides the touchdown reshape from this
        # page's picture only; the Physics page's Jet lag switch is the export's
        self.sw_show_lag = tk.BooleanVar(value=True)
        self.sw_color = tk.StringVar(value=_DEFAULT_DEPO_HEX)
        # Visualization only: scales every drawn fiber's diameter (the preview,
        # the Viewer and the STL tubes), after the print is laid and sagged.
        self.sw_diam_mult = tk.StringVar(value="1")
        self.sw_print_index = tk.IntVar(value=1)

    # the write-csv toggle never changes geometry; nor do the display toggles
    # (they restyle) -- everything else rebuilds
    _SW_NONGEOM = frozenset({"write_csv", "cal_tab",
                             # export spelling and the converter's quality:
                             # none of them moves a commanded point
                             "travel_as_g1", "gcode_convention",
                             "travel_in_stl", "travel_gcode_line", "faces",
                             "sag_mode", "sag_reset", "sag_reset_layers",
                             "sag_resolution", "mesh_tol"})

    def _sweeps_persist_entries(self):
        """sw.* persist keys (the gui_core default returns {}). These form their
        OWN preset kind ("sweep", shown as "Calibration presets"; see
        _sweep_persist_keys), so a calibration print's design is saved/loaded
        independently of the scaffold / machine / physics presets."""
        self._sw_vars_make()
        m = {f"sw.{k}": var for k, var in self.sw_vars.items()}
        m.update({"sw.show_plate": self.sw_show_plate,
                  "sw.show_depo": self.sw_show_depo,
                  "sw.show_gcode": self.sw_show_gcode,
                  "sw.show_travel": self.sw_show_travel,
                  "sw.show_labels": self.sw_show_labels,
                  "sw.show_tubes": self.sw_show_tubes,
                  "sw.show_lag": self.sw_show_lag,
                  "sw.color": self.sw_color,
                  "sw.diam_mult": self.sw_diam_mult})
        return m

    # -- the "sweep" preset KIND (parity with scaffold/machine/calibration) --- #
    def _sweep_persist_keys(self):
        """Every sw.* key: the page's whole key GROUP. It is what the scaffold
        kind subtracts (so no sw.* key ever rides a scaffold preset) and what
        Print History snapshots as its "sweep" group. The PRESETS are per tab
        -- kind "sweep:<tab>", `_sweep_tab_keys` -- and hold a subset."""
        return list(self._sweeps_persist_entries())

    #: print protocol -> the sw.* key prefixes of its own design fields. Every
    #: protocol's keys carry their own prefix (`_sw_vars_make`), so a new
    #: field joins its tab's presets by being named like its neighbours.
    #: "cts_" does not match "cts4_".
    _SW_PROTOCOL_PREFIXES = {
        "cts": ("cts_",), "cts4": ("cts4_",), "visc": ("visc_",),
        "diam": ("diam_",), "circle": ("circ_",), "sag": ("sag_",),
        "tube": ("tube_",),
    }
    #: shared print section (a `calibration_procedures.SHARED_CARDS` id) -> the
    #: sw.* keys it edits. A tab's preset takes the sections that tab SHOWS,
    #: because they are part of the print it saves: a plate tab reprinted from
    #: a preset needs its base pad back too.
    _SW_SHARED_KEYS = {
        "plate_feed_limit": ("margin", "max_feed"),
        "stabilization_base": ("stab_on", "stab_w", "stab_h", "stab_layers",
                               "stab_stepover", "stab_arc"),
    }
    #: the rod rows (where on the rod a mandrel print sits) belong to both
    #: mandrel protocols' presets.
    _SW_ROD_KEYS = ("rod_place", "rod_offset")

    def _sweep_preset_tabs(self):
        """[(tab key, print protocol)], in page order, of the Calibration tabs
        that keep presets: every tab with a print. A fit-only tab (Fusion)
        hides the presets card with the rest of the print design."""
        return [(k, p.print_protocol)
                for k, p in self._cal_procedures().items() if p.print_protocol]

    def _sweep_tab_keys(self, tab):
        """The sw.* persist keys tab `tab`'s presets hold: its protocol's own
        design fields, the shared sections it shows, and the rod rows on a
        mandrel tab.

        Left out on purpose, and so in NO tab's preset: `kind` and `cal_tab`
        (which print and which tab are the folder a preset lives in, not
        something it sets), the Export card's CSV toggle (a choice of output,
        not of print), and the preview's display toggles and colour (how the
        page draws, shared by every tab, so a preset of one tab must not
        repaint the others)."""
        proc = self._cal_procedures().get(tab)
        proto = getattr(proc, "print_protocol", None)
        prefixes = self._SW_PROTOCOL_PREFIXES.get(proto)
        if not prefixes:
            return []
        shown = getattr(proc, "shared_cards", None) or ()
        extra = set()
        for card, keys in self._SW_SHARED_KEYS.items():
            if card in shown:
                extra.update(keys)
        if proto in self._SW_ROTARY_KINDS:
            extra.update(self._SW_ROD_KEYS)
        self._sw_vars_make()
        return ["sw." + k for k in self.sw_vars
                if k.startswith(prefixes) or k in extra]

    # short, human column headings for the sweep preset comparison grid (the flat
    # grid falls back to the raw key otherwise). Only the fields worth eyeballing
    # across presets get a heading; the rest still appear under their key.
    _SWEEP_GRID_LABELS = {
        "sw.kind": "Print", "sw.margin": "Edge margin",
        "sw.cts_start": "CTS fastest", "sw.cts_stop": "CTS slowest",
        "sw.cts_step": "CTS step", "sw.cts_reps": "CTS lines",
        "sw.cts4_start": "CTS 4-axis fastest",
        "sw.cts4_stop": "CTS 4-axis slowest",
        "sw.cts4_step": "CTS 4-axis step",
        "sw.rod_place": "Position on mandrel", "sw.max_feed": "Printer max feed",
        "sw.stab_on": "Base", "sw.circ_eps": "Circle ε ladder",
        "sw.circ_reps": "Circle reps", "sw.circ_lr": "Lag/radius",
        "sw.circ_amp_prior": "Prior A", "sw.circ_layout": "Circle layout",
        "sw.circ_specimens": "Circle table",
        "sw.diam_temps": "Temp ladder", "sw.diam_speed": "Ladder feed",
        "sw.diam_reps": "Lines per temp",
        "sw.visc_temp": "Viscosity temp", "sw.visc_speed": "Viscosity feed",
        "sw.visc_reps": "Viscosity lines",
        "sw.sag_spans": "Sag spans", "sw.sag_lane": "Sag travel lane",
        "sw.sag_layers": "Sag wall layers",
        "sw.tube_angles": "Tube angles", "sw.tube_spacing": "Tube spacing",
        "sw.tube_lines": "Tube lines per band",
        "sw.tube_speed": "Tube print speed",
    }

    def _sweep_grid_labels(self):
        """The grid headings: the curated ones above, else the field's own
        label on the page (`_SW_FIELD_LABELS`), so a per-tab grid never
        heads a column with a raw key. A per-tab grid needs no "CTS"/"Tube"
        prefix to tell its columns apart."""
        labels = {"sw." + k: v for k, v in self._SW_FIELD_LABELS.items()}
        labels.update(self._SWEEP_GRID_LABELS)
        return labels

    def _after_sweep_preset_applied(self):
        """Re-sync after a calibration preset loads: rebuild the preview from the
        new fields. There is no section to swap any more -- each print protocol
        lives inside its own calibration card, and `sw.kind` only says which one
        the bottom bar's Generate targets."""
        if getattr(self, "_sw_panel", None) is not None:
            self._sw_on_edit()

    def _build_sweep_presets_tab(self, parent):
        """The Calibration page's Presets section, labelled "Calibration presets":
        ONE preset store PER TAB, kind "sweep:<tab>", each in its own subfolder
        `presets/calibration/<tab>/`, each capturing only that tab's print
        (`_sweep_tab_keys`). A preset saved on the lag tube never resets the CTS
        ladder.

        Per-tab KINDS rather than one store with a filter, because everything
        a store keeps -- its folder, its default pointer, its table, its
        selection -- is per tab, and the shared preset UI already keys every
        one of those on the kind: each tab's box is the SAME grid and buttons as
        the machine/physics boxes, built once per tab, and only the showing
        tab's box is gridded (`_sw_show_presets_tab`).

        The rig + material profile is NOT this box: it is kind "calibration", the
        Physics page's "Physics presets" (see the table at the top of gui_presets.py)."""
        parent.columnconfigure(0, weight=1)
        self._sw_preset_frames = {}
        for tab, _proto in self._sweep_preset_tabs():
            kind = SWEEP_KIND_PREFIX + tab
            self._init_pstore(kind, os.path.join(SWEEP_PRESETS_DIR, tab),
                              "sweep_default:" + tab, "calibration preset")
            fr = ttk.Frame(parent)
            fr.grid(row=0, column=0, sticky="nsew")
            name = self._cal_procedures()[tab].tab
            self._build_one_presets_tab(fr, kind, "Presets of the %s type." % name,
                                       repeat_ok=True)
            fr.grid_remove()
            self._sw_preset_frames[tab] = fr

    def _sw_preset_table(self, tab=None):
        """The preset grid of tab `tab` (default: the tab showing), or None."""
        if tab is None:
            var = getattr(self, "_cal_tab_var", None)
            tab = var.get() if var is not None else None
        st = getattr(self, "_pstores", {}).get(SWEEP_KIND_PREFIX + str(tab))
        return st.get("table") if st else None

    def _sw_show_presets_tab(self, tab):
        """Grid tab `tab`'s preset box and hide the others. Called by the page
        on every tab switch; a tab without presets shows none (its card is
        hidden anyway)."""
        for key, fr in getattr(self, "_sw_preset_frames", {}).items():
            if key == tab:
                fr.grid()
            else:
                fr.grid_remove()
        self._sw_refresh_card_summaries()

    def _sw_link_g2g3(self):
        """Whether the printer interpolates arcs is ONE setting: Klipper
        without [gcode_arcs] refuses G2, from any page. This form's box and
        Setup's (`s_g2g3`, the Machine preset's) are kept equal both ways,
        Setup's winning at start, so either box, a preset or Reproduce sets
        both and every print follows it."""
        make = getattr(self, "_g2g3_var_make", None)
        if make is None:
            return
        make()
        mine, design = self.sw_vars["g2g3"], self.s_g2g3

        def copy(src, dst):
            def _f(*_a):
                try:
                    val = bool(src.get())
                except tk.TclError:
                    return
                if bool(dst.get()) != val:
                    dst.set(val)
            return _f
        mine.set(bool(design.get()))
        mine.trace_add("write", copy(mine, design))
        design.trace_add("write", copy(design, mine))

    def _sw_install_traces(self):
        self._sw_link_g2g3()
        for key, var in self.sw_vars.items():
            if key in self._SW_NONGEOM:
                continue
            var.trace_add("write", lambda *_a: self._sw_on_edit())
        # sw_show_depo is deliberately absent: nothing draws from it any more.
        for var in (self.sw_show_plate, self.sw_show_gcode,
                    self.sw_show_travel, self.sw_show_labels):
            var.trace_add("write", lambda *_a: self._sw_refresh_display())
        self.sw_show_tubes.trace_add(
            "write", lambda *_a: self._sw_refresh_tubes())
        self.sw_color.trace_add("write", lambda *_a: self._sw_on_color())
        self.sw_diam_mult.trace_add(
            "write", lambda *_a: self._sw_on_diam_mult())
        self.sw_print_index.trace_add(
            "write", lambda *_a: self._sw_on_select())
        # the g-code view shows what Save g-code as writes (`_sw_emit_opts`)
        refresh = getattr(self, "_refresh_gcode_display_on_toggle", None)
        if callable(refresh):
            for key in ("travel_as_g1", "gcode_convention"):
                self.sw_vars[key].trace_add("write", lambda *_a: refresh())
        # physics conditions live in the Printhead-Simulation / Calibration tabs;
        # when one of those changes, rebuild THIS page's preview so it tracks
        # (gap, CTS, the physics-deposit toggle). Guarded -- some vars are created
        # by other mixins that may build after this one.
        for name in ("sim_physics_deposit", "sw_show_lag", "sim_sag_law",
                     "sim_standoff", "sim_voltage",
                     "sim_pressure", "sim_temperature", "sim_physical_sag",
                     "sim_physical_fusion"):
            var = getattr(self, name, None)
            if var is not None:
                try:
                    var.trace_add("write", lambda *_a: self._sw_on_edit())
                except (AttributeError, tk.TclError):
                    pass

    # ------------------------------------------------------------------ #
    # the form pieces the Calibration page's cards mount
    # ------------------------------------------------------------------ #
    # There is no page-level form any more. A card owns the knobs for ITS print
    # protocol and nothing else; everything common to both protocols is mounted once
    # by the page, below the cards.

    def _sw_build_protocol(self, parent, protocol, key=None):
        """Mount `protocol`'s knobs into a card's Process settings. "cts",
        "circle", "diam" or "visc". `key` is the Calibration tab they belong to,
        which is what lets each sub-section header say which Instructions step it
        is.

        "diam" (the Activation Ea ladder) and "visc" (the viscosity plate) run the
        SAME generator -- a viscosity plate is a one-rung ladder -- but they are
        separate protocols over separate variables, because the two quantities are
        recalibrated on different clocks and neither run may disturb the other's
        design. "diam" was mounted on both cards until then."""
        if protocol == "circle":
            self._sw_build_protocol_circle(parent, key)
        elif protocol == "diam":
            self._sw_build_protocol_diam(parent, key)
        elif protocol == "visc":
            self._sw_build_protocol_visc(parent, key)
        elif protocol == "sag":
            self._sw_build_protocol_sag(parent, key)
        elif protocol == "tube":
            self._sw_build_protocol_tube(parent, key)
        elif protocol == "cts4":
            self._sw_build_protocol_cts4(parent, key)
        else:
            self._sw_build_protocol_cts(parent, key)

    def _sw_build_protocol_tube(self, parent, key=None):
        """The mandrel lag tube's knobs: paper section 2.4.6's parameters, in
        the words the rest of the app uses ("Print speed", mm/min).

        The mandrel itself is not here. Its diameter is the Scaffold designer's
        4-axis mandrel, the gap is the Physics page's printhead elevation, and
        the rod and its datum are the default 4-axis machine's (`_sw_rig_view`);
        where the print sits on that rod is the two rod rows at the bottom. The
        fit reads all of them from the same places, so nothing is typed twice.
        The mandrel CTS is shown, not edited: its one home is `m_cts`, which
        the CTS (4-axis) tab's Apply writes."""
        F = gui_form.Form(parent)
        self._cal_form_section(
            F, key, "tube_bands", "Tube design",
            info="One band of parallel lines per winding angle, each line "
                 "printed the opposite way to its neighbour. The lag pushes "
                 "each line to one side, so the gaps between them alternate "
                 "wide and narrow. The 0° and 90° bands are checks: there the "
                 "gaps should come out equal.")
        # a live verdict, one var per MOUNT, as the sag card keeps its own
        note = tk.StringVar(value="")
        self._sw_tube_notes = getattr(self, "_sw_tube_notes", [])
        self._sw_tube_notes.append(note)
        F.live(None, textvariable=note, wrap=430)
        self._sw_grid_rows(F, [
            ("Winding angles", "tube_angles", "°",
             "One band per angle, printed in this order from the near end. "
             "0 is lines along the mandrel, 90 is rings; 0 and 90 are checks.",
             "text"),
            ("Line spacing", "tube_spacing", "mm",
             "Distance between neighboring lines, measured across them. "
             "Wider keeps the narrow gaps from closing up."),
            ("Lines per band", "tube_lines", "",
             "Blank prints as many lines as fit once around the mandrel; type "
             "a number to print exactly that many."),
            ("Band length", "tube_band_len", "mm",
             "How far each fitted band runs along the mandrel; the jet needs "
             "this length to settle after each turn. The 0° and 90° checks "
             "are a fixed %g mm: they have nothing to settle to."
             % mew_sweeps.CHECK_BAND_LEN),
            ("Gap between bands", "tube_band_gap", "mm",
             "Bare mandrel between neighboring bands, so you can tell them "
             "apart under the microscope."),
            ("Print speed", "tube_speed", "mm/min",
             "The surface speed every band is printed at. The fitted lag "
             "radii hold at this speed."),
            ("Lead-in turns", "tube_settle", "turns",
             "Turns of the mandrel printed before the first band, so the jet "
             "is steady when the first band starts."),
        ] + self._sw_rod_rows(shrinks=False))
        self._sw_rod_note_line(F, "tube")
        mcts = tk.StringVar(value="")
        self._sw_tube_cts_notes = getattr(self, "_sw_tube_cts_notes", [])
        self._sw_tube_cts_notes.append(mcts)
        F.live(None, textvariable=mcts, wrap=430)
        self._sw_update_tube_cts()

    def _sw_tube_cts_text(self):
        """The read-only mandrel CTS line: the value the CTS (4-axis) tab's
        Apply wrote to `m_cts` (mm/s, its one home), shown in mm/min like every
        speed on this page -- or, blank, the flat CTS the model falls back on."""
        text = getattr(self, "m_cts", None)
        text = text.get().strip() if text is not None else ""
        try:
            v = float(text) if text else None
        except ValueError:
            v = None
        if v is not None and v > 0.0:
            return ("Mandrel CTS: %.0f mm/min, measured with “CTS (mandrel)”."
                    % (v * 60.0))
        try:
            flat = "%.0f mm/min" % (float(self._sw_resolve_cts()) * 60.0)
        except Exception:  # noqa: BLE001 -- a readout, never a blocker
            flat = "the flat CTS"
        return ("Mandrel CTS: not measured yet, so the flat CTS (%s) is used. "
                "Measure it with the “CTS (mandrel)” type. Its Apply fills this in."
                % flat)

    def _sw_update_tube_cts(self):
        for var in getattr(self, "_sw_tube_cts_notes", []):
            var.set(self._sw_tube_cts_text())
        # the line follows the value it reads, wherever it is written from
        if not getattr(self, "_sw_tube_cts_traced", False):
            src = getattr(self, "m_cts", None)
            if src is not None:
                src.trace_add("write", lambda *_a: self._sw_update_tube_cts())
                self._sw_tube_cts_traced = True

    def _sw_build_protocol_sag(self, parent, key=None):
        """The sag ladder's knobs: the span list, the walls that make the gaps and
        the two fibres that bridge them.

        "Spans (mm)" is the wall CENTRELINE pitch, not the clear gap, and its
        tooltip says so -- an operator who reads it as the clear gap measures every
        droop at a span one fibre diameter off. The fibre diameter, the plate, its
        origin and the gap are read from the pages that own them at snapshot time,
        exactly as on the other protocol cards."""
        F = gui_form.Form(parent)
        self._cal_form_section(
            F, key, "sag_ladder", "Spans",
            info="The walls this print lays, and the gaps between them. Each "
                 "span is the distance between two neighboring wall centers, "
                 "not the open gap. The open gap is one fiber narrower. Two "
                 "fibers are laid across the whole row and sag into every "
                 "gap at once, so one print gives sag against span.")
        # One var per MOUNT, never one attribute -- see `_sw_build_protocol_diam`.
        # A live verdict (which spans the droop law says are measurable), so it
        # stays on screen rather than folding into a tooltip.
        note = tk.StringVar(value="")
        self._sw_sag_notes = getattr(self, "_sw_sag_notes", [])
        self._sw_sag_notes.append(note)
        F.live(None, textvariable=note, wrap=430)
        # The feed is mm/MIN here and the fit reads mm/S, the same crossing the
        # CTS card carries, so the unit is in the label on both sides.
        self._sw_grid_rows(F, [
            ("Spans", "sag_spans", "mm",
             "Wall center to wall center, narrowest first, separated by "
             "spaces. This is not the open gap, which is one fiber narrower.",
             "text"),
            ("Print each span twice", "sag_mirror", "",
             "Lay the span list forwards, then backwards, so every span is "
             "printed twice."),
            ("Wall length", "sag_wall_len", "mm",
             "How long each wall runs, per bridging fiber."),
            ("Wall height", "sag_layers", "layers",
             "Taller walls let the wide spans sag without touching down."),
            ("Travel lane", "sag_lane", "",
             "Where every gap-crossing travel fiber is put, so the bridging "
             "fiber can be seen side-on.", [
                 ("middle", "middle, 2 fibers"),
                 ("edge", "one end, 1 fiber")]),
            ("Layers per wall visit", "sag_visit", "",
             "Layers laid before the head hops to the next wall. Even, and a "
             "divisor of the wall height."),
            ("Fiber inset", "sag_inset", "mm",
             "How far each bridging fiber sits inboard of the outer wall ends. "
             "Jet lag leaves a wall short of its commanded end, so keep it "
             "well clear."),
            ("Lead-in / lead-out", "sag_lead", "mm",
             "Bridging fiber laid either side of the row."),
            ("Print feed", "sag_speed", "mm/min",
             "The one feed the whole ladder is printed at."),
            ("Stabilization scaffold → ladder gap", "sag_col_gap", "mm",
             "The distance from the stabilization scaffold to the ladder."),
            ("Stabilization scaffold feed", "sag_base_speed", "mm/min",
             "The feed the stabilization scaffold is printed at. Blank uses the "
             "print feed."),
        ])

    def _sw_build_protocol_visc(self, parent, key=None):
        """The viscosity plate's knobs: ONE slide at ONE temperature.

        Every caption here is singular. The ladder card next door says
        "Temperatures", "one slide each" and "Lines per temperature"; on this card
        there is one temperature and one slide, and a plural caption would be an
        instruction to design a print this tab does not lay."""
        F = gui_form.Form(parent)
        self._cal_form_section(
            F, key, "visc_conditions", "Temperature",
            info="The single nozzle temperature this plate is printed at. It "
                 "becomes ONE slide of identical lines. The temperature is not "
                 "in the g-code. You set it on the printer before the print, so "
                 "the fit is told it separately.")
        # One var per MOUNT, never one attribute: see `_sw_build_protocol_diam`.
        note = tk.StringVar(value="")
        self._sw_visc_notes = getattr(self, "_sw_visc_notes", [])
        self._sw_visc_notes.append(note)
        F.live(None, textvariable=note, wrap=430)
        self._sw_grid_rows(F, [
            # "Print temperature", not "Temperature": the fit box lower down the
            # SAME card is labelled "Temperature (°C)", and one card carrying two
            # fields of that name is a step no operator can follow.
            ("Print temperature", "visc_temp", "°C",
             "The one temperature this plate is printed at."),
            ("Print feed", "visc_speed", "mm/min",
             "The one feed this plate is printed at, which the fit is told."),
            ("Lines on the slide", "visc_reps", "",
             "Replicate fibers to measure."),
            ("Line length", "visc_line_len", "mm",
             "How long each line runs. Blank fills the slide to its far edge; "
             "20 mm is the minimum."),
            ("Line pitch", "visc_pitch", "mm",
             "The spacing between neighboring lines."),
            ("Stabilization scaffold → lines gap", "visc_col_gap", "mm",
             "The distance from the stabilization scaffold to the first line."),
            ("Stabilization scaffold feed", "visc_base_speed", "mm/min",
             "The feed the stabilization scaffold is printed at. Blank uses the "
             "print feed."),
            ("First line at bottom", "visc_bottom_up", "",
             "Lay the lines upwards instead of downwards."),
        ])

    def _sw_build_protocol_diam(self, parent, key=None):
        """The temperature ladder's knobs. One rung per slide; the operator sets the
        nozzle between prints, so no temperature word is emitted."""
        F = gui_form.Form(parent)
        self._cal_form_section(
            F, key, "diam_ladder", "Temperature ladder",
            info="The nozzle temperatures to print at, hottest first. Each one "
                 "becomes its OWN slide of identical lines. The temperature is "
                 "not in the g-code. You set it on the printer between prints. "
                 "Print slide 1 first: it runs on the freshest melt.")
        # A LIST, not an attribute holding one var. This protocol is mounted on
        # TWO cards, so the second build used to overwrite the attribute and leave
        # the first card's label bound to a variable nothing wrote to any more --
        # the Viscosity card rendered the line BLANK while the Activation Ea card
        # rendered it correctly, which is the same class of bug the page's
        # `_init_calibration_vars` docstring records. Every mounted label gets its
        # own var and `_sw_update_diam_readout` writes all of them.
        note = tk.StringVar(value="")
        self._sw_diam_notes = getattr(self, "_sw_diam_notes", [])
        self._sw_diam_notes.append(note)
        F.live(None, textvariable=note, wrap=430)
        # The feed is mm/MIN here and the fits read mm/S, the same crossing the CTS
        # card carries, so the unit is in the label on both sides.
        self._sw_grid_rows(F, [
            ("Temperatures", "diam_temps", "°C",
             "The nozzle temperatures, hottest first, separated by spaces. "
             "Each one gets its own slide.", "text"),
            ("Print feed", "diam_speed", "mm/min",
             "One feed for every rung. The fit needs it held fixed."),
            ("Lines per temperature", "diam_reps", "",
             "Replicate lines on each rung's slide."),
            ("Line length", "diam_line_len", "mm",
             "How long each line runs. Blank fills the slide to its far edge; "
             "20 mm is the minimum."),
            ("Line pitch", "diam_pitch", "mm",
             "The spacing between neighboring lines."),
            ("Stabilization scaffold → lines gap", "diam_col_gap", "mm",
             "The distance from the stabilization scaffold to the first line."),
            ("Stabilization scaffold feed", "diam_base_speed", "mm/min",
             "The feed the stabilization scaffold is printed at. Blank uses the "
             "print feed."),
            ("First line at bottom", "diam_bottom_up", "",
             "Lay the lines upwards instead of downwards."),
        ])

    def _sw_build_protocol_cts(self, parent, key=None):
        """The "CTS (3-axis)" tab's knobs: the paper's section 2.4.2 ladder on a
        slide. Every speed is mm/min, the app's unit for a print speed; the one
        mm/s on the tab is the Physics page's, converted inside Apply."""
        F = gui_form.Form(parent)
        self._cal_form_section(
            F, key, "cts_feeds", "Speed ladder",
            info="The speeds the slide prints. CTS is read off "
                 "the printed result, so these decide what you will be able "
                 "to read.")
        self._sw_grid_rows(F, self._sw_cts_ladder_rows("cts_", "slide") + [
            ("Line length", "cts_line_len", "mm",
             "How long each line runs. Blank fills the slide to its far edge."),
            ("Line spacing", "cts_pitch", "mm",
             "The distance between neighboring lines."),
            ("Stabilization scaffold to first line", "cts_col_gap", "mm",
             "The distance from the stabilization scaffold to the first line."),
            ("Stabilization scaffold print speed", "cts_base_speed", "mm/min",
             "The speed the stabilization scaffold is printed at. Blank uses the "
             "fastest speed."),
            ("Expected CTS", "cts_ref", "mm/min",
             "Written into the measurement CSV beside each speed, for your "
             "notes. It does not change the print."),
            ("Start at slowest speed", "cts_slowest_first", "",
             "Print the slowest speed first and end on the fastest. Untick to "
             "start on the fastest speed instead."),
            ("First line at bottom", "cts_bottom_up", "",
             "Lay the lines upwards instead of downwards."),
        ])
        self._sw_build_cts_loops(F)

    def _sw_build_cts_loops(self, F):
        """Linking loops between the CTS lines: Design's Turnaround radius and
        Loop speed, the same names, units and defaults, under a switch. The
        loop is the designer's own turnaround (`mew_sweeps._line_move`)."""
        F.section("Linking loops", first=False)
        self._sw_grid_rows(F, [
            ("Join each line to the next with a loop", "cts_loops_on", "",
             "Replace the straight hop from one line's end to the next "
             "line's start with the Scaffold designer's turnaround loop, out "
             "past the line ends. The lines are shortened so the loops stay "
             "on the slide."),
        ])
        self._sw_grid_rows(F, [
            ("Turnaround radius", "cts_loop_arc", "× spacing",
             "The loop's radius as a multiple of the line spacing. 1.5 = "
             "default, as in Design."),
            ("Loop speed", "cts_loop_speed", "mm/min",
             "Feed rate for the loops. Leave blank to use the speed of the "
             "line before each loop."),
        ], indent=1)
        on = self.sw_vars["cts_loops_on"]

        def sync(*_a):
            st = "normal" if on.get() else "disabled"
            for key in ("cts_loop_arc", "cts_loop_speed"):
                w = self._sw_field_widgets.get(key)
                if w is not None and w[1] is not None:
                    try:
                        w[1].configure(state=st)
                    except Exception:  # noqa: BLE001
                        pass
        on.trace_add("write", sync)
        sync()

    def _sw_cts_ladder_rows(self, p, where):
        """The four ladder rows both CTS tabs share, over prefix `p`. The words
        are the paper's section 2.4.2 and the app's "Print speed"."""
        return [
            ("Fastest speed", p + "start", "mm/min",
             "The first and fastest speed on the %s. Set it safely above the "
             "CTS you expect." % where),
            ("Slowest speed", p + "stop", "mm/min",
             "The last and slowest speed on the %s. Set it safely below the "
             "CTS you expect." % where),
            ("Speed step", p + "step", "mm/min",
             "How much the speed drops from one group of lines to the next. "
             "60 brackets CTS coarsely; 10 is Hrynevich's fine pass."),
            ("Lines per speed", p + "reps", "",
             "Identical lines printed at each speed. One wobbling line rules "
             "that speed out. Use 4 for the fine pass."),
        ]

    def _sw_build_protocol_cts4(self, parent, key=None):
        """The "CTS (4-axis)" tab's knobs: the same section 2.4.2 ladder, laid
        as short lines along the mandrel axis (paper section 2.4.6). The mandrel
        and the printhead elevation are not fields here: they have one home each
        (Scaffold designer, Physics page), and the live line under the rows says
        what this print will read from them."""
        F = gui_form.Form(parent)
        self._cal_form_section(
            F, key, "cts4_feeds", "Speed ladder",
            info="The speeds the mandrel prints, fastest first, each as a few "
                 "short lines along the mandrel axis.")
        self._sw_grid_rows(F, self._sw_cts_ladder_rows("cts4_", "mandrel") + [
            ("Line length", "cts4_line_len", "mm",
             "How far each line runs along the mandrel."),
            ("Line spacing", "cts4_pitch", "mm",
             "The distance between neighboring lines, measured around the "
             "mandrel."),
            ("Gap between speeds", "cts4_gap", "mm",
             "Bare mandrel between one speed's lines and the next, so you "
             "can tell the speeds apart."),
        ] + self._sw_rod_rows())
        var = tk.StringVar(value="")
        self._sw_cts4_on_var = var
        F.live(None, textvariable=var, wrap=430)
        for name in ("m_diameter", "sim_standoff"):
            src = getattr(self, name, None)
            if src is not None:
                src.trace_add("write", lambda *_a: self._sw_cts4_on_refresh())
        self._sw_cts4_on_refresh()
        self._sw_rod_note_line(F, "cts4")

    def _sw_cts4_on_refresh(self):
        """"Printed on the 3 mm mandrel ..., at 4.5 mm printhead elevation" --
        what the mandrel ladder reads from the pages that own those values."""
        var = getattr(self, "_sw_cts4_on_var", None)
        if var is None:
            return
        gap = getattr(self, "sim_standoff", None)
        gap = gap.get().strip() if gap is not None else ""
        var.set("Printed on the %g mm mandrel set on the Scaffold designer, "
                "at the “Nozzle–collector gap” on the Physics page (%s mm)."
                % (self._sw_mandrel_diameter(), gap or "?"))

    def _sw_rod_note_line(self, F, kind):
        """The plain line under a mandrel print's rod rows: what the rod did to
        the print (shrunk it to fit, left a band too short to settle).

        It sits in the tab's own settings card, beside the rows that cause it,
        and NOT in the page's red warning strip: on a fixed rod a shrink is the
        expected outcome, and the strip's red, dot-joined wall read as an error
        nobody could parse. One var per mount and per kind, so the tube's note
        never lands on the CTS card (`_sw_render_warnings`)."""
        var = tk.StringVar(value="")
        self._sw_rod_notes = getattr(self, "_sw_rod_notes", {})
        self._sw_rod_notes.setdefault(kind, []).append(var)
        F.live(None, textvariable=var, wrap=430)

    #: `rod_place` keys, as the Scaffold designer words its own choice
    #: (`gui_mandrel.PLACEMENTS`, the one table)
    _SW_ROD_PLACES = tuple((k, label) for k, label, _s in
                           __import__("gui_mandrel").PLACEMENTS)

    def _sw_rod_rows(self, mandrel_only=False, shrinks=True):
        """The two rows that put a mandrel calibration print on its rod,
        shared by the lag tube and the 4-axis CTS ladder (one rod, one
        placement). `shrinks` False for a print laid as designed however long
        it is (the lag tube), which the tooltip then says instead."""
        pre = "Mandrel only: " if mandrel_only else ""
        fit = ("a print longer than the room left on it is shortened to fit, "
               "every line and gap together, and the page says so."
               if shrinks else
               "a print longer than the mandrel is not shortened; the page says "
               "how much mandrel it needs.")
        return [
            ("Position on mandrel", "rod_place", "",
             pre + "Where the print sits along the mandrel. The mandrel is the "
             "default 4-axis printer's (Printer & collector, 4-axis), and its "
             "length is fixed: " + fit, list(self._SW_ROD_PLACES)),
            ("End offset", "rod_offset", "mm",
             pre + "Bare mandrel left at the end named above, so the first fiber "
             "lands on the collector. Ignored when centered, where the print "
             "takes the middle of the mandrel."),
        ]

    def _sw_build_protocol_circle(self, parent, key=None):
        F = gui_form.Form(parent)
        self._cal_form_section(
            F, key, "conditions", "Conditions",
            info="What the plate about to be laid is conditional on: the gap, "
                 "the believed CTS and the fiber diameter, each read live from "
                 "the page that owns it.")
        self._sw_circ_cts_var = tk.StringVar(value="")
        F.live(None, textvariable=self._sw_circ_cts_var, wrap=430)

        # -- the specimen table --------------------------------------------- #
        # One row per circle the plate will carry, with the radius the sizing rule lands
        # on SHOWN and editable. It replaced three universal fields, which could only
        # say the same thing about every specimen, so the plate you were about to print
        # was a calculation you could not see. Those three sw.* keys still exist and
        # still drive the engine while the table string is blank, which is what keeps
        # the default plate byte-identical.
        self._cal_form_section(
            F, key, "specimens", "Specimens",
            info="One row per circle the plate will carry: its speed ratio ε, "
                 "how many replicates, and the radius the sizing rule will "
                 "command for it.")
        tbl_tip = (
            "One row per speed ratio. Start at ε = 2, not near CTS. The lag's "
            "sensitivity to the believed CTS falls steeply with ε. A 5 % CTS "
            "error moves the fitted k by 21.7 % at ε 1.25, 6.5 % at 2 and "
            "2.1 % at 6, so the low rungs cost precision instead of buying "
            "it. The top rung is capped by the plate: the radius grows with "
            "the lag, so ε 8 costs a whole extra slide.\n\n"
            "The Radius column shows what the sizing rule below will command "
            "for that row. Type over it to pin that one ring to a radius of "
            "your own, for example to reproduce a plate you have already "
            "printed. It then stops following the lag/radius dial. “Reset to law sizing” "
            "gives the whole table back to the rule.")
        self._sw_circ_tbl = gui_form.mark(ttk.Frame(F.frame), "table")
        self._sw_circ_tip = tbl_tip
        for c, text in enumerate(("Speed ratio ε", "Reps", "Radius (mm)")):
            head = gui_form.mark(ttk.Label(self._sw_circ_tbl, text=text),
                                 "colhead")
            head.grid(row=0, column=c, padx=(0, gui_fonts.SP["xs"]), sticky="w")
            gui_tips.add_tip(head, tbl_tip)
        F.slot(None, self._sw_circ_tbl)
        F.frame.columnconfigure(3, weight=1)   # the table's extra width, not col 0/1
        self._sw_circ_rows = []
        self._sw_circ_busy = False
        btns = gui_form.mark(ttk.Frame(F.frame), "group")
        for i, (text, cmd, tip) in enumerate((
                ("Add row", self._sw_circ_add_specimen,
                 "Add a specimen one rung above the highest ratio."),
                ("Reset to law sizing", self._sw_circ_reset_table,
                 "Give every radius back to the sizing rule below."))):
            b = gui_form.mark(ttk.Button(btns, text=text, command=cmd), "button")
            b.pack(side="left", padx=((gui_fonts.SP["s"] if i else 0), 0))
            gui_tips.add_tip(b, tip)
        F.slot(None, btns)

        self._cal_form_section(
            F, key, "specimen_size", "Specimen size",
            info="The sizing rule the Radius column follows: how big a circle "
                 "each speed ratio gets, and the prior amplitude that sizing "
                 "assumes.")
        self._sw_grid_rows(F, [
            ("Lag / radius ratio", "circ_lr", "",
             "The design dial: R = lag ÷ this. Smaller makes the ε-flatness "
             "check work.\n\n"
             "Every error in the recovered A is a function of this one number. "
             "What sets it is not A, though. It is whether the ε-flatness check "
             "works at all. That check has a floor, because the placement rule "
             "trends across ε on its own, and the floor has to sit below the "
             "CTS error you are trying to catch. It is +5.7 % per decade at 0.25 "
             "against +9.3 % for a CTS 5 % low, but +18.9 % against +10.5 % at "
             "0.45, where the check is blind. The crossover is near 0.33. 0.25 "
             "is affordable only because the rings nest. Flat, it costs six "
             "slides."),
            ("Prior amplitude A", "circ_amp_prior", "",
             "The A the sizing assumes. It is a prior, not a result.\n\n"
             "The radius is sized from a PRIOR amplitude, because you do not "
             "know A yet. That is what the print measures. 2.8629 is "
             "Hrynevich's own 10.02/3.5, which at a 3.5 mm gap is a definition "
             "rather than a measurement. The design degrades gracefully if the "
             "printer's real A is anywhere from half to twice that."),
            ("Fixed radius", "circ_fixed_radius", "mm",
             "0 sizes every circle from the lag. Above 0 uses this radius "
             "verbatim."),
            ("Minimum radius", "circ_floor", "mm", "0 means no floor."),
        ])

        # "No nesting" was one of nine named protocol-preset chips. It was never
        # a protocol -- it is this one layout choice, and hiding it behind a chip
        # meant the only way to reach it was to load a preset that also rewrote
        # every other circle field. It is a checkbox, bound straight to the
        # engine's layout value (on = "chain", off = "nested"). Its help carries
        # the Instructions step line, so the row's tooltip stands in for the dot
        # the registry audit reads (`_cal_tip_note`).
        nest = F.check("No nesting — one separate circle per specimen",
                       self.sw_vars["circ_layout"], "",
                       onvalue="chain", offvalue="nested")
        self._cal_tip_note(key, "no_nesting", nest, (
            "Off (the default) the reps nest as rings about one target, which "
            "packs 2–3× tighter and is what makes a small lag/radius ratio "
            "affordable. On, each specimen is its own circle on a tangent "
            "baseline. It is slower on the plate, but every specimen is "
            "physically apart from the others."))

        csp = self._cal_section_disc(
            parent, key, "spacing_lead_in", "Spacing, lead-in & base speed",
            info=("Secondary knobs: gaps between circles, how travels route "
                  "around them, how straight run-up each row gets, the arc "
                  "sampling, and the stabilization-base speed."))
        F.slot(None, csp, sticky="ew")
        self._sw_grid_rows(gui_form.Form(csp.body), [
            ("Specimen spacing", "circ_spacing", "mm",
             "The gap between neighboring circles."),
            ("Travel clearance", "circ_travel_offset", "mm",
             "Hops route around specimens by this much. 0 goes straight "
             "through."),
            ("Lead-in ×lag", "circ_lead_mult", "",
             "The straight run-up for the first circle of a row, in lag "
             "lengths.\n\n"
             "It barely matters. With the entry sector excluded from the fit, "
             "even half a lag-length leaves under 5 µm of bias. Nested, it "
             "costs plate, because the outermost ring's run-up is the only "
             "thing reaching outside the stack and so sets its footprint."),
            ("Lead-in minimum", "circ_lead_min", "mm",
             "The shortest run-up, whatever the lag."),
            ("Arc tolerance", "circ_chord_tol", "mm",
             "How finely the lap is sampled. A coarse arc reads as "
             "amplitude."),
            ("Layers per circle", "circ_layers", "",
             "1 is a single fiber. A stacked lap is not a second "
             "measurement."),
            ("Stabilization scaffold speed ×CTS", "circ_stab_eps", "",
             "The stabilization scaffold's speed, as a multiple of CTS. Keep it "
             "above 1."),
        ])

    #: the shared print-design sections: section id -> (title, the text the ⓘ
    #: keeps under its "Step N of the Instructions" line). ONE set of widgets
    #: serves every tab, so the step line is retargeted per tab
    #: (`_CalibrationMixin._cal_place_shared`) rather than baked in here.
    #:
    #: Since the module grid each has its own home: plate & feed limit is the
    #: foot of the Calibration settings card, the stabilization scaffold and the
    #: export options are cards of their own, and the preview options are the
    #: preview panel's header row -- beside the picture they change, the same
    #: place the Scaffold designer keeps its own.
    _SW_SHARED_SECTIONS = (
        ("plate_feed_limit", "Plate & feed limit",
         "Where the calibration print is laid, and an optional printer feed "
         "limit. The plate SIZE and ORIGIN are the Printer & collector "
         "page's. Only the layout margin and the feed ceiling are set here."),
        ("stabilization_base", "Base",
         "A small dense pad printed first so the jet is steady before the test "
         "specimens start."),
        ("preview_options", "Preview options",
         "What the live preview draws: plate outline, the deposited fiber, "
         "the commanded g-code path, travel fibers, ε/feed labels, tubes and "
         "color. Picture only. Nothing here reaches a g-code file."),
        ("export_options", "Export",
         "What Save g-code as… writes, and how: every slide's g-code with "
         "the end-of-print exit, arcs and the travel and height spelling "
         "set here, plus the measurement-CSV template when it is ticked. The "
         "lag-circle tab reads that file back. It also sets which slide the "
         "bottom bar's Generate builds."),
    )

    def _sw_section(self, sid):
        """(title, info) of one shared section."""
        for s, title, info in self._SW_SHARED_SECTIONS:
            if s == sid:
                return title, info
        raise KeyError(sid)

    def _sw_add_print_cards(self, grid, plate_host):
        """The shared print-design sections, laid into the Calibration page's
        module grid: Plate & feed limit at the foot of the Calibration settings
        card (`plate_host`), then the Stabilization scaffold and Export cards.

        Built ONCE and on screen on every tab, the way the Process-settings
        disclosures they replaced were: a fit-only tab leaves the bottom bar
        pointed at the last print tab, so these still describe what Generate
        builds there. Each ⓘ is registered in `_sw_shared_dots`, which is what
        lets the page retarget its step line when the tab changes."""
        self._sw_shared_dots = getattr(self, "_sw_shared_dots", {})

        # -- Plate & feed limit: a sub-section of Calibration settings -------- #
        # A second Form under the tab's own, so `first=False`: the heading needs
        # the same air above it as any other section of the card.
        title, info = self._sw_section("plate_feed_limit")
        F = gui_form.Form(plate_host)
        head = F.section(title, first=False, info=info, title=title)
        self._sw_shared_dots["plate_feed_limit"] = (head.dot, title, info)
        self._sw_plate_var = tk.StringVar(value="")
        F.live(None, textvariable=self._sw_plate_var, wrap=430,
               tip="The plate is the Printer & collector page's; the diameter is the "
                   "Scaffold designer's (physics-predicted while the Physics "
                   "page's Fiber diameter switch is on).")
        self._sw_grid_rows(F, [
            ("Edge margin", "margin", "mm",
             "The layout inset from the plate edge."),
            ("Printer max feed", "max_feed", "mm/min",
             "Blank means no limit. When a specimen's feed exceeds it, the "
             "page warns.\n\n"
             "The printer model carries acceleration limits but no velocity "
             "ceiling, so this is the only place the app knows one. It only "
             "produces a warning, never a clamp."),
        ])
        # No Appearance rows: the fibre's colour is the ribbon's Color tool,
        # as in Design, and Design has no drawing-only diameter multiplier.

        # -- Stabilization scaffold ----------------------------------------- #
        # The base's size rows belong to its "Print a base" toggle, so they are
        # indented under it -- the one thing an indent means.
        title, info = self._sw_section("stabilization_base")
        self._sw_stab_summary = tk.StringVar(value="")
        stab = grid.add("stabilization", title, info=info,
                        status_var=self.sw_vars["stab_on"],
                        summary=self._sw_stab_summary)
        self._sw_shared_dots["stabilization_base"] = (stab.info_dot, title, info)
        _fr, SF = gui_form.framed(stab.body, 0)
        self._sw_grid_rows(SF, [
            ("Print a stabilization scaffold", "stab_on", "",
             "Print a small dense pad first, so the jet is steady before the "
             "specimens start."),
        ])
        self._sw_grid_rows(SF, [
            ("Stabilization scaffold width", "stab_w", "mm", "The pad's width."),
            ("Stabilization scaffold height", "stab_h", "mm", "The pad's height."),
            ("Stabilization scaffold layers", "stab_layers", "", "Layers laid in the pad."),
            ("Stabilization scaffold stepover", "stab_stepover", "mm",
             "The distance between neighboring passes of the pad. Default "
             "1.7."),
            ("Arc × spacing", "stab_arc", "",
             "The turnaround radius, as a multiple of the fiber spacing."),
        ], indent=1)

        # -- Export --------------------------------------------------------- #
        title, info = self._sw_section("export_options")
        self._sw_export_summary = tk.StringVar(value="")
        self._sw_count_var = tk.StringVar(value="of 1")
        self._sw_count_var.trace_add(
            "write", lambda *_a: self._sw_refresh_card_summaries())
        slide_info = ("Every slide is on screen at once, each its own row in "
                      "the tree. You print each slide on its own microscope "
                      "slide. Save g-code as… writes one file per slide.")
        self._cal_page_dot("target_slide", self._cal_slide_note, "Slides",
                           slide_info)
        export = grid.add("export", title, info=info,
                          summary=self._sw_export_summary)
        self._sw_shared_dots["export_options"] = (export.info_dot, title, info)
        _fr, EF = gui_form.framed(export.body, 0)
        self._sw_build_output(EF)

        # -- Quality -------------------------------------------------------- #
        # A card of its own, as on the Scaffold designer. Its dot is the
        # card's own help, not an Instructions step: no procedure step is about
        # how finely the STL is meshed.
        self._sw_quality_summary = tk.StringVar(value="")
        quality = grid.add("quality", "Quality", summary=self._sw_quality_summary,
                           info=("How good the slide's STL and simulation are: "
                                 "the tube's facets, the sag solver and the "
                                 "mesh detail. Nothing here changes the "
                                 "g-code."))
        _fr, QF = gui_form.framed(quality.body, 0)
        self._sw_build_quality(QF)

        for key in ("stab_on", "stab_w", "stab_h", "stab_layers", "write_csv",
                    "faces", "sag_mode", "exit_move", "g2g3"):
            self.sw_vars[key].trace_add(
                "write", lambda *_a: self._sw_refresh_card_summaries())
        self.sw_print_index.trace_add(
            "write", lambda *_a: self._sw_refresh_card_summaries())
        self._sw_refresh_card_summaries()

    def _sw_add_presets_card(self, grid):
        """The "Calibration presets" card. `experiment_presets` is the id and
        "sweep" the engine's preset kind; this box saves the calibration PRINT
        the page designs. The rig + material profile has its own box on the
        Physics page, "Physics presets"."""
        info = ("Save this tab’s calibration print as a named preset and "
                "reload it any time. Each tab keeps its own presets, so saving "
                "or loading one never changes another tab’s print. Each is its "
                "own file in the tab’s subfolder of “presets/calibration”; use "
                "Export / Import to share one. Select a preset’s row (or "
                "double-click it to load) and use the buttons.")
        self._sw_presets_summary = tk.StringVar(value="")
        # the one WIDE tile, as on the Scaffold designer, Physics and Machine
        # pages: its table wants the full pane
        pre = grid.add("presets", "Calibration presets", info=info, wide=True,
                       summary=self._sw_presets_summary)
        self._cal_page_dot("experiment_presets", pre.info_dot,
                           "Calibration presets", info)
        self._build_sweep_presets_tab(pre.body)
        for tab, _proto in self._sweep_preset_tabs():
            tree = self._sw_preset_table(tab)
            if tree is not None:
                gui_widgets.scroll_guard(tree)
        self._sw_refresh_card_summaries()

    def _sw_build_warnings(self, body, r):
        """The engine's warnings, under the grid and on screen in BOTH of its
        states -- a specimen dropped off the plate must not hide behind a closed
        card. Returns the next free grid row.

        `_sw_summary_var` (the engine's paragraph about the print) keeps no label
        any more; it was the third copy of what the form and the tiles say. It
        is still written on every finished build, and that write is what
        refreshes the tile summaries."""
        self._sw_summary_var = tk.StringVar(value="")
        self._sw_warn_var = tk.StringVar(value="")
        self._sw_warn_lbl = gui_form.mark(
            ttk.Label(body, textvariable=self._sw_warn_var, style="Hint.TLabel",
                      justify="left", wraplength=440, foreground="#c0392b"),
            "live")
        self._sw_warn_lbl._form_warn = True
        self._sw_warn_lbl.grid(row=r, column=0, sticky="w",
                               pady=(gui_fonts.SP["xs"], 0))
        r += 1
        self._sw_update_cts_readout()
        return r

    def _sw_refresh_card_summaries(self, *_a):
        """The one-line tile readouts of the three cards this mixin owns."""
        try:
            if getattr(self, "_sw_stab_summary", None) is not None:
                v = self.sw_vars
                if v["stab_on"].get():
                    self._sw_stab_summary.set(
                        "%s × %s mm · %s layers" % (v["stab_w"].get(),
                                                     v["stab_h"].get(),
                                                     v["stab_layers"].get()))
                else:
                    self._sw_stab_summary.set("Not printed")
            if getattr(self, "_sw_export_summary", None) is not None:
                count = getattr(self, "_sw_count_var", None)
                n = count.get().replace("of", "").strip() if count else "1"
                csv = ("G-code + measurement CSV"
                       if self.sw_vars["write_csv"].get() else "G-code")
                if self.sw_vars["g2g3"].get() and \
                        self.sw_vars["kind"].get() not in self._SW_ROTARY_KINDS:
                    csv += " · arcs"
                # a mandrel print is one print: no slide to name
                if self.sw_vars["kind"].get() in self._SW_ROTARY_KINDS:
                    self._sw_export_summary.set(csv)
                else:
                    k = int(n or 1)
                    self._sw_export_summary.set(
                        "%s · %d slide%s" % (csv, k, "s" * (k != 1)))
            if getattr(self, "_sw_quality_summary", None) is not None:
                lbl = {val: name for name, val in SAG_SOLVER_CHOICES}
                self._sw_quality_summary.set("%s facets · %s sag" % (
                    self.sw_vars["faces"].get(),
                    lbl.get(self.sw_vars["sag_mode"].get(), "?")
                    .split(" ")[0].lower()))
            if getattr(self, "_sw_presets_summary", None) is not None:
                table = self._sw_preset_table()
                self._sw_presets_summary.set(
                    "%d saved" % len(table.get_children()) if table else "")
        except (tk.TclError, AttributeError, ValueError):
            pass

    def _sw_grid_rows(self, F, rows, indent=0):
        """(label, key, unit, tip[, extra]) -> rows of the Form `F`: an entry,
        or a checkbox for a BooleanVar. `unit` goes into the label and `tip` is
        the hover help on label and field -- no grey text beside a parameter.

        `extra` is either a field-width kind ("text" for a typed list) or a list
        of (engine value, display text), which makes the row a read-only
        Combobox. Each row is recorded in `_sw_field_widgets[key]` as
        (label, input, None)."""
        self._sw_field_widgets = getattr(self, "_sw_field_widgets", {})
        for row in rows:
            label, key, unit, tip = row[:4]
            extra = row[4] if len(row) > 4 else None
            var = self.sw_vars[key]
            if isinstance(extra, (list, tuple)):
                rw = self._sw_combo_row(F, label, key, extra, tip, unit, indent)
            elif isinstance(var, tk.BooleanVar):
                rw = F.check(label, var, tip, indent=indent)
            else:
                rw = F.entry(label, var, tip, unit=unit or None,
                             width=(extra or "num"), indent=indent)
            self._sw_field_widgets[key] = (rw.label, rw.field, None)

    def _sw_combo_row(self, F, label, key, options, tip, unit=None, indent=0):
        """A read-only Combobox row whose StringVar holds the ENGINE VALUE
        (options = [(value, display), ...]) while the box shows the display text.
        The var stays authoritative for snapshot/persist and the display re-syncs
        when a preset writes the var. Returns the Form row."""
        var = self.sw_vars[key]
        val2disp = {val: disp for val, disp in options}
        disp2val = {disp: val for val, disp in options}
        disp = tk.StringVar(value=val2disp.get(var.get(), options[0][1]))
        rw = F.combo(label, disp, [d for _v, d in options], tip,
                     unit=unit or None, indent=indent)
        cb = rw.field

        def _on_pick(*_a):
            var.set(disp2val.get(disp.get(), options[0][0]))

        def _on_var(*_a):
            d = val2disp.get(var.get())
            if d and d != disp.get():
                disp.set(d)

        cb.bind("<<ComboboxSelected>>", _on_pick)
        var.trace_add("write", _on_var)
        return rw

    def _sw_build_preview_options(self, panel):
        """The preview's display toggles, as a wrapping row across the panel's
        header -- the Scaffold designer's shape, through the ONE toggle maker both
        previews share (`_OptionFlow.add_toggle`): a bare checkbox whose help is
        its hover tooltip.

        The section's own ⓘ -- the one carrying the Instructions step line, which
        `settings_notes` asks every tab to show -- sits beside the PREVIEW title,
        since the row itself has no title to hang it on. It is a section note, not
        a parameter's help, so it stays a dot.

        "Jet lag" is this preview's OWN box (`sw_show_lag`): it hides the
        touchdown reshape from the picture only. It draws only while the Physics
        page's Jet lag switch -- what the STL and the simulation follow -- is on
        too (`_sw_preview_lag_on`)."""
        body = panel.options_host

        body.add_toggle("Collector", self.sw_show_plate,
                        "Draw every slide's outline at the Printer & collector page's origin.")
        # NO "Deposited" TOGGLE, promising "the predicted deposited fiber --
        # where the jet actually lands". The actor it would hide and show draws the commanded
        # toolpath with the jet-lag relay on top (`_sw_extract`): no droop, no
        # rest-on-top, no contact geometry. On the sag ladder -- the one plate
        # whose measured quantity IS droop -- that is not the deposited fibre in
        # any sense the operator can use: "there is a deposited toggle, yet
        # deposited fibers are not rendered".
        #
        # Drawing the real thing was priced before dropping this, against
        # `gcode_to_fibers.apply_sag`
        # on the default ladder, 157 polylines:
        #
        #   apply_sag (exact)           2.7 s   top of stack 0.4425 mm
        #   apply_sag_approx            1.1 s   0.3075 mm   |dz| mean 92 um
        #   preview_sag.drape_sag       0.1 s   0.1575 mm   |dz| mean 77 um
        #
        # Both approximate engines miss the ladder's walls by five or six fibre
        # diameters, because the ladder builds a wall by re-laying strokes at
        # ONE commanded Z and the seven 2026-09 contact fixes that make those
        # stack (be6dc12..2cac358) landed in `apply_sag` alone. The engine that
        # is right costs 2.7 s on one slide, against a debounced live preview
        # that rebuilds on a keystroke and a five-slide plate. So: not cheap,
        # and the toggle goes instead. Porting the contact fixes into a raster
        # drape is a solver task, not a preview option.
        #
        # `sw_show_depo` itself STAYS -- it is a persisted key (`sw.show_depo`)
        # that saved presets carry and `test_ui_shell.py`'s parity guard counts.
        # `sync_visibility` no longer READS it: a preset saved while the toggle
        # existed can hold False, and there is no control left to undo that.
        body.add_toggle("G-code path", self.sw_show_gcode,
                        "The COMMANDED toolpath, as a wire. With physics on, the "
                        "gap between it and the deposited fiber IS the jet lag.")
        body.add_toggle("Travels", self.sw_show_travel,
                        "The G0 repositioning hops. Continuous-flow MEW lays "
                        "fiber on these too.")
        body.add_toggle("Labels", self.sw_show_labels,
                        "The ε or feed value floating over each specimen.")
        body.add_toggle("3-D tubes", self.sw_show_tubes,
                        "Draw fibers as round 3-D tubes. Off = flat centerlines, "
                        "which read more clearly at plate scale.")
        body.add_toggle(
            "Jet lag", self.sw_show_lag,
            "Draw the fiber where the jet lands: it trails the nozzle and "
            "rounds every corner. Preview only: it changes nothing that is "
            "generated or exported.\n\n"
            "Needs Jet lag on in its Physics-page card, which is what the "
            "generated STL follows.")

        title, info = self._sw_section("preview_options")
        dot = gui_tips.info_dot(panel.title_row, info, title=title)
        dot.pack(side="left", padx=(gui_fonts.SP["s"], 0))
        gui_form.exempt(dot, "the preview section's note (its Instructions "
                             "step line), not a parameter's help")
        self._sw_shared_dots = getattr(self, "_sw_shared_dots", {})
        self._sw_shared_dots["preview_options"] = (dot, title, info)
        panel.show_options()

    #: the exit row's words on a plate print and on a tube
    _SW_EXIT_TEXT = {
        False: ("Run nozzle off the slide when done",
                "A final travel this far past the nearest slide edge, routed "
                "around every specimen, so the polymer that keeps flowing "
                "after the last move lands off the print."),
        True: ("Run nozzle off the end of the tube when done",
               "A final travel this far past whichever END of the tube the "
               "print finishes on, along the mandrel axis, so trailing "
               "polymer flow misses the wall."),
    }

    def _sw_build_output(self, F):
        """The Export card: the Scaffold designer's Export rows that mean
        something for a calibration print, on this page's own `sw.*` keys, plus
        the measurement-CSV opt-in. Building and saving stay on the shared
        bottom bar, exactly like the scaffold designer.

        Left out, because a calibration print cannot use them: "Also render
        STL" (Generate exists to build the slide's STL; the g-code saves
        without one), the multi-scaffold deposition order (one print per
        file), and the per-scaffold Z step (one part). "Add printhead
        elevation to Z" is a G-code-zero datum, not a spelling, and the
        calibration export does not re-base onto that datum at all."""
        v = self.sw_vars
        F.check("Also write the measurement-CSV template", v["write_csv"],
                "When on, “Save g-code as…” writes a «basename»_measure.csv "
                "next to the per-slide g-code files, in the schema the "
                "data-folder scripts used. Jet lag (plate)'s “Load CSV…” "
                "reads it back.")
        text, tip = self._SW_EXIT_TEXT[False]
        row = F.check(text, v["exit_move"], tip, inline=True,
                      command=self._sw_toggle_exit)
        self._sw_exit_row = row
        self._sw_exit_entry = row.add_entry(
            "Exit distance", v["exit_margin"], tip, unit="mm", width="small")
        self._sw_toggle_exit()
        self._sw_g2g3_row = F.check(
            "Write curves as G2/G3 arcs", v["g2g3"],
            "Writes each lag circle's lap as four true G3 arcs, and the "
            "stabilization scaffold's turnaround loops as arcs, instead of a fan "
            "of straight G1 chords. For printers that interpolate arcs "
            "(Klipper needs [gcode_arcs]).\n\n"
            "The commanded circle is the same either way, and so is the "
            "layout. The preview, the STL and the simulation re-expand each "
            "arc at least as finely as the chord file would have drawn it "
            "(the circle tab's Arc tolerance), so the fit reads the same "
            "circle.\n\nPlate prints only: a tube is written in G1.\n\n"
            "THE PRINTER'S setting, on Setup and saved with the printer "
            "preset: one box, two places. The printer either interpolates "
            "arcs or it does not.")
        # the printer's spelling is ONE setting, Setup's: these rows act on
        # its vars (`_sw_spelling_vars`), as the G-code window's Output menu
        # does, so a printer that needs G0X gets it everywhere
        g1_var, conv_var = self._sw_spelling_vars()
        F.check(
            "Emit travel (G0) moves as G1", g1_var,
            "THE PRINTER'S setting, on Setup and saved with the printer "
            "preset: one box, two places.\n\n"
            "In the g-code “Save g-code as…” writes and the g-code view "
            "shows, every travel move is written as a G1 printing move "
            "instead of G0. In melt-electrowriting the jet keeps depositing "
            "while it travels.\n\nNothing on screen or in the STL changes. "
            "Untick to keep travels as G0 in the saved g-code.")
        conv_tip = ("THE PRINTER'S setting, on Setup and saved with the "
                    "printer preset: one choice, two places.\n\n"
                    "How the single-digit motion commands are spelled in the "
                    "saved g-code. GX: G0, G1, G3. G0X: zero-padded, as G00, "
                    "G01, G03. Some controllers require it. The same commands "
                    "either way.")
        cv = F.group("G-code convention", conv_tip)
        cv.add_radio("GX", conv_var, "GX", conv_tip)
        cv.add_radio("G0X", conv_var, "G0X", conv_tip)
        nlz = getattr(self, "no_layer_z", None)
        if nlz is not None:
            F.check(
                "Don't adjust height per layer", nlz,
                "THE MACHINE'S setting, shared with the Scaffold designer and "
                "saved with the printer preset: one box, two places.\n\n"
                "On: the saved g-code names its height once, at the start, "
                "and never again, so the collector is never told to move. "
                "Off: every layer of the base and of a multi-layer specimen "
                "carries its own Z word.\n\nIt changes the saved spelling "
                "only. The preview, the STL and the simulation still stack "
                "the layers.")
        if hasattr(self, "custom_gcode") and not hasattr(self, "_machine_grid"):
            self._build_custom_gcode_fields(F)   # the machine's; one var, two cards
        F.check(
            "Render travel (G0) fiber in the exported STL", v["travel_in_stl"],
            "The jet never stops, so the travel between specimens is real "
            "fiber. It is always solved, and the specimens rest on it where "
            "they touch. This decides only whether it reaches the EXPORTED "
            "STL. Off (the default), the mesh holds the print alone.")
        F.check(
            "Render travel (G0) as a G-code line instead of fiber",
            v["travel_gcode_line"],
            "Draw the commanded travels in the Viewer as plain g-code lines "
            "instead of fiber. A drawing choice only: it changes nothing "
            "about the fiber, the STL or the g-code. Regenerate to apply.")

    def _sw_toggle_exit(self):
        """The exit distance means nothing with the exit off."""
        ent = getattr(self, "_sw_exit_entry", None)
        if ent is not None:
            try:
                ent.configure(state="normal" if self.sw_vars["exit_move"].get()
                              else "disabled")
            except tk.TclError:
                pass

    def _sw_show_print_kind(self, rotary):
        """Re-word the Export card for a plate print or a tube: the exit row
        says which way the nozzle leaves, and the arcs row goes away on a
        tube, which the converter refuses arcs on."""
        row = getattr(self, "_sw_exit_row", None)
        if row is not None:
            text, tip = self._SW_EXIT_TEXT[bool(rotary)]
            try:
                row.field.configure(text=text)
            except (tk.TclError, AttributeError):
                pass
        g2 = getattr(self, "_sw_g2g3_row", None)
        if g2 is not None:
            g2.show(not rotary)

    def _sw_build_quality(self, F):
        """The Quality card: the Scaffold designer's Quality rows that the
        calibration converter and player read. "Curve quality" and "Corner
        resolution" are not here: they shape the physics-deposited curves,
        which this page takes from the designer's physics export prefix
        (`_sw_export_prefix`)."""
        v = self.sw_vars
        self._sw_grid_rows(F, [
            ("Tube facets", "faces", "",
             "How many flat sides approximate the round fiber in the STL. "
             "More gives a smoother surface and a larger file."),
            ("Sag solver", "sag_mode", "",
             "Which solver computes the deposited fibers' sag for the "
             "slide's STL and simulation.\n\nExact (the default here): the "
             "full contact solve, the reference the others are measured "
             "against. The sag ladder's walls only stack correctly under "
             "it.\n\nApproximate (fast): analytic contacts, roughly 5x "
             "faster, within a fraction of a fiber diameter of Exact.\n\n"
             "Off: every fiber straight, as commanded.",
             [(val, lbl) for lbl, val in SAG_SOLVER_CHOICES]),
            ("Reset sagging periodically", "sag_reset", "",
             "Lay every Nth layer straight and let sagging build up again "
             "from it. Off by default."),
            ("Reset sagging every", "sag_reset_layers", "layers",
             "The period of the reset above."),
            ("Sag resolution", "sag_resolution", "", SAG_RESOLUTION_TIP,
             [(val, lbl) for lbl, val in SAG_RESOLUTION_CHOICES]),
            ("Mesh detail", "mesh_tol", "",
             "How finely the solved centerline is sampled into tube rings "
             "for the STL. It changes the file, never the solve.",
             [(val, lbl) for lbl, val in MESH_DETAIL_CHOICES]),
        ])
        # the period means nothing with the reset off, or with no sag at all
        for key in ("sag_reset", "sag_mode"):
            v[key].trace_add("write", lambda *_a: self._sw_toggle_reset())
        self._sw_toggle_reset()

    def _sw_toggle_reset(self):
        v = self.sw_vars
        on = v["sag_mode"].get() != "off"
        for key, live in (("sag_reset", on),
                          ("sag_reset_layers", on and v["sag_reset"].get()),
                          ("sag_resolution", on)):
            w = getattr(self, "_sw_field_widgets", {}).get(key)
            if w is not None:
                try:
                    w[1].configure(state="normal" if live else "disabled")
                except tk.TclError:
                    pass

    def _sw_faces(self):
        """Tube facets for the converter and the player, as typed (validated
        by `_sw_quality_error` before any build)."""
        return self.sw_vars["faces"].get().strip() or "12"

    def _sw_quality_error(self):
        """A user-facing message for a Quality field that cannot be sent, or
        None. Checked before Generate, as the designer checks its own."""
        v = self.sw_vars
        try:
            if int(self._sw_faces()) < 3:
                raise ValueError
        except ValueError:
            return "Tube facets needs a whole number of at least 3."
        if v["sag_mode"].get() != "off" and v["sag_reset"].get():
            try:
                if int(v["sag_reset_layers"].get().strip()) < 1:
                    raise ValueError
            except ValueError:
                return ("Reset sagging every needs a whole number of layers, "
                        "at least 1.")
        return None

    def _sw_quality_args(self):
        """The Quality card as converter/player flags -- the designer's
        spelling (`gui_args._sag_solver_args`, `_mesh_tol_args_for`), sent to
        BOTH, because the solver and the arc density are the `.depo` cache
        key. `--sag-multiplier` is pinned at 1, as it is on the designer.
        `--arc-segments` is the RESULT's own (`SweepResult.arc_segments`), so
        an arc-mode circle re-expands at its Arc tolerance."""
        v = self.sw_vars
        mode = v["sag_mode"].get()
        # the Physics page's Fiber sagging law switch wins, as on the designer
        law = getattr(self, "sim_sag_law", None)
        if mode == "off" or (law is not None and not law.get()):
            a = ["--no-sag"]
        else:
            a = ["--sag-approx"] if mode == "approximate" else []
            reset = (v["sag_reset_layers"].get().strip()
                     if v["sag_reset"].get() else "0")
            a += ["--sag-multiplier", "1", "--sag-reset-layers", reset or "0"]
            # the designer's rule (`_sag_resolution_args_for`): nothing at
            # Standard, so a default slide's argv and .depo key are unchanged
            res = v["sag_resolution"].get().strip()
            if res in SAG_RESOLUTION_VAL2LBL and res != "1":
                a += ["--sag-resolution", res]
        if v["mesh_tol"].get() in dict((val, 1) for _l, val
                                       in MESH_DETAIL_CHOICES):
            a += ["--mesh-tolerance", v["mesh_tol"].get()]
        segs = getattr(getattr(self, "_sw_result", None), "arc_segments",
                       mew_sweeps.DEFAULT_ARC_SEGMENTS)
        return a + ["--arc-segments", str(int(segs))]

    def _sw_spelling_vars(self):
        """(travel-as-G1 var, convention var): Setup's, which every output
        reads, or this page's own where there is no designer."""
        make = getattr(self, "_spelling_vars_make", None)
        if make is not None:
            make()
        v = self.sw_vars
        return (getattr(self, "s_travel_as_g1", None) or v["travel_as_g1"],
                getattr(self, "s_gcode_convention", None)
                or v["gcode_convention"])

    def _sw_emit_opts(self):
        """(travel_as_g1, padded, drop_layer_z) for this page's user-facing
        g-code: Setup's two spelling settings and the machine's "Don't
        adjust height per layer". A calibration file is one part, so
        no per-scaffold Z boundary survives the drop."""
        g1_var, conv_var = self._sw_spelling_vars()
        nlz = getattr(self, "no_layer_z", None)
        return (bool(g1_var.get()),
                conv_var.get() == "G0X",
                bool(nlz is not None and nlz.get()))

    def _sw_slide_source(self, index, raw):
        """What Generate builds and the Simulation plays for slide `index`
        (0-based): the engine's bytes on the Machine page's g-code origin,
        in its flavor's rotary feeds."""
        return self._sw_rebase(self._sw_feeds(raw))

    def _sw_slide_text(self, index, raw, filename=None):
        """What slide `index` (0-based) saves as."""
        return self._sw_user_gcode(self._sw_rebase(self._sw_feeds(raw)),
                                   index, filename)

    def _sw_feeds(self, text):
        """A rod print in the rotary feed the Machine page's flavor reads,
        as the 4-axis designer writes one (`gui_mandrel._mandrel_rotary_
        feed`): the engine writes F as the surface speed, which a flavor
        that splits the feed (Aerotech: F linear, E rotary) would run as the
        X rate, so the surface speed a CTS rung measures would be wrong.
        A flat print, or a combined flavor, is returned as it is."""
        tube = self._sw_tube_geometry()
        if tube is None:
            return text
        import mew_gcode
        import scaffold_gcode
        try:
            split = mew_gcode.get_flavor(
                self._sw_export_flavor()).rotary_feed == "split"
        except Exception:  # noqa: BLE001 -- an unknown flavor: as written
            split = False
        if not split:
            return text
        unit = (self._feed_time_unit() if hasattr(self, "_feed_time_unit")
                else "min")
        return scaffold_gcode.split_rotary_feeds(
            text, float(tube["radius"]), float(tube["surface_z"]), unit)

    def _sw_rebase_shift(self):
        """(dx, dy, dz) that puts a flat calibration print on the Machine
        page's g-code origin, the re-base the scaffold generator applies to a
        design (`--origin-offset`, `scaffold_gcode.translate_gcode`). The
        engine lays a print in plate coordinates (the plate's bottom-left
        corner, Z on its top face), so Z moves by the plate top plus the
        offset's Z: nothing on the default top face, the plate's thickness on
        the bottom face, plus the printhead elevation when that box is ticked.
        A tube is generated on the datum already, so it does not move."""
        try:
            if self._sw_tube_geometry() is not None:
                return (0.0, 0.0, 0.0)
            dx, dy, dz = self._origin_offset()
            dz += self._plate_top_z()
        except (AttributeError, ValueError, TypeError, tk.TclError):
            return (0.0, 0.0, 0.0)
        # `+ 0.0` turns a negated 0.0 into 0.0, and a float sum into a clean 0
        return tuple(round(float(v), 9) + 0.0 for v in (dx, dy, dz))

    def _sw_rebase(self, text):
        """`text` (plate coordinates) on the g-code origin."""
        import scaffold_gcode
        return scaffold_gcode.translate_gcode(text, *self._sw_rebase_shift())

    #: the designer's own numbers in the print header, which describe the
    #: Design and not a calibration print: blank, so the header says
    #: "(not set)" rather than a scaffold nobody is printing
    _SW_DESIGN_ONLY_CONTEXT = ("spacing", "layers", "rotation",
                               "scaffold_count")

    def _sw_gcode_context(self, index=None, filename=None):
        """The machine header's print values for a calibration print: the
        Machine and Physics pages' values, as the designer's header has
        them, with the print's own speeds and (on a temperature ladder) its
        own temperature. `index` is a slide (0-based), None the whole
        layout. The date and time are those of the sweep, held, so the
        text does not change every minute."""
        fn = getattr(self, "_machine_gcode_context", None)
        if fn is None:
            return {}
        ctx = dict(fn(filename=filename or "calibration.gcode"))
        for key in self._SW_DESIGN_ONLY_CONTEXT:
            ctx[key] = ""
        res = getattr(self, "_sw_result", None)
        if res is None:
            return ctx
        held = getattr(self, "_sw_stamp", None)
        if held is None or held[0] is not res:
            held = self._sw_stamp = (res, ctx.get("date", ""),
                                     ctx.get("time", ""))
        ctx["date"], ctx["time"] = held[1], held[2]
        prints = (res.prints if index is None
                  else res.prints[index:index + 1])
        feeds = sorted({round(float(s.feed_mm_min), 3)
                        for p in prints for s in p.specimens
                        if getattr(s, "feed_mm_min", None)})
        if feeds:
            ctx["speed"] = ", ".join("%g" % f for f in feeds)
        if res.kind == "diam":
            wanted = None if index is None else index + 1
            temps = []
            for row in res.rows:
                t = row.get("T_C")
                if t in ("", None) or (wanted is not None
                                       and row.get("print") != wanted):
                    continue
                if t not in temps:
                    temps.append(t)
            if temps:
                # one rung: its own setpoint; several in one file: a list,
                # which writes no temperature command (not a number)
                ctx["temperature"] = ", ".join("%g" % float(t) for t in temps)
        return ctx

    def _sw_export_flavor(self):
        """The Machine page's flavor, as the designer's export uses it."""
        fn = getattr(self, "_machine_flavor_name", None)
        return fn() if fn is not None else "klipper"

    def _sw_output_options(self, index=None, filename=None):
        """Every Machine-page output setting for a calibration print, as the
        ONE value the engine takes -- the same settings the designer's
        `_gcode_output_options` reads: the start and end blocks, the
        automatic scripts, the incremental origin, the custom line, plus this
        printer's two spelling settings and "Don't adjust height per layer",
        and Setup's "Wait to print until target temperature is reached": a rig that needs the wait needs it for every print. The
        wait is at the print's own temperature (a ladder rung's setpoint),
        and a text holding several rungs gets none, since no one setpoint
        is right for it."""
        import mew_gcode
        g1, padded, drop_z = self._sw_emit_opts()
        inc, origin, rot = (False, (), "")
        if hasattr(self, "_origin_output_fields"):
            inc, origin, rot = self._origin_output_fields()
        blocks = getattr(self, "_machine_blocks", None)
        start, end = blocks() if blocks is not None else ("", "")
        recipes = getattr(self, "_machine_recipes", None)
        context = self._sw_gcode_context(index, filename)
        wait = getattr(self, "s_wait_for_temp", None)
        wait_temp = None
        if wait is not None and wait.get():
            wait_temp = context.get("temperature") or None
            if mew_gcode.start_block_waits(start):
                wait_temp = None  # the start block places the wait itself
        return mew_gcode.OutputOptions(
            travel_as_g1=g1, padded=padded, drop_layer_z=drop_z,
            boundaries=(), wait_temp=wait_temp,
            custom_line=self._custom_gcode_spec(),
            incremental=inc, origin=origin, rotary_axes=rot,
            start_block=start, end_block=end,
            context=context,
            rig_recipes=recipes() if recipes is not None else [])

    def _sw_user_gcode(self, text, index=None, filename=None):
        """One slide's g-code as the user gets it: the engine's bytes through
        the Machine page's output settings, in its flavor, exactly as the
        designer's Save writes a scaffold (`mew_gcode.apply_output_options`,
        then "Strip comments"). Each slide is a print of its own, so each
        file carries the whole start and end block."""
        import mew_gcode
        flavor = self._sw_export_flavor()
        text = mew_gcode.apply_output_options(
            text, self._sw_output_options(index, filename), flavor)
        strip = getattr(self, "machine_strip_comments", None)
        if strip is not None and strip.get():
            text = mew_gcode.strip_export_comments(text, flavor)
        return text

    # ------------------------------------------------------------------ #
    def _sw_gap(self):
        """The needle-to-collector gap (mm) = the Printhead-Simulation tab's
        standoff (sim_standoff). The experiment page has NO gap field of its own --
        gap is a physical condition owned by the sim tab (as CTS is owned by the
        Calibration tab), so there is one source of truth. Falls back to 4.5 mm."""
        try:
            return _finite(getattr(self, "sim_standoff",
                                   tk.StringVar(value="4.5")).get())
        except (tk.TclError, ValueError):
            return 4.5

    def _sw_plate_size(self):
        """(width, height) mm of the plate = the BUILD-PLATE page's plate size.

        This page carries no `plate_w`/`plate_h` of its own, which would agree
        with the machine's only until someone changed one of them. The plate a
        calibration slide is printed on is the plate, so it is read here
        rather than copied. Falls back to a 3x2 inch slide."""
        try:
            w = float(getattr(self, "plate_x", tk.StringVar(value="76.2")).get())
            h = float(getattr(self, "plate_y", tk.StringVar(value="50.8")).get())
            if w > 0 and h > 0:
                return w, h
        except (tk.TclError, ValueError, AttributeError):
            pass
        return 76.2, 50.8

    def _sw_origin(self):
        """(x, y) mm of the plate's lower-left corner = the Build-plate page's
        `plate_corner_x/y`, for the same reason as `_sw_plate_size`."""
        try:
            fp = self._plate_footprint_xy()
            return (fp[0], fp[2]) if fp else (0.0, 0.0)
        except (tk.TclError, ValueError, AttributeError, TypeError):
            return 0.0, 0.0

    def _sw_diam_mm(self):
        """The deposited fiber diameter (mm) = the SCAFFOLD designer's.

        A calibration print is not a place to choose a diameter: it is whatever the rig
        makes, and the app already holds exactly one number for it -- which with
        "Physics-deposited scaffold" on is the PREDICTED diameter. A page-local copy
        could disagree with the very physics this page uses to size the specimens.
        Falls back to the engine's own 0.015 mm default."""
        try:
            v = self.scaf_vars[0]["diameter"].get().strip()
            d = float(v)
            if d > 0:
                return d
        except (AttributeError, IndexError, KeyError, TypeError,
                ValueError, tk.TclError):
            pass
        return 0.015

    def _sw_physics_on(self):
        """Whether jet-lag reshaping is active for THIS page's generated STL and
        simulation -- the Physics page's Jet lag switch (the SAME one the
        scaffold page uses), never a page-local copy."""
        try:
            return bool(getattr(self, "sim_physics_deposit",
                                tk.BooleanVar(value=False)).get())
        except (tk.TclError, AttributeError):
            return False

    def _sw_preview_lag_on(self):
        """Whether THIS page's preview draws the jet-lag reshape: the Physics
        page's Jet lag switch AND the preview's own Jet lag box."""
        try:
            return self._sw_physics_on() and bool(self.sw_show_lag.get())
        except (tk.TclError, AttributeError):
            return self._sw_physics_on()

    def _sw_resolve_cts(self):
        """The critical translation speed (mm/s) for the jet-lag feeds, taken from
        the CALIBRATION tab (its CTS anchor + hooks) evaluated at the current gap
        and the sim tab's voltage, pressure and temperature (`MEWModel.cts_at`, so a
        fitted flow or viscosity exponent is honoured) -- the SAME speed the deposition model rounds by, so
        feed = eps x CTS is consistent with the predicted lag. Falls back to the
        model's factory anchor (7.5 mm/s) when calibration is unavailable. Never
        raises (called from the snapshot on the Tk thread)."""
        gap = self._sw_gap()

        def read(name, default):
            # A blank or garbled field takes its default, as `_sw_gap` does:
            # a blank pressure box must not cost the page its calibrated CTS.
            try:
                return _finite(getattr(self, name,
                                       tk.StringVar(value=default)).get())
            except (tk.TclError, ValueError):
                return float(default)

        voltage = read("sim_voltage", "5")
        pressure = read("sim_pressure", "3") * PSI_TO_PA
        temperature = read("sim_temperature", "100")
        try:
            from mew_physics import MEWModel, Nozzle
            models = self._calibration_models()
            if models is not None:
                material, nozzle, calibration = models
                model = MEWModel(material=material, nozzle=nozzle,
                                 working_distance=gap, calibration=calibration)
            else:
                model = MEWModel(nozzle=Nozzle(), working_distance=gap)
            cts = float(model.cts_at(voltage, pressure, temperature))
            if cts > 0:
                return cts
        except Exception:  # noqa: BLE001 -- physics missing / bad field -> default
            pass
        return 7.5

    def _sw_update_cts_readout(self):
        """Refresh the read-only readouts: the plate the print is laid on (from
        the Build-plate page), the fiber diameter (from the scaffold designer),
        the gap and resolved CTS (from the Physics page) and the physics state.
        Cheap; called on every edit and once after the page builds."""
        w, h = self._sw_plate_size()
        ox, oy = self._sw_origin()
        pvar = getattr(self, "_sw_plate_var", None)
        if pvar is not None:
            # the live values only; where each comes from is the row's tooltip
            pvar.set(f"Plate {w:g} x {h:g} mm, lower-left at ({ox:g}, {oy:g})")
        cts = self._sw_resolve_cts()
        gap = self._sw_gap()
        phys = "ON" if self._sw_physics_on() else "off"
        self._sw_update_circle_readout(cts, gap, phys)
        self._sw_update_diam_readout(cts)
        self._sw_update_sag_readout()
        # the ladder design decides how many measurement rows the Activation Ea
        # card shows and what the Viscosity card's conditions are seeded with, so
        # the two fit forms refresh on the same beat the readouts do. Guarded: the
        # classic developer build mounts gui_sweeps without the calibration cards.
        sync = getattr(self, "_cal_sync_diam_fit", None)
        if sync is not None:
            sync()
        # the sag ladder does the same for its own card: one measurement row per
        # span the row lays, one cell per reading of it
        sync_sag = getattr(self, "_cal_sync_sag_fit", None)
        if sync_sag is not None:
            sync_sag()

    def _sw_update_diam_readout(self, cts):
        """The conditions line on BOTH diameter cards: the pressure the fit will
        be told (the Physics page's, not a field here), and whether the chosen
        feed clears the CTS. The runbook picks the fixed speed as roughly twice
        the CTS at the HOTTEST rung, since CTS rises with temperature and a speed
        safe there is safe everywhere cooler.

        Two note lists, because the ladder and the viscosity plate are separate
        designs with separate feeds: writing one text into both would show the Ea
        tab's feed on the Viscosity card."""
        try:
            press = _finite(getattr(self, "sim_pressure",
                                    tk.StringVar(value="3")).get())
        except (tk.TclError, ValueError):
            press = 3.0
        for attr, feed_key, tail in (
                ("_sw_diam_notes", "diam_speed",
                 "and it must not move between rungs"),
                ("_sw_visc_notes", "visc_speed",
                 "and it is what the fit is told")):
            notes = getattr(self, attr, None)
            if not notes:
                continue
            try:
                feed = _finite(self.sw_vars[feed_key].get())
            except (tk.TclError, ValueError, KeyError):
                feed = 0.0
            ratio = (feed / 60.0 / cts) if cts > 0 else 0.0
            verdict = ("below the CTS, so the fiber will coil, not lay straight"
                       if ratio < 1.0 else
                       "under 2× CTS; the runbook suggests ≈2×" if ratio < 2.0
                       else "clears the runbook's ≈2× CTS")
            text = ("Printed at %g psi, from the Physics page. The pressure is "
                    "not a field here, %s. %.0f mm/min is %.1f× the believed CTS "
                    "(%.2f mm/s at the current gap): %s."
                    % (press, tail, feed, ratio, cts, verdict))
            for var in notes:
                var.set(text)

    def _sw_sag_law(self):
        """(c, p) from the live rig calibration -- the droop law's magnitude and span
        exponent -- falling back to mew_sweeps' shipped defaults when calibration is
        unavailable. Read for the MEASURABLE-BAND verdict only: which spans will
        flatten onto the collector is `c*span^p` against the wall height, so the
        answer moves when the Physics page's c or p moves."""
        dflt = mew_sweeps.SagParams()
        try:
            models = self._calibration_models()
            if models is None:
                return dflt.sag_coeff, dflt.sag_span_exp
            cal = models[2]
            return float(cal.sag_coeff), float(cal.sag_span_exp)
        except Exception:  # noqa: BLE001 -- physics/calibration absent or bad field
            return dflt.sag_coeff, dflt.sag_span_exp

    def _sw_update_sag_readout(self):
        """The conditions line on the Sag card: the row's footprint, the wall height
        in mm, and WHICH SPANS the droop law says can actually be measured.

        The wall height is the one number this design turns on and it is NOT a
        length the operator types -- it is a layer count times the fiber diameter
        the Scaffold designer owns, so the card has to do that multiplication on
        screen. What it is compared against is `c*span^p`, not the span: the two are
        both millimetres, which is how the first version of this line came to
        compare the wall height to the widest span directly and call a 2 mm span
        clear at 0.285 mm of wall when its predicted droop is 1.37 mm.
        """
        notes = getattr(self, "_sw_sag_notes", None)
        if not notes:
            return
        v = self.sw_vars

        def _f(key, dflt=0.0):
            try:
                return _finite(v[key].get())
            except (tk.TclError, ValueError, KeyError):
                return dflt

        gaps = mew_sweeps.sag_gaps(v["sag_spans"].get(),
                                   bool(v["sag_mirror"].get()))
        if not gaps:
            text = "Type at least one span in mm."
        else:
            diam = self._sw_diam_mm()
            layers = int(_f("sag_layers", 0))
            height = max(0, layers - 1) * diam
            width = sum(gaps) + 2 * _f("sag_lead")
            mid_lane = v["sag_lane"].get().strip() != "edge"
            depth_y = _f("sag_wall_len") * (2.0 if mid_lane else 1.0)
            coeff, exp = self._sw_sag_law()
            flat, thin, good = mew_sweeps.sag_band(
                sorted(set(gaps)), coeff, exp, diam, height)
            if not good:
                verdict = ("the sag law says NO span here is measurable, so "
                           "raise the wall height")
            elif flat or thin:
                verdict = ("only %s mm are measurable; %s"
                           % (" / ".join("%g" % s for s in good),
                              " and ".join(x for x in (
                                  ("%s flatten on the collector"
                                   % " / ".join("%g" % s for s in flat))
                                  if flat else "",
                                  ("%s sag less than three fiber diameters"
                                   % " / ".join("%g" % s for s in thin))
                                  if thin else "") if x)))
            else:
                verdict = "every span sags into the measurable band"
            text = ("%d span(s) over %d walls: %.1f × %g mm of plate, travel "
                    "lane %s. The walls are %d layers of %.0f µm fiber, so they "
                    "stand %.3f mm tall: %s."
                    % (len(gaps), len(gaps) + 1, width, depth_y,
                       "down the wall middle" if mid_lane else "at one end",
                       layers, diam * 1000, height, verdict))
        for var in notes:
            var.set(text)

    # ------------------------------------------------------------------ #
    # the circle SPECIMEN TABLE (one row per speed ratio)
    # ------------------------------------------------------------------ #
    def _sw_circ_specs(self):
        """The table as [CircleSpec], or None while it is blank (law sizing).

        Blank maps to None and NEVER to an empty list: `CircleParams.specimens`
        treats a list as the whole truth and raises on an empty one, so [] would
        turn "I have not touched the table" into an error."""
        var = getattr(self, "sw_vars", {}).get("circ_specimens")
        text = var.get().strip() if var is not None else ""
        if not text:
            return None
        try:
            specs = mew_sweeps.specs_from_str(text)
        except ValueError as exc:
            # named, and re-raised. specs_from_str refuses to skip a row it
            # cannot read, and neither do we: a silently dropped row is a
            # specimen that quietly stops being printed.
            raise ValueError("Specimen table: %s" % exc) from None
        return specs or None

    def _sw_circ_add_specimen(self):
        """Append a row, seeded one rung above the highest ratio on the table."""
        rows = getattr(self, "_sw_circ_rows", [])
        try:
            top = max(float(r["eps"].get()) for r in rows) if rows else 1.0
        except (ValueError, tk.TclError):
            top = 1.0
        try:
            reps = int(float(rows[-1]["reps"].get())) if rows else 2
        except (ValueError, tk.TclError):
            reps = 2
        specs = self._sw_circ_read_rows(extra=(max(2.0, top + 1.0), max(1, reps)))
        self._sw_circ_write(specs)

    def _sw_circ_remove_specimen(self, row):
        """Drop one row. The table never goes empty -- with no rows there is
        nothing to print and no control left to add one back, so the last row
        resets the table to law sizing instead."""
        rows = getattr(self, "_sw_circ_rows", [])
        if row not in rows:
            return
        if len(rows) <= 1:
            self._sw_circ_reset_table()
            return
        keep = [r for r in rows if r is not row]
        self._sw_circ_write(self._sw_circ_read_rows(only=keep))

    def _sw_circ_reset_table(self):
        """Clear the key -- the whole table goes back to the sizing rule."""
        self.sw_vars["circ_specimens"].set("")

    def _sw_circ_read_rows(self, only=None, extra=None):
        """The widget rows as [CircleSpec].

        A radius cell is an OVERRIDE when its text differs from the text this page last
        WROTE into it (`row["shown"]`), or when the row was ALREADY an override. Both
        halves are needed. The string comparison notices a fresh edit, since the page
        owns both sides of it. The sticky flag is what survives the next refresh:
        `circle_table` resolves an overridden row to the number the operator typed, so
        one beat later the cell matches `shown` again and the comparison alone would
        quietly hand the ring back to the sizing rule.

        An override is therefore released by "Reset to law sizing", not by retyping the
        law's number."""
        specs = []
        for row in (only if only is not None else self._sw_circ_rows):
            eps = float(row["eps"].get())
            reps = int(float(row["reps"].get()))
            text = row["rad"].get().strip()
            rad = (float(text) if (row.get("override")
                                   or text != row["shown"]) else None)
            specs.append(mew_sweeps.CircleSpec(eps=eps, reps=reps,
                                               radius_mm=rad))
        if extra is not None:
            specs.append(mew_sweeps.CircleSpec(eps=float(extra[0]),
                                               reps=int(extra[1])))
        return specs

    def _sw_circ_write(self, specs):
        """Push a table back into the ONE persisted key, which is what re-runs
        the preview through the existing debounce."""
        self.sw_vars["circ_specimens"].set(mew_sweeps.specs_to_str(specs))

    def _sw_circ_harvest(self, *_):
        """A cell was edited: re-derive the key from every row on screen.

        Silent on a half-typed cell -- the operator is mid-keystroke, and the
        last good table stays in the key until the cell parses."""
        if getattr(self, "_sw_circ_busy", False):
            return
        try:
            specs = self._sw_circ_read_rows()
        except (ValueError, tk.TclError):
            return
        if not specs:
            return
        self._sw_circ_write(specs)

    def _sw_circ_add_widget_row(self):
        ws = {}
        for key, width in (("eps", 8), ("reps", 6), ("rad", 10)):
            var = tk.StringVar()
            ent = gui_form.mark(ttk.Entry(self._sw_circ_tbl, textvariable=var,
                                          width=width), "cell")
            gui_tips.add_tip(ent, getattr(self, "_sw_circ_tip", ""))
            var.trace_add("write", self._sw_circ_harvest)
            ws[key] = var
            ws[key + "_w"] = ent
        ws["shown"] = ""
        ws["override"] = False        # sticky; see _sw_circ_read_rows
        ws["btn"] = gui_form.mark(ttk.Button(self._sw_circ_tbl, text="−",
                                             width=2), "button")
        gui_tips.add_tip(ws["btn"], "Remove this specimen.")
        ws["btn"].configure(command=lambda r=ws: self._sw_circ_remove_specimen(r))
        self._sw_circ_rows.append(ws)
        return ws

    def _sw_circ_drop_widget_row(self):
        ws = self._sw_circ_rows.pop()
        for key in ("eps_w", "reps_w", "rad_w", "btn"):
            ws[key].destroy()

    def _sw_circ_refresh_table(self):
        """Redraw the table from what the ENGINE says it will lay.

        The rows come from `mew_sweeps.circle_table`, which resolves every radius --
        law-sized or overridden -- to the number the generator will actually command, so
        the radius column shows the print rather than a re-derivation of it.

        `_sw_circ_busy` gates the harvest trace while this writes, or every refresh
        would write the key it just read from and the two would chase each other."""
        tbl = getattr(self, "_sw_circ_tbl", None)
        if tbl is None:
            return
        try:
            kind, params = self._sw_snapshot_params()
        except Exception:  # noqa: BLE001 -- a bad field elsewhere on the form
            return         # keep the last good table, like the preview does
        if kind != "circle":
            return
        try:
            table = mew_sweeps.circle_table(params)
        except Exception:  # noqa: BLE001 -- e.g. a repeated ratio
            return
        # which rows the KEY says are hand-set. circle_table resolves every
        # radius, so it cannot answer this -- only the key can.
        try:
            keyed = self._sw_circ_specs() or []
        except ValueError:
            keyed = []
        self._sw_circ_busy = True
        try:
            # counted, never `while len(...) != n`: a loop that cannot shrink
            # the list would spin forever
            for _ in range(len(self._sw_circ_rows) - len(table)):
                self._sw_circ_drop_widget_row()
            for _ in range(len(table) - len(self._sw_circ_rows)):
                self._sw_circ_add_widget_row()
            for i, (row, spec) in enumerate(zip(self._sw_circ_rows, table)):
                row["eps"].set(mew_sweeps._spec_num(spec.eps))
                row["reps"].set(str(int(spec.reps)))
                shown = "%.4g" % float(spec.radius_mm)
                row["shown"] = shown
                row["override"] = bool(i < len(keyed)
                                       and keyed[i].radius_mm is not None)
                row["rad"].set(shown)
                for c, key in enumerate(("eps_w", "reps_w", "rad_w", "btn")):
                    row[key].grid(row=1 + i, column=c,
                                  padx=(0, gui_fonts.SP["xs"]), pady=1,
                                  sticky="w")
        finally:
            self._sw_circ_busy = False

    def _sw_update_circle_readout(self, cts, gap, phys):
        """The lag-circle card's Conditions line. It carries two things nothing
        else on the page shows, because both are decided HERE and neither is
        visible from the numbers on the form: the radii the sizing rule lands
        on at this gap (they scale with it), and the fact that the k this print
        yields belongs to THIS gap and no other."""
        var = getattr(self, "_sw_circ_cts_var", None)
        if var is None:
            return
        try:
            lr = _finite(self.sw_vars["circ_lr"].get())
            ap = _finite(self.sw_vars["circ_amp_prior"].get())
            eps = [e for e in mew_sweeps.parse_float_list(
                self.sw_vars["circ_eps"].get()) if e > 1.0]
        except (tk.TclError, ValueError):
            eps, lr, ap = [], 0.0, mew_circle_fit.A_DEFAULT
        sizes = ""
        if eps and lr > 0:
            try:
                radii = [mew_sweeps.circle_radius(e, gap, lr, ap) for e in eps]
                insets = [r - (mew_circle_fit.contact_radius(r, e, gap=gap,
                                                             amp=ap) or r)
                          for e, r in zip(eps, radii)]
                sizes = (f" · radii {min(radii):.1f}–{max(radii):.1f} mm, "
                         f"inset {min(insets) * 1000:.0f}–"
                         f"{max(insets) * 1000:.0f} µm")
            except ValueError:
                # a gap or prior the sizing law refuses (0, negative): the
                # line reads without sizes rather than raising out of a trace
                sizes = ""
        # the CTS in the display unit (mm/min by default), as everywhere else
        var.set(gui_units.localize(
                f"Gap {gap:g} mm · CTS {cts:.4g} mm/s · physics "
                f"{phys}{sizes}. The amplitude this yields is fitted AT "
                f"{gap:g} mm and means nothing without it. Print at the "
                f"gap you work at. Edit gap / CTS on the Physics page."))
        # The specimen table's radii are functions of the gap and of the sizing
        # dials, so it refreshes on the same beat this line does rather than on
        # a trace of its own.
        self._sw_circ_refresh_table()

    # human labels for the numeric fields, so a blank/garbage entry raises a
    # message that NAMES the field ("Plate width needs a number") instead of the
    # cryptic "could not convert string to float: ''" -- surfaced on the Sweeps
    # panel status.
    _SW_FIELD_LABELS = {
        "margin": "Edge margin",
        "stab_w": "Stabilization scaffold width", "stab_h": "Stabilization scaffold height",
        "stab_layers": "Stabilization scaffold layers", "stab_stepover": "Stabilization scaffold fiber spacing",
        "stab_arc": "Arc × spacing",
        "cts_start": "Fastest speed", "cts_stop": "Slowest speed",
        "cts_step": "Speed step", "cts_reps": "Lines per speed",
        "cts_pitch": "Line spacing", "cts_col_gap": "Stabilization scaffold to first line",
        "cts_ref": "Expected CTS",
        "cts_loop_arc": "Turnaround radius", "cts_loop_speed": "Loop speed",
        "cts4_start": "Fastest speed", "cts4_stop": "Slowest speed",
        "cts4_step": "Speed step", "cts4_reps": "Lines per speed",
        "cts4_pitch": "Line spacing", "cts4_line_len": "Line length",
        "cts4_gap": "Gap between speeds",
        "tube_spacing": "Line spacing", "tube_band_len": "Band length",
        "tube_lines": "Lines per band",
        "tube_band_gap": "Gap between bands",
        "tube_speed": "Print speed", "tube_settle": "Lead-in turns",
        "rod_offset": "End offset",
        "circ_reps": "Reps per ε", "circ_lr": "Lag / radius ratio",
        "circ_specimens": "Specimen table",
        "circ_amp_prior": "Prior amplitude A",
        "circ_fixed_radius": "Fixed radius", "circ_floor": "Minimum radius",
        "circ_spacing": "Specimen spacing",
        "circ_travel_offset": "Travel clearance",
        "circ_lead_mult": "Lead-in ×lag", "circ_lead_min": "Lead-in minimum",
        "diam_speed": "Print feed", "diam_reps": "Lines per temperature",
        "diam_pitch": "Line pitch", "diam_col_gap": "Stabilization scaffold → lines gap",
        "circ_chord_tol": "Arc tolerance", "circ_layers": "Layers per circle",
        "circ_stab_eps": "Stabilization scaffold speed ×CTS",
        "exit_margin": "Exit distance", "faces": "Tube facets",
        "sag_reset_layers": "Reset sagging every",
    }

    # ------------------------------------------------------------------ #
    # snapshot (Tk thread; raises a NAMED error on a blank/garbage required
    # field -- the Sweeps worker keeps last-good)
    # ------------------------------------------------------------------ #
    def _sw_snapshot_params(self):
        v = self.sw_vars

        def _num(key):
            s = v[key].get().strip()
            try:
                return _finite(s)
            except ValueError:
                label = self._SW_FIELD_LABELS.get(key, key)
                raise ValueError(f"{label} needs a number"
                                 + (" (it's blank)" if not s
                                    else f", not “{s}”"))

        def F(key):
            return _num(key)

        def I(key):
            return int(_num(key))

        def Fzero(key):
            # fields whose default 0 means off/auto/none: a blank entry (a
            # legitimate "clear it" action, e.g. emptying Fixed side to leave
            # auto-size) reads as 0 instead of bricking the whole design.
            s = v[key].get().strip()
            return _num(key) if s else 0.0

        def Fopt(key):
            s = v[key].get().strip()
            return _num(key) if s else None

        stab = mew_sweeps.StabParams(
            enabled=bool(v["stab_on"].get()), width=F("stab_w"),
            height=F("stab_h"), layers=I("stab_layers"),
            stepover=F("stab_stepover"), arc_mult=F("stab_arc"))
        # The plate, its origin and the fiber diameter are READ from the pages
        # that own them (Build-plate, Scaffold designer) at snapshot time -- see
        # `_sw_plate_size` / `_sw_origin` / `_sw_diam_mm`. They are deliberately
        # not fields here, exactly as the gap and CTS are not.
        slide_w, slide_h = self._sw_plate_size()
        origin_x, origin_y = self._sw_origin()
        common = dict(diam=self._sw_diam_mm(), slide_w=slide_w,
                      slide_h=slide_h, margin=F("margin"),
                      origin_x=origin_x, origin_y=origin_y,
                      max_feed=Fopt("max_feed"), stab=stab,
                      g2g3=bool(v["g2g3"].get()),
                      exit_margin=self._sw_exit_margin(F))
        kind = v["kind"].get()
        if kind == "tube":
            # the mandrel tube: no plate, no stabilization pad -- see
            # `_sw_tube_params` for what it reads from the other pages
            return kind, self._sw_tube_params(F, I)
        if kind == "cts4":
            # the CTS ladder on the mandrel, laid on the rod the lag tube
            # uses, placed the same way
            return kind, self._sw_cts_mandrel_params(F, I)
        if kind == "lag":
            # The jet-lag SQUARES are hidden from the UI (no field on the page
            # reaches them any more) but `mew_sweeps.generate_lag` and its suite
            # are untouched, so a saved preset carrying the old kind must not
            # crash the page -- it degrades to the CTS sweep.
            kind = "cts"
        if kind in ("diam", "visc"):
            # ONE generator, two protocols. The viscosity plate IS a one-rung
            # ladder, so it is `generate_diameter` over a single-element `temps`;
            # what differs is which VARIABLES the design is read from, because the
            # two tabs are recalibrated on different clocks and must not share a
            # design.
            #
            # `pressure_psi` is an ANNOTATION carried into the CSV so a plate can be
            # traced back to what it was printed at. It is read from the Physics
            # page at snapshot time, never copied into a field of this page, and it
            # changes no geometry.
            try:
                press = float(getattr(self, "sim_pressure",
                                      tk.StringVar(value="3")).get())
            except (tk.TclError, ValueError):
                press = 0.0
            if kind == "visc":
                p_ = "visc_"
                # ONE temperature, so one slide. Parsed through the same list
                # helper the ladder uses, then truncated: a paste of several
                # numbers into a field labelled "Temperature (°C)" must lay the
                # plate this tab promises, not silently become a ladder.
                temps = mew_sweeps.parse_float_list(v["visc_temp"].get())[:1]
            else:
                p_ = "diam_"
                temps = mew_sweeps.parse_float_list(v["diam_temps"].get())
            params = mew_sweeps.DiamParams(
                temps=temps,
                speed=F(p_ + "speed"), reps=I(p_ + "reps"),
                line_len=Fopt(p_ + "line_len"), pitch=F(p_ + "pitch"),
                col_gap=F(p_ + "col_gap"),
                base_speed=Fopt(p_ + "base_speed"),
                bottom_up=bool(v[p_ + "bottom_up"].get()),
                pressure_psi=press, **common)
        elif kind == "sag":
            # The span list is the design, so it is parsed leniently (the same
            # helper the ladder's temperatures use) rather than through `F`: a
            # half-typed number mid-edit must leave the last good geometry
            # standing, not brick the whole card.
            params = mew_sweeps.SagParams(
                spans=mew_sweeps.parse_float_list(v["sag_spans"].get()),
                mirror=bool(v["sag_mirror"].get()),
                wall_len=F("sag_wall_len"), wall_layers=I("sag_layers"),
                travel_lane=v["sag_lane"].get().strip() or "middle",
                layers_per_visit=I("sag_visit"),
                fibre_inset=F("sag_inset"), lead=F("sag_lead"),
                speed=F("sag_speed"), col_gap=F("sag_col_gap"),
                base_speed=Fopt("sag_base_speed"),
                # the live droop law, so the bottoming-out warning is decided by
                # c*span^p rather than by the span in mm (see sag_span_warnings)
                **dict(zip(("sag_coeff", "sag_span_exp"), self._sw_sag_law())),
                **common)
        elif kind == "circle":
            params = mew_sweeps.CircleParams(
                cts=self._sw_resolve_cts(), gap=self._sw_gap(),
                eps=mew_sweeps.parse_float_list(v["circ_eps"].get()),
                reps=I("circ_reps"),
                reps_eps=mew_sweeps.parse_reps_overrides(
                    v["circ_reps_eps"].get()),
                # None while the table is blank -> the three fields above are
                # the truth and the plate is byte-identical to the pre-table
                # build. A table is the whole truth and they are ignored.
                specimens=self._sw_circ_specs(),
                lr_target=F("circ_lr"), amp_prior=F("circ_amp_prior"),
                layout=v["circ_layout"].get(),
                fixed_radius=Fzero("circ_fixed_radius"),
                floor=Fzero("circ_floor"),
                chord_tol=F("circ_chord_tol"),
                lead_mult=F("circ_lead_mult"), lead_min=F("circ_lead_min"),
                spacing=F("circ_spacing"),
                travel_offset=F("circ_travel_offset"),
                layers=I("circ_layers"),
                stab_speed_eps=F("circ_stab_eps"), **common)
        else:
            params = mew_sweeps.CtsParams(
                start=F("cts_start"), stop=F("cts_stop"), step=F("cts_step"),
                reps=I("cts_reps"), line_len=Fopt("cts_line_len"),
                pitch=F("cts_pitch"), col_gap=F("cts_col_gap"),
                cts_ref=F("cts_ref"),
                bottom_up=bool(v["cts_bottom_up"].get()),
                slowest_first=bool(v["cts_slowest_first"].get()),
                base_speed=Fopt("cts_base_speed"),
                **self._sw_cts_loop_params(v, F, Fopt),
                **common)
        return kind, params

    @staticmethod
    def _sw_cts_loop_params(v, F, Fopt):
        """The CTS ladder's linking loops, for `CtsParams`: off reads
        neither field, so a half-typed radius cannot block a print without
        loops."""
        if not v["cts_loops_on"].get():
            return {}
        return dict(loop_arc=F("cts_loop_arc"),
                    loop_speed=Fopt("cts_loop_speed"))

    # -- the mandrel lag tube: what it reads from the pages that own it ------- #
    def _sw_mandrel_diameter(self):
        """The mandrel's diameter (mm) = the Scaffold designer's 4-axis mandrel.

        The tube is printed on THE mandrel, and the app holds one number for it;
        the fit reads the same field when its own box is left blank, so the
        print and the fit cannot disagree about the radius. Falls back to 3 mm,
        that field's default."""
        try:
            d = _finite(getattr(self, "m_diameter",
                                tk.StringVar(value="3.0")).get())
            if d > 0:
                return d
        except (tk.TclError, ValueError):
            pass
        return 3.0

    def _sw_tube_place(self, length, radius, view=None):
        """`(start_x, rod, surface_z, datum_dx)` for a tube `length` mm long, in
        the g-code frame: where it starts, the rod's two ends, the Z of the bare
        surface, and the axial re-base already folded into those X values (the
        designer's `(-origin_u, 0, 0)`, which the player is told to undo).

        THE DESIGNER'S OWN PLACEMENT CODE, run on the rig view (`view`, from
        `_sw_rig_view`): the rod and the datum are the default 4-axis
        machine's, and the print's place on the rod this page's rod rows
        (`gui_mandrel._mandrel_axial_layout`, `_mandrel_origin_u`,
        `gui_buildplate._rotary_surface_z`). The X it returns is already
        re-based onto the datum -- the file needs no translation after, which
        is what `_sw_tube_player_args` then tells the player to undo. Pages
        that are not mounted (a headless test) fall back to a rod derived from
        the print and zero on its near end."""
        import gui_mandrel_preview
        v = self if view is None else view
        design = types.SimpleNamespace(length=float(length), params={})
        try:
            lay = v._mandrel_axial_layout(design)
            origin_u = float(v._mandrel_origin_u(design))
        except Exception:  # noqa: BLE001 -- no mandrel page: a derived rod
            lay = gui_mandrel_preview.axial_layout(design)
            origin_u = lay.rod_u0
        try:
            surface_z = float(v._rotary_surface_z(radius))
        except Exception:  # noqa: BLE001 -- no Machine page: the bare surface
            surface_z = 0.0
        # `+ 0.0` turns a negated 0.0 into 0.0, as `_mandrel_origin_offset` does
        return (lay.print_start_x - origin_u,
                (lay.rod_u0 - origin_u, lay.rod_u1 - origin_u), surface_z,
                -origin_u + 0.0)

    def _sw_exit_margin(self, F=None):
        """The end-of-print exit distance (mm) from this page's Export card, or
        None with the box unticked. A plate print runs off the slide edge
        (`mew_sweeps._with_exit`), a tube off the end of the rod. `F` is the
        snapshot's named-error reader; blank means the designer's default 5."""
        v = self.sw_vars
        if not v["exit_move"].get():
            return None
        if not v["exit_margin"].get().strip():
            return 5.0
        if F is not None:
            return F("exit_margin")
        try:
            return _finite(v["exit_margin"].get().strip())
        except ValueError:
            return 5.0

    def _sw_tube_exit_margin(self):
        """The tube's exit: this page's Export-card switch, as for a slide."""
        return self._sw_exit_margin()

    def _sw_tube_params(self, F, I):
        """The lag tube's `mew_sweeps.LagTubeParams` from this page's fields plus
        the conditions other pages own. `F` / `I` are the snapshot's own named-
        error readers, so a garbled field says which one it is."""
        import dataclasses as _dc
        v = self.sw_vars
        dia = self._sw_mandrel_diameter()
        speed = F("tube_speed")
        # the tube is laid as designed, never shrunk onto the rod: the room
        # is not passed, and a tube too long for it is warned about instead
        view, _room = self._sw_rig_for_print(F)
        base = mew_sweeps.LagTubeParams(
            mandrel_diameter=dia,
            angles=mew_sweeps.parse_float_list(v["tube_angles"].get()),
            spacing=F("tube_spacing"), lines=self._sw_tube_lines(F),
            band_len=F("tube_band_len"), band_gap=F("tube_band_gap"),
            speed=speed,
            settle_turns=F("tube_settle"), diam=self._sw_diam_mm(),
            exit_margin=self._sw_tube_exit_margin())
        start_x, rod, surface_z, datum_dx = self._sw_tube_place(
            mew_sweeps.lag_tube_length(base), 0.5 * dia, view)
        # the model's prediction, for the CSV and the merge warning only
        gap, cts = self._sw_gap(), self._sw_resolve_cts()
        try:
            cal = self._calibration_models()[2]
        except Exception:  # noqa: BLE001 -- no rig profile: the shipped one
            cal = None
        # ...on the mandrel's own values, when it has them: the prediction is
        # what the rotary lay will run on (`mew_lag_fit.mandrel_calibration`)
        extras = getattr(self, "_mandrel_cal_extras", lambda: {})()
        if extras:
            from mew_physics import Calibration
            cal = _dc.replace(cal or Calibration(), **extras)
            cts = float(extras.get("mandrel_cts") or cts)

        import mew_lag_fit

        def predict(alpha):
            return mew_lag_fit.predict_deviation(
                alpha, radius=0.5 * dia, gap=gap, speed=speed / 60.0,
                cts=cts, calibration=cal)

        try:
            lag = mew_lag_fit.predict_radii(radius=0.5 * dia, gap=gap,
                                            speed=speed / 60.0, cts=cts,
                                            calibration=cal)[0] or None
        except Exception:  # noqa: BLE001 -- a check, never a blocker
            lag = None
        return _dc.replace(base, start_x=start_x, start_z=surface_z, rod=rod,
                           datum_dx=datum_dx, deviation=predict, lag_mm=lag)

    def _sw_tube_lines(self, F):
        """"Lines per band" as an int, or None when blank (as many as fit once
        around). `F` names a garbled entry."""
        if not self.sw_vars["tube_lines"].get().strip():
            return None
        n = F("tube_lines")
        if n != int(n):
            raise ValueError("Lines per band needs a whole number")
        return int(n)

    def _sw_cts_mandrel_params(self, F, I):
        """`mew_sweeps.CtsMandrelParams` from the CTS (4-axis) tab's fields,
        placed on the rod exactly as the lag tube is (`_sw_tube_place`)."""
        import dataclasses as _dc
        dia = self._sw_mandrel_diameter()
        view, room = self._sw_rig_for_print(F)
        base = mew_sweeps.CtsMandrelParams(
            mandrel_diameter=dia, start=F("cts4_start"), stop=F("cts4_stop"),
            step=F("cts4_step"), reps=I("cts4_reps"),
            line_len=F("cts4_line_len"), pitch=F("cts4_pitch"),
            rung_gap=F("cts4_gap"), diam=self._sw_diam_mm(),
            exit_margin=self._sw_tube_exit_margin(), fit_len=room)
        start_x, rod, surface_z, datum_dx = self._sw_tube_place(
            mew_sweeps.cts_mandrel_length(base), 0.5 * dia, view)
        return _dc.replace(base, start_x=start_x, start_z=surface_z, rod=rod,
                           datum_dx=datum_dx)

    #: the engine kinds laid on the MANDREL: read through the rotary frame,
    #: drawn on the rod, exported with the mandrel flags
    _SW_ROTARY_KINDS = ("tube", "cts4")

    @staticmethod
    def _sw_tube_frame(params):
        """The rotary frame a tube file is read through: the mandrel's radius,
        and the g-code Z that means its bare surface."""
        import mew_frame
        return mew_frame.CylinderFrame(0.5 * float(params.mandrel_diameter),
                                       surface_z=float(params.start_z))

    def _sw_physics_scalars(self, kind):
        """The physics knob values (standoff mm, voltage kV, pressure Pa,
        temperature degC) for the preview relay -- ALL from the Printhead-Simulation
        tab (gap = sim_standoff for both experiment kinds), with safe defaults, so
        the preview matches the STL export (which drives the same sim knobs through
        _export_prefix)."""
        standoff = self._sw_gap()
        pressure = float(getattr(self, "sim_pressure",
                                 tk.StringVar(value="3")).get()) * PSI_TO_PA
        voltage = float(getattr(self, "sim_voltage",
                                tk.StringVar(value="5")).get())
        temperature = float(getattr(self, "sim_temperature",
                                    tk.StringVar(value="100")).get())
        return standoff, voltage, pressure, temperature

    def _sw_physics_options(self, kind):
        """A worker physics dict (jet-lag relay via mew_deposition) for the PREVIEW,
        or None. Gated on the Physics page's Jet lag switch (what the STL bakes)
        AND the preview's own Jet lag box (`_sw_preview_lag_on`). Robust: any
        missing module/param degrades to the commanded path."""
        if not self._sw_preview_lag_on():
            return None
        return self._sw_physics_dict(kind)

    def _sw_physics_dict(self, kind):
        """`_sw_physics_options` without its gate: the jet this print is
        printed with, for a picture whose own Jet lag box decides."""
        try:
            from mew_physics import MEWModel, Nozzle
        except Exception:  # noqa: BLE001
            return None
        try:
            gap, voltage, pressure, temperature = \
                self._sw_physics_scalars(kind)
            models = self._calibration_models()
            if models is not None:
                material, nozzle, calibration = models
                model = MEWModel(material=material, nozzle=nozzle,
                                 working_distance=gap, calibration=calibration)
            else:
                model = MEWModel(nozzle=Nozzle(), working_distance=gap)
            if kind in self._SW_ROTARY_KINDS:
                # the mandrel-only values, as a designed tube's preview folds
                # them in (gui_mandrel `_mandrel_physics_options`)
                extras = getattr(self, "_mandrel_cal_extras", lambda: {})()
                if extras:
                    import dataclasses as _dc
                    model.calibration = _dc.replace(model.calibration,
                                                    **extras)
            return dict(model=model, pressure=pressure, voltage=voltage,
                        temperature=temperature,
                        zeta=model.calibration.contact_damping_zeta,
                        coil=False, max_accel=0.0, square_corner_velocity=6.0,
                        # sizes the tube relay's corner grid; plates ignore it
                        fiber_diameter=self._sw_diam_mm())
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ #
    # edit -> debounce -> worker -> apply
    # ------------------------------------------------------------------ #
    def _sw_on_edit(self):
        if getattr(self, "_sw_panel", None) is None:
            return
        self._sw_update_cts_readout()          # cheap; keeps the readout live
        self._sw_state["dirty"] = True
        if self._sw_after is not None:
            try:
                self.root.after_cancel(self._sw_after)
            except (ValueError, tk.TclError):
                pass
        self._sw_after = self.root.after(SWEEP_DEBOUNCE_MS, self._sw_kick)

    def _sw_on_color(self):
        rgb = self._sw_parse_color()
        self._sw_state["color"] = rgb
        panel = getattr(self, "_sw_panel", None)
        if panel is not None:
            panel.set_depo_color(rgb)

    def _sw_diam_mult_value(self):
        """The visualization multiplier; blank, garbled or non-positive -> 1."""
        try:
            m = float(self.sw_diam_mult.get().strip())
            if m > 0:
                return m
        except (ValueError, tk.TclError):
            pass
        return 1.0

    def _sw_on_diam_mult(self):
        """Redraw the preview's tubes at the new factor; no print rebuild."""
        state = self._sw_state
        meta = state.get("meta")
        if meta is not None:
            meta["tube_diameter"] = meta["fiber_diameter"] *                 self._sw_diam_mult_value()
        state["tubes_pd"] = None
        self._sw_refresh_tubes()

    def _sw_parse_color(self):
        try:
            h = self.sw_color.get().strip().lstrip("#")
            if len(h) == 6:
                return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
                        int(h[4:6], 16) / 255.0)
        except (ValueError, tk.TclError):
            pass
        return (0.8, 0.25, 0.2)

    def _sw_kick(self):
        self._sw_after = None
        state = self._sw_state
        if not state["dirty"]:
            return
        try:
            kind, params = self._sw_snapshot_params()
            physics = self._sw_physics_options(kind)
        except Exception:  # noqa: BLE001 -- mid-typing values; keep last good
            state["status"] = "waiting for valid parameters…"
            self._sw_panel.set_status(state["status"])
            return
        state["dirty"] = False
        state["gen_sub"] += 1
        state["color"] = self._sw_parse_color()
        state["status"] = "updating…"
        self._sw_panel.set_status(state["status"])
        with self._sw_cv:
            # The mailbox is a single latest-only slot: a kick that overwrites an
            # un-dequeued slot COALESCES -- the worker will run once and put ONE
            # result. So _sw_pending must count DEQUEUES (== results the poller will
            # drain), which is exactly the number of empty->full transitions; a
            # per-kick increment leaks +1 on every coalesced kick, and _sw_poll then
            # re-arms its `after` forever.
            was_empty = self._sw_mailbox is None
            self._sw_mailbox = (state["gen_sub"], kind, params, physics)
            self._sw_cv.notify()
        if was_empty:
            self._sw_pending += 1
        self._sw_ensure_worker()
        self._sw_arm_poll()

    def _sw_ensure_worker(self):
        if self._sw_worker is None or not self._sw_worker.is_alive():
            self._sw_worker = threading.Thread(
                target=self._sw_worker_loop, daemon=True)
            self._sw_worker.start()

    def _sw_worker_loop(self):
        while True:
            with self._sw_cv:
                while self._sw_mailbox is None:
                    self._sw_cv.wait()
                gen, kind, params, physics = self._sw_mailbox
                self._sw_mailbox = None
            try:
                res, prints, plate = self._sw_run_generator(kind, params,
                                                            physics)
                self._sw_results.put((gen, res, prints, plate,
                                      float(params.diam), None))
            except Exception as exc:  # noqa: BLE001
                self._sw_results.put((gen, None, None, None, None, exc))

    @staticmethod
    def _sw_extract_tube(text, physics, frame, lift=0.0):
        """A tube file -> {depo, commanded, travels}, all WRAPPED onto the rod.

        The designer's own sequence (`gui_mandrel_preview.build_mandrel_preview`):
        parse through the rotary frame to unrolled `(u, v, h)`, relay through the
        touchdown transform WITH the frame -- without it the transform silently
        takes its flat branch, and the alternating deviation this print exists
        to show would not be drawn -- then wrap. The relay gets the designer's
        adaptive corner grid too, for the reason it gives there: on a tube
        nearly every vertex is a corner. No drape: one layer on a rod has
        nothing to sag onto, so the one thing the drape would do here is done
        directly -- `lift` (half a fibre diameter) raises the centreline to
        where a fibre resting on the bare rod has it, which is also what keeps
        it drawn ABOVE the rod's surface instead of inside it."""
        import mew_frame
        depo, travels, ptimes = mew_sweeps.extract_moves(
            text, with_times=True, frame=frame)
        depo = [np.asarray(p, dtype=np.float64) for p in depo]
        commanded = [p.copy() for p in depo]
        if physics is not None:
            try:
                import mew_deposition
                diam = float(physics.get("fiber_diameter") or 0.015)
                transform = mew_deposition.make_transform(
                    physics["model"], pressure=physics["pressure"],
                    voltage=physics["voltage"],
                    temperature=physics["temperature"],
                    zeta=physics.get("zeta", 1.0),
                    coil=physics.get("coil", False),
                    max_accel=physics.get("max_accel") or None,
                    square_corner_velocity=physics.get(
                        "square_corner_velocity", 6.0),
                    rotary_accel=physics.get("rotary_accel", 0.0),
                    corner_chord=mew_deposition.auto_corner_grid(diam))
                depo, _ = transform(depo, ptimes, frame=frame)
            except Exception:  # noqa: BLE001 -- commanded path fallback
                depo = commanded

        def wrap(paths):
            out = []
            for p in paths:
                if len(p) < 2:
                    continue
                q = np.array(p, dtype=np.float64, copy=True)
                q[:, 2] += lift
                out.append(np.asarray(mew_frame.wrap_polyline(frame, q),
                                      dtype=np.float64))
            return out
        # the unrolled fibre as well: the measurement map draws it flat
        return dict(depo=wrap(depo), commanded=wrap(commanded),
                    travels=wrap(travels),
                    depo_uv=[np.asarray(p, dtype=np.float64)[:, :2]
                             for p in depo if len(p) >= 2])

    @staticmethod
    def _sw_extract(text, physics, frame=None, lift=0.0,
                    arc_segments=mew_sweeps.DEFAULT_ARC_SEGMENTS):
        """One slide's g-code -> {depo, travels}. When physics is on, relay the
        deposition through mew_deposition.make_transform (the predicted touchdown
        path); any failure degrades to the commanded path. `frame` is the tube's
        rotary frame, or None for a plate slide."""
        if frame is not None:
            return _SweepsMixin._sw_extract_tube(text, physics, frame, lift)
        if physics is None:
            depo, travels = mew_sweeps.extract_moves(
                text, arc_segments=arc_segments)
            commanded = depo
        else:
            depo, travels, ptimes = mew_sweeps.extract_moves(
                text, with_times=True, arc_segments=arc_segments)
            try:
                import mew_deposition
                transform = mew_deposition.make_transform(
                    physics["model"], pressure=physics["pressure"],
                    voltage=physics["voltage"],
                    temperature=physics["temperature"],
                    zeta=physics.get("zeta", 1.0), coil=physics.get("coil", False),
                    max_accel=physics.get("max_accel") or None,
                    square_corner_velocity=physics.get(
                        "square_corner_velocity", 6.0),
                    rotary_accel=physics.get("rotary_accel", 0.0))
                dpaths = [np.asarray(p, dtype=np.float64) for p in depo]
                commanded = [np.asarray(p, dtype=np.float64) for p in depo]
                dpaths, _ = transform(dpaths, ptimes)
                depo = dpaths
            except Exception:  # noqa: BLE001 -- commanded path fallback
                commanded = depo
        return dict(depo=[np.asarray(p, dtype=np.float64) for p in depo],
                    # the COMMANDED toolpath, snapshotted before the touchdown
                    # relay -- what the "Show g-code" overlay draws. Physics off,
                    # the two are the same array, so the overlay lies exactly on
                    # the fiber, which is the truth.
                    commanded=[np.asarray(p, dtype=np.float64)
                               for p in commanded],
                    travels=[np.asarray(t, dtype=np.float64) for t in travels])

    def _sw_arm_poll(self):
        if self._sw_poll_id is None:
            self._sw_poll_id = self.root.after(SWEEP_POLL_MS, self._sw_poll)

    def _sw_poll(self):
        self._sw_poll_id = None
        try:
            while True:
                gen, res, prints, plate, diam, err = \
                    self._sw_results.get_nowait()
                self._sw_pending = max(0, self._sw_pending - 1)
                self._sw_apply_result(gen, res, prints, plate, diam, err)
        except queue.Empty:
            pass
        if self._sw_pending > 0:
            self._sw_arm_poll()

    def _sw_apply_result(self, gen, res, prints, plate, diam, err):
        state = self._sw_state
        if gen < state["gen_sub"]:
            return
        if err is not None:
            state["status"] = (str(err) if isinstance(err, ValueError)
                               else f"preview failed ({err})")
            self._sw_panel.set_status(state["status"])
            self._sw_warn_var.set(str(err) if isinstance(err, ValueError) else "")
            return
        self._sw_result = res
        self._sw_prints = prints
        self._sw_plate = plate
        self._sw_diam = diam
        n = len(prints)
        self._sw_count_var.set(f"of {n}")
        if self.sw_print_index.get() > n:
            self.sw_print_index.set(n)      # clamp the pager into range
        self._sw_summary_var.set(res.summary)
        self._sw_update_tube_note(res)
        # the bottom bar's Save g-code as… can write every slide now,
        # with or without a Generate (gui_shell._refresh_result_controls)
        refresh = getattr(self, "_refresh_result_controls", None)
        if refresh is not None and hasattr(self, "save_gcode_btn"):
            refresh()
        self._sw_render_warnings(res)
        self._sw_show_all()

    def _sw_render_warnings(self, res):
        # a mandrel print's warnings are about its rod, and they belong under
        # the rod rows of its own card (`_sw_rod_note_line`), not in the strip
        notes = getattr(self, "_sw_rod_notes", {})

        def sentence(w):
            w = w.strip()
            return (w[:1].upper() + w[1:]
                    + ("" if w.endswith((".", "!", "?")) else "."))
        for kind, vars_ in notes.items():
            text = (" ".join(sentence(w) for w in res.warnings)
                    if kind == res.kind else "")
            for var in vars_:
                var.set(text)
        if res.kind in self._SW_ROTARY_KINDS and res.kind in notes:
            self._sw_warn_var.set("  ·  ".join(res.oversize))
            return
        parts = list(res.oversize) + list(res.warnings)
        # a tube's own warning already names the rod it runs off
        if (not res.all_on_slide and res.kind not in self._SW_ROTARY_KINDS
                and not any("plate" in w for w in res.warnings)):
            parts.append("some geometry falls off the plate")
        self._sw_warn_var.set("  ·  ".join(parts))

    def _sw_show_all(self):
        """Show the slide the ribbon's Slide tool has picked, with its plate
        outline, specimens, travels and labels: the tree and the text say what
        that one slide holds, so the picture does too."""
        res, prints = self._sw_result, self._sw_prints
        if not prints:
            return
        if isinstance(self._sw_plate, dict):
            self._sw_show_tube()
            return
        ox, oy, w, h = self._sw_plate
        try:
            sel = max(1, min(int(self.sw_print_index.get()), len(prints))) - 1
        except tk.TclError:
            sel = 0
        pr = prints[sel]
        z_lab = self._sw_diam * 2.0
        all_paths = [np.asarray(p, dtype=np.float64) for p in pr["depo"]]
        all_cmd = [np.asarray(c, dtype=np.float64)
                   for c in pr.get("commanded") or []]
        all_travels = [np.asarray(t, dtype=np.float64) for t in pr["travels"]]
        all_labels = [(s.value_text, (s.center[0], s.center[1], z_lab))
                      for s in res.prints[sel].specimens]
        plates = [(ox, oy, w, h)]
        if not all_paths:
            all_paths = [np.zeros((1, 3))]
        pts = np.concatenate(all_paths, axis=0)
        corners = np.array([[p[0], p[1], 0.0] for p in plates]
                           + [[p[0] + p[2], p[1] + p[3], 0.0] for p in plates])
        lo = np.minimum(pts.min(axis=0), corners.min(axis=0))
        hi = np.maximum(pts.max(axis=0), corners.max(axis=0))
        state = self._sw_state
        state.update(
            paths=all_paths, travels=all_travels, plates=plates,
            plate_sel=0, labels=all_labels, rod=None, tube_map=None,
            lines_pd=None, tubes_pd=None, travel_pd=None, gcode_pd=None,
            display_mode=None,
            color=self._sw_parse_color(),
            # `gcode_paths` is the key the inherited overlay reads
            # (gui_preview.PreviewPanel._update_gcode_overlay). The page never
            # supplied it before, which is why "Show g-code" had nothing to draw.
            meta=dict(bbox=(tuple(float(x) for x in lo),
                            tuple(float(x) for x in hi)),
                      fiber_diameter=self._sw_diam, n_paths=len(all_paths),
                      tube_diameter=self._sw_diam * self._sw_diam_mult_value(),
                      gcode_paths=all_cmd),
            diag=float(np.linalg.norm(hi - lo)),
            status=self._sw_status_line())
        self._sw_panel.apply_state(state, self._sw_subtitle(sel))

    def _sw_tube_map_wanted(self):
        """True while the Instructions box is open: the
        "Where to measure" strip belongs to its Measure steps and is shown
        nowhere else."""
        grid = getattr(self, "_cal_grid", None)
        return grid is not None and grid.opened() == "instructions"

    def _sw_sync_tube_map(self):
        """Re-apply the strip's visibility after a card opens or closes."""
        panel = getattr(self, "_sw_panel", None)
        if panel is not None and hasattr(panel, "sync_tube_map"):
            panel.sync_tube_map()

    def _sw_show_tube(self):
        """A mandrel print in the preview -- the lag tube or the CTS (4-axis)
        ladder: the wrapped fibre on its rod.

        One print, so nothing is tiled and no plate is drawn; the rod takes the
        plate's place under the same "Collector" toggle. The labels float just
        above the rod's top (+Z, where v = 0 wraps to) over each band or speed,
        reading its winding angle or its speed."""
        res, pr = self._sw_result, self._sw_prints[0]
        x0, x1 = self._sw_plate["rod"]
        r = float(self._sw_plate["radius"])
        paths = list(pr["depo"]) or [np.zeros((1, 3))]
        z_lab = r + max(1.0, 0.5 * r)
        labels = [(s.value_text, (s.center[0], 0.0, z_lab))
                  for s in res.prints[0].specimens]
        pts = np.concatenate(paths, axis=0)
        lo = np.minimum(pts.min(axis=0), np.array([x0, -r, -r]))
        hi = np.maximum(pts.max(axis=0), np.array([x1, r, z_lab]))
        # the measurement map: each band's fibre unrolled and read at its
        # middle (`gui_tube_map`), from the same relayed path drawn above
        tube_map = None
        if res.kind == "tube" and pr.get("depo_uv"):
            import gui_tube_map
            try:
                tube_map = [gui_tube_map.band_view(pr["depo_uv"], row,
                                                   diam=self._sw_diam)
                            for row in res.rows
                            if row.get("section") == "meander"]
            except Exception:  # noqa: BLE001 -- a drawing, never a blocker
                tube_map = None
        state = self._sw_state
        state.update(
            paths=paths, travels=list(pr["travels"]), plates=[], plate_sel=0,
            rod=(x0, x1, r), labels=labels, tube_map=tube_map,
            lines_pd=None, tubes_pd=None, travel_pd=None, gcode_pd=None,
            display_mode=None, color=self._sw_parse_color(),
            meta=dict(bbox=(tuple(float(x) for x in lo),
                            tuple(float(x) for x in hi)),
                      fiber_diameter=self._sw_diam, n_paths=len(paths),
                      tube_diameter=self._sw_diam * self._sw_diam_mult_value(),
                      gcode_paths=list(pr.get("commanded") or [])),
            diag=float(np.linalg.norm(hi - lo)),
            status=self._sw_status_line())
        self._sw_panel.apply_state(state, self._sw_subtitle(0))

    def _sw_update_tube_note(self, res):
        """The live line under "Tube design": each band's line count and the
        spacing it is laid at, the gaps the model expects where it makes a
        prediction, and the tube's length. Read off the RESULT, so it is what
        the file holds rather than a second calculation of it."""
        self._sw_update_tube_cts()
        notes = getattr(self, "_sw_tube_notes", None)
        if not notes or res is None or res.kind != "tube":
            return
        bands = []
        for rec in res.rows:
            if rec.get("section") != "meander":
                continue
            what = "rings" if rec["angle_deg"] >= 90.0 else "lines"
            txt = "%g°%s: %d %s %.2f mm apart" % (
                rec["angle_deg"],
                " check" if rec.get("check_band") == "yes" else "",
                rec["lines"], what, rec["spacing_mm"])
            if rec.get("pred_wide_mm") is not None:
                txt += " (model gaps: wide %.2f, narrow %.2f)" % (
                    rec["pred_wide_mm"], max(0.0, rec["pred_narrow_mm"]))
            bands.append(txt)
        meta = res.prints[0]
        text = ("%s. The tube is %.1f mm long."
                % ("; ".join(bands) or "No bands",
                   meta.bbox[2] - meta.bbox[0]))
        for var in notes:
            var.set(text)

    def _sw_subtitle(self, sel):
        """The preview's one short line: which print, and -- only when there
        is more than one slide -- which of them Generate builds.

        It used to open with the engine's full summary ("CTS sweep 600 -> 240
        mm/min, step 60 (1.000 mm/s) x 3 reps: 7 speeds, 21 lines @ 43.3 mm,
        ..."), a paragraph restating the form beside it. That text is now the
        Calibration settings tile's summary, next to the fields it describes."""
        res = self._sw_result
        if res is None:
            return ""
        # the protocol TOKEN, not res.kind: the viscosity plate runs the
        # ladder's generator, so its result says "diam"
        kind = self.sw_vars["kind"].get()
        name = getattr(self, "_PROTOCOL_NAMES", {}).get(kind, kind)
        if name.startswith("the "):
            name = name[4:]
        parts = [name[:1].upper() + name[1:]]
        if res.n_slides > 1:
            parts.append(f"Generate builds slide {sel + 1} of {res.n_slides}")
        if self._sw_physics_on():
            parts.append("physics path")
        return " · ".join(parts)

    def _sw_status_line(self):
        res = self._sw_result
        if res is None:
            return ""
        total = sum(m.n_items for m in res.prints)
        travels = sum(len(p["travels"]) for p in self._sw_prints)
        off = sum(1 for m in res.prints if not m.on_slide)
        if res.kind == "tube":
            # a tube is on a rod, not a slide
            warn = "  ·  ⚠ longer than the mandrel" if off else ""
            return f"{total} bands · {travels} travels{warn}"
        warn = f"  ·  ⚠ {off} slide(s) off plate" if off else ""
        return (f"{total} specimens · {res.n_slides} slide(s) · "
                f"{travels} travels{warn}")

    def _sw_on_select(self):
        """The Slide tool moved: show that slide (no geometry rebuild)."""
        if self._sw_result is not None:
            self._sw_show_all()

    # ------------------------------------------------------------------ #
    # display restyles (no rebuild)
    # ------------------------------------------------------------------ #
    def _sw_refresh_display(self):
        panel = getattr(self, "_sw_panel", None)
        if panel is None:
            return
        panel.sync_visibility()
        panel._render_soon()

    def _sw_refresh_tubes(self):
        panel = getattr(self, "_sw_panel", None)
        if panel is None:
            return
        panel._update_display_mode(force=True)
        panel._render_soon()

    def _sw_on_panel_mapped(self):
        self._sw_trace_dark()
        state = self._sw_state
        if state.get("paths") is not None:
            self._sw_show_all()
        if state["dirty"] or state.get("paths") is None:
            state["dirty"] = True
            if self._sw_after is not None:
                try:
                    self.root.after_cancel(self._sw_after)
                except (ValueError, tk.TclError):
                    pass
                self._sw_after = None
            self._sw_kick()

    # ------------------------------------------------------------------ #
    # actions -- driven by the SHARED bottom action bar (like every other
    # design page): Generate builds the target slide + feeds the result flow;
    # Save STL as… / Save g-code as… export. No in-form action buttons.
    # ------------------------------------------------------------------ #
    def _sw_selected_slide_gcode(self):
        """Write the selected slide's g-code to the scratch session folder and
        return its path (the converter/player read a FILE). None if no result."""
        res = self._sw_result
        if res is None:
            return None
        try:
            idx = max(1, min(int(self.sw_print_index.get()),
                             len(res.gcode_texts))) - 1
        except tk.TclError:
            idx = 0
        path = os.path.join(SESSION_DIR, f"sweep_slide{idx + 1}.gcode")
        try:
            with open(path, "w", newline="") as fh:
                fh.write(self._sw_slide_source(idx, res.gcode_texts[idx]))
        except OSError as exc:
            self.status.set(f"could not stage g-code: {exc}")
            return None
        return path

    def _sw_player_color(self):
        return self.sw_color.get().strip()

    def _sw_fiber_params(self):
        """The player's fiber params for the slide this run built.

        `--render-travel` rides along for the same reason the scaffold and
        G-code panels send it (`gui_args._render_travel_arg`): it is a .depo
        CACHE KEY, not a mode the player implements. The export writes the
        travel fibre into the cache without changing the fibre count, so the
        player cannot tell a travel cache from a plain one and has to be told
        -- omit it and the locked sim rejects the very cache this build just
        wrote."""
        base = (["--diameter", repr(float(self._sw_diam)),
                 "--faces", self._sw_faces(),
                 "--diameter-multiplier", repr(self._sw_diam_mult_value())]
                + self._sw_quality_args() + ["--render-travel"]
                + (["--travel-gcode-line"]
                   if self.sw_vars["travel_gcode_line"].get() else []))
        # THE FIBER COLOUR, so the Viewer draws the slide the colour the preview
        # drew. A slide is one part with no `--scaffold-groups` list to carry a
        # colour (the same gap `gui_mandrel._player_args` closes for a tube), so
        # without this every fiber fell back to the player's off-white.
        # Blank or malformed -> flag omitted -> the player's own default.
        col = self._sw_player_color()
        if re.fullmatch(r"#?[0-9a-fA-F]{6}", col):
            base += ["--fiber-color", "#" + col.lstrip("#")]
        tube = self._sw_tube_geometry()
        if tube is None:
            return base
        # the player's collector is the rod, told in the designer's words
        # (`gui_mandrel._fiber_params_mandrel`): the rod's near end and length
        # in the g-code frame the file is written in, the bare-surface Z, and
        # the standoff; plus the same corner-grid tier the build used
        x0, x1 = tube["rod"]
        return base + ["--mandrel", repr(2.0 * tube["radius"]),
                       "--mandrel-start", repr(x0),
                       "--mandrel-length", repr(x1 - x0),
                       "--mandrel-surface-z", repr(tube["surface_z"]),
                       "--standoff", repr(self._sw_gap())] \
            + self._sw_tube_corner_args()

    def _sw_tube_geometry(self):
        """The last result's rod, when it is a tube: {"rod", "radius",
        "surface_z", "origin_dx"}; None for a plate print.

        Read off the RESULT the build exports, never the live fields, so the
        STL, the player and the preview all describe the file on disk."""
        plate = getattr(self, "_sw_plate", None)
        res = getattr(self, "_sw_result", None)
        if (not isinstance(plate, dict) or res is None
                or res.kind not in self._SW_ROTARY_KINDS):
            return None
        return dict(plate)

    def _sw_rig_view(self, place="left", offset=0.0, collector="mandrel"):
        """`(view, from_default)`: the app as a MANDREL calibration print sees
        it -- the Machine page's fields read from the DEFAULT 4-AXIS MACHINE
        preset, the stage a mandrel, and the print's place on the rod this
        page's own (`rod_place` / `rod_offset`, not the Scaffold designer's).
        `collector="plate"` is the same view for a FLAT print: the default
        3-axis machine preset, and no stage.

        The Machine page boots 3-axis and only the Collector toggle swaps its
        machine, so reading the live page put a calibration tube on a rod
        derived from the print (it grew with the ladder) and on the flat
        printer. Nothing here writes to the page (`_RigView`). No 4-axis
        default set: the live page's values, with the stage still a mandrel;
        `from_default` says which."""
        keys = self._keys_for_kind("machine")
        pmap = self._persist_map()
        st = getattr(self, "_pstores", {}).get("machine")
        name = st["defaults"].get(collector) if st else None
        idx = self._preset_name_index("machine", name) if name else None
        values = {}
        if idx is not None:
            data = self._migrate_preset_data(st["presets"][idx]["data"])
            defaults = getattr(self, "_field_defaults", {})
            for k in keys:
                v = data[k] if k in data else defaults.get(k)
                if v is not None and k in pmap:
                    values[id(pmap[k])] = v
        # the stage IS a mandrel (or absent, for a flat print);
        # `_machine_mode_sync` would read it off the Collector toggle, which a
        # calibration print does not follow
        self._rotary_vars_make()
        values[id(self.rotary_on)] = collector == "mandrel"
        label = getattr(self, "_rotary_kind_labels", {}).get("mandrel")
        if collector == "mandrel" and label and                 getattr(self, "rotary_kind", None) is not None:
            values[id(self.rotary_kind)] = label

        # one calibration tube: never the designer's copies or layer blocks
        methods = {"_mandrel_place": lambda: place,
                   "_mandrel_place_offset": lambda: float(offset),
                   "_mandrel_copies": lambda: 1,
                   "_mandrel_tube_overhang": lambda design: None}
        return view_with(self, values, methods), idx is not None

    def _sw_rod_placement(self, F):
        """`(place, offset)` from this page's two rod rows; `F` names a garbled
        offset. A negative offset would hang the print off the rod."""
        import gui_mandrel
        place = gui_mandrel.placement_from_saved(
            self.sw_vars["rod_place"].get())
        bad = gui_mandrel.placement_problem(place)
        if bad:
            raise ValueError(bad)
        offset = F("rod_offset")
        if offset < 0.0:
            raise ValueError("the end offset cannot be negative")
        return place, offset

    def _sw_rig_for_print(self, F):
        """`(view, room)` for a mandrel print being snapshot: the rig view
        placed by this page's rod rows, and the rod length it leaves free.
        A page without the Machine or mandrel mixins (a headless test) gets
        `(None, None)`: the old derived rod, laid as designed."""
        place, offset = self._sw_rod_placement(F)
        try:
            view, _from_default = self._sw_rig_view(place, offset)
        except Exception:  # noqa: BLE001 -- no Machine page to view
            return None, None
        return view, self._sw_rod_room(view, place, offset)

    @staticmethod
    def _sw_rod_room(view, place, offset):
        """The rod length free for the print (mm), or None when no rod length
        is declared -- the print is then laid as designed and the rod derived
        from it (`gui_mandrel_preview.axial_layout`)."""
        try:
            rod_len, _x0 = view._rotary_rod()
        except Exception:  # noqa: BLE001 -- no Machine page: no rod to fit
            return None
        if rod_len is None:
            return None
        return rod_len if place == "centre" else rod_len - offset

    def _sw_rig_machine_args(self):
        """`--machine` for the viewers. A MANDREL calibration print (the lag
        tube, the 4-axis CTS ladder) plays on the DEFAULT 4-AXIS MACHINE preset,
        whatever the Machine page holds. A FLAT one plays on the Machine page
        as it was at Generate when the Collector toggle was on 3 axis, and on
        the DEFAULT 3-AXIS preset (no stage) when it was not -- the page then
        held a rotary machine. Any other result plays on the machine it was
        generated on (`_viewer_machine_args`). Written by `_machine_args`, the
        one writer, run on the rig view (`_sw_rig_view`). No 4-axis default
        set -> the Machine page, said so in the log."""
        gco = getattr(self, "_sw_rig_gcode", None)
        coll = getattr(self, "_sw_rig_collector", "mandrel")
        if (gco is None or gco != getattr(self, "last_gcode", None)
                or coll == "page"):
            return self._viewer_machine_args()
        view, from_default = self._sw_rig_view(collector=coll)
        if not from_default and coll == "mandrel":
            self.append_log("[printer] no default 4-axis printer preset is set, so "
                            "this mandrel print plays on the Printer & collector page's "
                            "printer.\n")
        return view._machine_args()

    def _sw_tube_corner_args(self):
        """The 4-axis corner sampler, the designer's own flag for tubes; [] with
        physics deposition off, where the plain converter would reject it."""
        fn = getattr(self, "_auto_corner_grid_args", None)
        try:
            return list(fn(float(self._sw_diam))) if callable(fn) else []
        except Exception:  # noqa: BLE001
            return []

    # (self._sw_diam is the diameter the LAST result was built at -- taken from
    # `_sw_diam_mm` in the snapshot -- so the STL and the preview cannot differ.)

    @staticmethod
    def _sw_layer_by_deposition_arg(gcode_path):
        """`--layer-by-deposition` for a calibration slide, when its g-code needs it.

        The scaffold page gates this flag on whether the emitted objects depart from
        ascending Z (`gui_args._layer_by_deposition_arg`), reading the answer off the
        scaffold specs. A slide has no specs -- it is one generated g-code file -- so
        the same question is asked of the g-code itself, through the converter's OWN
        parser, so the criterion cannot drift from what the converter will do.

        A calibration slide is a genuine multi-body layout and it DOES depart: the
        sag ladder lays its bridging scaffold up to Z ~ 0.087, then travels back down
        to the plate to start the wall row at Z 0. Sorted by height, that descending
        travel's median Z puts it in layer 0, where it rests on the bed from its
        first vertex and runs through the scaffold's bottom layer at the corner
        (measured: first vertex at Z 0.01166 mm, 0.000 d of clearance; in deposition
        order 0.05303 mm, 1.897 d). In deposition order it becomes its own later
        layer and rests on what was already laid, which is what the jet does.

        Returns [] for an ascending-Z slide, where `_layer_ids_deposition` is
        identical to `_layer_ids` by construction (gcode_to_fibers.py:2031) and the
        flag would only churn the .depo cache key. Unreadable g-code -> [] as well:
        this is an ordering hint, never a reason to fail a build."""
        try:
            import gcode_to_fibers as _g2f
            paths, ptimes, travels = _g2f.parse_gcode(
                gcode_path, with_times=True, with_travels=True)
            # travels are deposited on this page (--render-travel above), so merge
            # them in first -- the descending travel is the very fibre at issue.
            paths, _ptimes = _g2f.merge_travels(paths, ptimes, travels)
            zs = [float(np.median(np.asarray(p, dtype=np.float64)[:, 2]))
                  for p in paths if len(p)]
        except Exception:  # noqa: BLE001 -- an ordering hint, never fatal
            return []
        departs = any(zs[j + 1] < zs[j] - 1e-9 for j in range(len(zs) - 1))
        return ["--layer-by-deposition"] if departs else []

    def _sw_export_prefix(self):
        """The converter command PREFIX for the slide build -- the SAME
        _export_prefix the scaffold page uses (gui_sim). When the Printhead-
        Simulation tab's 'Physics-deposited scaffold' toggle is ON it routes through
        mew_export.py with that tab's standoff / voltage / pressure / temperature
        (+ --physical-sag / --physical-fusion / --coil as configured), so the
        WRITTEN STL and .depo cache carry the jet-lag rounding on the squares AND
        the stabilization scaffold -- identical physics to a scaffold export. OFF -> the
        plain converter. No page-local physics knobs, so nothing can disagree."""
        return self._export_prefix("worker:convert", "converter")

    def _sw_dirty_or_pending(self):
        """True when the preview pipeline has NOT settled on the current form: the
        state is dirty, a debounce is armed, a worker compute is still in flight, or
        the mailbox holds un-dequeued params. Generate consults this so it never
        exports a result that lags the form by an edit (the debounced worker only
        catches up after the window closes)."""
        if self._sw_state.get("dirty"):
            return True
        if self._sw_after is not None:
            return True
        if self._sw_pending > 0:
            return True
        with self._sw_cv:
            return self._sw_mailbox is not None

    def _sw_run_generator(self, kind, params, physics):
        """Run the sweep engine + per-slide move extraction the SAME way the worker
        loop does, but synchronously (on the Tk thread). Returns (res, prints,
        plate). The worker loop calls this too, so the two cannot drift.

        A tube is read through its rotary frame, and its collector is a rod, not
        a plate: `plate` is then a dict -- the rod's ends `rod`, its `radius`,
        the bare-surface `surface_z` and the datum shift `origin_dx` the file
        was written with -- instead of `(ox, oy, w, h)`. `_sw_show_all`
        branches on that, and Generate and the player read it back
        (`_sw_tube_geometry`), so all three describe the same file."""
        res = mew_sweeps.generate(kind, params)
        frame = (self._sw_tube_frame(params)
                 if kind in self._SW_ROTARY_KINDS else None)
        prints = [self._sw_extract(text, physics, frame,
                                   0.5 * float(params.diam))
                  if frame is not None else
                  self._sw_extract(text, physics,
                                   arc_segments=res.arc_segments)
                  for text in res.gcode_texts]
        if frame is not None:
            # through the frame's interface, not its attribute (frame contract
            # rule 1): the bare surface is h = 0
            plate = {"rod": tuple(params.rod),
                     "radius": frame.surface_radius(0.0),
                     "surface_z": frame.surface_z,
                     "origin_dx": float(params.datum_dx)}
        else:
            plate = (params.origin_x, params.origin_y, params.slide_w,
                     params.slide_h)
        return res, prints, plate

    def _sw_ensure_fresh(self):
        """Guarantee _sw_result reflects the CURRENT form before Generate exports it.
        If the pipeline is settled this is a no-op (returns True). Otherwise snapshot
        + run the generator synchronously and adopt the result (bumping gen_sub so any
        in-flight worker output is discarded, and cancelling the pending debounce), so
        hitting Generate inside the debounce window or while the worker runs exports
        the geometry actually on the form -- not the previous edit. A snapshot / engine
        error surfaces the named message exactly like the debounce path (_sw_kick /
        _sw_apply_result) and returns False so the caller ABORTS the build."""
        if not self._sw_dirty_or_pending():
            return True
        try:
            kind, params = self._sw_snapshot_params()
            physics = self._sw_physics_options(kind)
        except Exception as exc:  # noqa: BLE001 -- mid-typing / blank required field
            self._sw_surface_error(exc)
            return False
        try:
            res, prints, plate = self._sw_run_generator(kind, params, physics)
        except Exception as exc:  # noqa: BLE001 -- engine ValueError / failure
            self._sw_surface_error(exc)
            return False
        if self._sw_after is not None:
            try:
                self.root.after_cancel(self._sw_after)
            except (ValueError, tk.TclError):
                pass
            self._sw_after = None
        self._sw_state["dirty"] = False
        self._sw_state["gen_sub"] += 1
        self._sw_apply_result(self._sw_state["gen_sub"], res, prints, plate,
                              float(params.diam), None)
        return True

    def _sw_surface_error(self, exc):
        """Show an aborting snapshot/engine error the way the debounce pipeline does:
        a named ValueError goes to the status line + the warnings readout; anything
        else reads as a generic preview failure."""
        named = isinstance(exc, ValueError)
        msg = str(exc) if named else f"preview failed ({exc})"
        self._sw_state["status"] = msg
        panel = getattr(self, "_sw_panel", None)
        if panel is not None:
            panel.set_status(msg)
        try:
            self._sw_warn_var.set(str(exc) if named else "")
        except Exception:  # noqa: BLE001
            pass

    def _sw_generate(self):
        """Bottom-bar Generate for the sweeps page. Build the TARGET slide's
        sagged STL + .depo through the shared converter/export pipeline
        (off-thread, like the scaffold Save-STL path) and stage its g-code, then
        feed the SHARED result state (last_gcode/last_stl/last_depo) so Save STL,
        Save g-code and every sidebar viewer (STL / locked- & live-sim) light up
        -- exactly like a scaffold Generate. Does not auto-open a viewer."""
        if getattr(self, "_sw_build_busy", False):
            return
        # The Physics-page check `gui_run.run()` makes for every other Generate;
        # this one does not pass through run(), and it exports with the same
        # profile, so a field reading "abc" would build at the factory default.
        err = (getattr(self, "_validate_calibration", lambda: None)()
               or self._sw_quality_error())
        if err:
            self.status.set(err)
            return
        # Never export a debounce-stale geometry: if the form changed since the last
        # settled preview, rebuild the result synchronously first (aborts on a named
        # snapshot error, surfaced like the debounce path).
        if not self._sw_ensure_fresh():
            return
        if self._sw_result is None:
            self.status.set("Waiting for valid sweep parameters. "
                            "Nothing to generate yet.")
            return
        gco = self._sw_selected_slide_gcode()
        if gco is None:
            return
        out = os.path.splitext(gco)[0] + ".stl"
        # a tube's export profile carries the mandrel-only values, exactly as
        # a designed tube's does (gui_mandrel `_calibration_args`)
        self._sw_tube_dispatch = self._sw_tube_geometry() is not None
        try:
            prefix = self._sw_export_prefix()
        finally:
            self._sw_tube_dispatch = False
        # `--progress` right after the input, as the G-code panel places it
        # (gui_args._build_args_gcode): the converter reports how far through its
        # work it is and the shared pump fills the bar (gui_run._on_progress).
        args = prefix + [
            gco, "--progress", "-d", repr(float(self._sw_diam)),
            "-f", self._sw_faces(),
            "--diameter-multiplier", repr(self._sw_diam_mult_value())]
        # the Quality card: solver, reset, mesh detail -- and the arc density
        # this result's arcs need. The player gets the same flags
        # (`_sw_fiber_params`), because they are the .depo cache key.
        args += self._sw_quality_args() + ["-o", out]
        # The jet deposits while travelling, always -- the same unconditional
        # flag the scaffold and G-code panels send (gui_args:439, :593). A
        # calibration print is the one place it MATTERS most: the ladder's
        # wall-to-wall hops and the CTS sweep's column connectors are real
        # fibre the operator will see on the slide, and without this they
        # reached the preview (which draws travels itself) but neither the STL
        # nor the .depo the sim reveals. `--travel-in-stl` stays the user's
        # choice, read from the control that owns it rather than duplicated
        # here, exactly as the plate and the fiber diameter are.
        args.append("--render-travel")
        if self.sw_vars["travel_in_stl"].get():
            args.append("--travel-in-stl")
        # Sag in DEPOSITION order when this slide's g-code departs from ascending Z
        # -- the same flag and the same criterion as the scaffold page
        # (`gui_args._layer_by_deposition_arg`), evaluated on the g-code because a
        # slide has no scaffold specs to read it off.
        args += self._sw_layer_by_deposition_arg(gco)
        # THE COLLECTOR DATUM. The same `_plate_args()` block the scaffold and
        # G-code panels send (gui_args:445, :604) -- and for the same reasons,
        # none of which stop at the page boundary:
        #
        #   --plate*     writes the grey <stem>_plate.stl the viewers show as
        #                the collector. Without it `_resolve_plate_path` finds
        #                nothing, `_collector_available()` is false, and the
        #                Collector switch is not even OFFERED, and the
        #                collector plate goes missing on a default sag ladder. A
        #                calibration print is laid on a
        #                microscope slide sitting on that plate; hiding the
        #                plate hides what the operator is looking at.
        #   --plate-top-z the print's floor. Left out, a plate thicker or
        #                thinner than the default put the slide's fibres at
        #                the wrong Z relative to the collector.
        #   --origin-offset the g-code-origin re-base. This one was WORSE than
        #                missing: `gui_viewers._player_args` sends the player
        #                `--origin` to UNDO an offset the calibration export
        #                never applied, so any non-zero origin datum moved the
        #                whole calibration scene off the plate.
        #
        # Guarded because the Build-plate page is a sibling mixin the headless
        # calibration tests do not always install; a bare call there is an
        # AttributeError, not a missing plate.
        tube = self._sw_tube_geometry()
        # a mandrel print plays on the default 4-axis machine once it is built,
        # and a flat one on a 3-axis machine (`_sw_rig_machine_args`);
        # committed in `_sw_generate_done`. A flat print generated with the
        # Collector toggle on 3 axis keeps the Machine page as it stands now.
        self._sw_rig_pending = tube is not None
        coll = getattr(self, "s_collector", None)
        self._sw_rig_pending_page = (tube is None and coll is not None
                                     and coll.get() == "plate")
        note = getattr(self, "_viewer_scene_note", None)
        if callable(note):
            note()
        plate_args = getattr(self, "_plate_args", None)
        if tube is not None:
            # A TUBE IS NOT A SLIDE. The converter reads the A word only
            # through a rotary frame (without `--mandrel-diameter` the file
            # parses as a flat zigzag, with no error), and the frame has to
            # know which Z is the bare surface -- the designer's own two flags
            # (`scaffold_gcode`'s mandrel branch forwards exactly these). No
            # plate args: there is no plate, and the file is already on the
            # user's datum, so there is no re-base for the converter to apply.
            args += ["--mandrel-diameter", repr(2.0 * tube["radius"]),
                     "--mandrel-surface-z", repr(tube["surface_z"])]
            args += self._sw_tube_corner_args()
        elif callable(plate_args):
            args += plate_args()
        # ...and freeze the offset those args just baked in, exactly as
        # gui_run.run does, so editing the origin fields between Generate and
        # Play cannot make the player undo a different one. A tube's offset is
        # the axial one it was generated with, which is what the designer
        # freezes for its tubes (`gui_mandrel._origin_offset`).
        try:
            self._frozen_origin_arg = (
                ["--origin={},0.0,0.0".format(repr(tube["origin_dx"]))]
                if tube is not None else list(self._viewer_origin_arg()))
        except Exception:  # noqa: BLE001  (play_gcode falls back to a live read)
            self._frozen_origin_arg = None
        self.status.set(
            "Building slide STL (physics-deposited)…" if self._sw_physics_on()
            else "Building slide STL (plain). Turn on Jet lag on the Physics "
                 "page for jet-lag rounding…")
        # Print history: a calibration print is one entry like any Generate
        # (gui_run.run makes the same call), finished in _sw_generate_done
        start = getattr(self, "_history_on_run_start", None)
        if callable(start):
            start("calibration", args)
        self._sw_launch_build(args, gco, out)

    def _sw_launch_build(self, args, gco, out):
        """Launch the slide-build subprocess off the Tk thread and route completion to
        _sw_generate_done. Uses Popen (NOT subprocess.run) and REGISTERS the handle as
        self.proc, so the shared bottom-bar Cancel can actually kill a heavy build.

        The worker touches only self.proc and a result box; completion is delivered by a
        MAIN-thread poller, because `self.root.after` may only be called from the Tk
        thread. Factored out so a test can drive the build/cancel path with a stub.

        THE OUTPUT GOES TO THE LOG, line by line, through the same `out_q` ->
        `_poll_output` -> `append_log` pump every scaffold run uses (gui_run:169,
        :186). It used to go to `proc.communicate()`, which drains the pipe and
        throws it away -- so a calibration Generate wrote NOTHING to the Log page
        and a user reporting a bad slide would have nothing to send. The pump is
        shared, so the parse counts, the sag timings, the
        travel-fibre line and any traceback all land where they do for a scaffold,
        and `[GCODE-READY]` lights the result buttons mid-build for free.

        Completion is NOT pushed as the pump's ("done", code): that event runs
        `gui_run._on_finished`, which owns the scaffold run's Keep/Save/Delete
        bookkeeping and would mislabel this build. The existing `poll()` below
        stays the one completion path, and waits for the queue to drain first so
        the status line cannot land above the worker's own last words."""
        self._sw_build_busy = True
        refresh = getattr(self, "_refresh_viewer_idle", None)
        if callable(refresh):
            refresh()    # Simulation's idle line says a run is under way
        self.cancelled = False        # the shared cancel() flag; a prior run's value
                                      # must not mislabel this build as cancelled
        self.run_btn.configure(state="disabled")
        try:
            self.cancel_btn.configure(state="normal")
        except (AttributeError, tk.TclError):
            pass
        # The bar is shared with the scaffold runs: empty and determinate, filled by
        # the converter's own `--progress` reports through the shared pump
        # (`_sw_generate` asks for them). It is NOT borrowed as a bouncer -- the
        # build reports a real fraction, and a bounce read as a glitch.
        self._progress_idle()
        creationflags = 0x08000000 if IS_WINDOWS else 0
        # the log opens on THIS run, headed by the command, exactly as gui_run.run
        # does (gui_run:100). A partial build in a headless test has no log widget.
        try:
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")
            self.append_log(
                "$ " + " ".join(f'"{a}"' if " " in a else a for a in args) + "\n\n")
        except (AttributeError, tk.TclError):
            pass
        # re-arm the one-shot [GCODE-READY] handler for this run, like gui_run.run:
        # left set from an earlier run it would swallow this build's announcement,
        # and it is never re-armed anywhere else.
        self._gcode_ready_seen = False
        box = {}

        def work():
            ok = False
            try:
                proc = subprocess.Popen(
                    args, cwd=HERE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, bufsize=1,
                    creationflags=creationflags, **WORKER_PIPE_UTF8)
                self.proc = proc          # register so the shared Cancel reaches it
                # READ the pipe rather than draining it into nothing: this both
                # feeds the log and keeps a chatty worker from stalling on a full
                # pipe, which is what communicate() was here for.
                for line in proc.stdout:
                    self.out_q.put(("line", line))
                ok = (proc.wait() == 0 and os.path.isfile(out))
            except Exception as exc:  # noqa: BLE001
                self.out_q.put(("line", f"\n[reader error: {exc}]\n"))
                ok = False
            box["ok"] = ok
        worker = threading.Thread(target=work, daemon=True)
        worker.start()

        def poll():
            # the drained-queue condition keeps _sw_generate_done's status line
            # below the worker's last output; _poll_output runs every 120 ms
            if worker.is_alive() or not self.out_q.empty():
                self.root.after(SWEEP_POLL_MS, poll)
                return
            self._sw_generate_done(box.get("ok", False), gco, out)
        self.root.after(SWEEP_POLL_MS, poll)

    def _sw_generate_done(self, ok, gco, out):
        self._sw_build_busy = False
        refresh = getattr(self, "_refresh_viewer_idle", None)
        if callable(refresh):
            refresh()    # the run is over
        self.proc = None
        self._progress_idle()
        self.run_btn.configure(state="normal")
        try:
            self.cancel_btn.configure(state="disabled")
        except (AttributeError, tk.TclError):
            pass
        # close the log the way gui_run._on_finished closes a scaffold run's, so a
        # log pasted into a bug report says how the build ENDED and not just what
        # it printed on the way
        def _close(note):
            try:
                self.append_log(note)
            except (AttributeError, tk.TclError):
                pass
        finish = getattr(self, "_history_on_run_finish", None)
        if not callable(finish):
            finish = lambda *_a: None  # noqa: E731
        if getattr(self, "cancelled", False):
            self.cancelled = False
            self.status.set("Build cancelled.")
            _close("\n[cancelled by user]\n")
            finish("cancelled", None)
            return
        if not ok:
            self.status.set("Slide STL build failed. Check the fiber diameter "
                            "and the plate, and see the log.")
            _close("\n[slide build failed]\n")
            finish("failed", None)
            return
        _close("\n[finished successfully]\n")
        self.last_gcode = gco
        # the g-code view rewrites a slide with THIS page's Export options
        # (`_gcode_emit_opts`), never with a scaffold run's left-over ones
        self._last_gcode_is_scaffold = False
        self._last_gcode_is_calibration = True
        self.last_stl = out
        self._sw_rig_gcode = gco
        self._sw_rig_collector = (
            "mandrel" if getattr(self, "_sw_rig_pending", False) else
            "page" if getattr(self, "_sw_rig_pending_page", False) else "plate")
        commit = getattr(self, "_viewer_scene_commit", None)
        if callable(commit):
            commit(gco)
        depo = os.path.splitext(out)[0] + DEPO_CACHE_EXT
        self.last_depo = depo if os.path.isfile(depo) else None
        # the build-plate sibling this run wrote, tracked off the -o STEM the same
        # way gui_run does (gui_run:309). The player's own search looks next to the
        # STL and next to the g-code, which DO both resolve here -- but tracking it
        # explicitly is what keeps a stale plate from an earlier scaffold Generate
        # from being handed to the player as this slide's collector.
        plate = os.path.splitext(out)[0] + "_plate.stl"
        self.last_plate = plate if os.path.isfile(plate) else None
        # a sweep slide is a printable export like any other, so it goes in the
        # index the Machine-link reveal matches against (gui_core.register_export)
        self.register_export(gco, self.last_depo)
        self.last_fiber_params = self._sw_fiber_params()
        res = self._sw_result
        what = ("every slide" if res is not None and res.n_slides > 1
                else "the print")
        # The operator is still in Calibrate -- a Generate never navigates --
        # so the line names the NEXT step of the procedure in the bottom row's
        # own words, not the scratch file it wrote (direction page, step 2).
        try:
            proc = self._cal_procedures().get(self._cal_type())
            name = "the “%s” print" % proc.tab if proc is not None else what
        except Exception:  # noqa: BLE001 -- a status line, never a blocker
            name = what
        # a send's notice is about the print before this one
        hide = getattr(self, "_gce_notice_hide_if", None)
        if callable(hide):
            hide("send")
        self.status.set(
            f"Generated {name}. Press “Save g-code as…” and print "
            f"{'each slide' if what == 'every slide' else 'it'}, then type "
            f"what you measure under Measurements. Simulation shows it first.")
        try:
            self._sync_result_buttons()
        except Exception:  # noqa: BLE001
            pass
        # Stamp the result so the Viewer knows it is newer than whatever an open
        # panel is showing, and reloads when the operator opens that page. This
        # build CANNOT ride `gui_embed._EmbedMixin._on_finished` -- completion
        # here deliberately never becomes the pump's ("done", code) event, see
        # `_sw_launch_build`'s docstring -- so without the stamp a Viewer left
        # open would keep showing the previous slide with nothing to say so.
        # It does NOT open or switch to the Viewer: a Generate leaves the
        # operator on the page they pressed it from, mid-calibration-workflow.
        # getattr: the classic UI has no embedded panels.
        mark = getattr(self, "_mark_result_fresh", None)
        if callable(mark):
            mark()
        # ...but when the operator is already ON Simulation, it loads there
        show = getattr(self, "_load_viewer_if_shown", None)
        if callable(show):
            show()
        finish("ok", 0)

    def _sw_export_gcode(self, folder, base, write_csv):
        """Write EVERY slide's g-code (base_print{i}.gcode) into folder, plus
        base_measure.csv when write_csv -- the batch the single-file classic
        export can't express. Each slide goes through the printer's
        output settings (`_sw_user_gcode`); the scratch files the converter,
        preview and player read keep the engine's bytes. Returns the filenames
        written; raises OSError on a write failure."""
        res = self._sw_result
        if res is None:
            return []
        written = []
        # one print is saved under the name typed; several are numbered.
        # Reported: "cal.gcode" typed for a one-file print wrote
        # cal_print1.gcode
        single = len(res.gcode_texts) == 1
        for i, text in enumerate(res.gcode_texts, 1):
            path = os.path.join(folder, f"{base}.gcode" if single
                                else f"{base}_print{i}.gcode")
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(self._sw_slide_text(i - 1, text,
                                             os.path.basename(path)))
            written.append(os.path.basename(path))
        if write_csv:
            csv_path = os.path.join(folder, f"{base}_measure.csv")
            with open(csv_path, "w", encoding="utf-8", newline="") as fh:
                fh.write(res.csv_text)
            written.append(os.path.basename(csv_path))
        return written

    #: the file name each print is offered under, by protocol
    _SW_FILE_NAMES = {"cts": "cts_sweep", "cts4": "cts_mandrel",
                      "circle": "lag_circles", "tube": "lag_tube",
                      "diam": "temperature_ladder", "visc": "viscosity_plate",
                      "sag": "sag_ladder"}

    def _sw_default_name(self):
        """The print's file stem: Save offers it, Send stores it on the
        printer and the header names it."""
        return self._SW_FILE_NAMES.get(self.sw_vars["kind"].get(),
                                       "calibration")

    def _sw_rung_files(self, name):
        """A temperature ladder of several rungs leaves the app as one file
        per rung, never as one print: the rung IS the slide, and the
        operator sets the nozzle's temperature between prints
        (`mew_sweeps.generate_diameter`). Returns [(file name, text, degC)],
        the bytes Save writes, named as Save names them
        (`<stem>_print<i>.gcode`, the stem taken from `name`), or None for
        any other print, which is sent as one file."""
        if not self._sw_rung_count():
            return None
        res = self._sw_result
        if getattr(self, "_cal_print_on", lambda: False)():
            self._cal_shelve()      # what is typed on the print is in it
        stem = os.path.splitext(os.path.basename(name or ""))[0] \
            or self._sw_default_name()
        temps = {}
        for row in res.rows:
            temps.setdefault(row.get("print"), row.get("T_C"))
        files = []
        for i, raw in enumerate(res.gcode_texts, 1):
            fn = "%s_print%d.gcode" % (stem, i)
            files.append((fn, self._sw_slide_text(i - 1, raw, fn),
                          temps.get(i)))
        return files

    def _sw_rung_count(self):
        """How many files Send makes of this print: a ladder's rung count,
        or 0 for a print that goes as one file. Cheap, for a label."""
        res = getattr(self, "_sw_result", None)
        if res is None or res.kind != "diam" or len(res.gcode_texts) < 2:
            return 0
        return len(res.gcode_texts)

    @staticmethod
    def _sw_rung_confirm(files, host):
        """Asked where Send is pressed, before anything goes up: a ladder
        is several files, and each needs its temperature set first."""
        rows = "\n".join(
            "    %s    %s °C" % (fn, "%g" % float(t) if t not in (None, "")
                                else "?") for fn, _text, t in files)
        return messagebox.askokcancel(
            "Send the temperature ladder",
            "This ladder goes to %s as %d files, one per temperature:\n\n%s"
            "\n\nNone of them starts on its own. Before you start a file, "
            "set the nozzle heater (extruder) to that file's temperature."
            % (host, len(files), rows))

    @staticmethod
    def _sw_rung_note(files, host):
        """What Send says for a ladder: how many files, which temperature
        each one is for, and that nothing starts on its own."""
        rungs = ", ".join(
            "%s at %s °C" % (fn, "%g" % float(t) if t not in (None, "")
                             else "?") for fn, _text, t in files)
        return ("Sending %d files to %s, one per temperature: %s. Nothing "
                "starts on its own. On the Printer link page, set the nozzle "
                "heater (extruder) to a file's temperature under "
                "Temperatures, then "
                "select that file under G-code files and press Start print."
                % (len(files), host, rungs))

    def _sw_export_gcode_dialog(self):
        """Shared 'Save g-code as…' for the sweeps page: one save dialog picks
        the destination folder + basename, then EVERY slide is written
        (base_print{i}.gcode) plus base_measure.csv when the Export-options toggle
        is on. A sweep is N files, which the classic single-file export can't do,
        so the shell routes Save g-code here when the sweeps page is active."""
        res = self._sw_result
        if res is None:
            self._refuse("Nothing to save yet: press Generate first, then save its g-code.")
            return
        default = self._sw_default_name()
        dest = filedialog.asksaveasfilename(
            **gui_prefs.save_dir_kw(),
            parent=self.root, title="Save sweep g-code as",
            defaultextension=".gcode", initialfile=f"{default}.gcode",
            filetypes=[("G-code", "*.gcode"), ("All files", "*.*")])
        if not dest:
            return
        folder = os.path.dirname(dest)
        base = os.path.splitext(os.path.basename(dest))[0] or default
        try:
            written = self._sw_export_gcode(
                folder, base, bool(self.sw_vars["write_csv"].get()))
        except OSError as exc:
            self._refuse("Not saved: %s. Pick another folder and save again." % exc)
            return
        note = "Saved " + ", ".join(written)
        if res.oversize:
            note += "  ·  ⚠ some specimens do not fit the plate"
        elif not res.all_on_slide:
            note += ("  ·  ⚠ the print does not fit the mandrel. Check before "
                     "printing" if res.kind in self._SW_ROTARY_KINDS else
                     "  ·  ⚠ a slide exceeds the plate. Check before printing")
        self.status.set(note)
