#!/usr/bin/env python3
"""test_scenario_grid.py -- one table of scenarios, one list of shared rules.

WHY THIS EXISTS. Editing a feature used to mean editing the suites that each
re-built one corner of the same pipeline. Here the pipeline is walked once per
ROW (a collector mode crossed with a step) through the real modern shell and
the real worker, and every RULE is applied to every row it covers. A new bug or
feature is one added row or one added rule, not a new suite.

THE WALK, per collector mode (plate = flat, mandrel = 4-axis):

  generate   click the Collector radio, shrink the design, build the worker argv
             the Generate button would send, run that worker (`mew_export.py`
             -> `scaffold_gcode.py` -> the converter, `--no-mesh`), keep the
             g-code and the `.depo.npz` (the sagged fibre centreline).
  reload     snapshot the scaffold preset, JSON round-trip it, change the
             design AND the collector, apply the snapshot, build the argv
             again and run it again.
  preview    wait for the embedded preview's own build of the same design.
  player     open the real player on the generate row's files, through the
             argv the Viewer sends, and wait for its first drawn frame.

Two checks walk the whole app rather than a row, so a new field is covered the
day it is added, with no edit here:

  every_field_reaches  change each input of a mode and rebuild the argv; an input
                       that moves nothing fails unless DEAD_FIELDS lists it.
  every_input_persists every input on the App must be in `_persist_map()` unless
                       NOT_PERSISTED lists it or it is a widget alias of one.

Three more walk the G-code CAD page (open it, then ~10 s in all):

  page_tools_open      every ribbon panel opens and closes on its own tool.
  page_round_trip      untouched, the page text is the Save seam's, byte for byte,
                       with no changes held and no `;` comment.
  page_speed_edit      Speed on one layer changes feed words there and nothing else
                       (the tool also writes a bare feed line after the layer);
                       one undo gives the text back.

RULES (physical or cross-mode; none reads a stored file, hash or timing):

  worker_ran        the worker exits 0 and writes a g-code file and a `.depo`.
  worker_mode       the argv carries this mode's flag and none of the other
                    modes'; the g-code uses exactly this mode's axis letters
                    (plate X Y Z, mandrel X Z A, surface X Y Z C A).
  design_reaches    each GUI field in PARAMS appears in the argv under its flag.
  clears_collector  no fibre centre is closer to the collector than half a
                    fibre diameter (plate z=0, rod radius, dome radius).
                    Applied to the worker's `.depo` and to the preview's paths.
  reload_same       the reload row's argv and g-code equal the generate row's.
  preview_matches   the preview reports the layer count and fibre diameter
                    the worker was given, and its drawn box lies inside the
                    worker's `.depo` box (the `.depo` also holds the exit move,
                    so the preview may be smaller, never larger) within TOL_BOX.
  player_opens      the player reaches its first frame, and the frame is not blank.

HOW TO EXTEND.
  * A new bug found on one path: add the mode/step to ROWS, or write a rule
    function `rule_xxx(row, ctx)` returning None (pass), a string (fail) or
    SKIP (does not apply), and append it to RULES.
  * A new GUI field: nothing to do, the walks find it. Add it to DEAD_FIELDS or
    NOT_PERSISTED, with the reason, only if it is meant not to.
  * A known product defect: add `(mode, step, rule): "reason"` to XFAIL. It
    prints XFAIL and does not fail the run; if it starts passing it prints
    XPASS and DOES fail, so the entry gets removed.

CONTROLS. Two kinds, both seen this session. GREEN: the unedited
tree passes 39 cases (1 XFAIL, removed on the G-code CAD branch once the 4-axis preview fix made it XPASS). RED: five real, temporary product edits, each
undone by editing it back with `git diff` empty afterwards, and every rule went
red under at least one of them:

  C1 gui_args.py plate "--stepover" -> "--stepoverX" (the break the 22-suite
     gate did not notice)           -> worker_ran, design_reaches, reload_same,
                                       preview_matches (7 cases)
  C2 gui_surface._build_args_scaffold sends the plate's argv for a dome
                                    -> worker_mode, design_reaches,
                                       clears_collector, preview_matches
  C3 gui_mandrel sends half the rod diameter in the argv
                                    -> clears_collector (-0.74 mm), design_reaches
  C4 gui_presets._apply_preset_for_kind skips every key containing "layers"
                                    -> reload_same on all three modes
  C5 gui_preview plate spec reports layers + 1
                                    -> preview_matches (plate only)

  C6 gui_args.py sends a constant for Arc segments, ignoring the field
                                    -> every_field_reaches (plate)
  C7 gui_presets._persist_map drops "s.speed"
                                    -> every_input_persists
  C8 gcode_player.main raises on launch
                                    -> player_opens on all three modes

  C9-C13 (in memory, 2026-10-31) the colour panel raising; the page text
     differing from the Save seam; a `;` in the text; a speed edit that moves
     an X word; a feed landing outside the chosen layer -> each one its own
     page_* check, and all three green on the clean tree.

Not each rule is red per mode: clears_collector is red on mandrel and surface
only, preview_matches on plate and surface only, so the plate and mandrel
halves of those two are shown by the same rule code but not by their own edit.

BLIND TO: the STL mesh is not built (`--no-mesh`), the player is opened but not
played through, only the dome is exercised on the curved collector, the preview
comparison is a box, not a path, and a field listed in DEAD_FIELDS is not walked
in the mode that switches it on. `test_five_axis_matrix.py` owns the
other curved shapes.

Self-running, no pytest: prints PASS/FAIL per case, exits non-zero on failure.
    ./.venv/Scripts/python.exe tests/test_scenario_grid.py [--keep]
"""
import os as _os, sys as _sys
# The suites live in tests/; the program they test is one folder up.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.join(_sys.path[0], "engine"))
import app_paths  # noqa: E402,F401  (puts every program folder on sys.path)
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from tkinter import ttk

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import capture_ui  # noqa: E402

