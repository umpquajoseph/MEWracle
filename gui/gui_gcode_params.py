#!/usr/bin/env python3
"""gui_gcode_params.py -- MODERN-UI ONLY: the G-code page's Parameters tables.

Fusion's Modify > Change Parameters, for scaffolds, in two tables that look
the same and float over the picture one at a time:

  * **Parameters** -- one column per scaffold on the plate, every plain
    scaffold setting a row. Change a cell, press Return (or leave it) and
    that cell is written; the preview takes every change at once when you
    press Done (or Esc, or switch collector).
  * **Layer parameters** -- one scaffold, one column per LAYER. A cell shows
    the value that layer prints with; grey means it is the scaffold's, and
    typing a different value makes that layer differ. Fiber diameter and the
    layer count stay per scaffold. Layer rotation and Start angle become one
    Fiber angle row, since one layer has one direction.

**Spacing** (a flat scaffold's) is one setting, **Fiber spacing**: the
stepover, centre to centre in mm. Its tooltip names the pore it leaves; no
row shows the pore. A tube's "Pore size" is its own setting
(`TUBE_PARAM_ROWS`) and is left alone.

**Grade the spacing** is a view over values the generator already reads,
so it adds no generator setting and an old design reads back as it was
(`_gcp_grade_*`):

  * across the layer (Left to right, Front to back, Corner to corner or a
    typed angle): `stepover_end` + `grade_angle`, the in-layer grade
    (`scaffold_gcode._graded_offsets`). Ticking it sets `stepover_end` equal
    to the spacing, which the generator lays as the even layer.
  * Up through the layers: `stepover_top`, the top layer's spacing, a design
    var only. `attach_layer_grade` keeps every layer's `stepover` override
    on the straight line from Fiber spacing to it, the same overrides the
    Layer parameters table's right-click Grade writes, and rewrites them
    whenever the spacing, the top or the layer count changes.

The grade rows show once a column grades; the Preview row draws each layer
direction from `scaffold_gcode._layer_passes` itself.

A layer that differs is counted beside its scaffold's cell (``0.5  ·3``) and
listed under the Parameters table; the page's tree shows a Layer parameters row
under that layer, read from `_gce_layer_overrides`.

Neither table owns state. Scaffold cells are views over the designer's own
Tk variables, `self.gce_scaf_vars[i][key]` (`gui_scaffold._init_scaffold_vars`);
layer cells over one more of them, `scaf_vars[i]["layer_overrides"]` -- JSON
``{"<layer, 1-based>": {key: value}}``, which the generator applies
(`scaffold_gcode.parse_layer_overrides`, `LAYER_OVERRIDE_KEYS`). Layer
rotation is a combobox WIDGET in the designer (`v["rotation"]`, parsed by
`_rotation_value`); the table reads and sets that widget. A field the
designer greys out is greyed here too.

Validation is the designer's: each cell runs the same `_ArgsMixin` check
`_validate_one_scaffold` runs for that field, with the same wording. A bad
value is never written; the cell is marked and the message shown under the
table.

On the 4-axis collector every page scaffold is a TUBE of its own, and the
table has the same one column per scaffold: `TUBE_PARAM_ROWS` over that
scaffold's own tube vars (`gui_gcode_twodesigns.TUBE_KEYS`, in its page
dict) and its plain Layers / speed / fiber. Every field the designer's Tube
card holds is a row -- its Resolved readout and its two ladders too, the
ladders as dropdowns -- and each column is resolved by the designer's own
Tube code inside `_gce_tube_scope(column)` (`_gcp_tube_info`). A row no
column reads is left out; a cell its own column does not read is greyed.
`TUBE_LAYER_ROWS` is one scaffold's layers over its `m_layer_overrides`,
the same JSON shape keyed by `scaffold_gcode.TUBE_LAYER_KEYS`. A layer set
to another pattern is a LAYER BLOCK: consecutive layers that ask for the
same thing are one block (`scaffold_gcode.tube_layer_blocks`), so "4
winding / 4 re-entrant / 4 winding" is layers 5-8 set to re-entrant.

Wiring (the interface the ribbon and tree call): `_gce_params_open(col=None)` and
`_gce_layer_params_open(scaffold, layer=None)`, all 0-based. Edits are
STAGED: a table write (`_staged`) holds `_gce_design_regen` back, and
`_gce_params_close` (the Done button) calls it once (`_gcp_flush`)."""

import json
import re

import tkinter as tk
from tkinter import ttk

import gui_form
import gui_revert
import gui_units
import gui_widgets
from gui_common import ROTATION_PRESETS
import gui_tips
from gui_tips import Tooltip, add_tip

#: the scaffold table's rows: (group, label, key, unit, kind). kind picks the
#: designer's validator for that field (see `_gcp_check`).
PARAM_ROWS = (
    ("Size", "Width X", "width", "mm", "pos"),
    ("Size", "Length Y", "height", "mm", "pos"),
    ("Size", "Fiber diameter", "diameter", "mm", "pos"),
    ("Size", "Layers", "layers", "", "layers"),
    ("Size", "Print speed", "speed", "mm/min", "pos"),
    ("Spacing", "Fiber spacing", "stepover", "mm", "pos"),
    ("Spacing", "Grade the spacing", "grade_on", "", "bool"),
    ("Spacing", "Grade", "grade_dir", "", "grade_dir"),
    ("Spacing", "Spacing where it ends", "grade_end", "mm", "pos"),
    ("Spacing", "Change", "grade_rate", "", "out"),
    ("Spacing", "Preview", "grade_sketch", "", "sketch"),
    ("Placement", "Start X", "start_x", "mm", "any"),
    ("Placement", "Start Y", "start_y", "mm", "any"),
    ("Placement", "Start Z", "start_z", "mm", "any"),
    ("Placement", "Layer rotation", "rotation", "deg", "rotation"),
    ("Placement", "Start angle", "start_angle", "deg", "any"),
    ("Placement", "Footprint angle", "footprint_angle", "deg", "any"),
    ("Turns", "Turnaround radius", "arc_radius_mult", "× spacing", "blank_nonneg"),
    ("Turns", "Arc segments", "arc_segments", "", "segments"),
    ("Turns", "Loop speed", "loop_speed", "mm/min", "blank_pos"),
    ("Turns", "Linking loops", "link_loops_on", "", "bool"),
    ("Travel", "Perimeter travel", "perimeter_travel", "", "bool"),
    ("Travel", "Distance from the part", "perimeter_travel_offset", "mm",
     "nonneg_mm"),
)

#: keys whose cells follow the Settings panel's display units, as the
#: Scaffold designer's fields do (gui_units): key -> (quantity, the unit the
#: var holds). The var keeps its unit; only the cell's text converts.
_DISPLAY_UNITS = {"diameter": ("fiber_d", "mm"), "speed": ("speed", "mm/min"),
                  "loop_speed": ("speed", "mm/min")}

#: the layer table's rows: (group, label, override key, unit, kind). The
#: override keys are `scaffold_gcode.LAYER_OVERRIDE_KEYS`.
LAYER_ROWS = (
    ("Size", "Width X", "width", "mm", "pos"),
    ("Size", "Length Y", "height", "mm", "pos"),
    ("Size", "Print speed", "speed", "mm/min", "pos"),
    ("Spacing", "Fiber spacing", "stepover", "mm", "pos"),
    ("Spacing", "Spacing where it ends", "stepover_end", "mm", "blank_end"),
    ("Spacing", "Grade direction", "grade_angle", "deg", "any"),
    ("Placement", "Start X", "start_x", "mm", "any"),
    ("Placement", "Start Y", "start_y", "mm", "any"),
    ("Placement", "Start Z", "start_z", "mm", "any"),
    ("Placement", "Fiber angle", "angle", "deg", "any"),
    ("Placement", "Footprint angle", "footprint_angle", "deg", "any"),
    ("Turns", "Turnaround radius", "arc_radius_mult", "× spacing", "nonneg"),
    ("Turns", "Arc segments", "arc_segments", "", "segments"),
    ("Turns", "Loop speed", "loop_speed", "mm/min", "pos"),
    ("Turns", "Linking loops", "link_loops", "", "bool"),
)

#: the 4-axis table: one column per scaffold, each scaffold a TUBE of its
#: own, and every field the designer's Tube card, its Advanced closure and
#: its ladders hold (`gui_mandrel`). The cells are views over that
#: scaffold's own tube vars (`gui_gcode_twodesigns.TUBE_KEYS`), read and
#: resolved inside `_gce_tube_scope(column)`. A row no column's pattern or
#: closure reads is left out, as the Tube card leaves it out; a cell its own
#: column does not read is greyed. "Resolved" is what the software built
#: (read off the design object, as the Tube card's Resolved column is);
#: "Options" are the Tube card's ladders, as dropdowns.
TUBE_PARAM_ROWS = (
    ("Pattern", "Pattern", "family", "", "family"),
    ("Pattern", "Custom cell", "cell_graph", "", "cell_edit"),
    ("Pattern", "Scaffold length", "length", "mm", "pos"),
    ("Pattern", "Winding angle", "winding_angle", "deg from axis", "pos"),
    ("Pattern", "Pore size", "pore", "µm", "pos"),
    ("Pattern", "Cell along", "cell_width", "mm", "pos"),
    ("Pattern", "Cell around", "cell_height", "mm", "pos"),
    ("Pattern", "Rib angle", "rib_angle", "deg", "rib"),
    ("Pattern", "Wave amplitude", "amplitude", "mm", "nonneg_mm"),
    ("Closure", "Closure", "design_mode", "", "choice"),
    ("Closure", "Set η and Rm myself", "closure_override", "", "bool"),
    ("Closure", "η divisor", "eta", "", "blank_int"),
    ("Closure", "Extra revolutions Rm", "rm", "", "nonneg_int"),
    ("Closure", "Length may miss by", "length_tol", "%", "pos"),
    ("Closure", "η search cap", "eta_cap", "", "blank_int"),
    ("Options", "Pore size", "helix_pick", "", "pick"),
    ("Options", "Cell fit", "cell_fit", "", "choice"),
    ("Options", "Cell count", "cell_pick", "", "pick"),
    ("Resolved", "Winding angle", "r_angle", "deg from axis", "out"),
    ("Resolved", "Pore size", "r_pore", "µm", "out"),
    ("Resolved", "Scaffold length", "r_length", "mm", "out"),
    ("Resolved", "Turn at each end", "r_turn", "deg", "out"),
    ("Resolved", "Cells", "r_cells", "", "out"),
    ("Layers", "Fiber diameter", "diameter", "mm", "pos"),
    ("Layers", "Layers", "layers", "", "layers"),
    ("Layers", "Layer step", "step", "mm", "blank_pos"),
    ("Layers", "Print speed", "speed", "mm/min", "pos"),
    ("Travel", "Perimeter travel", "turn_out_on", "", "bool"),
    ("Travel", "Offset", "turn_out", "mm", "nonneg_mm"),
    ("Placement", "Position on mandrel", "place", "", "choice"),
    ("Placement", "Offset", "place_offset", "mm", "nonneg_mm"),
    ("Placement", "Start out from mandrel", "start_out", "mm", "nonneg_mm"),
)

#: ...and its layer table, one scaffold's: the override keys are
#: `scaffold_gcode.TUBE_LAYER_KEYS`
TUBE_LAYER_ROWS = (
    ("Pattern", "Pattern", "family", "", "family"),
    ("Pattern", "Winding angle", "winding_angle", "deg from axis", "pos"),
    ("Pattern", "Pore size", "pore", "µm", "pos"),
    ("Pattern", "Cell along", "cell_width", "mm", "pos"),
    ("Pattern", "Cell around", "cell_height", "mm", "pos"),
    ("Pattern", "Rib angle", "rib_angle", "deg", "rib"),
    ("Pattern", "Wave amplitude", "amplitude", "mm", "nonneg_mm"),
    ("Layer", "Print speed", "speed", "mm/min", "pos"),
    ("Layer", "Step out", "step", "mm", "pos"),
    ("Placement", "Position on mandrel", "place", "", "choice"),
    ("Placement", "Offset", "place_offset", "mm", "nonneg_mm"),
)

#: tube-table key -> the scaffold's own tube var behind it (its key in the
#: page's scaffold dict); "diameter" / "layers" / "speed" are the scaffold's
#: plain keys, and "helix_pick" is the closure's own pin
#: (`_gcp_tube_var`)
_TUBE_VAR = {"family": "m_family", "cell_graph": "m_cell_graph",
             "length": "m_length", "winding_angle": "m_angle",
             "pore": "m_pore_target", "cell_width": "m_cell_w",
             "cell_height": "m_cell_h", "rib_angle": "m_rib_angle",
             "amplitude": "m_cell_amplitude", "design_mode": "m_design_mode",
             "closure_override": "m_closure_override", "eta": "m_eta",
             "rm": "m_rm", "length_tol": "m_length_tol",
             "eta_cap": "m_eta_cap", "cell_fit": "m_cell_fit",
             "cell_pick": "m_cell_pick", "step": "m_layer_step",
             "place": "m_place", "place_offset": "m_place_offset",
             "start_out": "m_start_out", "turn_out_on": "m_turn_out_on",
             "turn_out": "m_turn_out"}

#: a "choice" row's (token, shown) pairs; the var holds the token
_CHOICES = {
    "design_mode": (("turn", "Turn at the ends"),
                    ("pore", "Length closes, pore picked"),
                    ("angle", "Length closes, angle picked")),
    "cell_fit": (("scale", "Scaled"), ("fit", "Fitted")),
}
# the placement row reads the one table (`gui_mandrel.PLACEMENTS`): the var
# holds the designer's label, the cell shows the short one
import gui_mandrel as _gui_mandrel  # noqa: E402
_CHOICES["place"] = tuple((k, short) for k, _label, short
                          in _gui_mandrel.PLACEMENTS)

#: tube-table key -> the layer key that can make it differ (a layer's
#: placement is resolved to a shift along the rod by
#: `gui_mandrel._mandrel_tube_entries`, by the tube's own placement rule)
_TUBE_LAYER_KEY_OF = {k: k for k in ("family", "winding_angle", "pore",
                                     "cell_width", "cell_height", "rib_angle",
                                     "amplitude", "speed", "step", "place",
                                     "place_offset")}

#: the layer rows only a winding reads, and the ones only a cell does
_WINDING_ONLY = ("winding_angle", "pore")
_CELL_ONLY = ("cell_width", "cell_height")
#: ...only the re-entrant honeycomb, and only a wavy family
_RIB_ONLY = ("rib_angle",)
_WAVE_ONLY = ("amplitude",)

#: scaffold-table key -> the layer override that can make it differ
_LAYER_KEY_OF = {"width": "width", "height": "height",
                 "stepover": "stepover", "speed": "speed",
                 "start_x": "start_x", "start_y": "start_y",
                 "start_z": "start_z", "rotation": "angle",
                 "start_angle": "angle", "footprint_angle": "footprint_angle",
                 "arc_radius_mult": "arc_radius_mult",
                 "arc_segments": "arc_segments", "loop_speed": "loop_speed",
                 "link_loops_on": "link_loops"}

#: the designer variable a layer cell inherits from (angle is computed)
_VAR_OF_LAYER = {"link_loops": "link_loops_on"}

#: the Spacing group's grade rows: shown once a scaffold grades its spacing
_GRADE_ROWS = ("grade_dir", "grade_end", "grade_rate", "grade_sketch")
_GRADE_KEYS = ("grade_on",) + _GRADE_ROWS
#: the Grade choices a plate scaffold's spacing can grade along: label ->
#: the in-layer direction (deg, `grade_angle`), or None for the layer by
#: layer grade (`stepover_top`). Any typed angle is a direction too.
GRADE_DIRS = (("Left to right", 0.0), ("Front to back", 90.0),
              ("Corner to corner", 45.0), ("Up through the layers", None))