SKIP = object()
#: A mm allowance on the preview-inside-worker box. The preview is an
#: approximation (own sag, no exit move), so it may differ by a few fibre
#: diameters; this is not a timing or a stored number.
TOL_BOX = 0.2
WORKER_TIMEOUT_S = 180.0
PREVIEW_WAIT_S = 90.0
PLAYER_WAIT_S = 60.0

MODES = ("plate", "mandrel")
STEPS = ("generate", "reload", "preview", "player")
#: the rows of the table: every mode crossed with every step
ROWS = [(m, s) for m in MODES for s in STEPS]

#: (mode, step, rule) -> reason. A listed failure is expected, not hidden.
XFAIL = {}

#: seconds to wait for the G-code page to show its designer text
PAGE_WAIT_S = 60

#: flags that mark each mode's argv, and the axis letters its g-code uses
MODE_FLAG = {"plate": "--plate", "mandrel": "--mandrel"}
MODE_AXES = {"plate": set("XYZ"), "mandrel": set("XZA")}


def _var(app, name):
    if name.startswith("scaf:"):
        return app.scaf_vars[0][name[5:]]
    return getattr(app, name)


#: GUI field -> the worker flag that must carry it, per mode
PARAMS = {
    "plate": [("scaf:width", "--width"), ("scaf:height", "--height"),
              ("scaf:stepover", "--stepover"), ("scaf:layers", "--layers"),
              ("scaf:speed", "--speed"), ("scaf:diameter", "-d")],
    # --tube-length is NOT listed: the design snaps the length to whole cells
    # (a 6 mm field is sent as 5.9976), which is by design.
    "mandrel": [("m_diameter", "--mandrel"),
                ("scaf:layers", "--layers"), ("scaf:speed", "--speed"),
                ("scaf:diameter", "-d")],
}

#: Inputs that rightly move nothing in this mode's default design, by the option
#: that switches them on. Listed so that a NEW dead field fails.
DEAD_FIELDS = {
    "plate": {
        "CTS ratio mode is off": ("speed_ratio",),
        "linear pattern": ("linear_wavelength", "linear_spacing", "linear_periods",
                           "linear_grid", "linear_periods_combo"),
        "polygon footprint": ("shape_sides", "shape_grid", "shape_sides_entry"),
        "perimeter travel is off": ("perimeter_travel_offset",
                                    "perimeter_travel_entry"),
        # greyed on the page until there is a far edge to grade towards;
        # the top layer's spacing is the layer by layer grade's
        "Grade the spacing is off": ("grade_angle", "stepover_top"),
        "array of copies": ("array_nx", "array_ny", "array_nz", "array_dx", "array_dy",
                            "array_dz", "array_flip_x", "array_flip_y", "array_travel"),
    },
    "mandrel": {
        "a design mode other than the default": ("m_angle", "m_eta", "m_rm", "m_length_tol"),
        "placed, not centred": ("m_place_offset",),
        "winding-angle mode with the diamond helix": ("m_closure_override",),
        "pore mode (the default turn mode only displays it)": ("m_pore_target",),
        "more than one tube copy": ("m_copy_gap",),
        "perimeter travel is off": ("m_turn_out",),
        "the drawn unit cell": ("m_cell_w", "m_cell_h", "m_rib_angle", "m_cell_amplitude",
                                "m_cell_grid", "m_cell_view"),
    },
}
#: Inputs meant not to persist: output paths, view state, and fields derived
#: from another. Widget aliases are caught by ALIAS_RE.
NOT_PERSISTED = frozenset((
    "s_gcode_out", "s_stl_out", "m_cell_editmode",
    "m_cell_view", "m_cell_size", "linear_periods", "linear_editmode", "shape_editmode"))
ALIAS_RE = re.compile(r"_(entry|combo|readout|status|warning)(_\w+)?$")
INPUTS = (tk.Variable, tk.Entry, ttk.Entry)

#: the small design each mode runs, then the changed one the reload row starts
#: from. (var, value). Layers 2 keeps every worker run to seconds.
SMALL = {
    "plate": [("scaf:width", "4"), ("scaf:height", "4"), ("scaf:layers", "2")],
    "mandrel": [("m_length", "6"), ("scaf:layers", "2")],
}
CHANGED = {
    "plate": [("scaf:width", "7"), ("scaf:layers", "3")],
    "mandrel": [("m_length", "9"), ("scaf:layers", "3")],
}


class Row:
    """What one (mode, step) produced; rules read these fields."""

    def __init__(self, mode, step):
        self.mode, self.step = mode, step
        self.argv = None            # the worker argv, as sent
        self.rc = None
        self.out = ""
        self.gcode = None           # text
        self.depo = None            # (n, 3) array
        self.params = {}            # flag -> (gui value, argv value)
        self.layers = None
        self.diameter = None
        self.preview = None         # dict(paths=array, layers=, diameter=, bbox=)
        self.gcode_path = self.depo_path = None
        self.frame_ok = None
        self.error = None