_LAYERS_DIR = "Up through the layers"
#: the design vars the grade rows and the Preview are worked out from
_GRADE_SOURCES = ("stepover", "stepover_end", "stepover_top", "grade_angle",
                  "layers", "width", "height", "rotation", "start_angle",
                  "angle_pattern_on", "angle_pattern", "shape", "shape_sides",
                  "shape_polygon")

#: a flat scaffold's tip where its row means something other than a tube's
_PLATE_TIPS = {
    "stepover": gui_tips.pic(
        "stepover",
        "How far apart neighbouring fibers are laid, centre to centre. The "
        "open pore between them is this less the fiber diameter: 0.5 mm "
        "with a 25 µm fiber leaves a 475 µm pore. With Grade the spacing "
        "on, this is the spacing where the grade starts."),
}

#: the kinds a layer row can be graded in (a number that varies smoothly)
_GRADABLE = ("pos", "any", "nonneg", "blank_end")

#: the designer's entry widget for a key, when it keeps one; its state is
#: whether the designer currently lets that field be edited
_ENTRY_OF = {"width": "width_entry", "height": "height_entry",
             "diameter": "diameter_entry", "start_angle": "start_angle_entry",
             "footprint_angle": "footprint_angle_entry",
             "arc_segments": "arc_segments_entry", "rotation": "rotation"}

_TIPS = {
    "rotation": "Each layer turns by this much from the one below: 90 "
                "lays 0/90, 60 lays 0/60/120 and 45 lays 0/45/90/135. Pick "
                "one or type any angle. For a cycle that is not one even "
                "step, select the scaffold and use Layer angles.",
    "angle": "The direction this layer's fibers run, in degrees.",
    "diameter": "The fiber this scaffold prints. Grey means the Physics "
                "page predicts it from the pressure, the temperature and "
                "this scaffold's print speed, and it follows them as you "
                "change them: less pressure or more speed makes a thinner "
                "fiber. To type it instead, turn on Override fiber diameter "
                "in Setup > Physics > Fiber diameter.",
    "grade_angle": "The direction this layer's spacing grades in, in "
                   "degrees: 0 = from the left edge to the right (X), "
                   "90 = from the front edge to the back (Y). It turns "
                   "with the footprint. Used only when Spacing where it "
                   "ends is set.",
    "stepover_end": "Grades this layer's spacing across it: Fiber spacing "
                    "at the start edge to this at the far edge, along "
                    "Grade direction. Blank = the scaffold's.",
    "grade_on": "Change the fiber spacing smoothly across the scaffold, "
                "from Fiber spacing to Spacing where it ends, so the pores "
                "grow (or shrink) from one side to the other, or from the "
                "bottom layer to the top.",
    "grade_dir": "Which way the spacing changes. Left to right and Front "
                 "to back grade across each layer: fibers that run the same "
                 "way as the grade cannot change their spacing along their "
                 "length, so those layers stay even (the Preview shows "
                 "which). Corner to corner grades every layer of a 0/90 "
                 "scaffold. Up through the layers gives each layer its own "
                 "even spacing, from the bottom layer to the top. Or type "
                 "an angle in degrees.",
    "grade_end": "The spacing where the grade ends: at the far edge, or on "
                 "the top layer for Up through the layers.",
    "grade_rate": "How fast the spacing changes: per mm across the layer, "
                  "or per layer up through the layers.",
    "grade_sketch": "The spacing each layer direction prints with, drawn "
                    "from the toolpath itself. Up through the layers is "
                    "drawn from the side, bottom layer at the bottom.",
    "arc_radius_mult": "Corner-loop radius as a multiple of the fiber "
                       "spacing. 0 = sharp turns.",
    "loop_speed": "Feed rate for the corner loops. Blank = the print speed.",
    "link_loops_on": "A gentle lead-in loop at square corners, so the jet "
                     "rounds the corner off the part.",
    "link_loops": "A lead-in loop at the corner INTO this layer.",
    "perimeter_travel": "Run every automatic travel (the lead-in, the hop "
                        "between layers, the hop to the next scaffold and "
                        "the exit) along this scaffold's outline, instead "
                        "of round its box with 90 degree corners.",
    "perimeter_travel_offset": "How far outside the outermost fiber the "
                               "travel runs.",
    "family": "The pattern this tube or layer is wound in. A "
              "layer set to another pattern makes a layer block; the layers "
              "either side keep the tube's.",
    "step": "How far the nozzle steps OUT to the next layer. Blank = one "
            "fiber diameter; a winding's crossings stack about two.",
    "cell_graph": "The drawn unit cell of the Custom cell pattern. Edit… "
                  "opens it for this scaffold.",
    "length": "How long a tube you want. Turn at the ends builds it as "
              "typed; the two Length closes builds the nearest that closes.",
    "winding_angle": "The fiber's angle from the mandrel axis, 0 along it "
                     "and 90 a ring. Worked out for you under Length "
                     "closes, pore picked.",
    "pore": "The opening you want, as the side of a square of the same "
            "area. The software builds the nearest one this angle and mandrel "
            "can make.",
    "cell_width": "The repeating cell's size along the mandrel axis.",
    "cell_height": "The repeating cell's size around the circumference, as "
                   "arc length. A whole number of them has to close.",
    "rib_angle": "How far the re-entrant bow-tie is folded, from the "
                 "mandrel axis. Strictly between 0 and 90.",
    "amplitude": "How far the circumferential fibers wave along the axis. "
                 "0 leaves the rings straight; the chevron needs more.",
    "design_mode": "Turn at the ends: angle, pore and length are all built "
                   "as typed; the mandrel turns onto the next pivot at "
                   "each end. The two Length closes close the helix by "
                   "length alone (McColl 2018): the length you get is the "
                   "nearest that closes.",
    "closure_override": "Off: the software picks η and Rm. On: the two "
                        "numbers below are built as typed.",
    "eta": "McColl's closure divisor: the pattern goes around η times per "
           "pass. Worked out for you unless you set it.",
    "rm": "Whole extra revolutions per axial iteration.",
    "length_tol": "How far the closing length may fall from your request.",
    "eta_cap": "The largest η the pore search will try. Blank uses the "
               "solver's own ceiling.",
    "helix_pick": "The software's pick is the pore nearest yours; the "
                  "others are coarser or finer pores this tube can make.",
    "cell_fit": "Scaled keeps the cell's shape and moves the length; "
                "Fitted keeps the length and stretches the cell around.",
    "cell_pick": "Auto is the rounding a whole number of cells closing the "
                 "circumference forces; the others are its neighbours.",
    "place": "Which end of the mandrel the offset is measured from, or "
             "centered with equal bare mandrel at both ends. Two scaffolds side "
             "by side are two offsets.",
    "place_offset": "Bare mandrel from the end named above to this scaffold's "
                    "print. Ignored when centered.",
    "turn_out_on": "A tube that closes by turning at its ends: instead of "
                   "turning in place at the end, the head runs out past the "
                   "end along the mandrel, turns there, and comes back.",
    "turn_out": "How far past the tube's end the turn happens.",
    "start_out": "How far out from the bare mandrel this scaffold's first "
                 "layer starts. 0 lies on the mandrel; a scaffold stacked on "
                 "another starts where that one's wall ends.",
    "r_angle": "The winding angle the software built.",
    "r_pore": "The pore the software built, as the side of a square of "
              "the same area.",
    "r_length": "The scaffold length the software built.",
    "r_turn": "How far the mandrel turns at each end of a pass.",
    "r_cells": "How many cells close around and along, and their size.",
}

#: the line under each table's title, by (mode, tube)
_HINTS = {
    ("scaffold", False):
        "Every scaffold's settings, one column each. Change a value and "
        "press Return: that scaffold is rebuilt in the picture. Right-click "
        "a value to copy it to every scaffold, or to set it layer by layer. "
        "Grade the spacing makes the pores grow across the scaffold or "
        "from the bottom layer to the top. A dot count (·3) means that many layers "
        "differ. Start X/Y is the footprint's lower-left corner, measured "
        "from g-code zero.",
    ("layer", False):
        "One column per layer. Grey values are the scaffold's; type a "
        "different value to make that layer differ. To grade a value, type "
        "it in the layers at both ends, then right-click a layer between "
        "them and choose Grade. Layers outside the two, such as a sparse "
        "base, keep their values. Right-click a value to put the "
        "scaffold's back.",
    ("scaffold", True):
        "Every scaffold's tube, one column each. Change a value and press "
        "Return: that tube is rebuilt in the picture. Resolved is what "
        "the software built. Right-click a value to copy it to every "
        "scaffold. A dot count (·3) means that many layers differ.",
    ("layer", True):
        "One column per layer. Grey values are the scaffold's; type a "
        "different value or pick another pattern to make that layer "
        "differ. Right-click a value to put the scaffold's back.",
}

#: how many per-layer changes the list under the scaffold table names
_LIST_MAX = 8


#: the Tk names of the stepover_top vars `attach_layer_grade` follows
_GRADE_ATTACHED = set()


def _layer_ov(v):
    """Scaffold v's layer overrides, {layer 1-based str: {key: value}}."""
    try:
        raw = str(v["layer_overrides"].get()).strip()
        data = json.loads(raw) if raw else {}
        return {str(k): dict(x) for k, x in data.items() if x}
    except (ValueError, TypeError, AttributeError, KeyError, tk.TclError):
        return {}


def _set_layer_ov(v, ov):
    text = (json.dumps({k: ov[k] for k in sorted(ov, key=int) if ov[k]})
            if any(ov.values()) else "")
    if str(v["layer_overrides"].get()) != text:
        v["layer_overrides"].set(text)


def clear_layer_grade(v):
    """Drop every layer's own spacing: the layer by layer grade's."""
    ov = _layer_ov(v)
    for row in ov.values():
        row.pop("stepover", None)
    _set_layer_ov(v, ov)


def sync_layer_grade(v):
    """Up through the layers: every layer's `stepover` override on the
    straight line from the scaffold's Fiber spacing (layer 1) to
    `stepover_top` (the top layer) -- what the Layer parameters table's
    Grade writes. Nothing when `stepover_top` is blank."""
    try:
        top = str(v["stepover_top"].get()).strip()
        if not top:
            return
        s0, s1 = float(v["stepover"].get()), float(top)
        n = max(1, int(float(v["layers"].get())))
    except (KeyError, ValueError, tk.TclError):
        return
    if s0 <= 0 or s1 <= 0:
        return
    ov = _layer_ov(v)
    for row in ov.values():
        row.pop("stepover", None)
    for i in range(1, n):
        ov.setdefault(str(i + 1), {})["stepover"] = "%.6g" % (
            s0 + (s1 - s0) * i / (n - 1))
    _set_layer_ov(v, ov)


def attach_layer_grade(v):
    """Keep scaffold v's layer by layer grade on its line whoever changes
    the spacing, the top or the layer count (this table, a preset, Undo)."""
    top = v.get("stepover_top")
    if not isinstance(top, tk.Variable) or str(top) in _GRADE_ATTACHED:
        return
    _GRADE_ATTACHED.add(str(top))
    for k in ("stepover", "stepover_top", "layers"):
        x = v.get(k)
        if isinstance(x, tk.Variable):
            x.trace_add("write", lambda *_a: sync_layer_grade(v))