# --------------------------------------------------------------------------- #
# driving the real shell
# --------------------------------------------------------------------------- #
class Shell:
    def __init__(self):
        self.errors = []
        self.root, self.app = capture_ui.build_app(self.errors)
        capture_ui.silence_dialogs([])
        self.app._nav_select("scaffold")
        capture_ui.settle(self.root)
        self.tmp = tempfile.mkdtemp(prefix="scenario_grid_")
        # the argv build writes a sidecar beside this path; keep it out of the repo
        self.app.s_gcode_out.set(os.path.join(self.tmp, "out.gcode"))

    def close(self, keep=False):
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        if not keep:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def set_vars(self, pairs):
        for name, val in pairs:
            _var(self.app, name).set(val)
        capture_ui.settle(self.root)

    def click_mode(self, mode):
        self.app.s_collector.set(mode)
        self.app._collector_mode_chosen()
        capture_ui.settle(self.root)

    def argv(self):
        """The argv Generate sends for whatever collector the toggle now holds."""
        app = self.app
        mode = app.s_collector.get()
        app._mandrel_dispatch = mode == "mandrel"
        try:
            return list(app._build_args_scaffold())
        finally:
            app._mandrel_dispatch = False

    def run_worker(self, row, tag):
        a = list(row.argv)
        g = os.path.join(self.tmp, tag + ".gcode")
        s = os.path.join(self.tmp, tag + ".stl")
        a[a.index("-o") + 1] = g
        if "--stl" in a:
            a[a.index("--stl") + 1] = s
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        try:
            r = subprocess.run(a, capture_output=True, text=True, timeout=WORKER_TIMEOUT_S,
                               env=env, cwd=HERE)
            row.rc, row.out = r.returncode, (r.stdout + r.stderr)[-600:]
        except subprocess.TimeoutExpired:
            row.rc, row.out = -1, "worker timed out"
        if os.path.exists(g):
            row.gcode_path = g
            with open(g, encoding="utf-8") as fh:
                row.gcode = fh.read()
        for cand in os.listdir(self.tmp):
            if cand.startswith(tag) and cand.endswith(".depo.npz"):
                row.depo_path = os.path.join(self.tmp, cand)
                with np.load(row.depo_path, allow_pickle=True) as z:
                    row.depo = np.asarray(z["xyz"], dtype=float)

    def fill(self, row):
        """Record the argv/GUI pairing every rule needs."""
        a = row.argv
        for var, flag in PARAMS[row.mode]:
            gui = str(_var(self.app, var).get()).strip()
            got = a[a.index(flag) + 1] if flag in a else None
            row.params[flag] = (gui, got)
        row.layers = int(float(_var(self.app, "scaf:layers").get()))
        row.diameter = float(a[a.index("-d") + 1]) if "-d" in a else None
        # the collector's size comes from the FIELD the user sees, not from the
        # argv, so a wrong value in the argv cannot move the yardstick with it
        if row.mode == "mandrel":
            row.radius = float(self.app.m_diameter.get()) / 2.0

    # -- the preview -------------------------------------------------------- #
    def _pv_state(self, mode):
        app = self.app
        if mode == "plate":
            return app._pv_states[0]
        return app._mandrel_preview_state

    def _pv_ready(self, mode):
        st = self._pv_state(mode)
        if st.get("paths") is None:
            return False
        return not st.get("dirty") and st.get("gen_done") == st.get("gen_sub")

    def read_preview(self, row):
        end = time.time() + PREVIEW_WAIT_S
        while time.time() < end and not self._pv_ready(row.mode):
            capture_ui.settle(self.root, pumps=20, max_s=0.3)
        if not self._pv_ready(row.mode):
            row.error = "the preview never finished within %.0f s" % PREVIEW_WAIT_S
            return
        st = self._pv_state(row.mode)
        paths = np.vstack([np.asarray(p, dtype=float) for p in st["paths"]])
        meta = st.get("meta") or {}
        row.preview = dict(paths=paths, layers=meta.get("layers"),
                           diameter=meta.get("fiber_diameter"))


    # -- the player --------------------------------------------------------- #
    def open_player(self, row, gen):
        from test_viewer_embed import Child
        app = self.app
        app.last_gcode, app.last_stl, app.last_depo = gen.gcode_path, None, gen.depo_path
        child = Child(list(app._player_args(static=True)))
        try:
            child.expect("hello", timeout=PLAYER_WAIT_S)
            row.frame_ok = any(child.frame_bytes(child.expect("frame", timeout=PLAYER_WAIT_S, keep={})))
        except AssertionError as exc:
            row.error = str(exc)[-300:]
        finally:
            child.close()

    # -- the whole-app walks ------------------------------------------------- #
    def inputs(self, mode):
        app = self.app
        if mode == "plate":
            return {k: v for k, v in app.scaf_vars[0].items() if isinstance(v, INPUTS)}
        pre = {"mandrel": "m_"}[mode]
        return {k: v for k, v in vars(app).items()
                if k.startswith(pre) and isinstance(v, INPUTS)}

    def fields_that_move_nothing(self, mode):
        self.click_mode(mode)
        self.set_vars(SMALL[mode])
        base, dead = self.argv(), []
        for name, var in self.inputs(mode).items():
            old, new = var.get(), _bumped(var)
            if new is None:
                continue
            _put(var, new)
            try:
                if self.argv() == base:
                    dead.append(name)
            finally:
                _put(var, old)
        return dead

    def unpersisted_inputs(self):
        app = self.app
        held = {id(v) for v in app._persist_map().values()}
        found = {k: v for k, v in vars(app).items()
                 if re.match(r"(s|m|sf)_", k) and isinstance(v, INPUTS)}
        found.update((k, v) for k, v in app.scaf_vars[0].items() if isinstance(v, INPUTS))
        return sorted(k for k, v in found.items() if id(v) not in held
                      and k not in NOT_PERSISTED and not ALIAS_RE.search(k))


def _bumped(var):
    """A different value for an input, or None for one that takes free text."""
    val = var.get()
    if isinstance(var, tk.BooleanVar):
        return not val
    if re.fullmatch(r"-?\d+", str(val).strip()):
        return str(int(val) + 1)       # an integer box (Copies) refuses "2.5"
    try:
        return str(float(val) * 1.5 + 1.0)
    except (TypeError, ValueError):
        return None


def _put(var, val):
    if isinstance(var, tk.Variable):
        var.set(val)
        return
    state = str(var.cget("state"))
    var.configure(state="normal")
    var.delete(0, "end")
    var.insert(0, str(val))
    var.configure(state=state)


def build_rows(sh):
    """Walk the table. Returns {(mode, step): Row}."""
    rows, last = {}, {}
    for mode, step in ROWS:
        row = Row(mode, step)
        rows[(mode, step)] = row
        try:
            if step == "generate":
                sh.click_mode(mode)
                sh.set_vars(SMALL[mode])
                row.argv = sh.argv()
                sh.fill(row)
                sh.run_worker(row, mode + "_gen")
                last[mode] = dict(snap=json.loads(json.dumps(
                    sh.app._snapshot_for_kind("scaffold"))))
            elif step == "reload":
                other = MODES[(MODES.index(mode) + 1) % len(MODES)]
                sh.set_vars(CHANGED[mode])
                sh.app.s_collector.set(other)
                capture_ui.settle(sh.root)
                sh.app._apply_preset_for_kind("scaffold", last[mode]["snap"])
                capture_ui.settle(sh.root)
                row.argv = sh.argv()
                sh.fill(row)
                sh.run_worker(row, mode + "_rel")
            elif step == "preview":
                sh.click_mode(mode)
                sh.set_vars(SMALL[mode])
                row.argv = sh.argv()
                sh.fill(row)
                sh.read_preview(row)
            elif step == "player":
                gen = rows[(mode, "generate")]
                sh.click_mode(mode)
                row.argv = gen.argv
                if not gen.gcode_path:
                    row.error = "the generate row wrote no g-code"
                else:
                    sh.open_player(row, gen)
        except Exception as exc:  # noqa: BLE001
            row.error = "%s: %s" % (type(exc).__name__, exc)
    return rows


# --------------------------------------------------------------------------- #
# the rules
# --------------------------------------------------------------------------- #
def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def rule_worker_ran(row, ctx):
    if row.step in ("preview", "player"):
        return SKIP
    if row.error:
        return row.error
    if row.rc != 0:
        return "worker exit %s: %s" % (row.rc, row.out.strip().splitlines()[-1:] or "")
    if not row.gcode or row.depo is None or not len(row.depo):
        return "no g-code or no .depo written"
    return None


def rule_worker_mode(row, ctx):
    if row.step == "player":
        return SKIP
    a = row.argv or []
    mine = MODE_FLAG[row.mode]
    if mine not in a:
        return "argv lacks %s" % mine
    for m, f in MODE_FLAG.items():
        if m != row.mode and f in a:
            return "argv carries %s, which is the %s mode's" % (f, m)
    if row.step == "preview":
        return None
    if not row.gcode:
        return SKIP
    used = set()
    for line in row.gcode.splitlines():
        if line.startswith(("G0 ", "G1 ")):
            used |= set(re.findall(r"\b([XYZABC])-?\d", line))
    if used != MODE_AXES[row.mode]:
        return "g-code axis letters %s, expected %s" % (
            "".join(sorted(used)), "".join(sorted(MODE_AXES[row.mode])))
    return None