def _staged(fn):
    """Run a table write with the preview's regenerate held back:
    `_gce_design_regen` (fired by the design var traces) notes the design is
    stale in `_gcp_dirty` and returns; Done regenerates once."""
    def wrapper(self, *args, **kw):
        self._gcp_staging = getattr(self, "_gcp_staging", 0) + 1
        try:
            return fn(self, *args, **kw)
        finally:
            self._gcp_staging -= 1
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class _GcodeParamsMixin:
    """The Parameters tables. Sits before `_GcodeChangesMixin` in
    `ModernApp`'s bases; it only ADDS `_gce_params_*`, `_gce_layer_*`,
    `_gcp_*` and `_gcl_*` methods."""

    # ------------------------------------------------------------------ #
    # entry points (the page's ribbon and tree call these)
    # ------------------------------------------------------------------ #
    def _gce_params_open(self, col=None):
        """Open the Parameters box over the picture, with scaffold `col`
        (0-based) lit -- in the view (per scaffold / per layer) the toggle
        was last left on."""
        if getattr(self, "_gcp_view", "scaffold") == "layer":
            st = getattr(self, "_gcp", None)
            s = col if col is not None else (
                st["hi"] if st is not None and st["hi"] is not None else 0)
            self._gce_layer_params_open(s)
            return
        self._gce_open_scaffold_view(col)

    def _gce_open_scaffold_view(self, col=None):
        self._gcp_view = "scaffold"
        st = self._gcp_ready("scaffold")
        if st is None:
            return
        n = self._gcp_columns()
        if st["n"] != n or st.get("tube") != self._gcp_tube():
            self._gcp_fill(st)
        if col is not None:
            st["hi"] = max(0, min(int(col), n - 1))
        self._gcp_show(st)
        self._gcp_focus(st, 0, st["hi"] or 0)

    def _gce_layer_params_open(self, scaffold, layer=None, key=None):
        """Open the layer table for `scaffold`, with `layer` lit and
        scrolled into view (both 0-based). `key`, an override key, puts the
        cursor on that row."""
        self._gcp_view = "layer"
        st = self._gcp_ready("layer")
        if st is None:
            return
        scaffold = max(0, min(int(scaffold), self._gcp_columns() - 1))
        if (st["s"] != scaffold or st["n"] != self._gcl_layers(scaffold)
                or st.get("tube") != self._gcp_tube()):
            st["s"] = scaffold
            self._gcp_fill(st)
        if layer is not None:
            st["hi"] = max(0, min(int(layer), st["n"] - 1))
        self._gcp_show(st)
        gi = next((i for i, r in enumerate(self._gcp_rows(st))
                   if r[2] == key), 0)
        self._gcp_focus(st, gi, st["hi"] or 0)

    def _gce_params_close(self, _e=None):
        for name in ("_gcp", "_gcl"):
            st = getattr(self, name, None)
            if st is None or not st["shown"]:
                continue
            self._gcp_commit_focused(st)
            st["shown"] = False
            try:
                st["box"].place_forget()
            except tk.TclError:
                pass
        self._gcp_flush()
        self._gcp_tools_follow()
        return "break"

    def _gce_params_shown(self):
        return any(getattr(self, n, None) and getattr(self, n)["shown"]
                   for n in ("_gcp", "_gcl"))

    def _gce_layer_overrides(self, scaffold):
        """{layer (0-based): {override key: value}} for one scaffold, as
        saved -- what the tree reads for its Layer parameters rows. A
        garbled field reads as none."""
        try:
            raw = self._gcl_ov_var(scaffold).get().strip()
            data = json.loads(raw) if raw else {}
            return {int(k) - 1: dict(v) for k, v in data.items() if v}
        except (ValueError, TypeError, AttributeError, KeyError, IndexError,
                tk.TclError):
            return {}

    def _gcp_tube(self):
        """True when the page designs TUBES (the 4-axis collector): each
        column is then a scaffold's tube."""
        try:
            return self.s_collector.get() == "mandrel"
        except (AttributeError, tk.TclError):
            return False

    def _gcp_columns(self):
        """How many columns the scaffold table has: one per scaffold, a
        tube's as a plate's."""
        return self._gce_scaffold_count()

    def _gcp_tube_var(self, key, c):
        """The Tk variable behind a tube-table row in column `c`: scaffold
        c's own tube var (`_TUBE_VAR`), its plain Layers / speed / fiber, or
        -- the pore-size choice -- the pin its closure's ladder writes."""
        v = self.gce_scaf_vars[c]
        if key == "helix_pick":
            mode = v["m_design_mode"].get().strip()
            return v[{"pore": "m_helix_pick", "angle": "m_angle_pick"}.get(
                mode, "m_turn_pick")]
        return v[_TUBE_VAR.get(key, key)]

    def _gcl_ov_var(self, s):
        """The JSON per-layer field: scaffold `s`'s tube's, or its plate
        scaffold's."""
        if self._gcp_tube():
            return self.gce_scaf_vars[s]["m_layer_overrides"]
        return self.gce_scaf_vars[s]["layer_overrides"]

    def _gcp_scaffold_of(self, cell):
        """The scaffold (0-based) a cell belongs to."""
        st = cell["st"]
        return st["s"] if st["mode"] == "layer" else cell["c"]

    def _gcp_family(self, cell):
        """The engine family token a tube cell's column prints with."""
        st = cell["st"]
        s = self._gcp_scaffold_of(cell)
        if st["mode"] == "layer":
            o = self._gcl_override(s, cell["c"], "family")
            if o is not None:
                return o
        with self._gce_tube_scope(s):
            return self._mandrel_family_value()

    # ------------------------------------------------------------------ #
    # building
    # ------------------------------------------------------------------ #
    def _gcp_ready(self, mode):
        """The table for `mode`, built on first use; the other one hidden.
        None when the page has no picture to float over."""
        vp = getattr(self, "_gce_viewport", None)
        if vp is None:
            return None
        # an open edit is kept and closed: the boxes would overlap
        finish = getattr(self, "_gce_edit_finish", None)
        if finish is not None:
            finish(keep=True)
        # Undo changes goes back to the values as they were when the box
        # opened -- moving between the two tables is still one visit
        if not self._gce_params_shown():
            self._gcp_session = self._gcp_snapshot()
        name = "_gcp" if mode == "scaffold" else "_gcl"
        other = getattr(self, "_gcl" if mode == "scaffold" else "_gcp", None)
        if other is not None and other["shown"]:
            self._gcp_commit_focused(other)
            other["shown"] = False
            other["box"].place_forget()
        st = getattr(self, name, None)
        if st is None or not st["box"].winfo_exists():
            st = self._gcp_build(vp, mode)
            setattr(self, name, st)
        return st

    def _gcp_switch(self, st):
        """The view toggle was clicked in `st`'s box."""
        want = st["view"].get()
        st["view"].set(st["mode"])          # this box keeps its own view
        if want == st["mode"]:
            return
        if want == "layer":
            self._gce_layer_params_open(st["hi"] or 0)
        else:
            self._gce_open_scaffold_view(st["s"])

    def _gcp_show(self, st):
        # one box over the picture at a time: a tool's panel (Footprint,
        # Array, Presets...) sits where this box does, and left open it
        # showed through around it (`_gce_host_open` does the converse)
        if getattr(self, "_gce_form", None) is not None:
            self._gce_host_close()
        st["view"].set(st["mode"])
        self._gcp_highlight(st)
        self._gcp_refresh(st)
        st["shown"] = True
        self._gcp_place()
        self._gcp_tools_follow()

    def _gcp_build(self, vp, mode):
        page = (self._gce_formbox.master
                if getattr(self, "_gce_formbox", None) is not None
                else vp.winfo_toplevel())
        box = ttk.Frame(page, padding=(10, 6, 10, 8), borderwidth=1,
                        relief="solid")
        box.columnconfigure(0, weight=1)
        head = ttk.Frame(box)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        title = tk.StringVar(value="Parameters")
        tl = ttk.Frame(head)
        tl.grid(row=0, column=0, sticky="w")
        tlabel = ttk.Label(tl, text="Parameters", font="MewSection")
        tlabel.pack(side="left")
        btns = ttk.Frame(head)
        btns.grid(row=0, column=1, sticky="e")
        st = dict(mode=mode, box=box, title=title, n=0, s=0, hi=None,
                  cells={}, heads=[], badges={}, shown=False,
                  traces=[], place_after=None, refresh_after=None,
                  graded={}, typed=set())
        st["speed_units"] = tk.StringVar(
            value=gui_units.shown("speed", "mm/min"))
        st["speed_note"] = tk.StringVar(value="")
        # the one view toggle: every scaffold side by side, or one
        # scaffold's layers
        view = tk.StringVar(value=mode)
        st["view"] = view
        seg = gui_widgets.SegmentedBar(
            tl, [("scaffold", "Per scaffold"), ("layer", "Per layer")], view,
            command=lambda: self._gcp_switch(st))
        seg.pack(side="left", padx=(12, 0))
        add_tip(seg, "Per scaffold: one column per scaffold. Per layer: one "
                     "column per layer of the chosen scaffold, to make "
                     "single layers differ.")
        if mode == "layer":
            pick = tk.StringVar(value="")
            cb = gui_widgets.FitCombobox(tl, textvariable=pick,
                                         state="readonly")
            cb.pack(side="left", padx=(8, 0))
            cb.bind("<<ComboboxSelected>>", lambda _e: (
                self._gce_layer_params_open(cb.current()), "break")[1])
            add_tip(cb, "Whose layers the columns are.")
            st["pick"], st["pick_cb"] = pick, cb
        undo = ttk.Button(btns, text="Undo changes",
                          command=lambda: self._gcp_undo_all(st))
        undo.pack(side="left")
        add_tip(undo, "Put every value back as it was when the table "
                      "opened.")
        ttk.Button(btns, text="Done", style="Primary.TButton",
                   command=self._gce_params_close).pack(side="left",
                                                       padx=(6, 0))
        st["hint"] = tk.StringVar(value="")
        ttk.Label(box, style="Hint.TLabel", wraplength=440, justify="left",
                  textvariable=st["hint"]).grid(row=1, column=0, sticky="w",
                                                pady=(2, 6))
        # the body scrolls up and down when the picture is too short for
        # it (a tube has many rows); inside it the row labels stay put on
        # the left and the columns -- scaffolds or layers -- scroll sideways
        outer = tk.Canvas(box, highlightthickness=0, borderwidth=0)
        outer.grid(row=2, column=0, sticky="ew")
        ybar = ttk.Scrollbar(box, orient="vertical", command=outer.yview)
        outer.configure(yscrollcommand=ybar.set)
        body = tk.Frame(outer)
        outer.create_window(0, 0, window=body, anchor="nw")
        body.columnconfigure(1, weight=1)
        st["body"], st["outer"], st["ybar"] = body, outer, ybar
        st["lgrid"] = tk.Frame(body)
        st["lgrid"].grid(row=0, column=0, sticky="nw")
        cv = tk.Canvas(body, highlightthickness=0, borderwidth=0)
        cv.grid(row=0, column=1, sticky="ew")
        st["grid"] = tk.Frame(cv)
        cv.create_window(0, 0, window=st["grid"], anchor="nw")
        sb = ttk.Scrollbar(body, orient="horizontal", command=cv.xview)
        cv.configure(xscrollcommand=sb.set)
        st["canvas"], st["xbar"] = cv, sb
        box.bind_all("<Shift-MouseWheel>", lambda e: self._gcp_wheel(
            st, e, "x"), add="+")
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            box.bind_all(seq, lambda e: self._gcp_wheel(st, e, "y"),
                         add="+")
        st["err"] = tk.StringVar(value="")
        errl = ttk.Label(box, textvariable=st["err"], style="Hint.TLabel",
                         wraplength=440, justify="left")
        gui_form.mark(errl, "live")
        errl.grid(row=4, column=0, sticky="w", pady=(4, 0))
        st["errl"] = errl
        # a tube that will not resolve, named (the Tube card's refusal)
        st["note"] = tk.StringVar(value="")
        notel = ttk.Label(box, textvariable=st["note"], style="Hint.TLabel",
                          wraplength=440, justify="left")
        gui_form.mark(notel, "live")
        notel.grid(row=5, column=0, sticky="w", pady=(4, 0))
        st["notel"] = notel
        speedl = ttk.Label(box, textvariable=st["speed_note"],
                           style="Hint.TLabel", wraplength=440)
        speedl.grid(row=6, column=0, sticky="w")
        if mode == "scaffold":
            st["list"] = ttk.Frame(box)
            st["list"].grid(row=3, column=0, sticky="w", pady=(6, 0))
            self._gcp_build_exit(st)
        box.bind("<Escape>", self._gce_params_close, add="+")
        vp.bind("<Configure>", lambda _e: self._gcp_place_soon(), add="+")
        # any edge or corner resizes it, as the page's other boxes
        if hasattr(self, "_gce_resizable"):
            # never narrower than its title row; its sentences wrap to it
            st["head"] = head
            st["edges"] = self._gce_resizable(
                box, ("params", mode), "params:" + mode,
                min_size=lambda: (head.winfo_reqwidth() + 24, 160),
                relayout=lambda: self._gcp_place_one(st), laid_from_nw=True)
            wraps = [w for w in box.grid_slaves(column=0)
                     if isinstance(w, ttk.Label)]

            def rewrap(e):
                if e.widget is box:
                    for w in wraps:
                        w.configure(wraplength=max(200, min(440, e.width - 24)))
            box.bind("<Configure>", rewrap, add="+")
        # its title row moves it, as the page's other floating boxes: the
        # place lands under the key `_gcp_place_one` lays it from
        if hasattr(self, "_gce_drag_bind"):
            self._gce_drag_bind("params:" + mode, box, head, tl, tlabel)
        # The three message lines under the table are empty most of the
        # time, and an empty label still takes a line: together a blank
        # strip about 80 px tall at the box's foot. Each takes its row only
        # while it has something to say, and the box is re-laid when one
        # comes or goes. (After `wraps` is read: a removed label is not a
        # grid slave, and it must still rewrap when it returns.)
        def fold(var, w):
            def show(*_a):
                try:
                    if var.get().strip():
                        w.grid()
                    else:
                        w.grid_remove()
                except tk.TclError:
                    return
                self._gcp_place_soon()
            var.trace_add("write", show)
            show()
        fold(st["err"], errl)
        fold(st["note"], notel)
        fold(st["speed_note"], speedl)
        st["rt"] = gui_widgets.register(_Retheme(self, st))
        if mode == "scaffold":
            try:
                self.gce_s_count.trace_add(
                    "write", lambda *_a: self._gcp_count_changed())
            except AttributeError:
                pass
        self._gcp_fill(st)
        return st

    #: the exit move's words, per collector (the Export card's own)
    _GCP_EXIT_WORDS = {
        False: ("Run nozzle off the plate when done",
                "One last travel past the nearest plate edge, routed round "
                "the scaffold, so the polymer that keeps flowing after the "
                "last fiber lands off the part. Off: the print ends where "
                "its last fiber does."),
        True: ("Run nozzle off the end of the tube when done",
               "One last travel along the mandrel axis, past whichever end "
               "of the tube the print finishes nearer, so the polymer that "
               "keeps flowing after the last fiber misses the wall. Off: the "
               "print ends where its last fiber does."),
    }

    def _gcp_build_exit(self, st):
        """The print's one exit move, the first row of the Travel section
        (`_gcp_grid_exit` draws it on every fill): the App's own
        `s_exit_move` / `s_exit_margin` (`_build_export_tab`), so the
        Settings window's Export card, presets and sessions see the same
        values. The widgets hold copies, written to the App's vars on a
        click, a Return or leaving the field: a table write, so Done
        regenerates once."""
        mv = getattr(self, "s_exit_move", None)
        mg = getattr(self, "s_exit_margin", None)
        if not isinstance(mv, tk.Variable) or not isinstance(mg, tk.Variable):
            return
        on = st["exit_on"] = tk.BooleanVar(value=bool(mv.get()))
        dist = st["exit_dist"] = tk.StringVar(value=mg.get())
        st["exit_words"] = tk.StringVar(value=self._GCP_EXIT_WORDS[False][0])

        def follow(*_a):
            # a change made elsewhere (Settings, a preset, an Undo) shows
            try:
                if on.get() != bool(mv.get()):
                    on.set(bool(mv.get()))
                ent = st.get("exit_ent")
                typing = ent is not None and ent.winfo_exists()                     and ent.focus_get() is ent
                if not typing and dist.get() != mg.get():
                    dist.set(mg.get())
                if ent is not None and ent.winfo_exists():
                    ent.configure(state="normal" if mv.get() else "disabled")
            except (tk.TclError, KeyError):
                pass
        st["exit_follow"] = follow
        for x in (mv, mg):
            x.trace_add("write", follow)

    def _gcp_grid_exit(self, st, r, pal):
        """Draw the exit move at grid rows `r` and `r + 1`, shaped as the
        Perimeter travel pair below it: "Exit move" with one checkbox and
        "Exit distance" with one field, in the first column only, since
        they are the whole print's, not one scaffold's. Returns the rows
        used (0 when the App has no exit vars)."""
        if "exit_on" not in st:
            return 0
        made = []
        for i, (text, unit) in enumerate((("Exit move", ""),
                                          ("Exit distance", "mm"))):
            lab = tk.Label(st["lgrid"], text=text, background=_bg(),
                           foreground=pal["fg"], anchor="w", padx=0)
            lab.grid(row=r + i, column=0, sticky="w", padx=(0, 6))
            u = tk.Label(st["lgrid"], text=unit, font="MewHint",
                         background=_bg(), foreground=pal["hint"], anchor="w")
            u.grid(row=r + i, column=1, sticky="w", padx=(0, 8))
            u._gcp_kind = "unit"
            made.append(lab)
        chk = ttk.Checkbutton(st["grid"], variable=st["exit_on"],
                              command=lambda: self._gcp_exit_changed(st))
        chk.grid(row=r, column=st.get("col0", 0), sticky="w", padx=(3, 0),
                 pady=1)
        ent = ttk.Entry(st["grid"], textvariable=st["exit_dist"], width=8,
                        justify="right")
        ent.grid(row=r + 1, column=st.get("col0", 0), sticky="ew",
                 padx=(3, 0), pady=1)
        ent.bind("<FocusOut>", lambda _e: self._gcp_exit_changed(st), add="+")
        ent.bind("<Return>", lambda _e: self._gcp_exit_changed(st), add="+")
        ent.bind("<Escape>", self._gce_params_close)
        chk.bind("<Escape>", self._gce_params_close)
        st["exit_chk"], st["exit_ent"] = chk, ent
        st["exit_labs"] = made
        self._gcp_exit_words(st)
        st["exit_follow"]()
        return 2

    def _gcp_exit_words(self, st):
        chk = st.get("exit_chk")
        if chk is None:
            return
        words, tip = self._GCP_EXIT_WORDS[bool(st.get("tube"))]
        st["exit_words"].set(words)
        for w in (chk, st["exit_ent"], *st.get("exit_labs", ())):
            t = getattr(w, "_gcp_tip", None)
            if t is None:
                w._gcp_tip = Tooltip(w, tip)
            else:
                t.text = tip

    @_staged
    def _gcp_exit_changed(self, st):
        """The exit box or its distance was changed here: written to the
        App's vars and held until Done, as every other cell. A distance
        that is not a number >= 0 is refused with the designer's wording."""
        mv, mg = self.s_exit_move, self.s_exit_margin
        on, dist = bool(st["exit_on"].get()), st["exit_dist"].get().strip()
        if on != bool(mv.get()):
            mv.set(on)
            self._gcp_regen()
        if dist != mg.get().strip():
            _v, e = self._pos_float(dist or "5", "Exit distance",
                                    allow_zero=True)
            if e:
                st["err"].set(e)
                return
            mg.set(dist)
            st["err"].set("")
            if on:
                self._gcp_regen()

    def _gcp_wheel(self, st, e, axis):
        """The wheel over the box scrolls it (Shift: sideways); anywhere
        else it is not the table's."""
        try:
            if not st["shown"]:
                return None
            w = st["box"].winfo_containing(e.x_root, e.y_root)
            while w is not None and w is not st["box"]:
                w = w.master
            if w is None:
                return None
            n = -1 if (getattr(e, "num", 0) == 4
                       or getattr(e, "delta", 0) > 0) else 1
            (st["canvas"].xview_scroll if axis == "x"
             else st["outer"].yview_scroll)(n, "units")
        except (tk.TclError, KeyError, AttributeError):
            return None
        return "break"

    def _gcp_rows(self, st):
        if st.get("tube"):
            if st["mode"] == "scaffold":
                return st.get("rows") or TUBE_PARAM_ROWS
            return TUBE_LAYER_ROWS
        if st["mode"] == "scaffold":
            return st.get("rows") or PARAM_ROWS
        return LAYER_ROWS

    def _gcp_tube_rows(self, infos):
        """The tube rows some column reads (the Tube card leaves out a row
        its pattern and closure do not read; so does this table)."""
        return tuple(r for r in TUBE_PARAM_ROWS
                     if any(i["applies"].get(r[2]) for i in infos))

    # ------------------------------------------------------------------ #
    # 4 axis: one scaffold's tube, resolved by the Tube code
    # ------------------------------------------------------------------ #
    def _gcp_tube_info(self, c):
        """Everything the 4-axis table shows for scaffold `c` that is not a
        plain var: which rows its tube reads and which it lets be typed,
        what the software resolved, the ladders as dropdown options, and a
        refusal. All of it the designer's Tube code's own answer, inside
        `_gce_tube_scope(c)`."""
        import math
        import gui_mandrel
        info = dict(applies={}, editable={}, out={}, opts={}, error=None)
        with self._gce_tube_scope(c):
            fam = self._mandrel_family_value()
            mode = self._mandrel_design_mode()
            typed = self._mandrel_closure_typed()
            zig = fam == "zigzag"
            cells = not zig
            closes = zig and mode in ("pore", "angle")
            A = info["applies"]
            A.update(family=True, length=True, diameter=True, layers=True,
                     step=True, speed=True, place=True, place_offset=True,
                     start_out=True, r_length=True,
                     cell_graph=fam == gui_mandrel.MANDREL_DRAWN_FAMILY,
                     winding_angle=zig,
                     pore=zig and mode in ("turn", "pore"),
                     cell_width=cells, cell_height=cells,
                     rib_angle=fam == "reentrant",
                     amplitude=fam in gui_mandrel.MANDREL_WAVE_FAMILIES,
                     design_mode=zig, closure_override=zig and mode == "angle",
                     eta=closes, rm=closes, length_tol=closes,
                     eta_cap=zig and mode == "pore",
                     helix_pick=zig and not typed, cell_fit=cells,
                     cell_pick=cells, r_angle=zig, r_pore=zig,
                     r_turn=zig and mode == "turn", r_cells=cells,
                     turn_out_on=zig and mode == "turn",
                     turn_out=zig and mode == "turn")
            E = info["editable"]
            E.update({k: True for k in A})
            E.update(winding_angle=mode != "pore", eta=typed, rm=typed,
                     length_tol=not typed,
                     place_offset=self._mandrel_place() != "centre",
                     # the offset of a perimeter travel that is off does
                     # nothing, so it is not offered
                     turn_out=bool(self.m_turn_out_on.get()))
            gap = None
            try:
                d = self._mandrel_resolved_design()
            except gui_mandrel._LengthGap as exc:
                d, gap, info["error"] = None, exc, str(exc)
            except Exception as exc:  # noqa: BLE001 -- shown under the table
                d, info["error"] = None, str(exc)
            O = info["out"]
            miss = self._mandrel_miss_c
            if d is not None and zig:
                # `_mandrel_miss_c` brings its own leading space
                O["r_angle"] = "%.2f" % d.omega_deg + (
                    "" if mode == "pore" else miss(d.omega_deg,
                                                   self.m_angle.get()))
                side = 1000.0 * math.sqrt(d.pore_area)
                O["r_pore"] = "%.0f" % side + (
                    miss(side, self.m_pore_target.get())
                    if mode in ("turn", "pore") else "")
                O["r_length"] = "%.3f%s" % (d.length,
                                            miss(d.length, self.m_length.get()))
                if self._mandrel_is_turned(d):
                    O["r_turn"] = "%.1f (%.2f mm of fiber)" % (
                        d.end_turn_deg,
                        math.radians(d.end_turn_deg) * d.radius)
            elif d is not None:
                O["r_cells"] = "%d around × %d along, %.2f × %.2f mm" % (
                    d.n_around, d.n_axial, d.cell_w, d.cell_h)
                O["r_length"] = "%.3f%s" % (d.length,
                                            miss(d.length, self.m_length.get()))
            elif gap is not None:
                O["r_length"] = "does not close"
            opts = info["opts"]
            if gap is not None:
                # the nearest lengths that close, as the Tube card offers them
                pick = [("Use %.3f mm (pore %.0f µm)"
                         % (g.length, 1000.0 * math.sqrt(g.pore_area)),
                         ("length", g.length))
                        for g in (gap.shorter, gap.longer) if g is not None]
                opts["helix_pick"] = (pick, "")
            elif d is not None and zig and not typed:
                opts["helix_pick"] = self._gcp_helix_opts(mode, d)
            if d is not None and cells:
                opts["cell_pick"] = self._gcp_cell_opts()
        return info

    def _gcp_helix_opts(self, mode, design):
        """The helix ladder as ([(label, action)], shown): the solver's own
        pick first, as Auto; the rest pin a rung (`_mandrel_use_alternative`
        writes the same key)."""
        import math
        tag = {"pore": "_m_pore", "angle": "_m_angle", "turn": "_m_turn"}[mode]
        alts = getattr(self, tag + "_alternatives", None) or []
        best = getattr(self, tag + "_solver_pick", None)
        if best is None:
            return ([], "")
        out, seen = [], set()
        shown = ""
        now = self._helix_pick_key(design)
        pinned = bool((self._mandrel_pick_var().get() or "").strip())
        for k, alt in enumerate([best] + list(alts)):
            key = self._helix_pick_key(alt)
            if key in seen:
                continue
            seen.add(key)
            side = 1000.0 * math.sqrt(alt.pore_area)
            if mode == "turn":
                desc = "%.0f µm, turn %.1f°" % (side, alt.end_turn_deg)
            elif mode == "pore":
                desc = "%.0f µm, %.1f°, %.3f mm" % (side, alt.omega_deg,
                                                    alt.length)
            else:
                desc = "%.0f µm, %.3f mm" % (side, alt.length)
            label = ("Auto: " + desc) if k == 0 else desc
            out.append((label, ("auto",) if k == 0 else ("pin", key)))
            if (k == 0 and not pinned) or (pinned and key == now and k):
                shown = label
        return (out, shown or (out[0][0] if out else ""))

    def _gcp_cell_opts(self):
        """The cell-count ladder as ([(label, action)], shown); Auto is the
        rounding (`_mandrel_use_cell_alternative` writes the same pin)."""
        out = [("Auto", ("auto",))]
        pin = (self.m_cell_pick.get() or "").strip()
        shown = "Auto"
        for key, cells, refusal in self._mandrel_cell_ladder():
            if refusal is not None:
                continue
            label = "%s around × %s along, %s, %s mm" % (
                cells[1], cells[2], dict(_CHOICES["cell_fit"])[cells[0]],
                cells[3].split("(")[0].strip())
            out.append((label, ("cell", key)))
            if key == pin:
                shown = label
        return (out, shown)

    def _gcp_info(self, st, c):
        """Scaffold `c`'s tube info as the last refresh worked it out."""
        infos = st.get("info") or []
        if c < len(infos) and infos[c] is not None:
            return infos[c]
        return self._gcp_tube_info(c)

    def _gcp_fill(self, st):
        """(Re)build the grid: scaffolds or layers as the columns."""
        self._gcp_untrace(st)
        for g in {id(st["grid"]): st["grid"], id(st["lgrid"]): st["lgrid"]}.values():
            for w in g.winfo_children():
                w.destroy()
        st["cells"], st["heads"], st["badges"] = {}, [], {}
        lay = st["mode"] == "layer"
        tube = st["tube"] = self._gcp_tube()
        st["info"], st["rows"] = None, None
        if lay:
            s = st["s"]
            n = self._gcl_layers(s)
            st["title"].set(f"Layer parameters · Scaffold {s + 1}")
            st["pick_cb"].configure(values=[
                f"Scaffold {i + 1}" for i in range(self._gce_scaffold_count())])
            st["pick"].set(f"Scaffold {s + 1}")
        else:
            n = self._gcp_columns()
            st["title"].set("Parameters")
            if tube:
                # the rows are the ones some column's tube reads
                st["info"] = [self._gcp_tube_info(c) for c in range(n)]
                st["rows"] = self._gcp_tube_rows(st["info"])
            else:
                st["rows"] = self._gcp_plate_rows(n)
        st["hint"].set(_HINTS[(st["mode"], tube)])
        st.pop("exit_chk", None)
        st.pop("exit_ent", None)
        st["n"] = n
        if st["hi"] is not None and st["hi"] >= n:
            st["hi"] = None
        pal = gui_widgets.palette()
        grid, lgrid = st["grid"], st["lgrid"]
        for g in (grid, lgrid, st["body"], st["outer"], st["canvas"]):
            g.configure(background=_bg())
        # cell columns: scaffold c sits at 2c with its badge at 1 + 2c;
        # layer c at c
        col0, step = (0, 1) if lay else (0, 2)
        st["col0"], st["step"] = col0, step
        for c in range(n):
            lab = tk.Label(grid, text=(f"Layer {c + 1}" if lay
                                       else f"Scaffold {c + 1}"),
                           font="MewHint", background=_bg(),
                           foreground=pal["fg"], padx=6, cursor="hand2")
            lab.grid(row=0, column=col0 + step * c, sticky="ew",
                     pady=(0, 2))
            lab.bind("<Button-1>", lambda _e, cc=c: self._gcp_set_hi(st, cc))
            st["heads"].append(lab)
        # the label grid's header row, so the two grids' rows line up
        tk.Label(lgrid, text=" ", font="MewHint", background=_bg()
                 ).grid(row=0, column=0, pady=(0, 2))
        r = 1
        group = None
        rowmap = []
        # the whole print's exit move is the Travel section's first row
        exit_due = not lay

        def header(grp):
            g = tk.Label(lgrid, text=gui_widgets._track(grp),
                         font="MewHint", background=_bg(),
                         foreground=pal["hint"], anchor="w")
            g.grid(row=r, column=0, columnspan=2, sticky="w",
                   pady=(6 if r > 1 else 0, 1))
            g._gcp_kind = "group"
            rowmap.append(r)

        for gi, (grp, label, key, unit, kind) in enumerate(self._gcp_rows(st)):
            if grp != group:
                group = grp
                header(grp)
                r += 1
                if grp == "Travel" and exit_due:
                    used = self._gcp_grid_exit(st, r, pal)
                    exit_due = not used
                    for _i in range(used):
                        rowmap.append(r)
                        r += 1
            lab = tk.Label(lgrid, text=label, background=_bg(),
                           foreground=pal["fg"], anchor="w", padx=0)
            lab.grid(row=r, column=0, sticky="w", padx=(0, 6))
            tip = (None if st["tube"] else (
                self._gcp_start_tip(key) or _PLATE_TIPS.get(key))) \
                or _TIPS.get(key)
            if tip:
                add_tip(lab, tip)
            if key == "speed":
                u = gui_widgets.FitCombobox(
                    lgrid, textvariable=st["speed_units"],
                    values=(gui_units.shown("speed", "mm/min"), "× CTS"),
                    state="readonly")
                u.bind("<<ComboboxSelected>>", lambda _e: self._gcp_refresh(st))
                st["speed_combo"] = u
            else:
                # a tube's "diameter" is a length, left in mm
                if key in _DISPLAY_UNITS and unit \
                        and not (key == "diameter" and st["tube"]):
                    unit = gui_units.shown(_DISPLAY_UNITS[key][0], unit)
                u = tk.Label(lgrid, text=unit, font="MewHint", background=_bg(),
                             foreground=pal["hint"], anchor="w")
            u.grid(row=r, column=1, sticky="w", padx=(0, 8))
            u._gcp_kind = "unit"
            for c in range(n):
                cell = self._gcp_make_cell(st, gi, c, key, kind)
                cell["w"].grid(row=r, column=col0 + step * c, sticky="ew",
                               padx=(3, 0 if not lay else 3), pady=1)
                st["cells"][(gi, c)] = cell
                if not lay and key in (_TUBE_LAYER_KEY_OF if tube
                                       else _LAYER_KEY_OF):
                    b = tk.Label(grid, text="", font="MewHint",
                                 background=_bg(), foreground=pal["st_info"],
                                 width=3, anchor="w", cursor="hand2")
                    b.grid(row=r, column=1 + 2 * c, sticky="w")
                    b.bind("<Button-1>", lambda _e, cl=cell:
                           self._gcp_badge_click(cl))
                    b._gcp_kind = "badge"
                    b._gcp_tip = Tooltip(b, "")
                    st["badges"][(gi, c)] = b
            rowmap.append(r)
            r += 1
        if exit_due and "exit_on" in st:
            # no column shows a Travel row (a tube whose pattern reads no
            # perimeter travel): the section is the exit move alone
            header("Travel")
            r += 1
            for _i in range(self._gcp_grid_exit(st, r, pal)):
                rowmap.append(r)
                r += 1
        st["rows_total"] = r
        self._gcl_align_rows(st)
        gui_revert.skip(st["box"])
        self._gcp_traces(st)
        self._gcp_highlight(st)
        self._gcp_refresh(st)
        self._gcp_place_soon()

    def _gcl_align_rows(self, st):
        """The label grid and the scrolling cell grid are two grids; give
        each row one height in both so the rows line up."""
        def need(g, r):
            h = 0
            for w in g.grid_slaves(row=r):
                pad = [int(float(p)) for p in re.findall(
                    r"[\d.]+", str(w.grid_info().get("pady", 0)))] or [0]
                h = max(h, w.winfo_reqheight() + pad[0] + pad[-1])
            return h
        for r in range(st["rows_total"]):
            h = max(need(st["grid"], r), need(st["lgrid"], r))
            st["grid"].rowconfigure(r, minsize=h)
            st["lgrid"].rowconfigure(r, minsize=h)

    def _gcp_make_cell(self, st, gi, c, key, kind):
        grid = st["grid"]
        lay = st["mode"] == "layer"
        cell = dict(st=st, gi=gi, c=c, key=key, kind=kind, bad=False)
        if kind == "bool":
            var = tk.BooleanVar(value=False)
            w = ttk.Checkbutton(grid, variable=var,
                                command=lambda cl=cell: self._gcp_commit(cl))
            w.bind("<Escape>", self._gce_params_close)
        elif kind == "rotation":
            var = tk.StringVar(value="")
            # every listed cycle shows whole; a typed one has 15 characters
            w = gui_widgets.FitCombobox(grid, textvariable=var,
                                        values=ROTATION_PRESETS, width=15)
            w.bind("<<ComboboxSelected>>",
                   lambda _e, cl=cell: self._gcp_commit(cl), add="+")
        elif kind == "family":
            import gui_mandrel
            var = tk.StringVar(value="")
            w = gui_widgets.FitCombobox(grid, textvariable=var,
                                        state="readonly", values=[
                                            lbl for _v, lbl in
                                            gui_mandrel.MANDREL_FAMILY_CHOICES])
            w.bind("<<ComboboxSelected>>",
                   lambda _e, cl=cell: self._gcp_commit(cl), add="+")
            # hovering a pattern in the list shows its unit cell
            import gui_cell_hover
            gui_cell_hover.attach_combobox(
                w, gui_mandrel.MANDREL_FAMILY_LBL2VAL,
                graph_of=lambda cc=c: self._gce_m_fgraph_of(cc))
        elif kind in ("choice", "pick"):
            # a closed list: the Tube card's radio rows and its ladders
            var = tk.StringVar(value="")
            # a ladder's rungs arrive later (`_gcp_pull`); the fit
            # follows them
            w = gui_widgets.FitCombobox(grid, textvariable=var,
                                        state="readonly",
                                        values=[lbl for _t, lbl in
                                                _CHOICES.get(key, ())])
            w.bind("<<ComboboxSelected>>",
                   lambda _e, cl=cell: self._gcp_commit(cl), add="+")
        elif kind == "grade_dir":
            # a closed list, and any angle typed in degrees
            var = tk.StringVar(value="")
            w = gui_widgets.FitCombobox(grid, textvariable=var,
                                        values=[lbl for lbl, _a in GRADE_DIRS])
            w.bind("<<ComboboxSelected>>",
                   lambda _e, cl=cell: self._gcp_commit(cl), add="+")
        elif kind == "sketch":
            var = tk.StringVar(value="")
            k = _px(grid)
            w = tk.Canvas(grid, width=int(_SKETCH_W * k),
                          height=int(_SKETCH_H * k),
                          highlightthickness=0, background=_bg())
            add_tip(w, _TIPS["grade_sketch"])
            cell["var"], cell["w"] = var, w
            return cell
        elif kind == "out":
            # what the software built: read, never typed
            var = tk.StringVar(value="")
            w = ttk.Label(grid, textvariable=var, anchor="w")
        elif kind == "cell_edit":
            var = tk.StringVar(value="")
            w = ttk.Button(grid, text="Edit…", command=lambda cc=c: (
                getattr(self, "_gce_m_cell_open", lambda _c: None)(cc)))
            add_tip(w, _TIPS["cell_graph"])
        else:
            var = tk.StringVar(value="")
            w = ttk.Entry(grid, textvariable=var, width=8 if lay else 9,
                          justify="right")
        cell["var"], cell["w"] = var, w
        if kind in ("out", "cell_edit"):
            w.bind("<Button-3>", lambda e, cl=cell: self._gcp_menu(e, cl))
            return cell
        if kind != "bool":
            w.bind("<Return>", lambda _e, cl=cell: self._gcp_key(cl, 1))
            w.bind("<KP_Enter>", lambda _e, cl=cell: self._gcp_key(cl, 1))
            w.bind("<Down>", lambda _e, cl=cell: self._gcp_key(cl, 1))
            w.bind("<Up>", lambda _e, cl=cell: self._gcp_key(cl, -1))
            w.bind("<FocusOut>", lambda _e, cl=cell: self._gcp_commit(cl),
                   add="+")
            w.bind("<Escape>", lambda _e, cl=cell: self._gcp_cell_esc(cl))
            w.bind("<Key>", lambda e, cl=cell: self._gcp_keyed(cl, e),
                   add="+")
        w.bind("<FocusIn>", lambda _e, cl=cell: self._gcp_on_focus(cl),
               add="+")
        w.bind("<Button-3>", lambda e, cl=cell: self._gcp_menu(e, cl))
        return cell

    def _gcp_traces(self, st):
        """Follow the designer: a change made elsewhere (its page, a preset,
        an undo) shows in the table, unless a cell is mid-edit."""
        if st["tube"] and st["mode"] == "scaffold":
            # any value of a tube can change what another row shows or
            # allows (the family hides the size rows, the closure the
            # ladder), so every one of every column refreshes the table --
            # and so does the one shared thing a tube resolves against, the
            # mandrel itself
            srcs = [x for c in range(st["n"])
                    for x in self.gce_scaf_vars[c].values()
                    if isinstance(x, tk.Variable)]
            for name in ("m_diameter",):
                x = getattr(self, name, None)
                if isinstance(x, tk.Variable):
                    srcs.append(x)
        elif st["mode"] == "scaffold":
            for cell in st["cells"].values():
                keys = (cell["key"],)
                for k in keys:
                    src = self.gce_scaf_vars[cell["c"]].get(k)
                    if isinstance(src, tk.Variable):
                        tid = src.trace_add(
                            "write", lambda *_a, cl=cell: self._gcp_pull(cl))
                        st["traces"].append((src, tid))
            srcs = [self.gce_scaf_vars[c]["layer_overrides"]
                    for c in range(st["n"])]
            # the grade rows (and whether they show at all) and the
            # Preview follow every value they are worked out from
            srcs += [x for c in range(st["n"]) for k in _GRADE_SOURCES
                     for x in (self.gce_scaf_vars[c].get(k),)
                     if isinstance(x, tk.Variable)]
        elif st["mode"] == "layer":
            v = self.gce_scaf_vars[st["s"]]
            srcs = [x for x in v.values() if isinstance(x, tk.Variable)]
        for x in (getattr(self, "cal_vars", {}).get("cal.cts_ref"),
                  getattr(self, "m_cts", None), getattr(self, "s_collector", None),
                  getattr(self, "no_layer_z", None)):
            if isinstance(x, tk.Variable):
                srcs.append(x)
        for src in srcs:
            tid = src.trace_add("write",
                                lambda *_a: self._gcp_refresh_soon(st))
            st["traces"].append((src, tid))

    def _gcp_untrace(self, st):
        for var, tid in st["traces"]:
            try:
                var.trace_remove("write", tid)
            except (tk.TclError, ValueError):
                pass
        st["traces"] = []

    def _gcp_focus(self, st, gi, c):
        cell = st["cells"].get((gi, c))
        if cell is not None:
            try:
                cell["w"].focus_set()
            except tk.TclError:
                pass
        self._gcl_see(st, c)

    # ------------------------------------------------------------------ #
    # placement over the picture
    # ------------------------------------------------------------------ #
    def _gcp_place_soon(self):
        for name in ("_gcp", "_gcl"):
            st = getattr(self, name, None)
            if st is not None and st["shown"] and st["place_after"] is None:
                st["place_after"] = self.root.after_idle(self._gcp_place)

    def _gcp_place(self):
        """Float the shown table right of the tree, top-aligned with it, as
        Fusion floats a command's dialog; clamped to the picture."""
        for name in ("_gcp", "_gcl"):
            st = getattr(self, name, None)
            if st is None:
                continue
            st["place_after"] = None
            if st["shown"]:
                self._gcp_place_one(st)

    def _gcp_place_one(self, st):
        vp = self._gce_viewport
        try:
            W, H = vp.winfo_width(), vp.winfo_height()
        except tk.TclError:
            return
        m = getattr(self, "_GCE_OVERLAY_GAP", 10)
        br = getattr(self, "_gce_browser", None)
        left = m
        if br is not None and br.winfo_ismapped():
            left = br.winfo_width() + 2 * m
        box = st["box"]
        # the size the user dragged the box to, inside the picture
        sized = (self._gce_sized().get(("params", st["mode"]))
                 if hasattr(self, "_gce_sized") else None)
        if sized is not None:
            sized = (max(st["head"].winfo_reqwidth() + 24,
                         min(sized[0], W - 2 * m)),
                     max(120, min(sized[1], H - 2 * m)))
        # the columns get what the picture (or that size) has room for, and
        # scroll past it
        cv, grid = st["canvas"], st["grid"]
        grid.update_idletasks()
        gw, gh = grid.winfo_reqwidth(), grid.winfo_reqheight()
        room = max(160, W - left - m - 44 - st["lgrid"].winfo_reqwidth())
        if sized is not None:
            room = max(60, sized[0] - 44 - st["lgrid"].winfo_reqwidth())
        cw = min(gw, room)
        cv.configure(width=cw, height=gh, scrollregion=(0, 0, gw, gh))
        if gw > cw:
            st["xbar"].grid(row=1, column=1, sticky="ew")
        else:
            st["xbar"].grid_remove()
        # ...and the rows what it has room for, scrolling up and down
        outer, body = st["outer"], st["body"]
        body.update_idletasks()
        bw0, bh0 = body.winfo_reqwidth(), body.winfo_reqheight()
        outer.configure(width=bw0, height=bh0,
                        scrollregion=(0, 0, bw0, bh0))
        box.update_idletasks()
        over = box.winfo_reqheight() - (sized[1] if sized is not None
                                        else max(120, H - 2 * m))
        if over > 0:
            outer.configure(height=max(60, bh0 - over))
            st["ybar"].grid(row=2, column=1, sticky="ns")
        else:
            st["ybar"].grid_remove()
        box.update_idletasks()
        bw, bh = box.winfo_reqwidth(), box.winfo_reqheight()
        if sized is not None:
            bw, bh = sized
        x = max(m, min(left, W - bw - m))
        y = m
        if hasattr(self, "_gce_drag_spot"):
            x, y = self._gce_drag_spot("params:" + st["mode"], x, y, bw, bh,
                                       W, H)
        box.place(in_=vp, x=x, y=y, width=min(bw, max(200, W - x - m)),
                  height=min(bh, max(120, H - y - m)))
        box.lift()

    def _gcl_see(self, st, c):
        """Scroll the table so column `c` is in view."""
        if not st["n"]:
            return
        cv = st["canvas"]
        try:
            cv.update_idletasks()
            head = st["heads"][c]
            x0, w = head.winfo_x(), head.winfo_width()
            total = max(1, st["grid"].winfo_reqwidth())
            vis0, vis1 = cv.xview()
            if x0 / total < vis0 or (x0 + w) / total > vis1:
                cv.xview_moveto(max(0.0, (x0 - 20) / total))
        except (tk.TclError, IndexError):
            pass

    # ------------------------------------------------------------------ #
    # what a cell shows, and whether it may be edited
    # ------------------------------------------------------------------ #
    def _gcl_layers(self, s):
        try:
            return max(1, int(float(self.gce_scaf_vars[s]["layers"].get())))
        except (ValueError, tk.TclError):
            return 1

    def _gcl_inherit(self, s, key, layer, tube=None):
        """The value a layer prints with when it does not differ: the
        scaffold's, with its own per-layer angle list honoured. A tube's
        is that scaffold's tube's own (`_gcl_tube_inherit`). `tube`: the
        table's own kind, when a table asks; else the collector's."""
        if self._gcp_tube() if tube is None else tube:
            return self._gcl_tube_inherit(s, key)
        v = self.gce_scaf_vars[s]
        pattern_on = bool(v["angle_pattern_on"].get())
        if key == "angle":
            if pattern_on:
                angles, _e = self._parse_angle_pattern(v["angle_pattern"].get())
                if angles:
                    return f"{angles[layer % len(angles)]:g}"
            try:
                a = (float(v["start_angle"].get())
                     + layer * self._rotation_value(v["rotation"]))
                # one direction: 810 deg and 90 deg lay the same fibers
                return f"{a % 180.0:g}"
            except (ValueError, IndexError, KeyError, AttributeError,
                    tk.TclError):
                return ""
        if key == "stepover" and pattern_on:
            angles, _e = self._parse_angle_pattern(v["angle_pattern"].get())
            steps, _e = self._parse_layer_value_list(
                v["angle_stepover_pattern"].get())
            if angles and steps:
                k = layer % len(angles)
                if k < len(steps) and steps[k] is not None:
                    return f"{steps[k]:g}"
        if key == "link_loops":
            return bool(v["link_loops_on"].get())
        if key == "loop_speed" and not v["loop_speed"].get().strip():
            return self._gcl_override(s, layer, "speed") or v["speed"].get()
        return str(v[_VAR_OF_LAYER.get(key, key)].get())

    def _gcl_tube_inherit(self, s, key):
        """What a layer of scaffold `s`'s tube prints with when it does not
        differ. The family is the engine token; a blank Layer step is the
        fiber diameter."""
        v = self.gce_scaf_vars[s]
        if key == "family":
            with self._gce_tube_scope(s):
                return self._mandrel_family_value()
        if key == "step":
            return (v["m_layer_step"].get().strip()
                    or str(v["diameter"].get()))
        return str(self._gcp_tube_var(key, s).get())

    def _gcl_override(self, s, layer, key):
        return self._gce_layer_overrides(s).get(layer, {}).get(key)

    def _gcp_units(self, cell):
        """(quantity, held unit, shown unit) when this cell shows its value
        in a display unit (the fiber diameter, µm by default, and speeds),
        else None. A speed typed as × CTS is a ratio."""
        q = _DISPLAY_UNITS.get(cell["key"])
        if q is None or cell["kind"] == "out":
            return None
        if cell["key"] == "diameter" and cell["st"]["tube"]:
            return None             # a tube's diameter is a length, in mm
        if (cell["key"] == "speed"
                and cell["st"]["speed_units"].get() == "× CTS"):
            return None
        return q[0], q[1], gui_units.shown(q[0], q[1])

    def _gcp_current(self, cell):
        value = self._gcp_current_raw(cell)
        conv = self._gcp_units(cell)
        if conv is not None:
            return gui_units.convert_text(value, conv[0], conv[1], conv[2])
        if cell["key"] != "speed" or cell["st"]["speed_units"].get() != "× CTS":
            return value
        v = self.gce_scaf_vars[self._gcp_scaffold_of(cell)]
        try:
            cts, _source = v["speed"]._cts_current()
            return "%.8g" % (float(value) / (60.0 * cts)) if cts else ""
        except (ValueError, TypeError, AttributeError):
            return ""

    @staticmethod
    def _gcp_store_key(cell):
        """The setting a cell reads and writes (a layer override's key)."""
        return cell["key"]

    # ------------------------------------------------------------------ #
    # Grade the spacing: a view over stepover_end / grade_angle (across
    # the layer) and stepover_top (up through the layers)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _gcp_grade_mode(v):
        """None (even), "layers", or the in-layer direction in degrees."""
        try:
            if str(v["stepover_top"].get()).strip():
                return "layers"
            if str(v["stepover_end"].get()).strip():
                return float(v["grade_angle"].get() or 0.0)
        except (KeyError, ValueError, tk.TclError):
            pass
        return None

    @staticmethod
    def _gcp_dir_label(angle):
        for lbl, a in GRADE_DIRS:
            if a is not None and abs((angle - a) % 360.0) < 1e-9:
                return lbl
        return "%g°" % angle

    @staticmethod
    def _gcp_dir_parse(text):
        """A Grade choice as typed or picked: its angle, "layers", or None
        when it is neither."""
        t = str(text).strip()
        for lbl, a in GRADE_DIRS:
            if t.lower() == lbl.lower():
                return "layers" if a is None else a
        try:
            return float(t.rstrip("°").replace("deg", "").strip())
        except ValueError:
            return None

    def _gcp_grade_read(self, cell):
        """A grade row's value, for column `cell['c']`."""
        v = self.gce_scaf_vars[cell["c"]]
        mode = self._gcp_grade_mode(v)
        key = cell["key"]
        if key == "grade_on":
            return mode is not None
        if mode is None:
            return ""
        if key == "grade_dir":
            return (_LAYERS_DIR if mode == "layers"
                    else self._gcp_dir_label(mode))
        if key == "grade_end":
            return str(v["stepover_top" if mode == "layers"
                         else "stepover_end"].get()).strip()
        if key == "grade_rate":
            return self._gcp_grade_rate(v, mode)
        return ""

    def _gcp_grade_rate(self, v, mode):
        """"+0.05 mm per mm" across the layer, "+0.026 mm per layer" up."""
        try:
            s0 = float(v["stepover"].get())
            s1 = float(v["stepover_top" if mode == "layers"
                         else "stepover_end"].get())
            if mode == "layers":
                n = max(1, int(float(v["layers"].get())))
                if n < 2:
                    return "one layer: no change"
                return "%+.3g mm per layer" % ((s1 - s0) / (n - 1))
            span = self._gcp_grade_span(v, mode)
            if span <= 0:
                return ""
            return "%+.3g mm per mm" % ((s1 - s0) / span)
        except (KeyError, ValueError, ZeroDivisionError, tk.TclError):
            return ""

    def _gcp_footprint(self, v):
        """Scaffold v's footprint polygon, origin-centred, or None for the
        plain rectangle -- the outline the generator clips to (the spec
        `_build_scaffold_spec` writes)."""
        import scaffold_gcode as sg
        try:
            w, h = float(v["width"].get()), float(v["height"].get())
            shape = str(v["shape"].get()).strip() or "rectangle"
            if shape == "rectangle":
                return None
            sides = int(float(v["shape_sides"].get() or 64))
            poly = None
            if shape == "custom":
                pairs, _e = self._parse_linear_pattern(
                    v["shape_polygon"].get())
                if not pairs:
                    return None
                poly = [(a, b) for a, b in pairs]
            if shape in ("circle", "polygon"):
                h = w
            return sg.footprint_outline(shape, w, h, sides, poly)
        except Exception:                               # noqa: BLE001
            return None

    def _gcp_grade_span(self, v, angle):
        """How far the footprint reaches along the grade direction, mm."""
        import math
        w, h = float(v["width"].get()), float(v["height"].get())
        verts = self._gcp_footprint(v) or ((-w / 2, -h / 2), (w / 2, -h / 2),
                                           (w / 2, h / 2), (-w / 2, h / 2))
        g = math.radians(angle)
        us = [x * math.cos(g) + y * math.sin(g) for x, y in verts]
        return max(us) - min(us)

    def _gcp_grade_write(self, cell, value):
        """Write a grade row into column c's design vars; True if changed."""
        v = self.gce_scaf_vars[cell["c"]]
        before = tuple(str(v[k].get()) for k in (
            "stepover_end", "stepover_top", "grade_angle", "layer_overrides"))
        mode = self._gcp_grade_mode(v)
        step = str(v["stepover"].get()).strip()
        key = cell["key"]
        if key == "grade_on":
            if bool(value) == (mode is not None):
                return False
            if value:
                # even until an end is typed: an end equal to the spacing
                # is laid as the even layer
                v["stepover_end"].set(step)
            else:
                v["stepover_end"].set("")
                if mode == "layers":
                    v["stepover_top"].set("")
                    clear_layer_grade(v)
        elif key == "grade_dir":
            new = self._gcp_dir_parse(value)
            if new is None or mode is None:
                return False
            end = self._gcp_grade_read(dict(cell, key="grade_end")) or step
            if new == "layers":
                v["stepover_end"].set("")
                v["stepover_top"].set(end)
            else:
                if mode == "layers":
                    v["stepover_top"].set("")
                    clear_layer_grade(v)
                v["grade_angle"].set("%g" % new)
                v["stepover_end"].set(end)
        elif key == "grade_end":
            if mode is None:
                return False
            v["stepover_top" if mode == "layers"
              else "stepover_end"].set(str(value).strip())
        return before != tuple(str(v[k].get()) for k in (
            "stepover_end", "stepover_top", "grade_angle", "layer_overrides"))

    def _gcp_layer_angles(self, c, n, most=4):
        """The distinct fiber directions scaffold c's first layers print
        (deg, 0..180), in order, at most `most`."""
        out = []
        for i in range(min(n, 64)):
            a = self._gcl_override(c, i, "angle")
            if a is None:
                a = self._gcl_inherit(c, "angle", i)
            try:
                a = round(float(a) % 180.0, 6)
            except (TypeError, ValueError):
                continue
            if a not in out:
                out.append(a)
            if len(out) >= most:
                break
        return out or [0.0]

    def _gcp_draw_sketch(self, cell):
        """The grade Preview for column c: one small top view per layer
        direction, each the passes `scaffold_gcode._layer_passes` lays (so
        a layer whose fibers run along the grade shows even); for Up
        through the layers, a side view with one row of fibers per layer."""
        import scaffold_gcode as sg
        cv = cell["w"]
        cv.delete("all")
        pal = gui_widgets.palette()
        ink, edge, hint = pal["select_bg"], pal["hairline"], pal["hint"]
        cv.configure(background=_bg())
        k = _px(cv)
        W, H = _SKETCH_W * k, _SKETCH_H * k
        v = self.gce_scaf_vars[cell["c"]]
        mode = self._gcp_grade_mode(v)
        if mode is None:
            return
        try:
            s0 = float(v["stepover"].get())
            s1 = float(v["stepover_top" if mode == "layers"
                         else "stepover_end"].get())
            n = max(1, int(float(v["layers"].get())))
            w, h = float(v["width"].get()), float(v["height"].get())
        except (KeyError, ValueError, tk.TclError):
            return
        if s0 <= 0 or s1 <= 0 or w <= 0 or h <= 0:
            return
        font = "MewHint"
        cap = 14 * k
        if mode == "layers":
            # a side view: one row of fiber ends per layer, bottom layer at
            # the bottom, over the same few mm of the part
            ov = self._gce_layer_overrides(cell["c"])
            rows = min(n, 12)
            span = 6.0 * max(s0, s1)            # mm of the part shown
            sc = (W - 8 * k) / span
            top, bot = 6 * k, H - cap - 6 * k
            rad = max(1.5, 1.8 * k)
            for r in range(rows):
                i = round(r * (n - 1) / max(1, rows - 1)) if rows > 1 else 0
                try:
                    s = float(ov.get(i, {}).get("stepover", s0))
                except (TypeError, ValueError):
                    s = s0
                y = (bot - (bot - top) * r / (rows - 1) if rows > 1
                     else (top + bot) / 2)
                x = 4 * k
                while x <= W - 4 * k:
                    cv.create_oval(x - rad, y - rad, x + rad, y + rad,
                                   fill=ink, outline="")
                    x += s * sc
            cv.create_text(W / 2, H - cap / 2, fill=hint, font=font,
                           text="layer 1: %g mm, layer %d: %g mm"
                                % (s0, n, s1))
            return
        poly = self._gcp_footprint(v)
        if poly is not None and str(v["shape"].get()) == "custom":
            xs, ys = [p[0] for p in poly], [p[1] for p in poly]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
        angles = self._gcp_layer_angles(cell["c"], n)
        cols = 1 if len(angles) == 1 else 2
        rws = (len(angles) + cols - 1) // cols
        gap = 6 * k
        pw = (W - gap * (cols - 1)) / cols
        ph = (H - cap - gap * (rws - 1)) / rws - cap
        side = max(8.0, min(pw, ph))
        sc = side / max(w, h)
        verts = poly or ((-w / 2, -h / 2), (w / 2, -h / 2),
                         (w / 2, h / 2), (-w / 2, h / 2))
        # the panels and their captions, centred in the canvas
        used = rws * (side + cap) + gap * (rws - 1) + cap
        y0 = max(0.0, (H - used) / 2)
        for j, a in enumerate(angles):
            ox = (j % cols) * (pw + gap) + (pw - side) / 2
            oy = y0 + (j // cols) * (side + cap + gap)
            cx, cy = ox + side / 2, oy + side / 2
            pts = [(cx + x * sc, cy - y * sc) for x, y in verts]
            cv.create_polygon(*[q for p in pts for q in p], fill="",
                              outline=edge)
            try:
                passes = sg._layer_passes(a, w, h, s0, polygon=poly,
                                          stepover_end=s1, grade_angle=mode)
            except Exception:                           # noqa: BLE001
                passes = []
            for (ax, ay), (bx, by) in passes[:600]:
                cv.create_line(cx + ax * sc, cy - ay * sc,
                               cx + bx * sc, cy - by * sc, fill=ink)
            cv.create_text(cx, oy + side + cap / 2, fill=hint, font=font,
                           text="%g°" % a)
        cv.create_text(W / 2, y0 + used - cap / 2, fill=hint, font=font,
                       text="%g mm to %g mm" % (s0, s1))

    def _gcp_plate_rows(self, n):
        """The plate's scaffold rows: the grade rows only once some column
        grades its spacing."""
        on = any(self._gcp_grade_mode(self.gce_scaf_vars[c]) is not None
                 for c in range(n))
        return tuple(r for r in PARAM_ROWS
                     if on or r[2] not in _GRADE_ROWS)

    def _gcp_current_raw(self, cell):
        """The value the cell stands for, as text (or bool)."""
        st = cell["st"]
        if cell["key"] in _GRADE_KEYS and st["mode"] == "scaffold" \
                and not st["tube"]:
            return self._gcp_grade_read(cell)
        if cell["kind"] == "family":
            import gui_mandrel
            tok = self._gcp_family(cell)
            return gui_mandrel.MANDREL_FAMILY_VAL2LBL.get(tok, tok)
        if st["mode"] == "layer":
            o = self._gcl_override(st["s"], cell["c"], cell["key"])
            if o is None:
                o = self._gcl_inherit(st["s"], cell["key"], cell["c"],
                                      tube=st["tube"])
            if cell["kind"] == "bool":
                return bool(o)
            if cell["kind"] == "choice":
                return self._gcp_choice_shown(cell["key"], o)
            return str(o)
        if st["tube"]:
            kind, key = cell["kind"], cell["key"]
            if kind == "out":
                return self._gcp_info(st, cell["c"])["out"].get(key, "")
            if kind == "pick":
                return self._gcp_info(st, cell["c"])["opts"].get(
                    key, ([], ""))[1]
            if kind == "cell_edit":
                return ""
            val = self._gcp_tube_var(key, cell["c"]).get()
            if kind == "bool":
                return bool(val)
            if kind == "choice":
                return self._gcp_choice_shown(key, val)
            return str(val)
        v = self.gce_scaf_vars[cell["c"]]
        if cell["kind"] == "rotation":
            combo = v.get("rotation")
            return combo.get() if combo is not None else ""
        if cell["kind"] == "bool":
            return bool(v[cell["key"]].get())
        return str(v[cell["key"]].get())

    @staticmethod
    def _gcp_choice_shown(key, val):
        """A choice row's value as its cell shows it; a value that is not
        one of its choices shows as it is (the validator names it)."""
        text = str(val).strip()
        return dict(_CHOICES[key]).get(text, text)

    def _gcp_editable(self, cell):
        """Whether this field can be edited right now. The page's design
        has no widgets of its own, so the designer's per-design locks are
        read off the page's vars (an angle cycle replaces the rotation and
        Start angle; a round or drawn footprint drives its size); an
        app-wide lock (the diameter From Physics) is the designer's
        widget's. A tube row its column's pattern does not read is greyed
        (`_gcp_tube_editable`)."""
        st = cell["st"]
        if st["tube"]:
            return self._gcp_tube_editable(cell)
        s = st["s"] if st["mode"] == "layer" else cell["c"]
        v = self.gce_scaf_vars[s]
        key = cell["key"]
        if cell["kind"] == "rotation" and v.get("rotation") is None:
            return False
        if st["mode"] == "layer" and key in ("angle", "link_loops"):
            return True
        if st["mode"] == "layer" and key == "start_z":
            # "Don't adjust height per layer" (Machine page) writes one Z
            # per scaffold and drops every other: a layer's own height
            # would show in the picture and never reach the file
            flat = getattr(self, "no_layer_z", None)
            try:
                if flat is not None and flat.get():
                    return False
            except tk.TclError:
                pass
        if st["mode"] == "scaffold" and key in _GRADE_KEYS:
            return key == "grade_on" or (
                key != "grade_rate"
                and self._gcp_grade_mode(v) is not None)
        if st["mode"] == "layer" and key == "stepover" \
                and self._gcp_grade_mode(v) == "layers":
            return False        # Up through the layers sets every layer's
        if key == "grade_angle":
            end = (self._gcl_override(s, cell["c"], "stepover_end")
                   if st["mode"] == "layer" else None)
            try:
                return bool(end is not None
                            or str(v["stepover_end"].get()).strip())
            except (KeyError, tk.TclError):
                return False
        try:
            if key == "perimeter_travel_offset":
                # no perimeter travel is written while it is off, so its
                # distance would change nothing
                return bool(v["perimeter_travel"].get())
            shape = v["shape"].get()
            if key in ("rotation", "start_angle"):
                return not v["angle_pattern_on"].get()
            if key == "height":
                return shape not in ("circle", "polygon", "custom")
            if key == "width":
                return shape != "custom"
        except (KeyError, tk.TclError):
            pass
        # the page can hold more scaffolds than the designer has built
        # widgets for: a sixth scaffold raised IndexError here and left
        # the table half drawn
        if not 0 <= s < len(self.scaf_vars):
            return True
        w = self.scaf_vars[s].get(_ENTRY_OF.get(key, ""))
        if not hasattr(w, "cget"):
            return True
        if w is None:
            return True
        try:
            return "disabled" not in str(w.cget("state"))
        except tk.TclError:
            return True

    def _gcp_tube_editable(self, cell):
        """On the scaffold table, what that column's Tube code reads and
        lets be typed (`_gcp_tube_info`): a winding angle only on the
        diamond helix and not while the pore solver writes it, eta and Rm
        only when set by hand, and so on. On a layer: a winding row only on
        a winding layer, a cell size only on a tiled one, no step below
        layer 1. The fiber diameter is the designer's widget's lock."""
        import gui_mandrel
        st, key = cell["st"], cell["key"]
        lay = st["mode"] == "layer"
        if not lay:
            info = self._gcp_info(st, cell["c"])
            if not (info["applies"].get(key) and info["editable"].get(key)):
                return False
            if cell["kind"] == "pick":
                return bool(info["opts"].get(key, ([], ""))[0])
        else:
            fam = self._gcp_family(cell)
            if key in _WINDING_ONLY:
                return fam == "zigzag"
            if key in _CELL_ONLY:
                return fam in gui_mandrel.MANDREL_CELL_FAMILIES
            if key in _RIB_ONLY:
                return fam == "reentrant"
            if key in _WAVE_ONLY:
                return fam in gui_mandrel.MANDREL_WAVE_FAMILIES
            if key == "step":
                return cell["c"] > 0
            if key == "place_offset":
                # centred, the layer's offset is worked out, as the tube's
                place = self._gcl_override(st["s"], cell["c"], "place")
                if place is None:
                    place = self._gcl_tube_inherit(st["s"], "place")
                return place != "centre"
        if key == "diameter":
            w = self.scaf_vars[0].get("diameter_entry")
            try:
                return w is None or "disabled" not in str(w.cget("state"))
            except (tk.TclError, AttributeError):
                return True
        return True

    def _gcp_pull(self, cell):
        """Show the current value in the cell, unless it is being typed in."""
        w = cell["w"]
        if cell["kind"] == "sketch":
            try:
                if w.winfo_exists():
                    self._gcp_draw_sketch(cell)
            except tk.TclError:
                pass
            return
        try:
            if not w.winfo_exists():
                return
            if (cell["kind"] != "bool" and w.focus_get() is w
                    and str(cell["var"].get()) != str(self._gcp_current(cell))):
                return
        except (tk.TclError, KeyError):
            pass
        if cell["kind"] == "pick":
            # a ladder's rungs are that column's own, and move with it
            opts = self._gcp_info(cell["st"], cell["c"])["opts"].get(
                cell["key"], ([], ""))[0]
            w.configure(values=[lbl for lbl, _a in opts])
        cell["var"].set(self._gcp_current(cell))
        cell["bad"] = False
        try:
            w.state(["!disabled"] if self._gcp_editable(cell)
                    else ["disabled"])
            if cell["kind"] not in ("out", "cell_edit"):
                w.state(["!invalid"])
        except tk.TclError:
            pass
        if cell["st"]["mode"] == "layer" and cell["kind"] not in (
                "bool", "family"):
            own = self._gcl_override(cell["st"]["s"], cell["c"],
                                     self._gcp_store_key(cell)) is not None
            base = ("TCombobox" if isinstance(w, ttk.Combobox)
                    else "TEntry")
            w.configure(style=base if own else "GcpInherit." + base)

    def _gcp_refresh_soon(self, st):
        if st["refresh_after"] is None:
            st["refresh_after"] = self.root.after_idle(
                lambda: self._gcp_refresh(st))

    def _gcp_refresh(self, st):
        st["refresh_after"] = None
        if not st["box"].winfo_exists():
            return
        if st.get("tube") != self._gcp_tube():
            # built for the other collector: its cells name the other
            # kind's fields (a tube layer table, refreshed by a switch to
            # the plate, read a plate scaffold's "winding_angle" and
            # raised). Opening refills it; one on screen is refilled now.
            if st["shown"]:
                if st["mode"] == "layer":
                    self._gce_layer_params_open(st["s"], st["hi"])
                else:
                    self._gce_open_scaffold_view(st["hi"])
            return
        # the speed unit follows a Settings change made since the table
        # was built (× CTS is a mode, not a unit, and stays)
        speed = gui_units.shown("speed", "mm/min")
        if st["speed_units"].get() not in ("× CTS", speed):
            st["speed_units"].set(speed)
        combo = st.get("speed_combo")
        try:
            if combo is not None and combo.winfo_exists():
                combo.configure(values=(speed, "× CTS"))
        except tk.TclError:
            pass
        if st["mode"] == "layer" and st["n"] != self._gcl_layers(st["s"]):
            self._gcp_fill(st)
            return
        if not st["tube"] and st["mode"] == "scaffold" \
                and self._gcp_plate_rows(st["n"]) != st["rows"]:
            # a grade turned on or off: its rows come or go
            self._gcp_fill(st)
            return
        if st["tube"] and st["mode"] == "scaffold":
            # every column resolved again: a change in one row moves what
            # another shows (the Resolved rows, the ladders) or which rows
            # there are at all
            infos = [self._gcp_tube_info(c) for c in range(st["n"])]
            if self._gcp_tube_rows(infos) != st["rows"]:
                self._gcp_fill(st)
                return
            st["info"] = infos
            n = st["n"]
            st["note"].set("\n".join(
                (f"Scaffold {c + 1}: " if n > 1 else "") + i["error"]
                for c, i in enumerate(infos) if i["error"]))
            self._gcp_paint_err(st)
        else:
            st["note"].set("")
        if st["speed_units"].get() == "× CTS" and st["n"]:
            s = st["s"] if st["mode"] == "layer" else (st["hi"] or 0)
            try:
                cts, source = self.gce_scaf_vars[s]["speed"]._cts_current()
                st["speed_note"].set(
                    "Using %s CTS %.5g mm/min." % (source, cts * 60.0)
                    if cts else "Enter a positive CTS on the Physics page.")
            except (AttributeError, IndexError, TypeError):
                st["speed_note"].set("Enter a positive CTS on the Physics page.")
        else:
            st["speed_note"].set("")
        if st["mode"] == "layer":       # only the layer table's cells use it
            _inherit_style()
        for cell in st["cells"].values():
            self._gcp_pull(cell)
        if st["mode"] == "scaffold":
            self._gcp_badges(st)

    # ------------------------------------------------------------------ #
    # the scaffold table's layer counts and list
    # ------------------------------------------------------------------ #
    def _gcp_badges(self, st):
        """`·3` beside each scaffold value three layers override, and the
        list of every per-layer change under the table."""
        import gui_mandrel
        tube = st["tube"]
        rows = []
        per = {}
        for c in range(st["n"]):
            ov = self._gce_layer_overrides(c)
            for layer in sorted(ov):
                for k, val in ov[layer].items():
                    per.setdefault((c, k), []).append(layer)
                    rows.append((c, layer, k, val))
        key_of = _TUBE_LAYER_KEY_OF if tube else _LAYER_KEY_OF
        for (gi, c), b in st["badges"].items():
            k = key_of.get(self._gcp_rows(st)[gi][2])
            n = len(per.get((c, k), ()))
            b.configure(text=f"·{n}" if n else "")
            b._gcp_tip.text = (f"{n} layer{'s' if n != 1 else ''} of "
                               f"scaffold {c + 1} differ. Click to see "
                               f"them.") if n else ""
        box = st["list"]
        for w in box.winfo_children():
            w.destroy()
        if not rows:
            box.grid_remove()       # no empty strip at the box's foot
            self._gcp_place_soon()
            return
        box.grid()
        ttk.Label(box, text="Per-layer changes", style="Hint.TLabel").grid(
            row=0, column=0, sticky="w")
        label_of = {r[2]: r[1] for r in (TUBE_LAYER_ROWS if tube
                                         else LAYER_ROWS)}
        for i, (c, layer, k, val) in enumerate(rows[:_LIST_MAX]):
            shown = ("on" if val else "off") if isinstance(val, bool) else val
            if k == "family":
                shown = gui_mandrel.MANDREL_FAMILY_VAL2LBL.get(val, val)
            lab = ttk.Label(box, cursor="hand2",
                            text=f"Scaffold {c + 1} · Layer {layer + 1} · "
                                 f"{label_of.get(k, k)} {shown}")
            lab.grid(row=1 + i, column=0, sticky="w")
            lab.bind("<Button-1>", lambda _e, cc=c, ll=layer, kk=k:
                     self._gce_layer_params_open(cc, ll, kk))
        if len(rows) > _LIST_MAX:
            ttk.Label(box, style="Hint.TLabel",
                      text=f"and {len(rows) - _LIST_MAX} more").grid(
                row=1 + _LIST_MAX, column=0, sticky="w")
        self._gcp_place_soon()

    def _gcp_badge_click(self, cell):
        k = (_TUBE_LAYER_KEY_OF if cell["st"]["tube"]
             else _LAYER_KEY_OF).get(cell["key"])
        stored = self._gcp_store_key(dict(cell, key=k))
        ov = self._gce_layer_overrides(cell["c"])
        first = next((l for l in sorted(ov) if stored in ov[l]), None)
        if first is not None:
            self._gce_layer_params_open(cell["c"], first, k)

    # ------------------------------------------------------------------ #
    # checking and writing a value
    # ------------------------------------------------------------------ #
    def _gcp_check(self, cell, raw):
        """The designer's own check for this field: (value_to_write, error).
        Wording and rules are `_validate_one_scaffold`'s."""
        st = cell["st"]
        label = self._gcp_label(cell)
        who = f"Scaffold {self._gcp_scaffold_of(cell) + 1}"
        if st["mode"] == "layer":
            name = f"{who}, layer {cell['c'] + 1}: {label}"
        else:
            name = f"{who}: {label}"
        kind = cell["kind"]
        s = str(raw).strip()
        if kind == "pos":
            _, e = self._pos_float(s, name)
            if e is None and cell["key"] == "speed" and st["speed_units"].get() == "× CTS":
                v = self.gce_scaf_vars[self._gcp_scaffold_of(cell)]
                try:
                    cts, _source = v["speed"]._cts_current()
                except AttributeError:
                    cts = None
                if not cts:
                    e = "Enter a positive CTS on the Physics page."
        elif kind == "blank_end":
            e = self._pos_float(s, name)[1] if s else None
        elif kind == "grade_dir":
            e = (None if self._gcp_dir_parse(s) is not None else
                 f"{name}: pick a direction from the list or type an angle "
                 f"in degrees.")
        elif kind == "any":
            _, e = self._any_float(s, name)
        elif kind == "nonneg":
            _, e = self._pos_float(s, name + " (x spacing)", allow_zero=True)
        elif kind in ("layers", "copies"):
            _, e = self._int_at_least(s, name, 1)
        elif kind == "nonneg_mm":
            _, e = self._pos_float(s, name, allow_zero=True)
        elif kind == "segments":
            _, e = self._int_at_least(s, name, 2)
        elif kind == "blank_nonneg":
            e = (self._pos_float(s, name + " (x spacing)",
                                 allow_zero=True)[1] if s else None)
        elif kind == "blank_pos":
            e = self._pos_float(s, name)[1] if s else None
        elif kind == "rib":
            x, e = self._pos_float(s, name)
            if e is None and x >= 90.0:
                e = f"{name} must be less than 90."
        elif kind == "blank_int":
            e = self._int_at_least(s, name, 1)[1] if s else None
        elif kind == "nonneg_int":
            _, e = self._int_at_least(s, name, 0)
        elif kind == "rotation":
            try:
                float(s.split()[0])
                e = None
            except (ValueError, IndexError):
                e = f"{name} must be a number (e.g. 90, 60, 36)."
        else:
            e = None
        return s, e

    def _gcp_start_tip(self, key):
        """Start X/Y/Z's tips on the plate: what point of the scaffold they
        place, and from where. Start X/Y are the footprint's lower-left
        corner (`scaffold_gcode`'s `_layer_frame` centres the footprint at
        Start + half the size), measured from g-code zero
        (`_scaffold_origin_xy`): Start X 40 writes X40 in the file. A zero
        change rewrites them (`gui_buildplate._zero_moved`)."""
        if key == "start_z":
            return ("Height of the first layer above the plate top. 0 prints "
                    "on the plate; a scaffold stacked on another starts where "
                    "that one ends. A single layer's own Start Z is greyed "
                    "while the Machine page's \"Don't adjust height per "
                    "layer\" is on: the file then names one height per "
                    "scaffold.")
        if key not in ("start_x", "start_y"):
            return None
        axis = key[-1].upper()
        return ("%s of the lower-left corner of the scaffold's footprint "
                "(its outline's box, before Footprint angle turns it), "
                "measured from g-code zero (Setup's Collector card), so it "
                "is the number the file says. Changing zero rewrites it, so "
                "the scaffold stays where it is. The turnaround loops "
                "reach a little past it. New scaffold > Beside the last one "
                "places a scaffold for you." % axis)

    def _gcp_label(self, cell):
        return self._gcp_rows(cell["st"])[cell["gi"]][1]

    @staticmethod
    def _gcp_same(a, b):
        if isinstance(a, bool) or isinstance(b, bool):
            return bool(a) == bool(b)
        if str(a).strip() == str(b).strip():
            return True
        try:
            return float(a) == float(b)
        except (TypeError, ValueError):
            return False

    @_staged
    def _gcp_write(self, cell, value, held=False):
        """Write one value into the designer; True if it changed. `value` is
        as the cell shows it (its display unit) unless `held`."""
        st = cell["st"]
        if cell["key"] in _GRADE_KEYS and st["mode"] == "scaffold" \
                and not st["tube"]:
            return self._gcp_grade_write(cell, value)
        if cell["kind"] == "blank_end" and not str(value).strip():
            # blank: the layer follows the scaffold's far edge again
            value = None if st["mode"] == "layer" else ""
        conv = None if held else self._gcp_units(cell)
        if conv is not None:
            value = gui_units.convert_text(value, conv[0], conv[2], conv[1],
                                           store=True)
        if cell["key"] == "speed":
            v = self.gce_scaf_vars[self._gcp_scaffold_of(cell)]
            ratio_mode = st["speed_units"].get() == "× CTS"
            if ratio_mode:
                cts, _source = v["speed"]._cts_current()
                if not cts:
                    return False
                if st["mode"] == "layer":
                    return self._gcl_set_speed_ratio(st["s"], cell["c"], value, cts)
                before = (v["speed"].get(), v["speed_unit"].get())
                v["speed_unit"].set("× CTS")
                v["speed_ratio"].set(str(value))
                return before != (v["speed"].get(), v["speed_unit"].get())
            if st["mode"] != "layer":
                v["speed_unit"].set("Standard units")
        if cell["kind"] == "family":
            import gui_mandrel
            value = gui_mandrel.MANDREL_FAMILY_LBL2VAL.get(value, value)
        if st["mode"] == "layer":
            if cell["kind"] == "choice":
                # a layer holds what the scaffold's own var does
                value = {lbl: t for t, lbl in _CHOICES[cell["key"]]}.get(
                    value, value)
            return self._gcl_set(st["s"], cell["c"], cell["key"], value)
        if st["tube"]:
            return self._gcp_tube_write(cell, value)
        v = self.gce_scaf_vars[cell["c"]]
        if cell["kind"] == "rotation":
            combo = v.get("rotation")
            if combo is None or combo.get() == value:
                return False
            combo.set(value)
            return True
        if cell["kind"] == "bool":
            value = bool(value)
            if bool(v[cell["key"]].get()) == value:
                return False
            v[cell["key"]].set(value)
            tog = getattr(self, "_toggle_link_loops", None)
            if tog is not None and "link_loops_auto_rb" in v:
                try:
                    tog(v)
                except (tk.TclError, KeyError):
                    pass
            return True
        if str(v[cell["key"]].get()) == value:
            return False
        v[cell["key"]].set(value)
        return True

    def _gcp_tube_write(self, cell, value):
        """One value into scaffold c's tube, as the Tube card's own control
        for it writes it: a ladder rung is a pin (and a cell rung its fit
        too, `_mandrel_use_cell_alternative`), a gap's length the tube
        length (`_mandrel_use_length`), a fit change keeps the pinned count
        (`_mandrel_use_fit`), a family switch its backfill
        (`_mandrel_family_backfill`). Written into the page's vars directly:
        the designer's `_mandrel_use_*` also repaint the designer's panel,
        which is not this tube's."""
        c, key, kind = cell["c"], cell["key"], cell["kind"]
        v = self.gce_scaf_vars[c]

        def put(var, text):
            if var.get() == text:
                return False
            var.set(text)
            return True

        if kind == "pick":
            opts = dict(self._gcp_info(cell["st"], c)["opts"].get(
                key, ([], ""))[0])
            act = opts.get(value)
            if act is None:
                return False
            if act[0] == "length":
                return put(v["m_length"], "%.3f" % act[1])
            pin = self._gcp_tube_var(key, c)
            if act[0] == "auto":
                return put(pin, "")
            if act[0] == "cell":
                parts = act[1].split(",")
                changed = put(v["m_cell_fit"], parts[2]) if len(
                    parts) == 3 else False
                return put(pin, act[1]) or changed
            return put(pin, act[1])
        var = self._gcp_tube_var(key, c)
        if kind == "bool":
            value = bool(value)
            if bool(var.get()) == value:
                return False
            var.set(value)
            return True
        if kind == "choice":
            value = {lbl: t for t, lbl in _CHOICES[key]}.get(value, value)
        if not put(var, value):
            return False
        if key == "cell_fit":
            pick = (v["m_cell_pick"].get() or "").strip().split(",")
            if len(pick) == 3:
                put(v["m_cell_pick"], "%s,%s,%s" % (pick[0], pick[1], value))
        elif key == "amplitude":
            # typed: the chevron's backfill is theirs now, and stays
            with self._gce_tube_scope(c):
                self._m_amplitude_backfill = None
        elif key == "family":
            with self._gce_tube_scope(c):
                self._mandrel_family_backfill()
        return True

    @_staged
    def _gcl_set(self, s, layer, key, value):
        """Make `layer` of scaffold `s` differ in `key` -- or, `value` None
        or equal to the scaffold's, follow the scaffold again. True if the
        saved field changed."""
        ov = self._gce_layer_overrides(s)
        meta_changed = False
        if key == "speed":
            meta = self._gcl_speed_ratios(s)
            meta_changed = meta.pop(str(layer + 1), None) is not None
            self._gcl_speed_ratio_var(s).set(json.dumps(meta) if meta else "")
        row = dict(ov.get(layer, {}))
        if value is None or self._gcp_same(value,
                                           self._gcl_inherit(s, key, layer)):
            row.pop(key, None)
        else:
            row[key] = value
        if row:
            ov[layer] = row
        else:
            ov.pop(layer, None)
        text = (json.dumps({str(l + 1): ov[l] for l in sorted(ov)})
                if ov else "")
        var = self._gcl_ov_var(s)
        if var.get() == text:
            return meta_changed
        var.set(text)
        return True

    def _gcl_speed_ratio_var(self, s):
        key = "m_layer_speed_ratios" if self._gcp_tube() else "layer_speed_ratios"
        return self.gce_scaf_vars[s][key]

    def _gcl_speed_ratios(self, s):
        try:
            data = json.loads(self._gcl_speed_ratio_var(s).get() or "{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def _gcl_set_speed_ratio(self, s, layer, ratio, cts):
        ov = self._gce_layer_overrides(s)
        row = dict(ov.get(layer, {}))
        row["speed"] = "%.12g" % (float(ratio) * cts * 60.0)
        ov[layer] = row
        text = json.dumps({str(l + 1): ov[l] for l in sorted(ov)})
        meta = self._gcl_speed_ratios(s)
        meta[str(layer + 1)] = str(ratio)
        mtext = json.dumps(meta)
        var, mvar = self._gcl_ov_var(s), self._gcl_speed_ratio_var(s)
        changed = var.get() != text or mvar.get() != mtext
        var.set(text)
        mvar.set(mtext)
        return changed

    def _gcp_keyed(self, cell, e):
        """A key that edits the cell's text: the next commit is the user's
        typing, which makes a layer a grade's end even when it typed the
        value the layer already showed."""
        if (e.char and e.char.isprintable()) or e.keysym in (
                "BackSpace", "Delete"):
            cell["keyed"] = True

    def _gcp_note_typed(self, cell):
        st = cell["st"]
        if cell.pop("keyed", False) and st["mode"] == "layer":
            st["typed"].add((st["s"], cell["c"], self._gcp_store_key(cell)))

    def _gcp_commit(self, cell):
        """Validate the cell and, if it changed, write it (the preview waits for Done)."""
        st = cell["st"]
        if not self._gcp_editable(cell):
            return
        raw = cell["var"].get()
        if cell["kind"] == "bool":
            changed = self._gcp_write(cell, bool(raw))
        else:
            if str(raw) == str(self._gcp_current(cell)):
                if cell["bad"]:
                    self._gcp_mark(cell, False)
                self._gcp_note_typed(cell)
                return
            value, e = self._gcp_check(cell, raw)
            if e:
                self._gcp_mark(cell, True, e)
                return
            self._gcp_mark(cell, False)
            self._gcp_note_typed(cell)
            changed = self._gcp_write(cell, value)
        if changed:
            self._gcp_pull(cell)
            self._gcp_regen()

    def _gcp_mark(self, cell, bad, msg=""):
        st = cell["st"]
        cell["bad"] = bad
        try:
            cell["w"].state(["invalid"] if bad else ["!invalid"])
        except tk.TclError:
            pass
        if bad:
            st["err"].set(msg + " Press Esc in the cell to put it back.")
            self._gcp_paint_err(st)
        elif not any(c["bad"] for c in st["cells"].values()):
            st["err"].set("")

    def _gcp_commit_focused(self, st):
        try:
            f = st["box"].focus_get()
        except (tk.TclError, KeyError):
            return
        for cell in st["cells"].values():
            if cell["w"] is f and cell["kind"] != "bool":
                self._gcp_commit(cell)
                return

    def _gcp_cell_esc(self, cell):
        """Esc in a mid-edit cell puts the value back; Esc again closes."""
        if (cell["bad"] or str(cell["var"].get())
                != str(self._gcp_current(cell))):
            cell["var"].set(self._gcp_current(cell))
            self._gcp_mark(cell, False)
            cell.pop("keyed", None)
            return "break"
        return self._gce_params_close()

    def _gcp_key(self, cell, step):
        """Return / Down commit and move down the column; Up moves up."""
        self._gcp_commit(cell)
        if cell["bad"]:
            return "break"
        st = cell["st"]
        gi = cell["gi"] + step
        while 0 <= gi < len(self._gcp_rows(st)):
            nxt = st["cells"].get((gi, cell["c"]))
            if (nxt is not None
                    and nxt["kind"] not in ("bool", "out", "cell_edit")
                    and self._gcp_editable(nxt)):
                nxt["w"].focus_set()
                try:
                    nxt["w"].select_range(0, "end")
                    nxt["w"].icursor("end")
                except tk.TclError:
                    pass
                break
            gi += step
        return "break"

    def _gcp_on_focus(self, cell):
        self._gcp_set_hi(cell["st"], cell["c"])
        self._gcl_see(cell["st"], cell["c"])

    def _gcp_menu(self, e, cell):
        """Right-click a value: copy it across, or (a layer) follow the
        scaffold again; on a scaffold, open its layers."""
        st = cell["st"]
        menu = tk.Menu(cell["w"], tearoff=0)
        lab = self._gcp_label(cell)
        # a ladder's rungs are its own column's, a readout is no value
        edit = (self._gcp_editable(cell)
                and cell["kind"] not in ("pick", "out", "cell_edit"))
        if st["mode"] == "scaffold":
            n = st["n"]
            menu.add_command(label=f"Copy {lab} to all {n} scaffolds",
                             state="normal" if (n > 1 and edit)
                             else "disabled",
                             command=lambda: self._gcp_copy_row(cell))
            k = (_TUBE_LAYER_KEY_OF if st["tube"]
                 else _LAYER_KEY_OF).get(cell["key"])
            if k is not None:
                menu.add_command(
                    label=f"{lab} by layer…",
                    command=lambda: self._gce_layer_params_open(
                        cell["c"], None, k))
        else:
            own = self._gcl_override(st["s"], cell["c"],
                                     self._gcp_store_key(cell)) is not None
            menu.add_command(label="Use the scaffold's value",
                             state="normal" if own else "disabled",
                             command=lambda: self._gcl_clear(cell))
            menu.add_command(label=f"Copy {lab} to every layer",
                             state="normal" if edit else "disabled",
                             command=lambda: self._gcp_copy_row(cell))
            ends = self._gcl_grade_ends(cell)
            if cell["kind"] in _GRADABLE:
                menu.add_command(
                    label=(f"Grade {lab} from layer {ends[0] + 1} "
                           f"({ends[2]}) to layer {ends[1] + 1} ({ends[3]})"
                           if ends
                           else f"Grade {lab} (type the layers at both "
                                f"ends first)"),
                    state="normal" if (ends and edit) else "disabled",
                    command=lambda: self._gcl_grade(cell))
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _gcl_clear(self, cell):
        st = cell["st"]
        st["typed"].discard((st["s"], cell["c"], self._gcp_store_key(cell)))
        if self._gcl_set(st["s"], cell["c"], self._gcp_store_key(cell),
                         None):
            self._gcp_pull(cell)
            self._gcp_regen()

    def _gcl_typed(self, st, layer, key):
        """True when `layer` is an end a grade runs from or to: the user
        typed into it this session (even the scaffold's own value, which
        stores no override), or it holds a value of its own that Grade did
        not write."""
        if (st["s"], layer, key) in st["typed"]:
            return True
        own = self._gcl_override(st["s"], layer, key)
        if own is None:
            return False
        return st["graded"].get((st["s"], layer, key)) != own

    def _gcl_grade_ends(self, cell):
        """(lo, hi, shown lo value, shown hi value): the layers a grade
        through `cell` runs between, when there is one. The ends are the
        nearest typed layers at or below and above the clicked one, else
        the first and the last layer, so layers typed outside them (a
        sparse base) are kept. Clicked on the top end itself, the grade
        runs down to the typed layer below. Needs a layer between the ends
        and two numbers at them."""
        st = cell["st"]
        n = st["n"]
        if st["mode"] != "layer" or n < 3:
            return None
        key, c = self._gcp_store_key(cell), cell["c"]
        typed = [i for i in range(n) if self._gcl_typed(st, i, key)]
        below = [i for i in typed if i <= c]
        above = [i for i in typed if i > c]
        lo = below[-1] if below else 0
        hi = above[0] if above else n - 1
        if lo == c and not above and c > 0:
            lo, hi = ([i for i in typed if i < c] or [0])[-1], c
        if hi - lo < 2:
            return None
        first = st["cells"].get((cell["gi"], lo))
        last = st["cells"].get((cell["gi"], hi))
        if first is None or last is None:
            return None
        for end in (first, last):       # a value typed and not yet entered
            self._gcp_commit(end)
        a, b = self._gcp_current(first), self._gcp_current(last)
        try:
            float(a), float(b)
        except (TypeError, ValueError):
            return None
        return lo, hi, a, b

    def _gcl_grade(self, cell):
        """Fill the layers between the grade's two ends in a straight line:
        a pore size grading from one to the other, layer by layer. Each
        layer it writes is remembered as graded, so changing an end and
        grading again refills them rather than taking them for ends."""
        ends = self._gcl_grade_ends(cell)
        if ends is None:
            return
        st = cell["st"]
        lo, hi = ends[0], ends[1]
        a, b = float(ends[2]), float(ends[3])
        key = self._gcp_store_key(cell)
        changed = False
        for c in range(lo + 1, hi):
            other = st["cells"].get((cell["gi"], c))
            if other is None or not self._gcp_editable(other):
                continue
            value = "%.6g" % (a + (b - a) * (c - lo) / (hi - lo))
            _v, e = self._gcp_check(other, value)
            if e:
                self._gcp_mark(other, True, e)
                continue
            changed |= self._gcp_write(other, value)
            st["graded"][(st["s"], c, key)] = self._gcl_override(
                st["s"], c, key)
        self._gcp_refresh(st)
        if changed:
            self._gcp_regen()

    def _gcp_copy_row(self, cell):
        st = cell["st"]
        self._gcp_commit(cell)
        if cell["bad"]:
            return
        # a value in a display unit goes across as the var holds it, so a
        # round trip through mm/s cannot nudge the other columns
        held = self._gcp_units(cell) is not None
        value = (self._gcp_current_raw(cell) if held
                 else self._gcp_current(cell))
        changed = False
        for c in range(st["n"]):
            other = st["cells"].get((cell["gi"], c))
            if other is None or other is cell or not self._gcp_editable(other):
                continue
            changed |= self._gcp_write(other, value, held=held)
        self._gcp_refresh(st)
        if changed:
            self._gcp_regen()
        # say it: when the others already held the value, nothing in the
        # table moves, and a copy that shows nothing reads as one that failed
        if hasattr(self, "status"):
            lab = self._gcp_label(cell)
            shown = self._gcp_current(cell)
            unit = self._gcp_rows(st)[cell["gi"]][3]
            if isinstance(shown, bool):
                shown = "on" if shown else "off"
            elif unit and not held and not unit.startswith("×"):
                shown = "%s %s" % (shown, unit)
            where = ("every layer" if st["mode"] != "scaffold"
                     else "all %d scaffolds" % st["n"])
            self.status.set(
                "%s %s is now on %s%s." % (
                    lab, shown, where,
                    "" if changed else " (they already had it)"))

    # ------------------------------------------------------------------ #
    # highlight, undo, regenerate
    # ------------------------------------------------------------------ #
    def _gcp_set_hi(self, st, c):
        if st["hi"] != c:
            st["hi"] = c
            self._gcp_highlight(st)
            self._gcp_refresh_soon(st)
            # the lit column is the scaffold the design tools act on
            self._gcp_tools_follow()

    def _gcp_tools_follow(self):
        """Re-grey the ribbon's design tools: the Parameters table's lit
        column counts as the selected scaffold while the table shows."""
        refresh = getattr(self, "_gce_design_refresh", None)
        if refresh is not None:
            refresh()

    def _gcp_highlight(self, st):
        pal = gui_widgets.palette()
        hi = st["hi"]
        for c, lab in enumerate(st["heads"]):
            on = c == hi
            lab.configure(background=pal["hover"] if on else _bg(),
                          foreground=pal["st_info"] if on else pal["fg"],
                          font=("MewHint" if not on else "MewSection"))

    def _gcp_snapshot(self):
        """Every value both tables can change, for Undo changes."""
        snap = {}
        for c in range(len(self.gce_scaf_vars)):
            v = self.gce_scaf_vars[c]
            for _g, _l, key, _u, kind in PARAM_ROWS:
                if kind == "rotation":
                    combo = v.get("rotation")
                    if combo is not None:
                        snap[(c, key)] = combo.get()
                elif key in v:
                    snap[(c, key)] = v[key].get()
            # the grade rows are views of these
            for key in ("stepover_end", "grade_angle", "stepover_top"):
                if key in v:
                    snap[(c, key)] = v[key].get()
            snap[(c, "layer_overrides")] = v["layer_overrides"].get()
            for key in ("speed_unit", "speed_ratio", "layer_speed_ratios"):
                snap[(c, key)] = v[key].get()
            # its tube's own (`gui_gcode_twodesigns.TUBE_KEYS`)
            for name, x in v.items():
                if name.startswith("m_") and isinstance(x, tk.Variable):
                    snap[(c, name)] = x.get()
        # the print's own exit move, under the columns (`_gcp_build_exit`)
        for name in ("s_exit_move", "s_exit_margin"):
            x = getattr(self, name, None)
            if isinstance(x, tk.Variable):
                snap[(None, name)] = x.get()
        return snap

    @_staged
    def _gcp_undo_all(self, st):
        snap = getattr(self, "_gcp_session", None) or {}
        changed = False
        for (c, key), val in snap.items():
            if c is None:
                if getattr(self, key).get() != val:
                    getattr(self, key).set(val)
                    changed = True
                continue
            if c >= len(self.gce_scaf_vars):
                continue
            v = self.gce_scaf_vars[c]
            if key == "rotation":
                combo = v.get("rotation")
                if combo is not None and combo.get() != val:
                    combo.set(val)
                    changed = True
            elif v[key].get() != val:
                v[key].set(val)
                changed = True
                if key == "link_loops_on":
                    tog = getattr(self, "_toggle_link_loops", None)
                    if tog is not None and "link_loops_auto_rb" in v:
                        try:
                            tog(v)
                        except (tk.TclError, KeyError):
                            pass
        self._gcp_refresh(st)
        st["err"].set("")
        if changed:
            self._gcp_regen()

    def _gcp_regen(self):
        """A committed change: held until Done, which regenerates once."""
        self._gcp_dirty = True

    def _gcp_flush(self):
        """Done: the preview takes every change made since the table
        opened."""
        if not getattr(self, "_gcp_dirty", False):
            return
        self._gcp_dirty = False
        regen = getattr(self, "_gce_design_regen", None)
        if regen is not None:
            regen()

    def _gcp_count_changed(self):
        st = getattr(self, "_gcp", None)
        if st is None or not st["box"].winfo_exists():
            return
        if self._gcp_columns() != st["n"]:
            self._gcp_fill(st)

    def _gcp_paint_err(self, st):
        pal = gui_widgets.palette()
        for name in ("errl", "notel"):
            try:
                st[name].configure(foreground=pal["st_err"])
            except (tk.TclError, KeyError):
                pass


#: the grade Preview's size at 96 dpi (`_px` scales it to the display)
_SKETCH_W, _SKETCH_H = 178, 140


def _px(widget):
    """Pixels per 96-dpi pixel on this display."""
    try:
        return max(1.0, float(widget.winfo_fpixels("1i")) / 96.0)
    except (tk.TclError, ValueError):
        return 1.0


def _bg():
    """The floating box's own background (a plain ttk.Frame's), so the
    tk-drawn grid inside it does not show as a lighter card."""
    try:
        return ttk.Style().lookup("TFrame", "background") or \
            gui_widgets.palette()["bg"]
    except tk.TclError:
        return gui_widgets.palette()["bg"]


_inherit_for = None


def _inherit_style():
    """A layer cell that follows its scaffold: the entry, in the hint
    colour. Set only when the theme or palette changed: any
    `ttk.Style().configure` or `.map` restyles EVERY ttk widget in the
    app at the next idle, and every table refresh calls this (it made
    opening Parameters after New scaffold take most of a second)."""
    global _inherit_for
    pal = gui_widgets.palette()
    try:
        style = ttk.Style()
        key = (style.theme_use(), pal["hint"], pal["disabled"])
        if key == _inherit_for:
            return
        _inherit_for = key
        # the theme maps TEntry's foreground by state, and a map beats a
        # plain configure, so the grey is mapped too
        for name in ("GcpInherit.TEntry", "GcpInherit.TCombobox"):
            style.configure(name, foreground=pal["hint"])
            style.map(name, foreground=[("disabled", pal["disabled"]),
                                        ("!disabled", pal["hint"])])
    except tk.TclError:
        pass


class _Retheme:
    """Recolours the tables' plain-tk parts on the dark/light switch (the
    ttk parts follow the theme on their own)."""

    def __init__(self, app, st):
        self._app = app
        self._st = st

    def __hash__(self):
        return id(self)

    def retheme(self):
        st = self._st
        pal = gui_widgets.palette()
        if not st["box"].winfo_exists():
            return
        if st["mode"] == "layer":
            _inherit_style()
        for g in (st["grid"], st["lgrid"], st["body"]):
            g.configure(background=_bg())
            for w in g.winfo_children():
                if isinstance(w, tk.Label):
                    kind = getattr(w, "_gcp_kind", "")
                    w.configure(background=_bg(), foreground=(
                        pal["hint"] if kind in ("group", "unit")
                        else pal["st_info"] if kind == "badge"
                        else pal["fg"]))
        for name in ("canvas", "outer"):
            if st.get(name) is not None:
                st[name].configure(background=_bg())
        self._app._gcp_highlight(st)
        self._app._gcp_paint_err(st)