def rule_design_reaches(row, ctx):
    if row.step == "player":
        return SKIP
    bad = []
    for flag, (gui, got) in row.params.items():
        g, w = _num(gui), _num(got)
        same = (math.isclose(g, w, rel_tol=1e-9, abs_tol=1e-9)
                if g is not None and w is not None else gui == got)
        if not same:
            bad.append("%s: field %r, argv %r" % (flag, gui, got))
    return "; ".join(bad) or None


def _clearance(row, pts):
    """Smallest distance from a fibre centre to this mode's collector surface."""
    if row.mode == "plate":
        return float(pts[:, 2].min())
    return float(np.hypot(pts[:, 1], pts[:, 2]).min() - row.radius)


def rule_clears_collector(row, ctx):
    pts = row.preview["paths"] if row.step == "preview" and row.preview else row.depo
    if pts is None or not len(pts) or row.diameter is None:
        return SKIP
    c = _clearance(row, pts)
    need = 0.5 * row.diameter * 0.9
    if c < need:
        return "closest fibre centre %.4f mm off the collector, needs >= %.4f" % (c, need)
    return None


def rule_reload_same(row, ctx):
    if row.step != "reload":
        return SKIP
    gen = ctx[(row.mode, "generate")]
    if gen.argv is None or row.argv is None:
        return "no argv to compare"
    def norm(a):
        a = list(a)
        for flag in ("-o", "--stl"):
            if flag in a:
                a[a.index(flag) + 1] = ""
        return a
    if norm(gen.argv) != norm(row.argv):
        diff = [(x, y) for x, y in zip(norm(gen.argv), norm(row.argv)) if x != y]
        return "argv differs after reload: %s" % (diff[:3],)
    if gen.gcode is None or gen.gcode != row.gcode:
        return "g-code differs after reload"
    return None


def rule_preview_matches(row, ctx):
    if row.step != "preview":
        return SKIP
    pv, gen = row.preview, ctx[(row.mode, "generate")]
    if pv is None:
        return row.error or "no preview"
    if gen.depo is None:
        return "no worker output to compare with"
    if pv["layers"] is not None and int(pv["layers"]) != gen.layers:
        return "preview %s layers, worker %s" % (pv["layers"], gen.layers)
    if pv["diameter"] is not None and not math.isclose(
            float(pv["diameter"]), gen.diameter, rel_tol=1e-6):
        return "preview fibre %.5f, worker %.5f" % (pv["diameter"], gen.diameter)
    lo, hi = pv["paths"].min(0), pv["paths"].max(0)
    wlo, whi = gen.depo.min(0), gen.depo.max(0)
    if (lo < wlo - TOL_BOX).any() or (hi > whi + TOL_BOX).any():
        return ("preview box %s..%s outside the worker's %s..%s"
                % (lo.round(2), hi.round(2), wlo.round(2), whi.round(2)))
    return None


def rule_player_opens(row, ctx):
    if row.step != "player":
        return SKIP
    if row.error:
        return row.error
    return None if row.frame_ok else "the player's first frame is blank"


RULES = [("worker_ran", rule_worker_ran), ("worker_mode", rule_worker_mode),
         ("design_reaches", rule_design_reaches),
         ("clears_collector", rule_clears_collector),
         ("reload_same", rule_reload_same),
         ("preview_matches", rule_preview_matches),
         ("player_opens", rule_player_opens)]

# --------------------------------------------------------------------------- #
# the G-code CAD page: three quick walks of the whole page, not of a row
# --------------------------------------------------------------------------- #
def _page_ready(sh):
    """Open the G-code page and wait until it shows its designer text."""
    app = sh.app
    app._nav_select("gcode")
    end = time.time() + PAGE_WAIT_S
    while time.time() < end:
        capture_ui.settle(sh.root, pumps=10, max_s=0.2)
        if (app.gce.get_text() and app._gce_regen is None
                and getattr(app, "_gce_model", None) is not None):
            return None
    return "the G-code page never showed its text within %.0f s" % PAGE_WAIT_S


def page_tools_open(sh):
    """Every ribbon panel opens (and closes on the same tool again)."""
    bad = _page_ready(sh)
    if bad:
        return bad
    app = sh.app
    for key in sorted(app._GCE_PANEL_TITLES):
        try:
            app._gce_panel_open(key)
            capture_ui.settle(sh.root, pumps=10, max_s=0.2)
            if getattr(app, "_gce_panel_key", None) != key or app._gce_form is None:
                return "the %r panel did not open" % key
            app._gce_panel_open(key)
            capture_ui.settle(sh.root, pumps=10, max_s=0.2)
            if app._gce_form is not None:
                return "the %r panel did not close on its own tool" % key
        except Exception as exc:  # noqa: BLE001
            return "the %r panel raised %s: %s" % (key, type(exc).__name__, exc)
    return None


def page_round_trip(sh):
    """An untouched page saves what it shows. Its moves are the Save seam's,
    line for line, and its comments go exactly when Strip comments is on.

    The header is compared as the page renders it, not against the seam. Since
    ad163cc9 turned Strip comments off by default, a saved file keeps its
    header. The page fills that header from its own design
    (`_machine_gcode_context(use_gce=True)`). The seam
    (`_gcode_for_display_export`) fills it from the legacy designer's
    variables, which this grid shrinks to 6 mm / 2 layers while the page's
    print is 20 mm / 5 layers. While comments were stripped by default, both
    headers were thrown away and the two texts matched byte for byte. Now the
    seam's header would describe a print the page never made."""
    bad = _page_ready(sh)
    if bad:
        return bad
    app = sh.app
    text = app.gce.get_text()
    saved = (app._last_gcode_is_scaffold, app._last_gcode_is_calibration)
    app._last_gcode_is_scaffold, app._last_gcode_is_calibration = True, False
    try:
        seam = app._gcode_for_display_export(app._gce_model.base_text)
    finally:
        app._last_gcode_is_scaffold, app._last_gcode_is_calibration = saved
    saved_text = app._gce_save_ready(text)
    if app._gce_model.changes:
        return "a freshly opened page already holds %d change(s)" % len(app._gce_model.changes)

    def moves(t):
        return [ln for ln in t.splitlines() if ln.strip() and not ln.lstrip().startswith(";")]
    if moves(saved_text) != moves(seam):
        a, b = moves(saved_text), moves(seam)
        i = next((k for k, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        return ("the page saves %d command lines, Save seam would write %d; first "
                "difference at command %d" % (len(a), len(b), i + 1))
    strip = bool(app.gce_strip_comments.get())
    if not strip and saved_text != text:
        return "with Strip comments off, Save changes the page's text"
    semi = [i + 1 for i, ln in enumerate(saved_text.splitlines()) if ";" in ln]
    if strip and semi:
        return "';' comment on line(s) %s with Strip comments on" % semi[:5]
    if not strip and not semi:
        return "with Strip comments off, the saved file lost its header"
    return None


def page_speed_edit(sh):
    """Speed on one layer changes feed words on that layer and nothing else;
    one undo gives the text back exactly."""
    bad = _page_ready(sh)
    if bad:
        return bad
    app = sh.app
    layers = [i for i, inf in app._gce_tree_info.items() if inf.get("kind") == "layer"]
    if len(layers) < 3:
        return "the page lists only %d layer(s)" % len(layers)
    iid = layers[len(layers) // 2]
    info = app._gce_tree_info[iid]
    before = app.gce.get_text().splitlines()
    app._gce_select_node(iid)
    capture_ui.settle(sh.root, pumps=10, max_s=0.3)
    app._gce_tool_change("speed_set")
    capture_ui.settle(sh.root, pumps=10, max_s=0.3)
    app._gce_fs["vals"]["f"]["t:speed_set"]["mm_s"].set("15")
    app._gce_edit_flush()
    done = [w for w in app._gce_walk(app._gce_form)
            if isinstance(w, tk.ttk.Button) and w["text"] == "Done"][0]
    done.invoke()
    capture_ui.settle(sh.root, pumps=10, max_s=0.3)
    after = app.gce.get_text().splitlines()
    # The tool writes the new feed on the layer's first move and puts the old
    # one back with a bare `G1 F..` line after the layer. Take both out, and
    # every other word must be exactly what it was.
    feed = re.compile(r"\s*\bF[-+0-9.]+")
    bare = re.compile(r"^G[01]\s+F[-+0-9.]+\s*$")

    def core(lines):
        return [feed.sub("", ln).rstrip() for ln in lines if not bare.match(ln)]

    if core(after) != core(before):
        return "a speed change altered more than feed words"
    lit = [n + 1 for n, ln in enumerate(after) if "F900" in ln]
    if not lit:
        return "no line took the new feed (15 mm/s = F900)"
    outside = [n for n in lit if not info["first"] <= n <= info["last"] + 1]
    if outside:
        return "the new feed reached outside the chosen layer, line(s) %s" % outside[:5]
    app.gce.undo()
    capture_ui.settle(sh.root, pumps=10, max_s=0.5)
    if app.gce.get_text().splitlines() != before:
        return "one undo did not give the text back"
    return None


PAGE_CHECKS = [("page_tools_open", page_tools_open),
               ("page_round_trip", page_round_trip),
               ("page_speed_edit", page_speed_edit)]



def main():
    keep = "--keep" in sys.argv
    t0 = time.time()
    sh = Shell()
    fails, n = [], 0
    try:
        rows = build_rows(sh)
        for (mode, step), row in rows.items():
            for rname, fn in RULES:
                try:
                    verdict = fn(row, rows)
                except Exception as exc:  # noqa: BLE001
                    verdict = "rule raised %s: %s" % (type(exc).__name__, exc)
                if verdict is SKIP:
                    continue
                n += 1
                case = "%-8s %-8s %s" % (mode, step, rname)
                reason = XFAIL.get((mode, step, rname))
                if verdict is None and reason:
                    print("FAIL  %s  XPASS: remove the XFAIL entry (%s)" % (case, reason))
                    fails.append(case)
                elif verdict is None:
                    print("PASS  " + case)
                elif reason:
                    print("XFAIL %s  -- %s" % (case, reason))
                else:
                    print("FAIL  %s  -- %s" % (case, verdict))
                    fails.append(case)
        for mode in MODES:
            n += 1
            allowed = {f for fields in DEAD_FIELDS[mode].values() for f in fields}
            new = sorted(set(sh.fields_that_move_nothing(mode)) - allowed)
            if new:
                print("FAIL  %-8s every_field_reaches  -- moved nothing: %s" % (mode, ", ".join(new)))
                fails.append(mode + " every_field_reaches")
            else:
                print("PASS  %-8s every_field_reaches" % mode)
        for pname, pfn in PAGE_CHECKS:
            n += 1
            try:
                verdict = pfn(sh)
            except Exception as exc:  # noqa: BLE001
                verdict = "check raised %s: %s" % (type(exc).__name__, exc)
            if verdict is None:
                print("PASS  gcode    %s" % pname)
            else:
                print("FAIL  gcode    %s  -- %s" % (pname, verdict))
                fails.append(pname)
        n += 1
        loose = sh.unpersisted_inputs()
        if loose:
            print("FAIL  every_input_persists  -- not in the persist map: " + ", ".join(loose))
            fails.append("every_input_persists")
        else:
            print("PASS  every_input_persists")
        if sh.errors:
            print("FAIL  Tk swallowed %d exception(s): %s" % (len(sh.errors), sh.errors[0][-300:]))
            fails.append("tk-exceptions")
    finally:
        sh.close(keep)
    print("\n%d cases, %d failed, %.0f s" % (n, len(fails), time.time() - t0))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
