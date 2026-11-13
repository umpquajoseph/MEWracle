#!/usr/bin/env python3
"""gui_buildplate.py -- Build-plate mixin: the "Build plate" setup tab (machine
bounds + the circular/rectangular plate the print sits on) plus the validators and
CLI-arg builders those fields feed.

A GLOBAL setup tab (one machine, one bed), shown FIRST in the scaffold notebook,
because the plate datum and the machine envelope frame everything a scaffold is
generated into. The rectangular plate IS the old print slide, generalised to a round
wafer and to an arbitrary centre.

Coordinate model:
  * Machine bounds size (Lx, Ly, Lz); home selectable min|max per axis. The envelope
    spans [0, L] when home=min, else [-L, 0]. The machine FLOOR is the envelope's
    min-Z face: z=0 (home-Z=min) or z=-Lz (home-Z=max).
  * The plate rests on the floor: bottom = floor_z, top = floor_z + thickness.
  * Every scaffold starts on the plate top: print Z = floor_z + thickness + the
    scaffold's own Z offset. plate_top_z is computed here and handed to the workers,
    so the floor formula lives in exactly one place.

The bounds are a viewer overlay and a sanity check (the converter warns when the
toolpath leaves the envelope); they do not otherwise alter the g-code.

Part of the gcode_to_fibers_gui split; see gui_common.py."""
from gui_common import *

import machine_model
import mew_gcode
import flavor_presets
import gui_fonts
import gui_form
import gui_tips
import json
import re
from datetime import datetime

# Kinematics presets the GUI offers, as (friendly label, machine_model key). The
# label is what the combobox shows + persists; _machine_args maps it to the key
# machine_model.CHAINS understands. "custom" reveals the per-part axes/parent block.
_KINEMATICS = [
    ("XY head + Z bed", "mewron"),
    ("XYZ head (static bed)", "xyz_head"),
    ("XY bed + Z head", "xy_bed_z_head"),
    ("Custom (set axes below)", "custom"),
]
_KINEMATICS_LABELS = [label for label, _key in _KINEMATICS]
#: Labels older builds showed and SAVED (presets, sessions, gui_defaults). The
#: lab's printer name was in the first one; a stranger's printer is not ours.
#: A var set to one is rewritten to today's label (`_normalise_kinematics`), and
#: the key lookup still answers it, so every old save loads unchanged.
_KINEMATICS_LEGACY = {"XY head + Z bed (MEWron)": "XY head + Z bed"}
_KINEMATICS_KEY = dict(_KINEMATICS)
_KINEMATICS_KEY.update({old: _KINEMATICS_KEY[new]
                        for old, new in _KINEMATICS_LEGACY.items()})


def _normalise_label(var, legacy):
    """Rewrite `var` to today's label when it holds an old one in `legacy`.
    A write trace, so a preset load, a session restore and a test's set() are
    all covered by the one place that knows the old names."""
    try:
        v = var.get()
    except Exception:  # noqa: BLE001 -- a var torn down with its window
        return
    new = legacy.get(v)
    if new is not None and new != v:
        var.set(new)


class _CurrentFlavorLabel:
    """`_normalise_label`'s map for a flavor box: an old built-in label or a
    label a saved flavor had before it was renamed -> the label it shows now
    (`flavor_presets.by_label`). A preset, session or design saved under the
    old name then loads showing the new one."""

    @staticmethod
    def get(label):
        fl = flavor_presets.by_label(label)
        return fl.label if fl is not None else None


CURRENT_FLAVOR_LABEL = _CurrentFlavorLabel()

# The firmware dialects the Machine page offers are the engine's built-ins
# (mew_gcode.FLAVORS) plus the lab's saved flavors (flavor_presets); the
# combobox shows + persists a flavor's LABEL and `flavor_presets.by_label`
# maps it back. The list is read each time it drops down.


#: the Machine page's feed time unit, shown -> the engine's "min" / "s"
def _zero_mm(value):
    """A Start X/Y a zero change rewrote, as a field shows it: no float noise
    (40 - 76.2 + 76.2 reads 40, not 40.00000000000001), no "-0"."""
    text = ("%.6f" % value).rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


FEED_TIME_LABELS = ("per minute", "per second")
_FEED_TIME_UNIT = {"per minute": "min", "per second": "s"}

# NO MOUNT CHOICE. A rotary stage is bolted to the body in the bed slot and
# moves exactly as the kinematics moves that body, so the page always writes
# RotaryAxis.role "bed" (see `_build_rotary_mount_fields`). machine_model
# still reads "frame" from a hand-written machine.json.
_ROTARY_ROLE = "bed"

# WHAT THE STAGE IS, once one is declared. A mandrel is ONE revolute joint and a
# five-axis collector table is TWO, and they are alternatives rather than options
# on one thing: `MachineProfile` takes at most two joints, `--mandrel` needs
# exactly one and `--surface` exactly two. Keeping this behind the existing
# "a stage is installed" tick is what preserves the 3-axis byte-identity that
# `_build_rotary_group`'s docstring promises -- unticked still emits nothing.
_ROTARY_KINDS = [
    ("Mandrel — one rotary (4-axis)", "mandrel"),
    ("Two-axis collector table (5-axis)", "pair"),
]
_ROTARY_KIND_LABELS = [label for label, _key in _ROTARY_KINDS]
_ROTARY_KIND_KEY = dict(_ROTARY_KINDS)

# The group's own heading follows the kind, because "(4-axis)" over a two-joint
# table is exactly the stale label this page has been bitten by before.
_ROTARY_GROUP_TITLE = {
    "mandrel": "Rotary collector (4-axis)",
    "pair": "Rotary collector: two-axis table (5-axis)",
}

# The axis a joint turns about, spelled out. A/B/C imply x/y/z on their own
# (`machine_model.ROTARY_WORDS`), but a five-axis table's words are the rig's --
# Terranova's are U and V, which ISO 6983 reserves for secondary LINEAR axes --
# so a pair names its axes explicitly and the emitter reads them off the joints.
_ROTARY_AXES = ("x", "y", "z")


def _home_is_consumed():
    """Whether this build's machine model actually READS the g-code home.

    INTERIM, and it exists so the page can never claim a home is in force when
    it is not. The writer (this page, `bed_calibrator`, the emitter) and the
    reader (`machine_model` + `gcode_player`) were built by two different
    workers in one wave; if the reader is not in the tree yet, the picked point
    is inert and `_sync_pair_home_note` says so instead of turning green.

    A BEHAVIOUR CHECK, not a version string or a `hasattr` on the class: `home`
    would be an instance attribute, so the only honest question is whether a
    joint built from a dict carrying one gives it back. Delete this function
    together with the rest of the interim home, once there is nothing to be
    unsure about."""
    try:
        j = machine_model.RotaryAxis.from_dict(
            {"word": "C", "axis": "z", "home": [1.0, 2.0, 3.0]})
        if j is None:
            return False
        got = getattr(j, "home", None)
        # THE VALUE, not the attribute. `home` defaults to a ZEROS array beside
        # a `home_set` flag -- the same shape `pivot` has -- so a reader that
        # drops the key still leaves a non-None `home` and an `is not None`
        # probe answers True for every build ever made. That is a probe which
        # cannot swing both ways, which is the one thing this function may not
        # be: comparing against the planted point is what makes it evidence.
        return got is not None and [float(v) for v in got] == [1.0, 2.0, 3.0]
    except Exception:                                 # noqa: BLE001
        return False

# WHERE A TUBE PRINT IS ZEROED, along the mandrel axis and radially. Two ladders
# rather than one ten-item list, for the same reason the plate's datum is a face plus
# a corner: they are independent choices and a user knows which one they are changing.
#
# Along the axis the datum is an END of the ROD, never of the print -- the rod is what
# you can touch the nozzle to. "near" is the -X end, "far" is +X for a rig whose
# access is the other way round, and "mid" is the rod's half-length.
#
# Radially there are exactly two points you can name on a round rod: its top SURFACE
# (the fiber lands there, and it is what g-code Z has always meant on this collector)
# and its AXIS, R0 below the surface. Y is not a ladder at all: a tube program emits
# no Y word, so laterally the datum IS the mandrel axis by construction.
_ROTARY_ORIGIN_AXIAL = [
    ("Mandrel near end (-X)", "near"),
    ("Mandrel far end (+X)", "far"),
    ("Mandrel mid-length", "mid"),
    # a point picked on the mandrel in 3-D (`rotary_origin_custom`): a slide
    # mark or a clip, anywhere along the rod
    ("Custom", "custom"),
]
_ROTARY_ORIGIN_AXIAL_LABELS = [label for label, _k in _ROTARY_ORIGIN_AXIAL]
_ROTARY_ORIGIN_AXIAL_KEY = dict(_ROTARY_ORIGIN_AXIAL)
# the words a save made before the collector was called a mandrel
# still name the same point, so an old preset or settings file loads unchanged
_ROTARY_ORIGIN_AXIAL_KEY.update({"Rod near end (-X)": "near",
                                 "Rod far end (+X)": "far",
                                 "Rod mid-length": "mid"})

_ROTARY_ORIGIN_RADIAL = [
    ("Mandrel surface (top)", "surface"),
    ("Mandrel axis (center)", "axis"),
]
_ROTARY_ORIGIN_RADIAL_LABELS = [label for label, _k in _ROTARY_ORIGIN_RADIAL]
_ROTARY_ORIGIN_RADIAL_KEY = dict(_ROTARY_ORIGIN_RADIAL)
_ROTARY_ORIGIN_RADIAL_KEY.update({"Rod surface (top)": "surface",
                                  "Rod axis (centre)": "axis",
                                  "Mandrel axis (centre)": "axis"})

#: What "G-code zero" means, said the same way on every page that shows it.
GCODE_ZERO_INFO = (
    "Where the nozzle is at X0 Y0 Z0 in your g-code. Zero your printer here "
    "before printing.\n\n"
    "This is not the position your printer goes to after homing.\n\n"
    "It is a point on the collector: a face and corner of the plate, or a "
    "point on the mandrel. The print rides the "
    "collector, so where the collector sits on the printer does not change "
    "the g-code.")


class _BuildPlateMixin:
    def _build_buildplate_tab(self, parent):
        """The CLASSIC Machine-and-Collector tab: the section builders below,
        stacked as labelled groups.

        The modern shell lays out the SAME section builders as module cards
        (`gui_pages._build_machine_grid`), one card per question -- collector,
        machine STLs, calibration, g-code origin -- so a field added to a
        section builder appears in both UIs. The variables are made first and
        separately (`_init_machine_vars`) because the cards are not built in
        this order."""
        pad = self.pad
        parent.columnconfigure(0, weight=1)
        self._init_machine_vars()

        bp = ttk.LabelFrame(parent, text="Plate / slide / wafer", padding=8)
        bp.grid(row=0, column=0, sticky="ew", **pad)
        self._build_plate_fields(bp, pad)

        og = ttk.LabelFrame(parent, text="G-code zero (0,0,0)", padding=8)
        og.grid(row=1, column=0, sticky="ew", **pad)
        self._build_origin_fields(og, pad)

        # --- printer model (3D simulation) --------------------------------- #
        self._build_machine_model_group(parent, 2, pad)

        # --- acceleration limits ------------------------------------------- #
        # The machine's acceleration limits are printer properties, so they live here
        # with the printer model. _SimMixin renders the linear/rotary accel and corner
        # speed fields into this row; the unoverridden hook leaves it empty and the
        # row collapses.
        self._build_machine_motion_limits(parent, 3, pad)

        # --- rotary (4-axis) collector -------------------------------------- #
        # Last, because it is the only group a 3-axis rig never opens.
        self._build_rotary_group(parent, 4, pad)

        self._toggle_plate_shape()

    def _init_machine_vars(self):
        """Create every Machine-page Tk variable, and no widgets. Idempotent, so
        either UI's layout can call it first."""
        if hasattr(self, "plate_shape"):
            return


        # default plate reproduces the old slide footprint: a 3in x 2in (76.2 x
        # 50.8 mm) rectangle, 1 mm thick. Its footprint is anchored at (0, 0) (for a
        # circle, the corner of its diameter-sized bounding square). Where it sits
        # ON THE PRINTER is the bed offset (`slide_offset_x/y`), and where g-code
        # zero is on it is the origin face/corner below.
        #
        # NO "Plate corner X/Y" and NO World origin. A corner would place the plate
        # in g-code space, which only means anything under a fixed World origin:
        # under the Collector origin it cancels out of the g-code entirely, and in
        # the simulation it merely adds to the bed offset -- two fields for one
        # placement. Zero is always set on the collector.
        # `gui_presets._migrate_plate_corner` folds an old preset's corner into its
        # bed offset.
        self.plate_shape = tk.StringVar(value="rectangular")
        self.plate_x = tk.StringVar(value="76.2")
        self.plate_y = tk.StringVar(value="50.8")
        self.plate_diameter = tk.StringVar(value="50")
        self.plate_thickness = tk.StringVar(value="1")
        self.show_plate = tk.BooleanVar(value=True)
        # optionally raise every exported Z by the printhead elevation, so the
        # g-code commands the nozzle at its working (standoff) height -- e.g. start
        # Z 0 + elevation 5 -> the print starts at Z5. Off by default.
        self.add_elevation_z = tk.BooleanVar(value=False)
        # THE EXPORTED SPELLING OF THE HEIGHT LADDER, and nothing else. Normally
        # layer i sits one fiber-diameter above layer i-1 (layer height == fiber
        # diameter) and every layer carries its own Z word. With this set the
        # USER-FACING g-code -- the g-code text view and Save-g-code-as -- keeps
        # the first Z word and drops the per-layer ones, so the collector is
        # never told to move and the machine owns the height.
        #
        # It is a REWRITE, not a geometry change: the scratch file the converter,
        # the .depo cache, the preview, the viewer and the player read still
        # carries the full ladder, because even at ONE commanded Z the fibers
        # still sag and stack, and that is the physical result worth predicting.
        # So it rides `gui_scaffold._rewrite_gcode_lines`
        # beside travel-as-G1 and the G00 padding -- that function is the one
        # place a user-facing spelling is allowed to differ from the solved file.
        # ON by default: a MEWron does not drop its collector per layer.
        #
        # It is a NEW persist key (`origin.no_layer_z`), not the renamed
        # `origin.flat_layer_z`, and deliberately: that key recorded a GEOMETRY
        # flag (it sent `--flat-layers`, which flattened the STL along with the
        # file), so a saved `false` is a value for a different control. Reusing
        # it would have let every existing session restore OFF and never see
        # this default at all. The old key is simply not in the persist map any
        # more; `_apply_preset` walks the map, so a preset still carrying it is
        # ignored rather than an error.
        self.no_layer_z = tk.BooleanVar(value=True)
        # ... and the same for the step BETWEEN scaffolds. Off by default, so a
        # stack of scaffolds still names each one's own start Z once. Only
        # meaningful while no_layer_z is on, which is also when it is shown.
        # The stabilization base is NOT a scaffold here: it inherits scaffold 1's
        # start Z, so the program's first Z word already covers it.
        self.no_scaffold_z = tk.BooleanVar(value=False)
        # both change only how the file spells height, so the G-code page
        # (and a calibration print on it) is spelled again when one flips
        for _cv in (self.no_layer_z, self.no_scaffold_z):
            _cv.trace_add("write", lambda *_a: getattr(
                self, "_refresh_gcode_display_on_toggle", lambda: None)())
        # which face (top/bottom) x XY corner-or-centre of the plate is g-code
        # (0,0,0). The defaults (Top + Bottom-left) reproduce the prior fixed datum.
        self.origin_face = tk.StringVar(value="Top")
        self.origin_anchor = tk.StringVar(value="Bottom-left")
        # ... or "Custom": a point picked on the plate in 3-D (`_calibrate_zero`),
        # one "z,x,y" triple in the plate's own frame -- X/Y from its
        # bottom-left corner, Z from its bottom face -- the picker's one
        # format. Blank = never picked. Only read while the Corner says Custom,
        # so a printer that never picks writes exactly what it always wrote.
        self.origin_custom = tk.StringVar(value="")
        self._zero_follow_install()
        # A user-typed g-code line spliced into the exported file after line N
        # (0 = the very top), for a rig command MEWracle has no field for (a
        # filament feeder's extrude, say). The rig's, not the job's: it rides
        # the machine preset beside no_layer_z, and one var shows on both the
        # Scaffold designer's and the Calibration page's Export cards. Empty =
        # off. User-facing only: see gui_scaffold `_insert_custom_gcode`.
        self.custom_gcode = tk.StringVar(value="")
        self.custom_gcode_after = tk.StringVar(value="0")
        # Off: a saved file keeps the app's header (what made it, with which
        # settings). Kept under its own preset key, `origin.strip_comments_on_save`,
        # since every printer saved before it was written True by default
        self.machine_strip_comments = tk.BooleanVar(value=False)
        # On: a saved design file says where each layer starts, with a
        # ``;LAYER:n`` comment (Cura's spelling), so a file that names its
        # height once still opens with its layers on another computer and
        # in other viewers (`mew_gcode.OutputOptions.layer_markers`).
        # Strip comments removes them with the rest.
        self.machine_layer_markers = tk.BooleanVar(value=True)
        self.machine_layer_markers.trace_add(
            "write", lambda *_a: getattr(
                self, "_refresh_gcode_display_on_toggle", lambda: None)())
        self.machine_gcode_start = tk.StringVar(value=mew_gcode.DEFAULT_START_BLOCK)
        self.machine_gcode_end = tk.StringVar(value="")
        # "Leave out the start and end blocks": a file with neither block,
        # for a rig that sets itself up by hand or a run spliced into another
        # program. Off (the default) writes them as always. The blocks stay
        # as typed; only the written file goes without (`_machine_blocks`).
        self.machine_omit_blocks = tk.BooleanVar(value=False)
        # What Save's pre-flight no longer asks about on THIS printer: a
        # comma list of "voltage", "pressure", "temperature", "end" -- the
        # things the file leaves to the printer that the researcher has
        # accepted (their HV supply is switched by hand, say). A printer
        # fact, so it rides the machine preset.
        self.preflight_accepted = tk.StringVar(value="")
        # Empty means an older setup has yet to import its automatic recipes.
        self.machine_gcode_rules = tk.StringVar(value="")
        self.gcode_header_notes = tk.StringVar(value="")
        # the firmware dialect: a flavor's LABEL, built-in or saved
        # (flavor_presets); the G-code page's new documents start in it
        self._flavors_ready()
        self.gcode_flavor = tk.StringVar(
            value=mew_gcode.FLAVORS["klipper"].label)
        self._machine_last_flavor = "klipper"
        # the COPY of a saved flavor the machine preset carries
        # (`flavor_presets.snapshot`), so the preset brings its flavor to
        # another computer; "" for a built-in. An older preset holds the single
        # Custom slot's rows under this key instead, which
        # `flavor_presets.adopt` turns into a saved flavor "Custom".
        self.gcode_flavor_custom = tk.StringVar(value="")
        self.gcode_flavor.trace_add(
            "write", lambda *_a: self._machine_flavor_set())
        self.gcode_flavor.trace_add(
            "write", lambda *_a: _normalise_label(
                self.gcode_flavor, CURRENT_FLAVOR_LABEL))
        self.gcode_flavor_custom.trace_add(
            "write", lambda *_a: self._machine_flavor_copy_loaded())
        # INCREMENTAL COORDINATES. A controller that cannot be zeroed (an
        # Aerotech runs in machine absolutes) is told where G-code zero is by
        # going there and switching to incremental. Off (the default): the
        # file is absolute and zero is G-code zero, as always. On: the user
        # types the machine's readout, after homing, with the nozzle at
        # G-code zero; the export homes, moves there, switches to incremental
        # and writes every move as a delta (`mew_gcode.machine_render`). The
        # rotary start angles are optional: blank writes none. The scratch
        # file every reader takes stays absolute.
        self.origin_incremental = tk.BooleanVar(value=False)
        self.origin_machine_x = tk.StringVar(value="0")
        self.origin_machine_y = tk.StringVar(value="0")
        self.origin_machine_z = tk.StringVar(value="0")
        self.origin_machine_r1 = tk.StringVar(value="")
        self.origin_machine_r2 = tk.StringVar(value="")
        # The controller's feed time unit, for a split-feed flavor's F and E:
        # a SETTING on the controller, so it sets the numbers and nothing
        # about it is written into the file. Defaults to per second: only the
        # split-feed flavor (Aerotech) reads it, and its F and E are mm/s, deg/s.
        self.feed_time_base = tk.StringVar(value=FEED_TIME_LABELS[1])
        for _cv in (self.origin_incremental, self.origin_machine_x,
                    self.origin_machine_y, self.origin_machine_z,
                    self.origin_machine_r1, self.origin_machine_r2,
                    self.feed_time_base):
            _cv.trace_add("write", lambda *_a: getattr(
                self, "_refresh_gcode_display_on_toggle", lambda: None)())
        self.origin_incremental.trace_add(
            "write", lambda *_a: self._sync_incremental_rows())
        for _cv in (self.custom_gcode, self.custom_gcode_after):
            _cv.trace_add("write", lambda *_a: self._machine_block_changed())
        for _cv in (self.machine_gcode_start, self.machine_gcode_end,
                    self.gcode_header_notes, self.machine_omit_blocks):
            _cv.trace_add("write", lambda *_a: self._machine_block_changed())

        # Printer model (3D simulation): optional STL parts the player animates as the
        # real machine -- the printhead (carries the nozzle), an optional
        # carriage/gantry, the bed (carries the print) and a static frame -- instead of
        # a single nozzle marker. Off by default. The kinematics preset says which part
        # moves on which axis; the bounds_* fields slide the build onto the model.
        # Persisted with the machine preset. The _stl var names stay axis-keyed
        # (x=head, y=gantry, z=bed) to keep old presets' machine.*_stl keys valid.
        # EVERY toggle on this page is TRACED, not merely given a `command=`: a
        # `command` / `<<ComboboxSelected>>` callback speaks for a CLICK and says
        # nothing when a value is SET, and a preset only ever sets, so a preset left
        # the control showing the loaded value and the widgets it governs settled for
        # the old one. `gui_presets._apply_all_toggles` is the answer to this class.
        self.machine_show = tk.BooleanVar(value=False)
        self.machine_show.trace_add(
            "write", lambda *_a: self._sync_machine_hide_enable())
        # per-part visibility (3D-sim only): hide an individual body while the model
        # is shown -- e.g. drop the frame to see inside, or hide the printhead to
        # swap in the small nozzle frustum. Default all-visible; only consulted when
        # machine_show is on (off -> no model at all, just the frustum marker).
        self.machine_hide_head = tk.BooleanVar(value=False)
        self.machine_hide_gantry = tk.BooleanVar(value=False)
        self.machine_hide_bed = tk.BooleanVar(value=False)
        self.machine_hide_frame = tk.BooleanVar(value=False)
        self.machine_kinematics = tk.StringVar(value=_KINEMATICS_LABELS[0])
        self.machine_kinematics.trace_add(
            "write", lambda *_a: _normalise_label(self.machine_kinematics,
                                                  _KINEMATICS_LEGACY))
        # both halves of what <<ComboboxSelected>> does, so a SET settles the page
        # the same way a selection does -- the custom-chain block AND the motion
        # limit rows, which show only the axes the chosen chain drives.
        self.machine_kinematics.trace_add(
            "write", lambda *_a: (self._toggle_kinematics_custom(),
                                  self._refresh_motion_limits()))
        self.machine_x_stl = tk.StringVar(value="")
        self.machine_y_stl = tk.StringVar(value="")
        self.machine_z_stl = tk.StringVar(value="")
        self.machine_static_stl = tk.StringVar(value="")
        # Printhead elevation -- a PHANTOM var. _SimMixin overrides both
        # _machine_standoff_field and _standoff_var to use the Physics page's
        # sim_standoff instead, so this one is neither shown, read, nor persisted.
        # Kept because the default hook bodies (and the bare-mixin test) still
        # resolve self.machine_standoff. Do not 'clean it up'.
        self.machine_standoff = tk.StringVar(value="2")
        self.machine_scale = tk.StringVar(value="1")
        # the collector/print slide's XY position on the printer, measured from the
        # bottom-left corner of the bed STL's top face (world frame when no bed STL).
        # Sim-only: the exported g-code is invariant to it (origin = collector).
        self.slide_offset_x = tk.StringVar(value="0")
        self.slide_offset_y = tk.StringVar(value="0")
        # MANUAL bed datum (bed STL frame): the print-surface Z + its bottom-left
        # corner. Blank -> the player auto-detects (find_bed_top_face). Set these (by
        # hand or via the Calibrate button) when the bed STL is a full assembly the
        # auto-detect can't read -- they override detection.
        self.bed_datum_z = tk.StringVar(value="")
        self.bed_corner_x = tk.StringVar(value="")
        self.bed_corner_y = tk.StringVar(value="")
        # the "axis located / NOT located" read-out is derived from these three,
        # so it has to follow a hand edit as well as a Calibrate write-back --
        # a stale green line saying the axis is known is the same defect as the
        # stale datum itself.
        for _v in (self.bed_datum_z, self.bed_corner_x, self.bed_corner_y):
            _v.trace_add("write", lambda *_a: self._sync_axis_datum_note())
        self._rotary_vars_make()
        # per-part MOTION LIMITS (3D-sim only): blank min/max per axis a part drives,
        # the travel range the rendered body is clamped to. Blank pair = no limit.
        # Keyed (role, axis) -> (min_var, max_var); only the driven axes are shown.
        self.machine_limits = {}
        for role in ("head", "gantry", "bed"):
            for ax in ("x", "y", "z"):
                self.machine_limits[(role, ax)] = (tk.StringVar(value=""),
                                                   tk.StringVar(value=""))
        # custom-chain config (used only when kinematics == "custom"): the axes each
        # part drives + which body the head AND the bed ride on. Defaults reproduce the
        # MEWron chain. The gantry is the one intermediate body and always mounts on the
        # frame; EITHER the head rides it or the bed does -- if neither does, a gantry
        # that drives an axis is orphaned, on neither chain, and would never move, so
        # the bed-parent choice is what makes an under-the-bed carriage live.
        self.machine_head_parent = tk.StringVar(value="gantry")
        self.machine_bed_parent = tk.StringVar(value="frame")
        self.machine_head_x = tk.BooleanVar(value=True)
        self.machine_head_y = tk.BooleanVar(value=False)
        self.machine_head_z = tk.BooleanVar(value=False)
        self.machine_gantry_x = tk.BooleanVar(value=False)
        self.machine_gantry_y = tk.BooleanVar(value=True)
        self.machine_gantry_z = tk.BooleanVar(value=False)
        self.machine_bed_x = tk.BooleanVar(value=False)
        self.machine_bed_y = tk.BooleanVar(value=False)
        self.machine_bed_z = tk.BooleanVar(value=True)
        self._machine_axis_vars = {
            "head":   (self.machine_head_x, self.machine_head_y, self.machine_head_z),
            "gantry": (self.machine_gantry_x, self.machine_gantry_y,
                       self.machine_gantry_z),
            "bed":    (self.machine_bed_x, self.machine_bed_y, self.machine_bed_z),
        }
        # contact points (numeric overrides on top of auto-detect): the nozzle tip
        # in the head STL's frame (blank = auto-detect lowest ring) and a manual Z
        # nudge on the bed actor (added on top of the player's contact alignment --
        # which raises a Z-driven bed, or drops the tool onto a fixed bed).
        self.machine_tip_x = tk.StringVar(value="")
        self.machine_tip_y = tk.StringVar(value="")
        self.machine_tip_z = tk.StringVar(value="")
        self.machine_bed_nudge = tk.StringVar(value="0")

    def _build_plate_fields(self, bp, pad):
        """The flat build plate / slide / wafer: shape, size, thickness, and
        whether the viewers draw it. Gridded into `bp` through `gui_form`, so its
        rows share the card's label and field edges."""
        F = gui_form.Form(bp)
        shape = F.group("Shape", "A rectangular slide or plate, or a round "
                                 "wafer. The size rows below follow the shape.")
        for text, value in (("Rectangular", "rectangular"),
                            ("Circular", "circular")):
            shape.add_radio(text, self.plate_shape, value,
                            "The collector is %s." % text.lower(),
                            command=self._toggle_plate_shape)

        # rectangular dims and circular dims: the shape toggle greys the inactive
        # block (both stay visible so switching is discoverable)
        self.plate_rect_frame, R = gui_form.framed(bp, F.row)
        R.entry("Size X", self.plate_x, "The plate's width along X.", unit="mm")
        R.entry("Size Y", self.plate_y, "The plate's depth along Y.", unit="mm")
        self.plate_circ_frame, C = gui_form.framed(bp, F.row + 1)
        C.entry("Diameter", self.plate_diameter, "The wafer's diameter.",
                unit="mm")
        _common, M = gui_form.framed(bp, F.row + 2)
        M.entry("Thickness Z", self.plate_thickness,
                "How thick the plate is. The print starts on its top face.",
                unit="mm")
        M.check("Show collector in viewers", self.show_plate,
                "Draw the plate in the 3D simulation and the viewers. It never "
                "changes the print.")

    def _build_origin_fields(self, og, pad, export_z=True, intro=True):
        """G-CODE ZERO: which point of the collector is (0,0,0), gridded into
        `og`. Zero is ALWAYS a point on the collector -- the page has no World
        origin (see `_init_machine_vars`).

        One name everywhere, "G-code zero", because the thing it names is where
        the nozzle is at X0 Y0 Z0 in the file -- NOT where the printer parks
        after homing, which "origin" and "home" both invite.

        Row 3 is left EMPTY on purpose: the modern shell puts the five-axis zero
        pick there (`_build_rotary_home_row`), beside the other two answers --
        the plate's face/corner on row 1 and the rod's ladder on row 2.

        `export_z` also grids the exported-Z spelling options below (rows 5-7),
        as the classic tab always has; the modern shell passes False and puts
        them on Setup's G-code card (`_build_export_z_fields`).
        `intro=False` drops the one-line lead-in, for a host that already has a
        "G-code zero" heading.

        What "G-code zero" means (GCODE_ZERO_INFO) and that Scaffold Start X/Y
        are measured from it ride every zero row's tooltip; there is no grey
        line under the rows."""
        # Start X/Y are measured from g-code zero (`_scaffold_origin_xy`):
        # Start X 40 writes X40. Changing zero rewrites them by the shift
        # (`_zero_moved`), so the scaffold stays where it is. A page that
        # overrides it to place from elsewhere gets the other sentence.
        follows = (getattr(type(self), "_scaffold_origin_xy", None)
                   is _BuildPlateMixin._scaffold_origin_xy)
        tail = (("\n\nScaffold Start X/Y are measured from this point. "
                 "Changing it rewrites them, so the scaffold stays where it "
                 "is on the plate.\n\n"
                 if follows else
                 "\n\nChanging zero changes the numbers in the g-code, not "
                 "where the scaffold prints: Start X/Y are measured from the "
                 "plate's bottom-left corner.\n\n")
                + GCODE_ZERO_INFO)
        if intro:
            gui_form.Form(og).intro("G-code zero is a point on the collector. "
                                    "Zero your printer there.")
        # which point of the COLLECTOR is (0,0,0): a face (top/bottom) x an XY
        # corner-or-centre. Two compact dropdowns rather than one 10-item list.
        self._origin_face_row, P = gui_form.framed(og, 1)
        # greyed while the Corner says Custom: the picked point carries its
        # own height, so the face has nothing to add (`_zero_custom_sync`)
        self._origin_face_field = P.combo(
            "Zero on face", self.origin_face, ("Top", "Bottom"),
            "Which face of the plate g-code zero is on." + tail,
            width="num").field
        corner = P.combo(
            "Corner", self.origin_anchor,
            ("Bottom-left", "Bottom-right", "Top-left", "Top-right",
             "Center", "Custom"),
            "Which corner of that face, or its center, is zero. Custom: "
            "click the point on the plate in 3-D, such as a mark or the edge "
            "of a clip." + tail, width="num")
        self._zero_custom_tail(corner, "plate", tail)
        # THE ROTARY DATUM. A round rod has no corners, so the plate's face + corner
        # ladders cannot name a point on it; these two can. Greyed with the rotary
        # tick, so a 3-axis user sees that 4-axis has its own zero.
        # ROW 2, directly under the plate's face/corner: both answer "which
        # point of the collector is zero", with row 3 held for the five-axis pick
        # (see this method's docstring). Gridding a new control on a row another
        # already holds is what ee6c9c7 fixed; do not repeat it.
        rod_tip = ("Where you zero the nozzle for a TUBE print, on the mandrel. "
                   "Along the axis: an end of the mandrel, or its mid-length. "
                   "Radially: the mandrel's top surface or its axis, one radius "
                   "lower. There is no sideways choice. A tube program emits "
                   "no Y word, so zero is on the mandrel axis by construction."
                   + tail)
        self._rotary_origin_row, Q = gui_form.framed(og, 2)
        along = Q.combo("Zero along the mandrel", self.rotary_origin_axial,
                        _ROTARY_ORIGIN_AXIAL_LABELS,
                        rod_tip.replace(
                            "or its mid-length.", "or its mid-length, or "
                            "Custom: click the point on the mandrel in 3-D, "
                            "such as a mark or a clip.", 1))
        self._zero_custom_tail(along, "mandrel", tail)
        Q.combo("Radially", self.rotary_origin_radial,
                _ROTARY_ORIGIN_RADIAL_LABELS, rod_tip)
        # ROW 8: rows 5-7 are the classic tab's export-Z rows and 3 is the
        # five-axis pick, so the machine's own coordinates go below them all.
        if export_z:
            self._build_incremental_rows(og, 8)
            self._build_export_z_fields(og, pad, row=5)
            self._build_gcode_flavor_field(gui_form.Form(og, row=10))

    def _build_incremental_rows(self, parent, row):
        """The "Incremental coordinates" tick and, under it while ticked, the
        machine's position at G-code zero: X Y Z, plus each rotary axis the
        machine has as an optional start angle. See `_init_machine_vars`."""
        inc_tip = (
            "For a printer you cannot zero, such as an Aerotech that works in "
            "its own absolute coordinates.\n\n"
            "Off: the file is absolute, and X0 Y0 Z0 is G-code zero. Zero "
            "your printer there.\n\n"
            "On: type where the printer is when the nozzle sits at G-code "
            "zero. The file then homes, moves there, switches to incremental "
            "and writes every move as a step from the one before.")
        pos_tip = (
            "Where the printer is when the nozzle sits at G-code zero: the "
            "position its readout shows, after homing. In mm.")
        rot_tip = (
            "Optional. The rotary axis's angle at G-code zero, in degrees. "
            "Leave it blank to leave the axis where homing put it.")
        fr, F = gui_form.framed(parent, row)
        self._incremental_frame = fr
        F.check("Incremental coordinates", self.origin_incremental, inc_tip)
        g = F.group("Printer position at zero", pos_tip, unit="mm", indent=1)
        self._origin_pos_row = g
        parts = []
        for L, var in (("X", self.origin_machine_x),
                       ("Y", self.origin_machine_y),
                       ("Z", self.origin_machine_z)):
            parts.append((None, g.add_label(L), g.add_entry(
                "", var, pos_tip, width="small")))
        for n, var in ((1, self.origin_machine_r1),
                       (2, self.origin_machine_r2)):
            parts.append((n, g.add_label("A"), g.add_entry(
                "", var, rot_tip, width="small")))
        self._origin_pos_parts = parts
        for v in ("rotary_on", "rotary_kind", "rotary_word", "rotary1_word",
                  "rotary2_word", "s_collector"):
            var = getattr(self, v, None)
            if var is not None:
                var.trace_add("write",
                              lambda *_a: self._sync_incremental_rows())
        self._sync_incremental_rows()
        return fr

    def _origin_rotary_letters(self):
        """The rotary letters the machine has: none, the rod's, or the
        five-axis pair's two."""
        try:
            if not self.rotary_on.get():
                return ()
            if self._rotary_kind_key() == "pair":
                return (self.rotary1_word.get().strip().upper(),
                        self.rotary2_word.get().strip().upper())
            return (self.rotary_word.get().strip().upper(),)
        except (AttributeError, tk.TclError):
            return ()

    def _sync_incremental_rows(self):
        g = getattr(self, "_origin_pos_row", None)
        if g is None:
            return
        on = bool(self.origin_incremental.get())
        for w in g.widgets:
            w.grid() if on else w.grid_remove()
        letters = self._origin_rotary_letters()
        for n, lbl, ent in self._origin_pos_parts:
            lbl.pack_forget()
            ent.pack_forget()
        for n, lbl, ent in self._origin_pos_parts:
            if n is not None:
                if n > len(letters) or not letters[n - 1]:
                    continue
                lbl.configure(text=gui_form.label_text(letters[n - 1], "deg"))
            lbl.pack(side="left", padx=(gui_fonts.SP["s"], 0))
            ent.pack(side="left", padx=(gui_fonts.SP["xs"], 0))

    def _machine_flavor_name(self):
        """The engine's name for the Machine page's flavor ("klipper", ...)."""
        try:
            fl = flavor_presets.by_label(self.gcode_flavor.get())
        except (AttributeError, tk.TclError):
            fl = None
        return fl.name if fl is not None else "klipper"

    def _feed_time_unit(self):
        """The controller's feed time unit, "min" or "s"."""
        try:
            return _FEED_TIME_UNIT.get(self.feed_time_base.get(), "min")
        except (AttributeError, tk.TclError):
            return "min"

    def _origin_output_fields(self):
        """`mew_gcode.OutputOptions`' incremental fields, from this page:
        ``(incremental, origin, rotary_axes)``. A blank or unreadable value
        is left out (`_validate_origin` refuses to generate with one)."""
        try:
            inc = bool(self.origin_incremental.get())
        except (AttributeError, tk.TclError):
            return False, (), ""
        letters = self._origin_rotary_letters()
        origin = []
        if inc:
            pairs = [("X", self.origin_machine_x), ("Y", self.origin_machine_y),
                     ("Z", self.origin_machine_z)]
            pairs += [(L, v) for L, v in zip(
                letters, (self.origin_machine_r1, self.origin_machine_r2)) if L]
            for L, var in pairs:
                try:
                    origin.append((L, float(var.get())))
                except (ValueError, tk.TclError):
                    pass
        return inc, tuple(origin), "".join(letters)

    def _zero_custom_error(self, kind):
        """A refusal while a zero dropdown says Custom and no point is picked
        (a preset can carry the choice without the point), else None."""
        if self._zero_is_custom(kind) and self._zero_custom_point(kind) is None:
            return ("G-code zero is Custom, but no point is picked on the %s. "
                    "Use Pick in 3D… beside it on Setup's Collector card "
                    "(Printer & collector), or choose %s." % (
                        kind, "an end" if kind == "mandrel" else "a corner"))
        return None

    def _validate_origin(self):
        """An error message for an unreadable machine position, or None."""
        e = (None if self._header_is_mandrel()
             else self._zero_custom_error("plate"))
        if e:
            return e
        if not self.origin_incremental.get():
            return None
        for L, var in (("X", self.origin_machine_x),
                       ("Y", self.origin_machine_y),
                       ("Z", self.origin_machine_z)):
            try:
                float(var.get())
            except ValueError:
                return ("Printer position at zero: %s must be a number (mm). "
                        "It is on Setup's Collector card (Printer & "
                        "collector)." % L)
        for L, var in zip(self._origin_rotary_letters(),
                          (self.origin_machine_r1, self.origin_machine_r2)):
            raw = var.get().strip()
            if raw:
                try:
                    float(raw)
                except ValueError:
                    return ("Printer position at zero: %s must be a number "
                            "(deg), or blank. It is on Setup's Collector card "
                            "(Printer & collector)." % L)
        return None

    def _build_gcode_flavor_field(self, F, include_feed=True):
        """The machine's g-code flavor: the firmware dialect a NEW G-code-page
        document is written in (the document can override it). A machine
        property, so it rides the machine preset (`origin.gcode_flavor`, and
        a saved flavor's copy in `origin.gcode_flavor_custom`). The modern
        shell grids it on the Machine page's Machine card; the classic tab
        under its G-code zero rows. "Edit flavors..." opens the flavor editor
        the G-code page's Syntax menu also opens (`gui_flavor_editor`)."""
        flavor_tip = (
            "The firmware your printer runs. MEWracle writes its own moves "
            "the same way for all of them; the flavor decides how they are "
            "spelled for your controller (LINEAR instead of G1, say), and "
            "how a pause, a wait for temperature or a message is written.\n\n"
            "Pick the one your printer's firmware is: Klipper, "
            "RepRapFirmware (Duet), Marlin, GRBL or Aerotech. Klipper is "
            "selected until you choose. Edit flavors… changes any of them "
            "(Reset to default brings a built-in back) or makes your own "
            "with New or Duplicate. A g-code document in the G-code window starts in this "
            "flavor and can be switched there.")
        row = F.combo("G-code flavor", self.gcode_flavor,
                      self._flavor_labels(), flavor_tip)
        box = row.field
        box.configure(postcommand=lambda: box.configure(
            values=self._flavor_labels()))
        self._machine_flavor_box = box
        row.add_button(
            "Edit flavors\u2026",
            lambda: self._open_flavor_editor(
                self.gcode_flavor.get(), self.gcode_flavor.set,
                "Use on this printer"),
            "See every flavor, change how one spells each command, make a "
            "new one or rename your own. The G-code page's Syntax menu opens "
            "the same editor.")
        if include_feed:
            self._build_feed_time_field(F)
        return row

    def _build_feed_time_field(self, F):
        F.combo("Feed time unit", self.feed_time_base, FEED_TIME_LABELS,
                "The time unit your controller reads F (and E) in. It is a "
                "setting on the controller, so nothing about it is written "
                "into the file: set it the same on both.\n\n"
                "It matters for a flavor that splits the feed, such as "
                "Aerotech: F is the linear rate and E the rotary rate, both "
                "worked out from your surface speed in this unit.")

    def _build_machine_gcode_fields(self, parent):
        """Machine's language, framing blocks, and coordinate settings."""
        F = gui_form.Form(parent, stretch=True)
        # which of the many rows below matter: a newcomer with a home-built
        # printer met the whole list with no way to tell (vireo, item 3)
        F.intro("Most printers need two things here: G-code flavor, the "
                "firmware your printer runs (a home-built printer is most "
                "often Klipper or Marlin), and Start and end of print, the "
                "commands your printer runs before and after every print. "
                "Leave the other rows as they are unless your controller "
                "asks for something else.", wrap=560)
        flavor = self._build_gcode_flavor_field(F, include_feed=False)
        F.check("Strip comments", self.machine_strip_comments,
                "Remove comments when saving for a controller that cannot read them. "
                "Commands in the start and end blocks still run.")
        F.check("Mark each layer in the file", self.machine_layer_markers,
                "Write a ;LAYER:n comment where each layer starts, so the "
                "file opens with its layers in MEWracle on any computer and "
                "in other g-code viewers, even when it names its height "
                "only once. Strip comments removes them too.")
        # the printer's spelling and its heat wait: what every print, a
        # design's or a calibration's, is written with
        rows = getattr(self, "_build_printer_output_rows", None)
        if rows is not None:
            rows(F)
        arcs = getattr(self, "_build_g2g3_row", None)
        if arcs is not None:
            arcs(F)
        self._build_feed_time_field(F)
        self._build_incremental_rows(parent, F._next(None))
        # how the exported g-code spells height: Machine-preset keys, so on
        # the page whose preset saves them
        F.section("Height in the exported g-code", first=False)
        self._build_export_z_fields(parent, self.pad, F=F)
        F.section("Start and end of print", first=False)
        F.check("Leave out the start and end blocks", self.machine_omit_blocks,
                "Write the print without the start block (the header) and "
                "the end block. The moves, the layer marks, the automatic "
                "commands and the exit move are still written.")
        tip = ("One command per line. Write [[name]] where a value from this "
               "print goes: [[pressure]], [[spacing]], [[nozzle_height]], "
               "[[polymer]], [[notes]] and more (press Placeholders\u2026 for "
               "all of them). Use [[pressure_command]], [[voltage_command]] or "
               "[[temperature_command]] for the selected flavor's setpoints; a "
               "flavor with none prints no line. [[temperature_command]] is "
               "the flavor's heat wait, written only while \"Wait to print "
               "until target temperature is reached\" is ticked. [[layer]] "
               "and [[z]] belong to scripts, not to this block.")
        if hasattr(self, "_build_header_notes_row"):
            self._build_header_notes_row(F)
        for label, var in (("Start of print (the header)", self.machine_gcode_start),
                           ("End of print", self.machine_gcode_end)):
            heading = ttk.Frame(parent)
            gui_form.mark(ttk.Label(heading, text=label, style="Section.TLabel"),
                          "section").pack(side="left")
            help_button = gui_form.mark(ttk.Button(
                heading, text="Placeholders\u2026", command=lambda: messagebox.showinfo(
                    "G-code placeholders", self._placeholder_help_text(),
                    parent=self.root)), "button")
            help_button.pack(side="left", padx=(gui_fonts.SP["m"], 0))
            F.slot(None, heading, tip, sticky="ew")
            editor = ttk.Frame(parent)
            editor.columnconfigure(0, weight=1)
            editor.rowconfigure(0, weight=1)
            text = tk.Text(editor, width=50, wrap="none", undo=True,
                           height=(len(mew_gcode.DEFAULT_START_BLOCK.splitlines())
                                   if label.startswith("Start") else 5))
            gui_form.exempt(text, "editable multiline printer g-code block")
            text.insert("1.0", var.get())
            text.grid(row=0, column=0, sticky="nsew")
            scroll = ttk.Scrollbar(editor, orient="vertical", command=text.yview)
            scroll.grid(row=0, column=1, sticky="ns")
            text.configure(yscrollcommand=scroll.set)
            F.slot(None, editor, tip, sticky="ew")
            if label.startswith("Start"):
                self._machine_start_text = text
                warning = F.live(None, text=(
                    "Comments in this block are removed when you save; commands still run."),
                    wrap=560).field
                def sync_warning(*_a, widget=warning):
                    if self.machine_strip_comments.get():
                        widget.grid()
                    else:
                        widget.grid_remove()
                self.machine_strip_comments.trace_add("write", sync_warning)
                sync_warning()
            else:
                self._machine_end_text = text
            def changed(_e, widget=text, value=var):
                if widget.edit_modified():
                    widget.edit_modified(False)
                    new = widget.get("1.0", "end-1c")
                    if new != value.get():
                        value.set(new)
            def refresh(*_a, widget=text, value=var):
                if widget.winfo_exists() and widget.get("1.0", "end-1c") != value.get():
                    widget.delete("1.0", "end")
                    widget.insert("1.0", value.get())
                    widget.edit_modified(False)
            text.edit_modified(False)
            text.bind("<<Modified>>", changed, add="+")
            var.trace_add("write", refresh)
        self._build_header_preview(parent, F)
        if hasattr(self, "_build_automatic_section"):
            self._build_automatic_section(parent, F)
        return flavor.field

    # -- the header's live proof: what it will say, and what will not fill ---- #
    @staticmethod
    def _placeholder_help_text():
        lines = ["Write [[name]] in the start or end block, or in a script; it "
                 "is filled in when the g-code is written.", "",
                 "In the blocks and in scripts:"]
        lines += ["  [[%s]]  \u2014  %s" % kd for kd in mew_gcode.BLOCK_PLACEHOLDERS]
        lines += ["", "In scripts only (the value at the layer or move the "
                      "script runs on; the blocks are written before any move):"]
        lines += ["  [[%s]]  \u2014  %s" % kd for kd in mew_gcode.RULE_PLACEHOLDERS]
        return "\n".join(lines)

    def _build_header_preview(self, parent, F):
        """Under the blocks: the header exactly as it will read with this
        design's values, and a red line for any [[name]] that will not fill."""
        F.section("What the header will say", first=False)
        pv = ttk.Frame(parent)
        pv.columnconfigure(0, weight=1)
        shown = tk.Text(pv, width=50, height=len(mew_gcode.DEFAULT_START_BLOCK.splitlines()),
                        wrap="none", state="disabled", relief="flat")
        gui_form.exempt(shown, "read-only header preview")
        shown.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(pv, orient="vertical", command=shown.yview)
        sb.grid(row=0, column=1, sticky="ns")
        shown.configure(yscrollcommand=sb.set)
        F.slot(None, pv, "The start block with this print's values filled in, "
                         "as the g-code will begin. A line whose value is "
                         "blank says (not set); a setpoint line your flavor "
                         "does not have is left out.", sticky="ew")
        bad = tk.StringVar(value="")
        warn = F.live(None, textvariable=bad, wrap=560, warn=True).field
        warn.configure(foreground="#c0392b")
        self._build_preflight_box(F)
        self._header_preview_widget = shown
        self._header_preview_problems = bad
        self._header_preview_after = None
        traced = set()

        def follow():
            """Trace every value the header reads, including ones made after
            this card (Physics vars) or by another page (a G-code window
            scaffold's layers, winding, length): the preview sits on Setup
            while they are edited elsewhere, and stayed at the values it was
            first drawn with."""
            vs = [getattr(self, n, None) for n in (
                "sim_pressure", "sim_voltage", "sim_temperature",
                "sim_standoff", "sim_polymer", "s_collector",
                "add_elevation_z", "s_wait_for_temp", "m_diameter",
                "rotary_origin_axial", "rotary_origin_radial",
                "rotary_origin_custom", "origin_anchor", "origin_face",
                "origin_custom")]
            for groups in (getattr(self, "gce_scaf_vars", None) or [],
                           getattr(self, "scaf_vars", None) or []):
                for g in groups:
                    vs += [g.get(k) for k in (
                        "layers", "speed", "stepover", "m_angle", "m_length",
                        "m_family", "m_design_mode", "m_pore_target")]
            for v in vs:
                if v is not None and hasattr(v, "trace_add") \
                        and str(v) not in traced:
                    traced.add(str(v))
                    v.trace_add("write", soon)

        def paint(*_a):
            self._header_preview_after = None
            try:
                if not shown.winfo_exists():
                    return
            except tk.TclError:
                return
            follow()
            body = (self._header_preview_text()
                    if hasattr(self, "_header_preview_text") else "")
            shown.configure(state="normal")
            shown.delete("1.0", "end")
            shown.insert("1.0", body)
            shown.configure(state="disabled")
            said = (list(self._header_problems())
                    if hasattr(self, "_header_problems") else [])
            gaps = getattr(self, "_header_process_gaps", None)
            if callable(gaps):
                said.append(gaps())
            bad.set(" ".join(s for s in said if s))

        def soon(*_a):
            if self._header_preview_after is None:
                try:
                    self._header_preview_after = self.root.after(150, paint)
                except tk.TclError:
                    pass
        self._header_preview_paint = paint
        for var in (self.machine_gcode_start, self.machine_gcode_end,
                    self.gcode_header_notes, self.gcode_flavor,
                    self.machine_omit_blocks):
            var.trace_add("write", soon)
        # `follow` traces the print values at every paint; a value made since
        # the last paint is caught when the preview next comes on screen
        self._repaint_on_show(pv, soon)
        paint()

    def _machine_blocks(self):
        """(start, end): the two blocks as a written file carries them, both
        empty while "Leave out the start and end blocks" is ticked."""
        if self.machine_omit_blocks.get():
            return "", ""
        return self.machine_gcode_start.get(), self.machine_gcode_end.get()

    def _machine_recipes(self):
        """The automatic scripts as dicts (gui_scripts overrides this)."""
        return []

    def _machine_block_changed(self):
        if (not getattr(self, "_machine_migrating", False)
                and not getattr(self, "_loading_preset", False)
                and hasattr(self, "_gce_doc") and hasattr(self, "gce")):
            self._refresh_gcode_display_on_toggle()

    def _machine_gcode_context(self, filename=None, use_gce=False):
        """Values in the current design, for machine blocks and recipes."""
        now = datetime.now()
        def val(name, fallback=""):
            v = getattr(self, name, None)
            try:
                return str(v.get()) if v is not None else fallback
            except (AttributeError, tk.TclError):
                return fallback
        try:
            if use_gce and hasattr(self, "gce_scaf_vars"):
                n = self._gce_scaffold_count()
                groups = self.gce_scaf_vars[:n]
            else:
                n = self._scaffold_count()
                groups = self.scaf_vars[:n]
        except (AttributeError, tk.TclError):
            groups = []
        def each(key):
            return ", ".join(str(g[key].get()) for g in groups if key in g)
        def rotations():
            out = []
            for g in groups:
                try:
                    out.append("%g" % self._rotation_value(g["rotation"]))
                except (KeyError, ValueError, tk.TclError):
                    out.append("")
            return ", ".join(out)
        nozzle = getattr(self, "cal_vars", {}).get("cal.noz_inner_diameter")
        try:
            nozzle_text = ("%g mm inner diameter" % float(nozzle.get())
                           if nozzle else "")
        except (ValueError, tk.TclError):
            nozzle_text = ""
        # a source copy with no git and no build stamp: say so, rather than
        # "(not set)", which reads as a setting the user missed
        version = (getattr(self, "_history_app_version", lambda: "")()
                   or "unversioned build")
        # A mandrel print has no spacing or layer rotation, and a plate print
        # no winding: a value that does not describe this print is
        # NOT_THIS_PRINT, and the header leaves its line out rather than
        # carrying the other collector's numbers.
        na = mew_gcode.NOT_THIS_PRINT
        mandrel = self._header_is_mandrel()
        if mandrel:
            winding, length = self._header_tube_values(groups, use_gce)
            spacing = rotation = na
            try:
                collector = "mandrel, %g mm diameter" % float(
                    self.m_diameter.get())
            except (AttributeError, ValueError, tk.TclError):
                collector = "mandrel"
        else:
            winding = length = na
            spacing, rotation = each("stepover"), rotations()
            collector = "flat plate"
        zero = self._gcode_zero_short()

        def wait_on():
            var = getattr(self, "s_wait_for_temp", None)
            try:
                return bool(var is not None and var.get())
            except tk.TclError:
                return False

        def melt(key):
            # the melt constants the Material card holds, to 6 figures
            var = getattr(self, "cal_vars", {}).get("cal." + key)
            try:
                text = str(var.get()).strip() if var is not None else ""
                return "%.6g" % float(text) if text else ""
            except (ValueError, tk.TclError):
                return ""
        return {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"),
                "eta_ref": melt("mat_viscosity_ref"),
                "ea": melt("mat_activation_energy"),
                "t_ref": melt("mat_temperature_ref"),
                "filename": filename or getattr(self, "_gcode_export_filename", "scaffold.gcode"),
                "collector": collector, "zero": zero,
                "spacing": spacing, "layers": each("layers"),
                "rotation": rotation, "scaffold_count": str(len(groups)),
                "winding_angle": winding, "scaffold_length": length,
                "pressure": val("sim_pressure"), "voltage": val("sim_voltage"),
                "temperature": val("sim_temperature"), "gap": val("sim_standoff"),
                "speed": each("speed"), "nozzle": nozzle_text,
                "polymer": val("sim_polymer"),
                "notes": val("gcode_header_notes"), "version": version,
                # the start block's [[temperature_command]] follows the
                # heat-wait box (`mew_gcode.machine_placeholder_context`)
                "heat_wait": "1" if wait_on() else ""}

    def _build_preflight_box(self, F):
        """"Ask before Save ...": ticked, Save asks whenever the file leaves
        a process setting or the end to the printer; unticked (or "don't ask
        again" on that dialog), this printer has accepted them. One var
        under it, `preflight_accepted`, rides the machine preset."""
        ask = tk.BooleanVar(value=True)
        note = tk.StringVar(value="")
        busy = {"on": False}
        keys = ("voltage", "pressure", "temperature", "end")
        words = {"voltage": "the voltage", "pressure": "the pressure",
                 "temperature": "the temperature", "end": "an empty End block"}

        def from_saved(*_a):
            have = [k for k in keys
                    if k in self.preflight_accepted.get().split(",")]
            note.set(("Save does not ask about %s on this printer. Tick to "
                      "ask again." % ", ".join(words[k] for k in have))
                     if have else "")
            if busy["on"]:
                return
            busy["on"] = True
            try:
                ask.set(not have)
            finally:
                busy["on"] = False

        def from_box(*_a):
            if busy["on"]:
                return
            busy["on"] = True
            try:
                self.preflight_accepted.set("" if ask.get() else ",".join(keys))
            finally:
                busy["on"] = False
        self.preflight_accepted.trace_add("write", from_saved)
        ask.trace_add("write", from_box)
        from_saved()
        self._preflight_ask = ask
        F.check("Ask before Save when the file leaves something to the "
                "printer", ask,
                "Ticked: Save g-code as… stops to say when the file does not "
                "set the voltage, pressure or temperature, or the End block "
                "is empty, with the way here to add them. Untick when this "
                "printer's are set by hand and you know it. Saved with the "
                "printer.")
        F.live(None, textvariable=note, wrap=560)

    def _repaint_on_show(self, widget, fn):
        """Call `fn` whenever `widget`, or anything holding it up to the
        window, comes on screen: a page's tab, a card opening. For a readout
        of values another page owns, which no trace here can see change.
        The page stack keeps every page mapped and says a switch with
        <<NotebookTabChanged>>, so both are bound on every holder."""
        w = widget
        while w is not None and w is not self.root:
            try:
                w.bind("<Map>", fn, add="+")
                w.bind("<<NotebookTabChanged>>", fn, add="+")
            except tk.TclError:
                break
            w = w.master

    def _build_zero_readout(self, F):
        """Under "Add ... gap to Z": what X0 and Z0 mean in the file this
        printer setup writes, in one sentence that follows every setting it
        reads (the collector, its datum, the gap and the box itself)."""
        var = tk.StringVar(value="")
        row = F.live(None, textvariable=var, wrap=560)
        pending = {"id": None}

        def paint():
            pending["id"] = None
            try:
                var.set(self._gcode_zero_text())
            except (AttributeError, tk.TclError, ValueError):
                pass

        def soon(*_a):
            if pending["id"] is None:
                try:
                    pending["id"] = self.root.after(100, paint)
                except tk.TclError:
                    pass
        self._rotary_vars_make()
        make = getattr(self, "_mandrel_vars_make", None)
        if callable(make):
            make()
        names = ("add_elevation_z", "s_collector", "rotary_origin_axial",
                 "rotary_origin_radial", "origin_anchor", "origin_face",
                 "origin_custom", "rotary_origin_custom",
                 "m_diameter", "sim_standoff")
        for name in names:
            v = getattr(self, name, None)
            if v is not None:
                v.trace_add("write", soon)
        try:
            self._standoff_var().trace_add("write", soon)
        except (AttributeError, tk.TclError):
            pass
        field = getattr(row, "field", None)
        if field is not None:
            self._repaint_on_show(field, soon)
        paint()

    def _header_is_mandrel(self):
        fn = getattr(self, "_mandrel_is_active", None)
        try:
            return bool(fn()) if callable(fn) else False
        except tk.TclError:
            return False

    def _header_tube_values(self, groups, use_gce):
        """(winding angles, scaffold lengths) of a mandrel print, as text,
        one per scaffold: what the generator BUILDS (the resolved design,
        which a closing length may move off the typed angle), else the typed
        value. A cell pattern has no winding angle."""
        import contextlib
        angles, lengths = [], []
        scope = getattr(self, "_gce_tube_scope", None)
        for i, _g in enumerate(groups):
            try:
                with (scope(i) if (use_gce and callable(scope))
                      else contextlib.nullcontext()):
                    zig = self._mandrel_family_value() == "zigzag"
                    try:
                        d = self._mandrel_resolved_design()
                    except Exception:  # noqa: BLE001 -- the header falls back
                        d = None
                    if d is not None:
                        a = ("%.4g" % d.omega_deg) if zig else ""
                        n = "%.4g" % d.length
                    else:
                        a = self.m_angle.get().strip() if zig else ""
                        n = self.m_length.get().strip()
            except (AttributeError, tk.TclError):
                a = n = ""
            if a:
                angles.append(a)
            lengths.append(n)
        return (", ".join(angles) if angles else mew_gcode.NOT_THIS_PRINT,
                ", ".join(x for x in lengths if x))

    def _gcode_zero_short(self):
        """The header's Zero line: just the point of the collector that is
        X0 Y0 (Z0), e.g. "plate's bottom-left corner". The gap has its own
        header line, so the Z explanation stays on the Setup card and in
        Save's pre-flight (`_gcode_zero_text`)."""
        if self._header_is_mandrel():
            axial, radial = self._rotary_origin_keys()
            if axial == "custom":
                return "custom"
            end = {"near": "near end", "far": "far end",
                   "mid": "mid-length"}.get(axial, "near end")
            return "mandrel's %s, %s" % (
                end, "axis" if radial == "axis" else "surface")
        anchor = self.origin_anchor.get().strip().lower()
        face = self.origin_face.get().strip().lower() or "top"
        names = {"bottom-left": "bottom-left corner",
                 "bottom-right": "bottom-right corner",
                 "top-left": "top-left corner", "top-right": "top-right corner",
                 "center": "centre"}
        if anchor not in names:
            return "custom"
        text = "plate's %s" % names[anchor]
        if face.startswith("bottom"):
            text += ", bottom face"
        return text

    def _gcode_zero_text(self):
        """One sentence: where the saved g-code's zero is on the collector,
        and what Z0 means for the nozzle, with the gap added or not. The
        header carries it, the Setup card shows it under "Add ... gap to Z",
        and Save's pre-flight says it."""
        gap = self._elevation_value()
        added = bool(self.add_elevation_z.get())
        surface_z = None
        if self._header_is_mandrel():
            axial, radial = self._rotary_origin_keys()
            xpart = "X0 is %s" % {
                "near": "the mandrel's near end (-X)",
                "far": "the mandrel's far end (+X)",
                "mid": "the mandrel's mid-length"}.get(axial, "the mandrel's near end")
            if axial == "custom":
                p = self._zero_custom_point("mandrel")
                xpart = ("X0 is a custom point on the mandrel, %g mm from its "
                         "near end" % round(p[1], 3) if p is not None else
                         "X0 is the mandrel's near end (-X): Custom is not "
                         "picked yet")
            if radial == "axis":
                try:
                    surface_z = float(self.m_diameter.get()) / 2.0
                except (AttributeError, ValueError, tk.TclError):
                    surface_z = None
                where = "the mandrel axis"
            else:
                where = "the mandrel surface"
        else:
            anchor = self.origin_anchor.get().strip().lower()
            face = self.origin_face.get().strip().lower() or "top"
            xpart = ("X0 Y0 is the plate's centre" if anchor == "center"
                     else "X0 Y0 is the plate's %s corner" % (anchor or "bottom-left"))
            where = "the plate's %s face" % face
            if anchor == "custom":
                p = self._zero_custom_point("plate")
                if p is None:
                    xpart = ("X0 Y0 is the plate's bottom-left corner: Custom "
                             "is not picked yet")
                    where = "the plate's top face"
                else:
                    xpart = ("X0 Y0 is a custom point on the plate, X%g Y%g "
                             "from its bottom-left corner"
                             % (round(p[1], 3), round(p[2], 3)))
                    where = "that point"
        if surface_z is not None:
            zpart = ("Z0 is level with the mandrel axis, so the surface is "
                     "Z%g" % surface_z)
            zpart += ("; the %g mm gap is added to every Z" % gap if added
                      else "; the %g mm gap is not added to Z" % gap)
        elif added:
            zpart = ("Z0 is the nozzle touching %s; the %g mm gap is added to "
                     "every Z, so the first layer prints at Z%g"
                     % (where, gap, gap))
        else:
            zpart = ("Z0 is the nozzle at its working height, %g mm above %s: "
                     "the gap is not added to Z, so zero your printer's Z "
                     "there, not touching" % (gap, where))
        return "%s. %s." % (xpart, zpart)

    # ------------------------------------------------------------------ #
    # saved flavors (flavor_presets) -- shared with the G-code page
    # ------------------------------------------------------------------ #
    def _flavors_dir(self):
        migrate_preset_layout_once()
        return FLAVOR_PRESETS_DIR

    def _flavors_ready(self):
        """Register the lab's saved flavors, once per app."""
        if not getattr(self, "_flavors_loaded", False):
            self._flavors_loaded = True
            flavor_presets.load(self._flavors_dir())

    def _flavor_labels(self):
        self._flavors_ready()
        return flavor_presets.labels()

    def _open_flavor_editor(self, select=None, on_use=None, use_text=None):
        import gui_flavor_editor
        self._flavors_ready()
        return gui_flavor_editor.FlavorEditor.open(self, select, on_use,
                                                   use_text)

    def _machine_flavor_set(self):
        """The machine's flavor was picked (or loaded): carry its copy."""
        if getattr(self, "_cf_loading", False):
            return
        fl = flavor_presets.by_label(self.gcode_flavor.get())
        if fl is None:
            return      # a saved flavor whose copy is still on its way in
        self._machine_last_flavor = fl.name
        self._cf_loading = True
        try:
            self.gcode_flavor_custom.set(flavor_presets.snapshot(fl))
        finally:
            self._cf_loading = False

    def _machine_flavor_copy_loaded(self):
        """A machine preset set the carried copy: make that flavor available
        here (this computer's own wins, `flavor_presets.adopt`) and show it."""
        if getattr(self, "_cf_loading", False):
            return
        text = self.gcode_flavor_custom.get()
        fl = flavor_presets.adopt(self._flavors_dir(), text)
        if fl is None:
            self._machine_flavor_set()   # an empty copy: carry the box's own
            return
        try:
            carried = json.loads(text).get("label",
                                           flavor_presets.LEGACY_LABEL)
        except (ValueError, AttributeError):
            carried = None
        self._cf_loading = True
        try:
            cur = self.gcode_flavor.get()
            if flavor_presets.by_label(cur) is None or (
                    cur == carried and cur != fl.label):
                self.gcode_flavor.set(fl.label)
            if flavor_presets.by_label(self.gcode_flavor.get()) is fl:
                self.gcode_flavor_custom.set(flavor_presets.snapshot(fl))
        finally:
            self._cf_loading = False
        self._flavors_changed()

    def _machine_presets_follow_rename(self, renamed):
        """A saved flavor was renamed ({old label: new}): every saved printer
        preset that names it is rewritten to the new name, with a fresh
        carried copy. A preset names it when its label is the old one and its
        carried copy is that flavor's (or it carries none). A preset file
        this misses still loads: `flavor_presets.by_label` knows the old
        name."""
        st = getattr(self, "_pstores", {}).get("machine")
        if not st:
            return
        touched = False
        for p in st["presets"]:
            data = p.get("data")
            if not isinstance(data, dict):
                continue
            old = data.get("origin.gcode_flavor")
            if old not in renamed:
                continue
            fl = flavor_presets.by_label(renamed[old])
            if fl is None:
                continue
            try:
                carried = json.loads(
                    data.get("origin.gcode_flavor_custom") or "{}")
            except ValueError:
                carried = {}
            if isinstance(carried, dict) and carried.get("name") not in (
                    None, fl.name):
                continue            # another computer's flavor of that name
            data["origin.gcode_flavor"] = fl.label
            data["origin.gcode_flavor_custom"] = flavor_presets.snapshot(fl)
            if self._write_preset_file("machine", p):
                touched = True
        if touched:
            try:
                self._refresh_preset_table("machine")
            except (tk.TclError, AttributeError, KeyError):
                pass

    def _flavors_changed(self, renamed=None, deleted=None, edited=None):
        """A flavor was saved, renamed, duplicated or deleted: the Machine
        page's choice follows a rename, falls back to Klipper when its flavor
        went, and re-carries the copy; the G-code page follows too."""
        cur = self.gcode_flavor.get()
        if renamed:
            self._machine_presets_follow_rename(renamed)
        if renamed and cur in renamed:
            self.gcode_flavor.set(renamed[cur])
        elif deleted and cur == deleted:
            self.gcode_flavor.set(mew_gcode.FLAVORS["klipper"].label)
        else:
            self._machine_flavor_set()
        box = getattr(self, "_machine_flavor_box", None)
        if box is not None:
            try:
                box.configure(values=self._flavor_labels())
            except tk.TclError:
                pass
        ed = getattr(self, "_flavor_editor", None)
        if ed is not None and ed.alive() and not ed.dirty():
            ed.refresh()            # a flavor made or gone outside the editor
        hook = getattr(self, "_gce_flavors_changed", None)
        if callable(hook):
            hook(renamed or {}, deleted, edited)
        # a flavor that gained a pressure or voltage word changes what the
        # header writes and what the card warns is left out
        paint = getattr(self, "_header_preview_paint", None)
        if callable(paint):
            try:
                paint()
            except tk.TclError:
                pass

    def _build_export_z_fields(self, og, pad, row=0, F=None):
        """How the exported g-code spells height: add the printhead elevation,
        and drop the per-layer / per-scaffold Z words. Rows `row`..`row + 3`
        of `og`, or the next rows of Form `F` when the caller already has one
        (Setup's G-code card, so these land on its grid).
        Export spelling only -- the persist keys stay `origin.*` and stay in
        the MACHINE preset, which is where every shipped rig already carries
        them."""
        # NB: the elevation tooltip below is build-specific wording. Elevation lives
        # on the Physics page as sim_standoff and this group renders no elevation
        # field, so it says "set on the Physics page". Keep it in step with
        # _machine_standoff_field / _standoff_var.
        if F is None:
            F = gui_form.Form(og, row=row)
        modern = hasattr(self, "_gce_page_scope")
        gap = "nozzle–collector gap" if modern else "nozzle–collector gap"
        F.check("Add " + gap + " to Z", self.add_elevation_z,
                "Commands the nozzle at its working height: start Z + "
                "gap. Raises every exported Z by the " + gap + " "
                "value (set on the Physics page), so the print is commanded at "
                "the nozzle's standoff height. The 3D simulation is unchanged. "
                "Only the exported g-code and STL shift.")
        self._build_zero_readout(F)
        self._no_layer_z_box = F.check(
            "Don't adjust height per layer", self.no_layer_z,
            "On (default): the exported g-code names its height ONCE, at the "
            "start, and never again. The collector is never told to move "
            "and the printer owns the standoff. Off: layer height equals fiber "
            "diameter, so layer i carries a Z word one fiber-diameter above "
            "layer i-1.\n\n"
            "It changes the EXPORTED SPELLING only: the g-code text view and "
            "Save-g-code-as. The simulation, the STL and the preview still "
            "solve the full stacked ladder, because at one commanded Z the "
            "fibers still sag and stack, and that is the physical result "
            "worth predicting.\n\n"
            "With \"Add " + gap + " to Z\" ticked, that one Z word is "
            "the standoff rather than zero.").field
        # ... and the per-SCAFFOLD step, which belongs to the box above: shown
        # only while it is on, because on its own it says nothing -- with the
        # per-layer step still in the file, suppressing the scaffold step would
        # leave a ladder that climbs through a part and then does not climb
        # between parts. A Row, which the sync shows and hides whole.
        self._no_scaffold_row = F.check(
            "Don't adjust height per scaffold", self.no_scaffold_z,
            "Off (default): a stack of scaffolds still names each scaffold's "
            "own start Z once, so the exported g-code steps between the PARTS "
            "even though it never steps between layers. On: the whole program "
            "keeps the one Z word it opened with.\n\n"
            "The stabilization print inherits "
            "scaffold 1's start Z, so the first Z word already covers it.\n\n"
            "Exported spelling only, exactly like the box above.",
            indent=1)
        self._no_scaffold_z_box = self._no_scaffold_row.field
        self.no_layer_z.trace_add(
            "write", lambda *_a: self._sync_no_scaffold_row())
        self._sync_no_scaffold_row()

    def _build_custom_gcode_fields(self, F):
        """The custom g-code line and the line number it goes after, as one row
        of Form `F`. Called by both Export cards (Scaffold designer and
        Calibration page) on the same two machine vars."""
        tip = ("A g-code line of your own, written into the g-code MEWracle "
               "saves and shows, for a command it has no setting for. For "
               "example a filament feeder's extrude, OCTO1 -500.\n\n"
               "\"After line\" counts lines of the saved file: 0 puts it at the "
               "very top, 2 puts it after the second line. With \"Wait to print "
               "until target temperature is reached\" on and no "
               "[[temperature_command]] in the start block, line 1 is that M109, "
               "so after line 1 runs your command once the nozzle is hot. A "
               "number past the end puts it last.\n\n"
               "Leave it empty for none. It is written exactly as typed. "
               "Nothing on screen or in the STL changes, and an imported g-code "
               "file is never edited.")
        row = F.entry("Custom g-code line", self.custom_gcode, tip,
                      width="text")
        row.add_entry("After line", self.custom_gcode_after, tip,
                      width="small")
        return row

    def _sync_no_scaffold_row(self):
        """Show "Don't adjust height per scaffold" only while the per-LAYER box
        is on. Guarded on the row's existence so a preset load or a persist
        restore that writes `no_layer_z` before the page is built (or in a
        headless test host that never builds it) is a no-op rather than an
        AttributeError. The row is a `gui_form.Row`; `grid()` with no options
        puts each of its widgets back where the builder first placed it."""
        row = getattr(self, "_no_scaffold_row", None)
        if row is None:
            return
        try:
            if self.no_layer_z.get():
                row.grid()
            else:
                row.grid_remove()
        except tk.TclError:
            # a copy of the row the Settings window drew and has since
            # closed: its trace outlives its widgets, and there is nothing
            # left to show or hide
            pass

    def _build_machine_model_group(self, parent, row, pad):
        """The 'Printer model' group: STL parts the simulation animates as the real
        machine (printhead / optional gantry / bed + static frame), the kinematics
        preset that says which part moves on which axis, the standoff/scale, the
        nozzle-tip + bed contact overrides, and the bounds-box position offset.
        Optional -- with 'Show' off (or no printhead STL) the player falls back to
        its single nozzle marker."""
        mm = ttk.LabelFrame(parent, text="Printer model (3D simulation)", padding=8)
        mm.grid(row=row, column=0, sticky="ew", **pad)
        mm.columnconfigure(1, weight=1)
        self._build_machine_stl_fields(mm, pad)
        # the calibration half BELOW the STLs' hint row, in its own frame so
        # the modern shell can put the same builder in its own card
        cal = ttk.Frame(mm)
        cal.grid(row=20, column=0, columnspan=3, sticky="w")
        self._build_machine_calibration_fields(cal, pad)
        self._build_bed_offset_row(cal, 2, pad)

    def _build_machine_stl_fields(self, mm, pad, limits_into=None):
        """The printer's own bodies: show/hide, the kinematics chain, one STL
        slot per part with its travel range, the custom chain and the STL
        scale. Gridded into `mm` through `gui_form` (rows 0-14; the modern card
        puts the rotary rows from 15).

        `limits_into` moves the per-part travel-range rows out of the file list
        into a frame of their own (rows 0-2, each named for its part). None
        keeps the classic layout, one range row under each file.

        Handles for the Instructions checklist: `_machine_stl_entries[role]` is
        each STL's entry and `_machine_kin_combo` the kinematics dropdown."""
        F = gui_form.Form(mm, stretch=True)
        # master toggle + the per-part "hide" row that belongs to it. Master off
        # -> the whole model is replaced by a single nozzle frustum; master on ->
        # each ticked part is dropped (the printhead is swapped for the frustum
        # so the nozzle still reads). The hide row greys out when the master is
        # off (it does nothing then).
        F.check("Show the printer model in the simulation", self.machine_show,
                "The parts move as the real printer instead of a marker. Turn "
                "it off for just the nozzle frustum. In the 3D viewer, press M "
                "to toggle the model.",
                command=self._sync_machine_hide_enable)
        hide = F.group("Hide", "Tick a part to drop it from the view. A hidden "
                               "printhead falls back to the nozzle frustum.",
                       indent=1)
        for text, var in (("Printhead", self.machine_hide_head),
                          ("Carriage / gantry", self.machine_hide_gantry),
                          ("Bed", self.machine_hide_bed),
                          ("Frame", self.machine_hide_frame)):
            hide.add_check(text, var, "Hide the %s in the simulation."
                           % text.lower())
        self._machine_hide_row = hide.field
        # kinematics preset: which body moves on which axis
        kin = F.combo("How it moves", self.machine_kinematics,
                      _KINEMATICS_LABELS,
                      gui_tips.GLOSSARY.get("kinematics", "") + "\n\nPick the "
                      "kinematics to match your printer, or Custom for an "
                      "arbitrary Cartesian printer. On a home-built mandrel "
                      "printer, describe its X, Y and Z: which part moves on "
                      "each. The mandrel's rotation is set by Rotary axis "
                      "letter.\n\nThis moves the printer model in Simulation "
                      "and picks which motion limits apply. The g-code is "
                      "the same whichever you pick.", width="text")
        kin.field.bind("<<ComboboxSelected>>",
                       lambda _e: (self._toggle_kinematics_custom(),
                                   self._refresh_motion_limits()))
        self._machine_kin_combo = kin.field
        # custom-chain block (axes per part + head parent) -- shown only for
        # "custom"; its rows belong to "How it moves", so they are indented
        self.machine_custom_frame, _cf = gui_form.framed(mm, F.row,
                                                         stretch=True)
        F.row += 1
        self._build_machine_custom(self.machine_custom_frame, pad)
        # STL rows -- labelled by ROLE (axes come from the kinematics preset). Each
        # moving part gets a travel-range row right beneath it, showing only the
        # axes that part drives. The vars stay axis-keyed (x=head, y=gantry,
        # z=bed) for preset back-compat.
        self._limit_axis_frames = {}
        self._limit_row_frames = {}
        L = gui_form.Form(limits_into) if limits_into is not None else F

        def limits(role, name):
            self._machine_limit_row(L, role,
                                    label=("%s travel" % name
                                           if limits_into is not None
                                           else "Travel range"),
                                    indent=(0 if limits_into is not None
                                            else 1))

        frame_tip = ("Export the parts in one shared frame (mm, Z up) so they "
                     "import assembled.")
        self._machine_stl_entries = {}
        for role, label, var, tip in (
                ("head", "Printhead STL", self.machine_x_stl,
                 "Required. The part that carries the nozzle; the tip is "
                 "found automatically."),
                ("gantry", "Carriage / gantry STL", self.machine_y_stl,
                 "Optional. The carriage or gantry, if your printer has one."),
                ("bed", "Bed STL", self.machine_z_stl,
                 "Optional. The body that carries the print."),
                ("frame", "Frame STL", self.machine_static_stl,
                 "Optional. The static frame; it never moves.")):
            r = self._machine_file_row(F, label, var, tip + "\n\n" + frame_tip)
            self._machine_stl_entries[role] = r.field
            if role == "head":
                self._printhead_stl_label = r.label
            elif role == "bed":
                self._bed_stl_label = r.label
                self._bed_stl_row = r
            if role != "frame":
                limits(role, label[:-4])
        # the printhead-elevation field goes through a hook: _SimMixin renders
        # nothing here (elevation lives on the Physics page)
        self._machine_standoff_field(F)
        F.entry("STL scale", self.machine_scale,
                "Multiplies every part STL, for parts exported in another unit "
                "(25.4 for inches). " + frame_tip)
        assert F.row <= 15, "rows 15+ belong to the modern card's rotary rows"
        self._toggle_kinematics_custom()
        self._refresh_motion_limits()
        self._sync_machine_hide_enable()

    def _build_machine_calibration_fields(self, cal, pad):
        """Where the printer's parts meet the print: the nozzle tip, the bed
        (or mandrel-holder) datum, the bed Z nudge and the collector's offset
        on the bed. Each point has its own Calibrate picker. Gridded into `cal`."""
        # contact points: nozzle-tip override (blank = auto) + bed-top Z nudge
        cp = ttk.Frame(cal)
        cp.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(cp, text="Nozzle (mm, blank = auto):").grid(
            row=0, column=0, sticky="e", **pad)
        _field(cp, "X:", self.machine_tip_x, 0, 1, pad, width=6)
        _field(cp, "Y:", self.machine_tip_y, 0, 3, pad, width=6)
        _field(cp, "Z:", self.machine_tip_z, 0, 5, pad, width=6)
        # ...and the same picker the datum row uses, pointed at the PRINTHEAD.
        # `find_tip` takes the centroid of the head STL's lowest points, which is right
        # for a bare nozzle and wrong the moment a fan shroud, duct or probe hangs
        # lower.
        ttk.Button(cp, text="Pick in 3D…", command=self._calibrate_tip).grid(
            row=0, column=7, sticky="w", **pad)
        nudge_entry = _field(cp, "Bed height fine-tune (mm):",
                             self.machine_bed_nudge, 1, 0, pad, width=6)
        gui_tips.add_tip(nudge_entry,
                         "A manual up/down shift of the bed surface in the view "
                         "only. Fine-tune it if auto-detect is slightly off. It "
                         "does not change the print.")
        # bed datum (bed STL frame): the print-surface Z + corner the slide seats on.
        # Blank -> auto-detect; the Calibrate button opens a 3D view to pick them.
        bd = ttk.Frame(cal)
        bd.grid(row=1, column=0, columnspan=3, sticky="w")
        bd_lbl = ttk.Frame(bd)
        bd_lbl.grid(row=0, column=0, sticky="e", **pad)
        self._bed_datum_label = ttk.Label(
            bd_lbl, text="Bed datum (bed STL frame, blank = auto):")
        self._bed_datum_label.pack(side="left")
        gui_tips.glossary_dot(bd_lbl, "datum").pack(side="left", padx=(4, 0))
        # THREE NUMBERS, TWO MEANINGS. On a flat collector they are the print surface's
        # Z and the corner the plate seats on. On a rotary one they are the MANDREL AXIS
        # itself, in the holder's own frame. Deliberately the same three fields, the
        # same `bed_top_z`/`bed_corner` profile keys and the same Calibrate picker;
        # `_sync_collector_words` renames them. (Built by hand rather than through
        # `_field`, which returns the entry: it is the LABELS that get renamed.)
        self._bed_datum_field_labels = []
        for col, (text, var) in enumerate(
                (("Top Z:", self.bed_datum_z),
                 ("Bed-STL corner X:", self.bed_corner_x),
                 ("Bed-STL corner Y:", self.bed_corner_y))):
            lbl = ttk.Label(bd, text=text)
            lbl.grid(row=0, column=1 + 2 * col, sticky="e", **pad)
            ttk.Entry(bd, textvariable=var, width=7).grid(
                row=0, column=2 + 2 * col, sticky="w", **pad)
            self._bed_datum_field_labels.append(lbl)
        ttk.Button(bd, text="Pick in 3D…", command=self._calibrate_bed).grid(
            row=0, column=7, sticky="w", **pad)

    def _build_bed_offset_row(self, parent, row, pad):
        """WHERE THE COLLECTOR SITS ON THE BED: its XY from the bed datum's
        corner, in the simulation only (the exported g-code is invariant to it).
        On a plate it is the plate's bottom-left corner. It is placement, not a
        pick, so the modern shell puts it on the Collector card; the bed datum
        it is measured from is picked on the calibration card.

        Ungridded for a five-axis TABLE (`_sync_rotary_enable`): there the
        collector's XY is joint 1's picked axis, and a second field saying the
        same thing is a field that can disagree with it."""
        self._slide_offset_row, F = gui_form.framed(parent, row)
        tip = self._SLIDE_OFFSET_TIPS[0]
        r = F.group(self._COLLECTOR_WORDS["_slide_offset_label"][0], tip)
        r.add_entry("X", self.slide_offset_x, tip, unit="mm")
        r.add_entry("Y", self.slide_offset_y, tip, unit="mm")
        self._slide_offset_label = r.label
        self._slide_offset_tip_row = r
        return self._slide_offset_row

    def _sync_machine_hide_enable(self):
        """Grey the per-part 'Hide' checkboxes when the printer model is off -- with
        no model drawn (just the nozzle frustum) they have nothing to act on."""
        row = getattr(self, "_machine_hide_row", None)
        if row is None:
            return                          # traced var, tab not built yet
        self._set_widgets_state(
            row, "normal" if self.machine_show.get() else "disabled")

    # -- the rotary (4-axis) collector -------------------------------------- #
    def _build_rotary_group(self, parent, row, pad):
        """The CLASSIC 'Rotary collector (4-axis)' group -- the MOUNT, and nothing else.

        A mandrel is two kinds of number and they belong in two places. The ROD is a
        consumable set by the job: its diameter and printed length come from the 4-axis
        designer, and the player installs them onto the profile at play time
        (`machine_model.RotaryAxis` states this itself). The MOUNT is a machine fact --
        which body the stage is bolted to, and how high its axis sits above that body.

        With no mount the rod is drawn at the flat-plate datum, floating wherever the
        plate corner happened to be. The axis' XY is the slide offset in the group
        above, deliberately shared, so a rotary rig registers off the bed exactly as a
        plate rig does.

        Unticked (the default) the group emits nothing at all, so a 3-axis machine.json
        is byte-identical to the one this page has always written.

        Every row below is its own section builder (`_build_rotary_*`), because the
        modern shell splits them across four cards -- the collector, the machine
        STLs, calibration and the g-code origin -- and there the tick and the kind
        radios are not shown at all: the page's 3/4/5-axis toggle sets both
        (`gui_pages._machine_mode_sync`).
        """
        # The title, the tick label and the rod row all say "mandrel" until a
        # five-axis table is picked, at which point they are wrong -- so the
        # group keeps a handle on all three and `_sync_rotary_enable` follows
        # the kind. A page describing a 5-axis table under a "(4-axis)" heading
        # is the same class of stale label as a datum that means two things.
        self._rotary_group = rg = ttk.LabelFrame(
            parent, text=_ROTARY_GROUP_TITLE["mandrel"], padding=8)
        rg.grid(row=row, column=0, sticky="ew", **pad)
        rg.columnconfigure(1, weight=1)
        on_row = ttk.Frame(rg)
        on_row.grid(row=0, column=0, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(on_row, variable=self.rotary_on,
                        text="A rotary collector stage is installed",
                        command=self._sync_rotary_enable).pack(side="left")
        gui_tips.info_dot(
            on_row,
            "Tick this when the printer has a 4th (rotary) axis fitted. It "
            "describes the STAGE, not the mandrel: the mandrel's diameter and "
            "length are part of the print and are set on the Scaffold "
            "designer's 4-axis mode, because the same stage takes any mandrel. "
            "The axis' X/Y comes from the slide offset above, the same datum "
            "a flat collector uses, so only its HEIGHT is asked for here. "
            "With the stage off, the plate above is the collector and "
            "nothing on this page changes.",
            title="Rotary collector").pack(side="left", padx=(6, 0))

        # WHICH KIND, and it sits OUTSIDE the greyed body on purpose. It was a
        # readonly Combobox inside the body, so with the stage unticked the one
        # control that says 4-axis still exists was greyed AND showed only the
        # option already selected -- so on a profile whose saved kind was the
        # 5-axis one, this page read as if the 4-axis controls were gone and
        # replaced by 5-axis ones. Two radios show BOTH
        # answers at once and stay legible with no stage installed, which is what
        # discoverable means here. The var still holds the same label strings, so
        # `rotary.kind` and every preset that carries it are unchanged.
        self._rotary_kind_row = kr = ttk.Frame(rg)
        kr.grid(row=1, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(kr, text="Collector table:").grid(row=0, column=0, sticky="e",
                                                    **pad)
        for c, label in enumerate(_ROTARY_KIND_LABELS):
            ttk.Radiobutton(kr, text=label, value=label,
                            variable=self.rotary_kind).grid(
                row=0, column=1 + c, sticky="w", **pad)
        body = ttk.Frame(rg)
        body.grid(row=2, column=0, columnspan=3, sticky="ew")
        body.columnconfigure(0, weight=1)
        gui_tips.info_dot(
            kr,
            "A mandrel is ONE rotary joint and a five-axis collector table is "
            "TWO, and they are alternatives: a printer preset carries at most "
            "two joints, a tube print needs exactly one and a curved-surface "
            "print exactly two. Pick the table here and the second joint's row "
            "appears below.\n\nThe curved-collector (5-axis) mode on the "
            "Scaffold designer cannot generate without a table declared here. "
            "That is what it means when Generate reports that the printer "
            "profile has fewer than two rotary joints.",
            title="Mandrel or collector table").grid(
            row=0, column=1 + len(_ROTARY_KIND_LABELS), sticky="w", padx=(6, 0))

        # ONE ROW PER SECTION, each builder gridding its own frame at the row it
        # is handed. Gridding a new control on a row another already holds is
        # the defect ee6c9c7 fixed once in this group already.
        self._build_rotary_mount_fields(body, 0, pad)
        self._build_rotary_joint_word_rows(body, 1, pad)
        self._build_rotary_joint_stl_rows(body, 2, pad)
        self._build_rotary_joint_calibration(body, 3, pad)
        self._build_rotary_home_row(body, 4, pad)
        self._build_rotary_axis_note(body, 5, pad)
        self._build_rotary_rod_row(body, 6, pad)
        self._build_rotary_limits_rows(body, 7, pad)
        # ROW 3 of the GROUP, not of `body`: the kind radios took row 1 and
        # `body` is row 2.
        self._build_rotary_holder_hint(rg, 3, pad)
        # `body` holds every widget the tick governs, so the group greys as one.
        self._rotary_body = body
        self._rotary_sections_built = True
        self._sync_rotary_enable()

    def _build_rotary_mount_fields(self, parent, row, pad):
        """The mandrel's rotary axis LETTER. Mandrel only; gridded as one frame
        at `row` of `parent`, and that frame IS `_rotary_mandrel_row` -- one
        frame, so the collector gate and `_sync_rotary_enable` grid the same
        widget and a disagreement between them is visible.

        NO "Stage rides" CONTROL. Whether the stage is bolted to the bed or to
        the static frame is something the kinematics chain already says: a stage
        in the bed slot moves exactly as the bed does, and
        on a rig whose bed drives no axis it holds still. Measured in the
        Player, "Frame" on such a rig put the rod ONE RADIUS below its picked
        axis, and "Bed" put it on the pick. So the page writes `role: "bed"` always."""
        wr, F = gui_form.framed(parent, row)
        self._rotary_mount_row = self._rotary_mandrel_row = wr
        self._rotary_letter_entry = F.combo(
            "Rotary axis letter", self.rotary_word, ("A", "B", "C"),
            "The letter your g-code uses for the mandrel's rotation: A turns "
            "about X, B about Y, C about Z. Use the axis name your firmware "
            "gives the mandrel's motor. Its value is in degrees and counts "
            "up through the whole print (A3600 is ten turns).\n\nA wrong "
            "letter sends every rotation to another axis, or to none. Check "
            "it with the mandrel bare before the first print.",
            width="small").field
        return wr

    def _build_rotary_joint_word_rows(self, parent, row, pad):
        """THE PAIR'S TWO JOINTS, collector-first: what each joint IS -- its word
        and the linear axis it turns about. Five-axis table only."""
        self._rotary_pair_words = pw = ttk.Frame(parent)
        pw.grid(row=row, column=0, columnspan=3, sticky="w")
        joints_help = None
        for j, (label, wvar, avar) in enumerate((
                ("Joint 1, spins the collector:", self.rotary1_word,
                 self.rotary1_axis),
                ("Joint 2, tilts it from below:", self.rotary2_word,
                 self.rotary2_axis)), start=1):
            r = j - 1
            ttk.Label(pw, text=label).grid(row=r, column=0, sticky="e", **pad)
            wf = ttk.Frame(pw)
            wf.grid(row=r, column=1, sticky="w")
            if j == 1:
                joints_help = wf          # the ⓘ goes beside the FIRST joint's
                                          # word, not alone in a far-right column
            ttk.Label(wf, text="word").grid(row=0, column=0, sticky="e", **pad)
            ttk.Entry(wf, textvariable=wvar, width=4).grid(row=0, column=1,
                                                           sticky="w", **pad)
            ttk.Label(wf, text="turns about").grid(row=0, column=2, sticky="e",
                                                   **pad)
            ttk.Combobox(wf, textvariable=avar, width=3, state="readonly",
                         values=_ROTARY_AXES).grid(row=0, column=3, sticky="w",
                                                   **pad)
        gui_tips.info_dot(
            joints_help,
            "COLLECTOR-FIRST: joint 1 is the one carrying the collector and "
            "joint 2 is the one under it. That is the order the g-code writer "
            "assumes.\n\nA word is a property of YOUR printer, "
            "not a standard: ISO 6983 names A/B/C for rotations about X/Y/Z "
            "and reserves U/V/W for secondary LINEAR axes, so a five-axis "
            "table's words have to be named here together with the axis each "
            "one turns about. Nothing in the g-code writer hardcodes them.\n\nThe "
            "defaults are the words in Terranova 2024's six published "
            "five-axis files, which are the only real ones there are. Change "
            "them to match your printer.\n\nEach joint's BODY STL is the part "
            "that turns. On a C-over-A table, that is the C plate and the A cradle. "
            "Load it, then its axis's Pick in 3D… button opens the same picker the mandrel "
            "axis uses, aimed at that body: press P on the joint's bore and "
            "creep the axis line onto its center with the arrows. That pick is "
            "the ONLY source of the rotation center; there is nowhere to type "
            "one, on purpose.\n\nThe body UNDER the table goes in the Bed STL "
            "slot, as a bed.",
            title="The two joints").grid(row=0, column=4, sticky="w",
                                         padx=(6, 0))
        return pw

    def _build_rotary_joint_stl_rows(self, parent, row, pad):
        """Each five-axis joint's BODY STL -- the part that turns."""
        self._rotary_pair_stls = ps = ttk.Frame(parent)
        # "ew" and a weighted column: the Body STL entries hold ABSOLUTE PATHS,
        # and at a natural width they show "C:\MEWron 0.2\Printer S" -- which is
        # the same for both joints, so the one thing the field exists to tell
        # you (WHICH body is loaded) is the part that gets cut off. The
        # Printhead / gantry / bed rows already stretch for this reason.
        ps.grid(row=row, column=0, columnspan=3, sticky="ew")
        ps.columnconfigure(1, weight=1)
        for j, svar in ((1, self.rotary1_stl), (2, self.rotary2_stl)):
            r = j - 1
            ttk.Label(ps, text=("Joint 1 body STL (spins):" if j == 1
                                else "Joint 2 body STL (tilts):")).grid(
                row=r, column=0, sticky="e", **pad)
            ttk.Entry(ps, textvariable=svar).grid(row=r, column=1,
                                                  sticky="ew", **pad)
            ttk.Button(ps, text="Browse…",
                       command=lambda v=svar: self._browse_machine_part(v)).grid(
                row=r, column=2, sticky="w", **pad)
        return ps

    #: The collector seat, explained once for both UIs.
    _SEAT_INFO = (
        "Where the collector stands: the point its bottom edge rests on, "
        "picked on joint 1's body.\n\nThe picker draws the collector standing "
        "on the marker, at the shape and size the Scaffold designer's 5-axis "
        "mode is set to, so you are aiming a part at a mounting face.\n\n"
        "This is not joint 1's axis. That pick is a point on the axis of "
        "rotation; this is the point the part stands on, and on any collector "
        "that is not a flat disc they are different points.\n\nUntil it is "
        "picked, the collector is placed on joint 1's axis point, so a curved "
        "collector floats above the table.")

    def _build_rotary_joint_calibration(self, parent, row, pad, seat=True):
        """The five-axis picks made ON a joint's body: each joint's axis of
        rotation, and -- with `seat` -- the collector seat on joint 1 at row 2
        of the same frame. The modern shell passes `seat=False` and puts the
        seat on its Collector card (`_build_rotary_seat_row`), because where
        the collector stands is set with the collector. The read-out of both
        axes is `_build_rotary_axis_note`."""
        self._rotary_pair_cal = pc = ttk.Frame(parent)
        pc.grid(row=row, column=0, columnspan=3, sticky="w")
        # You cannot pick an axis on a body you have not loaded, and Body and
        # Calibrate sit in different cards, so the button says so itself rather than
        # opening an empty
        # window (`_calibrate_rotary_joint`).
        for j in (1, 2):
            ttk.Label(pc, text="Joint %d axis:" % j).grid(
                row=j - 1, column=0, sticky="e", **pad)
            ttk.Button(pc, text="Pick in 3D…",
                       command=lambda n=j: self._calibrate_rotary_joint(n)).grid(
                row=j - 1, column=1, sticky="w", **pad)
        if seat:
            self._build_rotary_seat_row(pc, 2, pad)
        return pc

    def _build_rotary_seat_row(self, parent, row, pad, note=True):
        """COLLECTOR SEAT: label, Pick button and (with `note`) the classic
        read-out, as a block at `row` of `parent`. Returns the Pick button."""
        _sf, F = gui_form.framed(parent, row)
        r = F.button("Collector seat", "Pick in 3D…", self._calibrate_seat,
                     self._SEAT_INFO)
        if note:
            # A READ-OUT, not an Entry -- the same doctrine the pivots and the
            # home follow: the calibrator pick is the only writer, because three
            # free-typed numbers with no picker is what cost
            # `rotary.axis_height` its existence.
            self._rotary_seat_note = F.live(None, text="").field
        return r.field

    def _build_rotary_joint_rows(self, parent, row, pad):
        """THE FIVE-AXIS TABLE, one block per joint: its body STL, its g-code
        letter and the axis it turns about -- one fact per row, on the card's
        own edges. The modern Machine card's form of
        `_build_rotary_joint_word_rows` + `_build_rotary_joint_stl_rows`, which
        the classic group still stacks as two blocks.

        Returns the frame; `self._joint_row_widgets[n]` is `(entry, None)` for
        the caller to focus a joint's row."""
        self._rotary_pair_words = self._rotary_pair_stls = pj = ttk.Frame(parent)
        pj.grid(row=row, column=0, columnspan=4, sticky="ew")
        F = gui_form.Form(pj, stretch=True)
        about = ("A five-axis collector table has two rotary joints. Joint 1 "
                 "carries the collector and spins it; joint 2 is underneath and "
                 "tilts it. The body under the table goes in the Bed STL slot.")
        self._joint_row_widgets = {}
        for j, (verb, wvar, avar, svar) in enumerate((
                ("spins the collector", self.rotary1_word, self.rotary1_axis,
                 self.rotary1_stl),
                ("tilts it, underneath", self.rotary2_word, self.rotary2_axis,
                 self.rotary2_stl)), start=1):
            r = self._machine_file_row(
                F, "Joint %d STL" % j, svar,
                "Joint %d %s. Load the STL of the part that turns. " % (j, verb)
                + about)
            r2 = F.combo("Joint %d letter" % j, wvar, (), (
                "The letter your g-code uses for joint %d. Letters are your "
                "printer's choice: there is no standard for a five-axis table."
                % j), readonly=False, width="small", indent=1)
            r2.add_combo("turns about", avar, _ROTARY_AXES,
                         "The printer axis joint %d turns about." % j,
                         width="small")
            self._joint_row_widgets[j] = (r.field, None)
        return pj

    def _build_rotary_limits_rows(self, parent, row, pad):
        """WHAT THE TWO ROTARIES CAN PHYSICALLY DO. Five-axis table only;
        gridded as one frame at `row` of `parent`, and returned so the caller
        can register it for the "surface" collector like every other pair-only
        block.

        RIG FACTS, which is why they are on this page and not on the
        Curved-collector designer where the limiter that consumes them was
        written. This page's preset kind is the one a rig is described by
        (`gui_presets`: which persist keys belong to "machine" is decided by
        `_machine_persist_keys`), and preset ownership follows the TAB the
        field lives on. Left on the designer they could not ride the 5 Axis PhD
        Printer preset at all, so the number would have to be retyped per
        scaffold preset -- and a rig limit retyped per design is a rig limit
        that is eventually wrong.

        The defaults and the persist keys both come from
        `rotary_limits.RIG_LIMIT_FIELDS`, which is also what the verification
        matrix generates with, so the suite's files are the files a user gets.
        """
        rl, F = gui_form.framed(parent, row)
        why = (
            "Every five-axis file Generate writes is held inside the rotary "
            "limits, on the clock the controller will actually run it with: no "
            "move in the file asks either joint for more.\n\n"
            "Staying perpendicular to a curved collector costs rotary "
            "acceleration that grows with the SQUARE of the print speed, so a "
            "turnaround at 800 mm/min can ask for thousands of deg/s², "
            "and a collector that whips like that breaks the jet. Where "
            "the surface asks for more than these allow, Generate first slows "
            "the feed, never below the Min print speed in the Scaffold "
            "designer's 5-axis mode. Only then does it let the nozzle leave "
            "the perpendicularity tolerance.")
        unmeasured = ("\n\n0 means not limited. No rate and no maximum feed "
                      "has been measured on either joint, so these ship at 0. "
                      "Fill one in when you know it and save the printer "
                      "preset.")
        accel = ("\n\n720 deg/s² is the printer's recorded limit for the SPIN "
                 "(C) axis; the same figure is used for the tilt (A) axis, "
                 "where nothing has been measured.")
        F.section("Rotary limits", first=False)
        F.entry("Spin rate", self.rot_max_spin_rate,
                "The fastest joint 1 may turn. " + why + unmeasured,
                unit="deg/s")
        F.entry("Spin accel", self.rot_max_spin_accel,
                "The hardest joint 1 may accelerate. " + why + accel,
                unit="deg/s²")
        F.entry("Tilt rate", self.rot_max_tilt_rate,
                "The fastest joint 2 may turn. " + why + unmeasured,
                unit="deg/s")
        F.entry("Tilt accel", self.rot_max_tilt_accel,
                "The hardest joint 2 may accelerate. " + why + accel,
                unit="deg/s²")
        F.entry("Max feed", self.rot_max_feed,
                "The fastest feed any five-axis move may ask for. " + why
                + unmeasured, unit="mm/min")
        return rl

    def _build_rotary_home_row(self, parent, row, pad, note=True):
        """G-CODE ZERO on a five-axis table -- INTERIM: where the nozzle tip is
        at G0 X0 Y0 Z0, picked on joint 1's body. `note=False` is the modern
        card: no read-out (the Instructions checklist carries the status)."""
        self._rotary_home_row, F = gui_form.framed(parent, row)
        tip = ("Where the nozzle is at X0 Y0 Z0 in your g-code. Zero your "
               "printer here before printing. This is not the position your "
               "printer goes to after homing.\n\n"
               "Pick in 3D opens joint 1's body with the collector drawn on "
               "its seat: put the marker where the nozzle should start. That "
               "is often at the collector's edge or off the part, so the "
               "travel fiber does not cross the print.\n\n"
               "Until it is picked, the nozzle starts on the collector's own "
               "origin, which on a curved collector is inside the part.\n\n"
               "Scaffold Start X/Y are measured from this point.\n\n"
               "Interim: a proper homing method for five-axis tables is still "
               "to be designed.")
        r = F.button("G-code zero", "Pick in 3D…", self._calibrate_home, tip)
        self._rotary_home_button = r.field
        self._rotary_home_host = self._rotary_home_row
        # A READ-OUT, not an Entry. Same doctrine as the two pivots: the
        # calibrator pick is the only writer, and there is nowhere to type
        # three numbers that place the nozzle.
        self._rotary_home_note = None
        if note:
            self._rotary_home_note = F.live(None, text="").field
        return self._rotary_home_row

    def _build_rotary_axis_note(self, parent, row, pad):
        """WHERE THE AXIS IS -- a read-out, not a knob. Both rotary kinds."""
        # An "Axis height above the holder's top face" field lived here and was
        # a guess on top of `find_bed_top_face`, which looks for a flat plane a
        # U-bracket holder has not got: right on the holder it was tuned for and
        # wrong on any other. A plausible number in this slot is what let a STALE
        # datum inherited from a previously loaded bed STL sit in the shipped
        # preset unnoticed. So the single source is the datum, and the
        # un-calibrated case is SAID rather than filled in.
        hr = ttk.Frame(parent)
        hr.grid(row=row, column=0, columnspan=3, sticky="w")
        self._rotary_axis_note_row = hr
        # Wrapped: the uncalibrated sentence is the longest thing in this group
        # (it names both joints and what happens until they are picked), and a
        # read-out that runs off the pane is a read-out nobody finishes reading.
        self._axis_datum_note = ttk.Label(hr, text="", wraplength=620,
                                          justify="left")
        self._axis_datum_note.grid(row=0, column=0, sticky="w", **pad)
        # STL scale vs a picked rotation centre -- refused by `MachineProfile`,
        # so it is said here first. Gridded only when it applies (row 1 of `hr`,
        # under the read-out it qualifies).
        self._rotary_scale_warn = ttk.Label(hr, foreground="#c0392b",
                                            wraplength=620, justify="left")
        self._rotary_scale_warn.grid(row=1, column=0, sticky="w", **pad)
        self._rotary_scale_warn.grid_remove()
        self.machine_scale.trace_add(
            "write", lambda *_a: self._sync_pair_scale_warning())
        gui_tips.info_dot(
            hr,
            "AN AXIS OF ROTATION HAS EXACTLY ONE SOURCE HERE: a pick in the "
            "3D picker window, on the body that turns. There is nowhere to "
            "type one, on purpose: three free-typed numbers with no calibrator "
            "cannot be checked.\n\nMANDREL: the mandrel-axis datum row, picked on the "
            "holder. That window is also where the mandrel is POSITIONED. It "
            "draws the mandrel at your diameter on the holder and lets you nudge "
            "it onto the seat, so you can see where the mandrel ends up "
            "without generating anything.\n\nCOLLECTOR TABLE: each joint's own "
            "axis, picked on that joint's body. The bed datum is "
            "then just a bed again.",
            title="Where the axis is").grid(row=0, column=1, sticky="w",
                                            padx=(6, 0))
        return hr

    def _build_rotary_rod_row(self, parent, row, pad):
        """THE ROD. Its length and where its near end sits along the axis -- the
        rotary answer to the plate's size + Corner X/Y, and the reason a tube can
        be zeroed on a rod END rather than on its own first fiber. Mandrel only."""
        self._rotary_rod_row, F = gui_form.framed(parent, row)
        tip = ("The MANDREL, as a physical object, measured in the print's own "
               "frame (the holder's mandrel-axis datum puts that frame on the "
               "printer). The 4-axis designer then decides where along it to "
               "print, and G-code zero is a point on this mandrel.")
        # The diameter is the design's (the winding maths needs it, and the
        # scaffold preset keeps it), shown here too so the mandrel is set up in
        # one place: the same variable as Design > Collector, so the two
        # always read alike.
        make = getattr(self, "_mandrel_vars_make", None)
        if make is not None:
            make()
        if hasattr(self, "m_diameter"):
            F.entry("Mandrel diameter", self.m_diameter,
                    "The mandrel's outside diameter. The same number as "
                    "Design > Collector: change it in either place. It is "
                    "saved with the design, not the printer, because the "
                    "winding is worked out for it. " + tip, unit="mm")
        F.entry("Mandrel length", self.rotary_rod_length,
                "How long the mandrel is. " + tip, unit="mm")
        F.entry("Mandrel near end X", self.rotary_rod_x,
                "Where the mandrel's near (-X) end sits along the axis. " + tip,
                unit="mm")
        return self._rotary_rod_row

    def _build_rotary_holder_hint(self, parent, row, pad):
        """The one-line reminder that a 4-axis rig's holder goes in the bed slot.
        Mandrel only."""
        self._rotary_holder_hint = hint = ttk.Frame(parent)
        hint.grid(row=row, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(hint, foreground="#777",
                  text="The HOLDER goes in the Bed STL slot, and its datum is "
                       "the mandrel axis.").pack(
            side="left")
        gui_tips.info_dot(
            hint,
            "A 4-axis printer has no plate, so the body that carries the "
            "print IS the mandrel holder: load it into the Bed STL slot and it "
            "rides the collector chain like any bed. Then the bed-datum row "
            "stops meaning a top face and a corner and starts meaning the "
            "MANDREL AXIS in that STL's own frame. Use Pick in 3D… to place it. That "
            "one point places the mandrel completely: its X/Y put the axis under "
            "the printhead and its Z sets the nozzle gap. The mandrel's diameter is "
            "in the Collector card above, and in Design > Collector.",
            title="Where the mandrel is").pack(side="left", padx=(6, 0))
        return hint

    #: (flat, MANDREL, five-axis TABLE) wording for every widget whose MEANING
    #: changes with the collector. The bed slot and its datum are not renamed for
    #: cosmetics: on a 4-axis rig there is no bed, the body in that slot is the
    #: mandrel holder, and the three datum numbers stop being a top face plus a
    #: corner and become the mandrel AXIS in that STL's frame.
    #:
    #: A five-axis TABLE takes the FLAT words back, unchanged -- column 2 is a
    #: copy of column 0 rather than a third wording, and that is the point. The
    #: two rotary BODIES have their own STL slots and their own picked axes in
    #: the group below, so the body in the bed slot is whatever is UNDER the
    #: table: on the development rig the Aerotech X plate, a real bed with a real top
    #: face and a real corner. Naming it "Collector table STL (carries the
    #: collector)" would leave TWO slots
    #: both claiming to carry the collector, which is exactly the redundancy
    #: this page has been bitten by. One body, one slot, one meaning.
    _COLLECTOR_WORDS = {
        "_bed_stl_label": ("Bed STL:",
                           "Mandrel holder STL:",
                           "Bed STL:"),
        "_bed_datum_label": ("Bed top face (blank = auto):",
                             "Mandrel axis (on the holder):",
                             "Bed top face (blank = auto):"),
        # the pair column is never shown: a table has no bed offset
        "_slide_offset_label": ("Position on the bed:",
                                "Offset from the axis datum:",
                                "Position on the bed:"),
    }

    #: The collector offset's tooltip, in the same three columns: what it is
    #: measured FROM changes with the collector. The five-axis column is never
    #: shown (a table has no offset row).
    _SLIDE_OFFSET_TIPS = (
        "Where the plate sits on the printer: its bottom-left corner, measured "
        "from the corner picked on the bed's top face (Calibrate → Bed top "
        "face). It places the collector under your printer model in the 3D "
        "simulation, so it does nothing until a printhead STL is loaded "
        "(Printer model card). The exported g-code is measured from G-code "
        "zero, which rides the collector.",
        "Where the mandrel sits, measured from its axis datum (Calibrate → "
        "Mandrel axis). It places the collector under your printer model in "
        "the 3D simulation, so it does nothing until a printhead STL is "
        "loaded (Printer model card). The exported g-code is measured from "
        "G-code zero, which rides the collector.",
        "A five-axis table has no offset: the collector's XY is joint 1's "
        "picked axis.")

    #: The bed slot's tooltip, in the same three columns: what the body in the
    #: slot IS changes with the collector, so its hover text does too.
    _BED_STL_TIPS = (
        "Optional. The body that carries the print.",
        "A 4-axis printer has no plate, so the body that carries the print IS "
        "the mandrel holder: load it here and it rides the collector chain like "
        "any bed. Its datum on the Calibrate card is then the MANDREL AXIS in "
        "this STL's own frame, and that one point places the mandrel. The mandrel's "
        "diameter is on the 4-axis designer.",
        "The body UNDER the collector table: a real bed with a top face and a "
        "corner. The two rotary joints have their own STL slots.")
    _BED_DATUM_WORDS = (("Top Z (mm):", "Axis Z (mm):", "Top Z (mm):"),
                        ("Corner X (mm):", "Axis X (mm):", "Corner X (mm):"),
                        ("Corner Y (mm):", "Axis Y (mm):", "Corner Y (mm):"))

    def _sync_collector_words(self):
        """Rename the bed slot and its datum when a rotary stage is installed.

        THE SLOT IS THE SAME SLOT, deliberately. A 4-axis rig has no build plate, so the
        body that carries the print is the mandrel holder, and it belongs on the
        collector chain exactly where a bed does -- one part slot, one datum, one
        Calibrate button, and `machine.json` unchanged. What differs is only what the
        three datum numbers MEAN, and that is what these words say.
        """
        # 0 flat, 1 mandrel, 2 five-axis table -- see _COLLECTOR_WORDS.
        i = 0
        if self.rotary_on.get():
            i = 2 if self._rotary_kind_key() == "pair" else 1
        for attr, words in self._COLLECTOR_WORDS.items():
            w = getattr(self, attr, None)
            if w is not None:
                w.configure(text=words[i])
        off = getattr(self, "_slide_offset_tip_row", None)
        if off is not None:
            off.set_tip(self._SLIDE_OFFSET_TIPS[i])
        bed = getattr(self, "_bed_stl_row", None)
        if bed is not None:
            bed.set_tip(self._BED_STL_TIPS[i] + "\n\nExport the parts in one "
                        "shared frame (mm, Z up) so they import assembled.")
        for lbl, words in zip(getattr(self, "_bed_datum_field_labels", ()),
                              self._BED_DATUM_WORDS):
            lbl.configure(text=words[i])
        self._sync_axis_datum_note()

    def _sync_axis_datum_note(self):
        """Say whether the mandrel axis has been located.

        There is no silent fallback, so this read-out IS the no-datum behaviour: an
        uncalibrated rotary rig draws its rod on the holder's auto-detected top face,
        which is almost certainly not where the mandrel sits.
        `gcode_player._log_placement` prints the same thing on the run log."""
        note = getattr(self, "_axis_datum_note", None)
        if note is None:
            return
        if not self.rotary_on.get():
            note.configure(text="", foreground="")
            return
        if self._rotary_kind_key() == "pair":
            self._sync_pair_axis_note()
            return
        vals = [self.bed_datum_z.get().strip(), self.bed_corner_x.get().strip(),
                self.bed_corner_y.get().strip()]
        if all(vals):
            note.configure(
                text="Mandrel axis: Z %s, X %s, Y %s (from the datum above)"
                     % tuple(vals),
                foreground="#2e7d32")
        else:
            note.configure(
                text="Mandrel axis NOT located. Use Pick in 3D… on the holder. "
                     "Until then the mandrel is drawn on the holder's top face.",
                foreground="#b23b00")

    #: `(word_var, axis_var, stl_var, pivot_var)` per joint, COLLECTOR-FIRST --
    #: the order `rotaries` is written in and the order `place_scene` reads. One
    #: tuple rather than four parallel lookups, because a pair is walked in four
    #: places (the widgets, the read-out, the calibrator and the profile) and
    #: four hand-written joint-1/joint-2 pairs is four chances to swap them.
    def _rotary_pair_vars(self):
        self._rotary_vars_make()
        return ((self.rotary1_word, self.rotary1_axis, self.rotary1_stl,
                 self.rotary1_pivot),
                (self.rotary2_word, self.rotary2_axis, self.rotary2_stl,
                 self.rotary2_pivot))

    @staticmethod
    def _parse_pivot(text):
        """`(z, x, y)` from a stored pivot string, or None if it is blank or
        unparseable. Blank IS the uncalibrated state, so an unreadable value is
        treated as uncalibrated rather than as a zero pivot: a pivot silently
        taken as the origin is `RotaryAxis`'s own named failure -- geometry that
        is plausible and wrong everywhere except on the axis itself."""
        try:
            z, x, y = (float(v) for v in str(text).strip().split(","))
        except (ValueError, AttributeError):
            return None
        return (z, x, y)

    def _sync_pair_axis_note(self):
        """The five-axis table's read-out: where each joint's axis of rotation
        is, or which joint still has none.

        This is the SLOT the mandrel's "axis located / NOT located" line
        occupies, doing the same job for two joints. It replaces an orange
        "UNCALIBRATED — two assumptions" note that said the collector was
        assumed concentric with joint 1's spin axis and joint 2's tilt axis
        assumed through the collector origin. Both statements stop being true
        once a pivot per joint is picked and `machine_model` relativizes the
        pair to joint 1: joint 1's pivot is then identically zero in the
        collector's frame (structural, not assumed) and joint 2's is the real
        axis-to-axis offset. A disclosure that has been superseded is worse than
        none -- it reads as a live caveat."""
        note = getattr(self, "_axis_datum_note", None)
        if note is None:
            return
        lines, missing = [], []
        for n, (wvar, _avar, svar, pvar) in enumerate(self._rotary_pair_vars(),
                                                      start=1):
            word = (wvar.get() or "").strip().upper() or str(n)
            got = self._parse_pivot(pvar.get())
            if got is None:
                missing.append("%s (joint %d)" % (word, n))
            else:
                # THE BODY IS NAMED BESIDE ITS NUMBERS. The two picks are in two
                # DIFFERENT files' frames, so three numbers on their own do not
                # say which body they belong to -- and on the development rig,
                # where every STL shares one frame, the two triples look alike
                # enough to swap without noticing.
                body = os.path.basename(svar.get().strip())
                lines.append("%s axis: Z %.4f, X %.4f, Y %.4f%s"
                             % ((word,) + got +
                                ((" (in %s)" % body) if body else "",)))
        if missing:
            # "DOES NOT TURN AT ALL", not "turns about the STL origin". That is
            # the safer behaviour: a joint with no picked pivot contributes no
            # rotation to its own body OR to anything mounted on it, because
            # turning a body about its file's origin would fling it across the
            # scene at a plausible-looking angle. Pinned by
            # `test_machine_model.test_an_unpicked_joint_turns_no_body_and_a_
            # mandrel_holder_never_moves`, and `gcode_player`'s run log says the
            # same sentence per joint so the page and the log cannot drift.
            note.configure(
                text="Rotation center NOT picked for %s. Load that body's STL "
                     "on the Printer model card and pick its axis. Until then that joint does not "
                     "turn at all, and neither does anything mounted on it."
                     % ", ".join(missing),
                foreground="#b23b00")
        else:
            note.configure(
                text="  ·  ".join(lines) + "   (picked on each body, in that "
                                           "body's own STL frame)",
                foreground="#2e7d32")
        self._sync_pair_scale_warning()
        # The home read-out lives in the pair's own row, so it is settled from
        # the one place that runs whenever the pair is on screen. Its variable
        # also traces to it, so a pick updates it without waiting for this.
        self._sync_pair_home_note()
        self._sync_pair_seat_note()

    def _sync_pair_seat_note(self):
        """Say where the collector stands, or that it will fall back to joint
        1's axis point because nothing has been picked.

        Two states, and the unpicked one names the CONSEQUENCE rather than just
        reporting a blank -- the same rule the uncalibrated-axis note follows.
        The consequence is the defect this field was added for: with no seat,
        the collector's own origin is put on the axis point, and on a dome that
        origin is the centre of curvature, so the part hangs above the table by
        however far its bottom edge sits up its own axis."""
        note = getattr(self, "_rotary_seat_note", None)
        if note is None:
            return
        got = self._parse_pivot(self.rotary_seat.get())
        if got is None:
            note.configure(
                text="NOT picked. The collector falls back to joint 1's own "
                     "axis point, which is a point on the axis of ROTATION "
                     "rather than the face it stands on, so a curved collector "
                     "floats above the body. Pick in 3D… to seat it.",
                foreground="#b23b00")
            return
        note.configure(
            text="collector's bottom edge rests at:  Z %.4f, X %.4f, Y %.4f"
                 "   (picked on joint 1's body, in that body's own STL frame)"
                 % got,
            foreground="#2e7d32")

    def _sync_pair_home_note(self):
        """Say where the g-code home is, or that the nozzle will start on joint
        1's own axis point because nothing has been picked. INTERIM.

        Three states, and the third is the one that matters. Picked and CONSUMED
        is green. Not picked is orange and names the consequence, the way the
        uncalibrated-axis note does. Picked and NOT consumed -- a build whose
        `machine_model` does not yet read `rotaries[0]["home"]` -- is orange and
        says so out loud, because a control that silently does nothing is worse
        than one that admits it: the user picks a point, the nozzle does not
        move, and the picker is what gets blamed.

        The consumption test is a real capability check on the module that would
        do the consuming, not a version string: `RotaryAxis` either carries the
        field or it does not."""
        note = getattr(self, "_rotary_home_note", None)
        if note is None:
            return
        got = self._parse_pivot(self.rotary_home.get())
        if got is None:
            note.configure(
                text="NOT picked. The nozzle starts on the collector's own "
                     "origin, which on a curved collector is a point inside "
                     "the part. Pick in 3D… to place it, and pick the collector "
                     "seat first so the picker can draw what you are aiming "
                     "at.",
                foreground="#b23b00")
            return
        where = "nozzle at G0 X0 Y0 Z0:  Z %.4f, X %.4f, Y %.4f" % got
        if _home_is_consumed():
            note.configure(text=where + "   (picked on joint 1's body, in that "
                                        "body's own STL frame)",
                           foreground="#2e7d32")
        else:
            note.configure(
                text=where + ".   PICKED BUT NOT YET USED: this build's "
                             "printer model does not read G-code zero, so the "
                             "printer view still starts the nozzle on joint 1's "
                             "axis point.",
                foreground="#b23b00")

    #: What to say when STL scale and a picked rotation centre are both set.
    #: ONE string, shown live under the joints AND raised at dispatch, so the
    #: warning and the refusal cannot say different things.
    _PIVOT_SCALE_MSG = (
        "A rotation center is a point in MILLIMETERS, so a printer model with "
        "STL scale %s cannot carry one.\n\nThe pivot travels down two chains "
        "that scale differently: the body is drawn in STL coordinates and takes "
        "the scale, while the collector chain is the same arithmetic the "
        "generator wrote the g-code with and the converter reads it back with, "
        "and neither of those has a scale to apply. They agree exactly at scale "
        "1.\n\nExport the body STLs in mm and set STL scale to 1. (A scaled "
        "model with no picked rotation center is unaffected.)")

    def _pivot_scale_conflict(self):
        """The STL scale, if it is not 1 AND a pair has any picked pivot; else
        None.

        `machine_model.MachineProfile` REFUSES that combination outright,
        so without this the user meets it as an
        exception at play time. The page owns the field, so the page says it
        first -- the same shape as the custom-chain orphan warning, which is
        live under its table and repeated at dispatch."""
        if not (self.rotary_on.get() and self._rotary_kind_key() == "pair"):
            return None
        if not any(self._parse_pivot(p.get()) is not None
                   for *_r, p in self._rotary_pair_vars()):
            return None
        raw = (self.machine_scale.get() or "").strip()
        try:
            if float(raw) == 1.0:
                return None
        except ValueError:
            return None                     # unparseable -> _machine_args uses 1.0
        return raw

    def _sync_pair_scale_warning(self):
        """Show the scale/pivot conflict live, under the two joints."""
        lbl = getattr(self, "_rotary_scale_warn", None)
        if lbl is None:
            return
        raw = self._pivot_scale_conflict()
        if raw is None:
            lbl.config(text="")
            lbl.grid_remove()
            return
        lbl.config(text="⚠ STL scale is %s. A rotation center is a point in "
                        "millimeters and cannot be scaled. Export the bodies "
                        "in mm and set STL scale to 1, or the printer model "
                        "will be refused." % raw)
        lbl.grid()

    def _sync_rotary_enable(self):
        """Grey the mount fields when no rotary stage is installed -- the same pattern
        the per-part Hide row uses when the printer model is off.

        Driven by a TRACE on `rotary_on`, not only by the checkbutton's command. A
        `command` fires on a click and says nothing when the value is SET, so a preset
        with the stage ticked loaded the tick and left the whole group greyed until the
        user toggled it off and on again. `_apply_all_toggles` re-settles exactly this
        class after a bulk field change and simply did not list this group.

        Callable before the tab exists: the vars are lazy (`_rotary_vars_make`) and are
        read from the 4-axis designer, so a preset can land on them with no widgets
        built. `_build_rotary_group` ends by calling this."""
        if not getattr(self, "_rotary_sections_built", False):
            return                          # tab not built yet; it syncs on build
        state = "normal" if self.rotary_on.get() else "disabled"
        body = getattr(self, "_rotary_body", None)
        if body is not None:                # the classic group greys as one
            self._set_widgets_state(body, state)
        # One kind at a time: a mandrel row and a pair row on screen together is
        # a form where half the fields do not reach the profile.
        pair = self._rotary_kind_key() == "pair"
        # A five-axis table has no ROD and no mandrel holder, so the rod row and
        # the holder hint go with the mandrel word. Leaving them on screen for a
        # pair is the same defect as leaving both word rows up: fields that
        # cannot reach the profile the page is writing.
        #
        # THE MODERN CARDS OPT OUT (`_rotary_rows_by_mode`): there the same rows
        # are registered with the page's collector toggle, which is what sets
        # rotary_on and the kind in the first place, and two managers gridding
        # one frame disagree the moment their traces fire in the other order.
        rows = () if getattr(self, "_rotary_rows_by_mode", False) else (
                            (getattr(self, "_rotary_mandrel_row", None), not pair),
                            (getattr(self, "_rotary_rod_row", None), not pair),
                            (getattr(self, "_rotary_holder_hint", None), not pair),
                            (getattr(self, "_rotary_pair_words", None), pair),
                            (getattr(self, "_rotary_pair_stls", None), pair),
                            (getattr(self, "_rotary_pair_cal", None), pair),
                            (getattr(self, "_rotary_home_row", None), pair),
                            # ONE source for the collector's XY. On a pair that
                            # is joint 1's picked axis, so the slide offset --
                            # which lives in the Printer-model group above and
                            # is not governed by this tick -- is taken off the
                            # page and emitted as zero (`_machine_args`).
                            (getattr(self, "_slide_offset_row", None),
                             not (pair and self.rotary_on.get())))
        for frame, want in rows:
            if frame is None:
                continue
            if want:
                frame.grid()
            else:
                frame.grid_remove()
        grp = getattr(self, "_rotary_group", None)
        if grp is not None:
            grp.configure(
                text=_ROTARY_GROUP_TITLE["pair" if pair else "mandrel"])
        # the datum ladders live in the origin group but are governed by the
        # same tick -- there is no rotary zero without a rotary stage
        row = getattr(self, "_rotary_origin_row", None)
        if row is not None:
            self._set_widgets_state(row, state)
        self._sync_collector_words()

    def _machine_limit_row(self, F, role, label="Travel range", indent=0):
        """A travel-range row for one moving part, on Form `F`: per-axis min/max
        entry pairs. All three axes are built; _refresh_motion_limits grids only
        the ones the part drives under the current kinematics (so the row tracks
        the chosen rig)."""
        tip = ("How far this part can travel on each axis it drives, "
               "simulation only: a body never draws past its rails.")
        r = F.group(label, tip, unit="mm", indent=indent)
        # GRIDDED, not packed, in the row's group frame: the refresh shows and
        # hides one axis at a time, and grid() puts it back in its own column
        for c, ax in enumerate(("x", "y", "z")):
            mn, mx = self.machine_limits[(role, ax)]
            axf = ttk.Frame(r.host())
            gui_form.mark(axf, "group")
            for w in (gui_form.mark(ttk.Label(axf, text=f"{ax.upper()}:"),
                                    "sublabel"),
                      gui_form.mark(ttk.Entry(axf, textvariable=mn, width=6),
                                    "field"),
                      gui_form.mark(ttk.Label(axf, text="–"), "sublabel"),
                      gui_form.mark(ttk.Entry(axf, textvariable=mx, width=6),
                                    "field")):
                w.pack(side="left", padx=(2, 1))
            axf.grid(row=0, column=c, sticky="w",
                     padx=((0 if c == 0 else gui_form.PAD["padx"]), 0))
            r._tip(axf, tip)
            self._limit_axis_frames[(role, ax)] = axf
        self._limit_row_frames[role] = r

    def _driven_axes(self, role):
        """The axes the given part drives under the current kinematics -- from the
        preset's CHAINS entry, or the ticked custom-axis checkboxes for 'custom'."""
        key = self._machine_kinematics_key()
        if key == "custom":
            return tuple(a for a, v in zip("xyz", self._machine_axis_vars[role])
                         if v.get())
        base = machine_model.CHAINS.get(key)
        return tuple(base["axes"].get(role, ())) if base else ()

    def _refresh_motion_limits(self, *_):
        """Show each part's Motion-limits entries only for the axes it drives, and hide
        a part's whole row when it drives nothing (e.g. a gantry on the XYZ-head rig)."""
        if getattr(self, "_limit_axis_frames", None) is None:
            return                          # traced var, tab not built yet
        for role in ("head", "gantry", "bed"):
            driven = self._driven_axes(role)
            for ax in ("x", "y", "z"):
                axf = self._limit_axis_frames.get((role, ax))
                if axf is None:
                    continue
                (axf.grid() if ax in driven else axf.grid_remove())
            fr = self._limit_row_frames.get(role)
            if fr is not None:
                (fr.grid() if driven else fr.grid_remove())

    def _build_machine_custom(self, parent, pad):
        """The custom-chain rows: for each part (head/gantry/bed) tick the axes
        it translates on; the head OR the bed can ride the gantry (or attach
        straight to the frame). Only consulted when kinematics == 'custom' (the
        presets define their own chain). Defaults reproduce the MEWron chain.
        Built with `gui_form` into `parent`, one indent in: every row belongs to
        "How it moves"."""
        F = gui_form.Form(parent)
        labels = {"head": "Printhead", "gantry": "Gantry", "bed": "Bed"}
        for part in ("head", "gantry", "bed"):
            g = F.group("%s moves on" % labels[part],
                        "Tick each axis the %s translates on."
                        % labels[part].lower(), indent=1)
            for ax, axv in zip("XYZ", self._machine_axis_vars[part]):
                g.add_check(ax, axv, "The %s moves on %s."
                            % (labels[part].lower(), ax),
                            command=self._on_machine_custom_change)
        # the parent choices: the gantry is the one intermediate body and mounts on
        # the frame; EITHER the head rides it or the bed does. See the same note in
        # _build_machine_model_group for why an orphaned gantry never moves.
        hc = F.combo("Printhead rides", self.machine_head_parent,
                     ("gantry", "frame"),
                     "What the printhead is mounted on.", width="num",
                     indent=1).field
        hc.bind("<<ComboboxSelected>>", self._on_machine_custom_change)
        bc = F.combo("Bed rides", self.machine_bed_parent, ("frame", "gantry"),
                     "What the bed is mounted on.", width="num",
                     indent=1).field
        bc.bind("<<ComboboxSelected>>", self._on_machine_custom_change)
        # live orphan warning (a driving body on neither chain -> lost motion)
        self.machine_custom_warn = F.live(None, warn=True, indent=1).field
        self.machine_custom_warn.configure(foreground="#c0392b")
        self._refresh_machine_custom_warning()

    def _on_machine_custom_change(self, *_):
        """A custom-chain edit: re-grade the motion-limit rows AND the orphan warning."""
        self._refresh_motion_limits()
        self._refresh_machine_custom_warning()

    def _toggle_kinematics_custom(self):
        """Show the custom-chain block only when the 'custom' preset is selected."""
        frame = getattr(self, "machine_custom_frame", None)
        if frame is None:
            return                          # traced var, tab not built yet
        if self._machine_kinematics_key() == "custom":
            frame.grid()
        else:
            frame.grid_remove()

    def _machine_kinematics_key(self):
        """The machine_model kinematics key for the selected dropdown label."""
        return _KINEMATICS_KEY.get(self.machine_kinematics.get(), "mewron")

    def _machine_custom_orphans(self):
        """{role:[axes]} orphaned under the CURRENT custom-chain selection, else {} (a
        driving body on neither chain -> its axis vanishes from the scene). Empty unless
        kinematics is 'custom'. Single-sources the check via machine_model so the tab
        warning and the generate/play guard agree."""
        if self._machine_kinematics_key() != "custom":
            return {}
        axes = {part: [a for a, v in zip("xyz", vs) if v.get()]
                for part, vs in self._machine_axis_vars.items()}
        parents = {"head": self.machine_head_parent.get().strip() or "gantry",
                   "gantry": "frame",
                   "bed": self.machine_bed_parent.get().strip() or "frame"}
        try:
            prof = machine_model.MachineProfile.from_dict(
                {"parts": {"head": "h.stl"}, "kinematics": "custom",
                 "axes": axes, "parents": parents})
            return prof.orphaned_axes()
        except Exception:  # noqa: BLE001
            return {}

    def _refresh_machine_custom_warning(self, *_):
        """Show/hide the live orphan warning under the custom-chain table."""
        lbl = getattr(self, "machine_custom_warn", None)
        if lbl is None:
            return
        orphans = self._machine_custom_orphans()
        if orphans:
            who = ", ".join(f"{r} ({'/'.join(a).upper()})"
                            for r, a in orphans.items())
            lbl.config(text=(f"⚠ {who} drives an axis but rides neither the "
                             "printhead nor the bed chain, so that motion will not "
                             "render. Set 'Printhead rides: gantry' or 'Bed rides: "
                             "gantry'."))
            lbl.grid()
        else:
            lbl.config(text="")
            lbl.grid_remove()

    def _machine_file_row(self, F, label, var, tip):
        """One 'label / path entry / Browse' row for a machine-part STL, on Form
        `F`. Returns the Row; its LABEL is renamed at runtime on the bed slot,
        because a 4-axis rig has no bed and that slot carries the mandrel HOLDER
        (`_sync_collector_words`)."""
        return F.file(label, var, tip,
                      lambda: self._browse_machine_part(var))

    def _browse_machine_part(self, var):
        path = filedialog.askopenfilename(
            title="Select printer-part STL",
            filetypes=[("STL files", "*.stl"), ("All files", "*.*")])
        if path:
            var.set(path)

    def _machine_standoff_field(self, F):
        """Render the printhead-elevation row on the Printer-model Form `F`.

        Unoverridden default: a row bound to machine_standoff. _SimMixin
        overrides this to render nothing, which is what happens in the
        assembled App (elevation lives on the Physics page)."""
        F.entry("Nozzle–collector gap", self.machine_standoff,
                "The nozzle's hover gap above the layer.", unit="mm")

    def _standoff_var(self):
        """The StringVar holding the printhead elevation (mm) -- the single source the
        optional g-code Z bump (_elevation_value) and the printer-model profile
        (_machine_args) both read.

        Unoverridden default: the Printer-model group's own machine_standoff field.
        _SimMixin overrides this to return the Printhead-sim tab's sim_standoff, so ONE
        elevation field feeds everything rather than a duplicate here."""
        return self.machine_standoff

    def _calibrate_bed(self):
        """Open the visual bed-datum picker on the Bed STL (a separate VTK process, so
        its loop stays clear of Tk's), then poll for it to close and read the picked
        datum back into the Bed-datum fields. Needs a Bed STL set."""
        bed = self.machine_z_stl.get().strip()
        if not bed or not os.path.isfile(bed):
            rotary = bool(getattr(self, "rotary_on", None)
                          and self.rotary_on.get())
            self._refuse("Load the mandrel holder into the Bed STL slot first, then "
                "use Pick in 3D…: the point you pick is the mandrel axis."
                if rotary else
                "Set a Bed STL (carries print) first, then try again.")
            return
        out = os.path.join(SESSION_DIR, "bed_datum.txt")
        try:
            os.remove(out)
        except OSError:
            pass
        argv = worker_cmd("worker:bedcal", bed, "--out", out, unbuffered=False)
        # With a rotary stage fitted the three fields are the mandrel AXIS, not a top
        # face plus a corner, so the picker becomes the place the mandrel is POSITIONED:
        # it is told which linear axis the rotary word turns about, and the rod's real
        # size, so what it draws on the holder is the rod the job will print on. The
        # diameter is the 4-axis designer's and the length the Machine page's -- the
        # same split RotaryAxis states.
        if bool(getattr(self, "rotary_on", None) and self.rotary_on.get()):
            word = (self.rotary_word.get() or "A").strip().upper()
            argv += ["--axis", machine_model.ROTARY_WORDS.get(word.lower(), "x")]
            try:
                dia = float(self.m_diameter.get())
            except (AttributeError, ValueError, tk.TclError):
                dia = 0.0                     # 4-axis page never built / blank
            if dia > 0:
                argv += ["--mandrel-diameter", f"{dia:g}"]
            rod_len, _rod_x = self._rotary_rod()
            if rod_len:
                argv += ["--mandrel-length", f"{float(rod_len):g}"]
        cur = [self.bed_datum_z.get().strip(), self.bed_corner_x.get().strip(),
               self.bed_corner_y.get().strip()]
        if all(cur):
            argv += ["--datum", "{},{},{}".format(*cur)]
        try:
            proc = subprocess.Popen(argv, cwd=HERE)
        except Exception as exc:  # noqa: BLE001
            self._refuse('Could not open the 3-D picker: %s. Try again, or restart the app if it keeps failing.' % exc)
            return
        self._poll_bed_datum(proc, out)

    @staticmethod
    def _read_datum_triple(path):
        """`(z, x, y)` from a calibrator output file, or None.

        The picker writes ONE format for every mode -- bed corner, mandrel axis and
        nozzle tip alike -- and it is Z-FIRST, while the tip fields are labelled X, Y, Z
        on the page. Naming the swap here means it happens once, in a function a test
        can call, rather than twice inline where a transposed tip is invisible until a
        nozzle crashes into the collector."""
        try:
            with open(path) as fh:
                z, x, y = (float(v) for v in fh.read().strip().split(","))
        except (OSError, ValueError):
            return None
        return (z, x, y)

    def _tip_calibrator_argv(self, head, out):
        """argv for picking the NOZZLE TIP on the printhead STL."""
        return worker_cmd("worker:bedcal", head, "--out", out,
                          "--pick", "tip", unbuffered=False)

    def _calibrate_tip(self):
        """Pick the nozzle tip on the Printhead STL, in that STL's own frame.

        Deliberately the same worker, window and output file as the bed/axis
        datum: three numbers in one part's frame is the same job, and a second
        picker would be a second thing to keep in step."""
        head = self.machine_x_stl.get().strip()
        if head:
            import machine_model
            head = machine_model.resolve_part(head)
        if not head or not os.path.isfile(head):
            self._refuse("Set a Printhead STL (nozzle) first, then use Pick in 3D…: the point "
                "you pick is the nozzle, in that STL's own frame.")
            return
        out = os.path.join(SESSION_DIR, "tip_datum.txt")
        try:
            os.remove(out)
        except OSError:
            pass
        argv = self._tip_calibrator_argv(head, out)
        cur = [self.machine_tip_z.get().strip(), self.machine_tip_x.get().strip(),
               self.machine_tip_y.get().strip()]          # the file's z,x,y order
        if all(cur):
            argv += ["--datum", "{},{},{}".format(*cur)]
        try:
            proc = subprocess.Popen(argv, cwd=HERE)
        except Exception as exc:  # noqa: BLE001
            self._refuse('Could not open the 3-D picker: %s. Try again, or restart the app if it keeps failing.' % exc)
            return
        self._poll_tip_datum(proc, out)

    def _poll_tip_datum(self, proc, out):
        """Wait (non-blocking) for the tip picker to close, then load what it
        wrote. Closed without picking -> the fields are left alone, so blanking
        them to return to auto-detect is not undone by opening the window."""
        if proc.poll() is None:
            self.root.after(400, lambda: self._poll_tip_datum(proc, out))
            return
        got = self._read_datum_triple(out)
        if got is None:
            return
        z, x, y = got
        self.machine_tip_x.set(f"{x:.4f}")
        self.machine_tip_y.set(f"{y:.4f}")
        self.machine_tip_z.set(f"{z:.4f}")

    def _rotary_calibrator_argv(self, n, stl, out):
        """argv for picking joint `n`'s AXIS OF ROTATION on its own body STL.

        Split out for the same reason `_tip_calibrator_argv` is: the flags are
        the whole contract with the picker and a test can read them without
        opening a window. NO `--mandrel-diameter` / `--mandrel-length` -- a
        collector table turns no rod, and `--pick joint` is what says so."""
        wvar, avar, _svar, _pvar = self._rotary_pair_vars()[n - 1]
        word = (wvar.get() or "").strip().upper() or str(n)
        axis = (avar.get() or "").strip().lower()
        return worker_cmd("worker:bedcal", stl, "--out", out,
                          "--axis", axis, "--pick", "joint",
                          "--joint-label",
                          "%s (joint %d, turns about %s)"
                          % (word, n, axis.upper()),
                          unbuffered=False)

    def _home_calibrator_argv(self, stl, out):
        """argv for picking the G-CODE HOME on joint 1's body STL. INTERIM.

        Split out for the same reason `_rotary_calibrator_argv` and
        `_tip_calibrator_argv` are: the flags ARE the contract with the picker,
        and a test can read them without opening a window.

        `--axis` is JOINT 1's axis, not a choice: the home is a point on that
        joint's line of rotation, so the line the picker draws has to be the
        line the point must lie on."""
        _wvar, avar, _svar, _pvar = self._rotary_pair_vars()[0]
        axis = (avar.get() or "").strip().lower()
        argv = ["--axis", axis, "--pick", "home"]
        # THE COLLECTOR, DRAWN ON ITS PICKED SEAT. You cannot judge "at the edge
        # of the collector, or off it" against an empty table, and that judgement
        # is the whole content of this pick. With no seat picked the flag is
        # omitted and the picker says it is drawing nothing.
        seat = self._parse_pivot(self.rotary_seat.get())
        if seat is not None:
            argv += list(self._collector_calibrator_args())
            argv += ["--collector-seat", "{},{},{}".format(*seat)]
        return worker_cmd("worker:bedcal", stl, "--out", out, *argv,
                          unbuffered=False)

    def _collector_calibrator_args(self):
        """`--collector...` flags describing the collector to DRAW, or `()`.

        Read off the Scaffold page's Surface designer, live, at the moment the
        window opens -- never copied into a variable of this page's own. The
        five words go straight to `scaffold_gcode.surface_frame`, the ONE
        builder the designer and the emitter both call, so the collector in the
        picker and the collector in the file are the same collector.

        `()` whenever the designer is not on a curved collector or a field is
        not a number: the picker then draws no part and SAYS it is drawing
        none, which is a better answer than a dome built from stale defaults.
        """
        try:
            shape = self._surface_shape_id()
            f = self._surface_floats()
        except Exception:                              # noqa: BLE001
            return ()
        return ("--collector", str(shape),
                "--collector-radius", repr(float(f["radius"])),
                "--collector-ring", repr(float(f["ring"])),
                "--collector-half-angle", repr(float(f["half_angle"])),
                "--collector-span", "%r,%r" % (float(f["span_from"]),
                                               float(f["span_to"])))

    def _seat_calibrator_argv(self, stl, out):
        """argv for picking the COLLECTOR SEAT on joint 1's body STL.

        Split out for the reason the other three builders are: the flags ARE
        the contract with the picker, and a test can read them without opening
        a window.

        `--axis` is joint 1's axis because that is the direction the collector
        stands along -- the picker aims the drawn part down it, and the seat is
        walked back up it by the collector's own bottom-edge coordinate."""
        _wvar, avar, _svar, _pvar = self._rotary_pair_vars()[0]
        axis = (avar.get() or "").strip().lower()
        return worker_cmd("worker:bedcal", stl, "--out", out,
                          "--axis", axis, "--pick", "seat",
                          *self._collector_calibrator_args(),
                          unbuffered=False)

    def _calibrate_seat(self):
        """Pick where the collector's bottom edge rests, on joint 1's body STL.

        SAME BODY, SAME WINDOW, SAME FILE FORMAT as joint 1's axis pick -- it is
        a point on the same body in the same frame. What differs is `--pick
        seat`, which draws the COLLECTOR standing on the marker and makes every
        line of the read-out say collector rather than axis of rotation.

        THE SEED IS THE CENTRE OF THAT BODY'S TOP FACE (`find_spin_face`), and
        it fits this pick better than the axis pick it was written for: a
        collector bolted to the middle of the mounting face is where one lands
        by default. Shown, not saved -- the page's blank field IS the
        uncalibrated switch."""
        wvar, avar, svar, _pvar = self._rotary_pair_vars()[0]
        stl = svar.get().strip()
        word = (wvar.get() or "").strip().upper() or "1"
        if not stl or not os.path.isfile(stl):
            self._refuse("Load joint 1's Body STL first, then use Pick in 3D…: the point you "
                "pick is where the collector's bottom edge rests on the %s "
                "body, in that STL's own frame." % word)
            return
        if (avar.get() or "").strip().lower() not in _ROTARY_AXES:
            self._refuse("Set which linear axis joint 1 turns about first. The "
                "collector stands along that axis, and the picker aims it "
                "down the line.")
            return
        out = os.path.join(SESSION_DIR, "rotary_seat.txt")
        try:
            os.remove(out)
        except OSError:
            pass
        argv = self._seat_calibrator_argv(stl, out)
        cur = self._parse_pivot(self.rotary_seat.get())
        if cur is not None:
            argv += ["--datum", "{},{},{}".format(*cur)]
        try:
            proc = subprocess.Popen(argv, cwd=HERE)
        except Exception as exc:  # noqa: BLE001
            self._refuse('Could not open the 3-D picker: %s. Try again, or restart the app if it keeps failing.' % exc)
            return
        self._poll_rotary_datum(proc, out, self.rotary_seat)

    def _calibrate_home(self):
        """Pick where the nozzle tip sits at G0 X0 Y0 Z0, on joint 1's body STL.
        INTERIM -- see `self.rotary_home`.

        SAME BODY, SAME WINDOW, SAME FILE FORMAT as joint 1's axis pick, because
        it is a point on the same line in the same frame. What differs is
        `--pick home`, which is what makes the read-out say NOZZLE rather than
        axis of rotation -- the one thing separating this pick from a rotation
        centre is the words, so they are the mode's whole job.

        THE SEED IS THE COLLECTOR'S OWN ORIGIN when nothing has been picked
        yet -- the seat if there is one, else joint 1's axis point. That is
        exactly where the nozzle starts without this pick, so the window opens
        on the current behaviour and the user creeps away from it. It is passed
        as `--datum`, so the picker shows it and writes nothing until it is
        moved, the same shown-not-saved rule the tip and joint seeds follow."""
        wvar, avar, svar, _pvar = self._rotary_pair_vars()[0]
        stl = svar.get().strip()
        word = (wvar.get() or "").strip().upper() or "1"
        if not stl or not os.path.isfile(stl):
            self._refuse("Load joint 1's Body STL first, then use Pick in 3D…: the point you "
                "pick is where the nozzle sits at G0 X0 Y0 Z0, on the %s "
                "axis, in that STL's own frame." % word)
            return
        if (avar.get() or "").strip().lower() not in _ROTARY_AXES:
            self._refuse("Set which linear axis joint 1 turns about first. The picker "
                "draws that line and G-code zero is a point on it.")
            return
        out = os.path.join(SESSION_DIR, "rotary_home.txt")
        try:
            os.remove(out)
        except OSError:
            pass
        argv = self._home_calibrator_argv(stl, out)
        cur = (self._parse_pivot(self.rotary_home.get())
               or self._parse_pivot(self.rotary_seat.get())
               or self._parse_pivot(self.rotary1_pivot.get()))
        if cur is not None:
            argv += ["--datum", "{},{},{}".format(*cur)]
        try:
            proc = subprocess.Popen(argv, cwd=HERE)
        except Exception as exc:  # noqa: BLE001
            self._refuse('Could not open the 3-D picker: %s. Try again, or restart the app if it keeps failing.' % exc)
            return
        # the SAME poller the joint picks use -- one place that knows the picker
        # writes on close, and `rotary_home`'s own trace redraws the read-out.
        self._poll_rotary_datum(proc, out, self.rotary_home)

    def _calibrate_rotary_joint(self, n):
        """Pick joint `n`'s axis of rotation on that joint's own body STL.

        THE SAME WORKER, WINDOW AND FILE FORMAT as the bed datum and the nozzle
        tip: three numbers in one part's frame is the same job however many
        joints a rig has, and a second picker would be a second thing to keep in
        step. What differs is only `--pick joint`, which drops the mandrel rod
        (a collector table turns no rod, so drawing one would show a part that
        is not on the machine) and leaves the axis LINE as the instrument.

        `--axis` is the joint's own linear axis, so the line the user is
        centring is the line the profile will turn about. Getting that wrong is
        not silently wrong: the line visibly runs the wrong way across the
        body."""
        wvar, avar, svar, pvar = self._rotary_pair_vars()[n - 1]
        stl = svar.get().strip()
        word = (wvar.get() or "").strip().upper() or str(n)
        if not stl or not os.path.isfile(stl):
            self._refuse("Load joint %d's Body STL first, then use Pick in 3D…: the point you "
                "pick is a point on its axis of rotation, in that STL's own "
                "frame." % n)
            return
        axis = (avar.get() or "").strip().lower()
        if axis not in _ROTARY_AXES:
            self._refuse("Set which linear axis joint %d turns about first. The picker "
                "draws that line through the point you pick." % n)
            return
        out = os.path.join(SESSION_DIR, "rotary%d_datum.txt" % n)
        try:
            os.remove(out)
        except OSError:
            pass
        argv = self._rotary_calibrator_argv(n, stl, out)
        cur = self._parse_pivot(pvar.get())
        if cur is not None:
            argv += ["--datum", "{},{},{}".format(*cur)]
        try:
            proc = subprocess.Popen(argv, cwd=HERE)
        except Exception as exc:  # noqa: BLE001
            self._refuse('Could not open the 3-D picker: %s. Try again, or restart the app if it keeps failing.' % exc)
            return
        self._poll_rotary_datum(proc, out, pvar)

    def _poll_rotary_datum(self, proc, out, pvar):
        """Wait (non-blocking) for a joint's picker to close and store what it
        wrote, as the one `z,x,y` string. Closed without picking -> the field is
        left alone, so a joint that was deliberately blanked back to
        uncalibrated is not re-filled just by opening the window (the same rule
        the tip picker follows)."""
        if proc.poll() is None:
            self.root.after(400, lambda: self._poll_rotary_datum(proc, out, pvar))
            return
        got = self._read_datum_triple(out)
        if got is None:
            return
        pvar.set("{:.4f},{:.4f},{:.4f}".format(*got))
        self._sync_axis_datum_note()

    # -- a CUSTOM g-code zero: a point picked on the plate or the mandrel --- #
    # "Custom" on the Corner dropdown (plate) or on "Zero along the mandrel"
    # opens the 3-D picker (`bed_calibrator --pick zero`) on the collector,
    # drawn from its size; the point comes back as one "z,x,y" triple in the
    # collector's own frame (`origin_custom` / `rotary_origin_custom`) and
    # `_origin_datum` / `_mandrel_origin_u` make it X0 Y0 Z0. Every other
    # choice never reads it, so a corner or an end writes what it always did.
    def _zero_vars(self, kind):
        """(choice var, picked-point var) for "plate" or "mandrel"."""
        if kind == "mandrel":
            self._rotary_vars_make()
            return self.rotary_origin_axial, self.rotary_origin_custom
        return self.origin_anchor, self.origin_custom

    def _zero_is_custom(self, kind):
        try:
            if kind == "mandrel":
                return self._rotary_origin_keys()[0] == "custom"
            return self.origin_anchor.get().strip().lower() == "custom"
        except (AttributeError, tk.TclError):
            return False

    def _zero_custom_point(self, kind):
        """The picked `(z, x, y)`, or None while nothing has been picked."""
        return self._parse_pivot(self._zero_vars(kind)[1].get())

    def _zero_custom_words(self, kind):
        """Where the picked point is, in the page's words."""
        p = self._zero_custom_point(kind)
        if p is None:
            return "Not picked yet."
        z, x, y = p
        if kind == "mandrel":
            return "Picked: %g mm from the near end." % round(x, 3)
        top = self._plate_top_z()
        on = ("on the top face" if abs(z - top) < 1e-3 else
              "on the bottom face" if abs(z) < 1e-3 else
              "%g mm above the bottom face" % round(z, 3))
        return ("Picked: X %g, Y %g mm from the bottom-left corner, %s."
                % (round(x, 3), round(y, 3), on))

    def _zero_custom_tail(self, row, kind, tip):
        """Pick in 3D… and the picked point, after a zero dropdown, shown only
        while it says Custom. Choosing Custom opens the picker at once; closed
        with nothing ever picked, the dropdown goes back to what it said."""
        choice, point = self._zero_vars(kind)
        note = tk.StringVar(value="")
        row.add_button("Pick in 3D…", lambda: self._calibrate_zero(kind),
                       "Open the collector in 3-D and click the point your "
                       "printer is zeroed on." + tip)
        host = row._host()
        row.add_widget(ttk.Label(host, textvariable=note,
                                 style="Hint.TLabel"))
        last = {"value": None}

        def sync(*_a):
            on = self._zero_is_custom(kind)
            if not on:
                last["value"] = choice.get()
            note.set(self._zero_custom_words(kind) if on else "")
            try:
                host.grid() if on else host.grid_remove()
                if kind == "plate":
                    self._origin_face_field.configure(
                        state="disabled" if on else "readonly")
            except (AttributeError, tk.TclError):
                pass
        self._zero_last_choice = getattr(self, "_zero_last_choice", {})
        self._zero_last_choice[kind] = last
        for v in (choice, point):
            v.trace_add("write", sync)
        sync()

        def chosen(_e=None):
            if self._zero_is_custom(kind):
                self._calibrate_zero(kind)
        row.field.bind("<<ComboboxSelected>>", chosen, add="+")

    def _zero_rod_span(self):
        """`(near end u, far end u, radius)` of the mandrel the tube prints on,
        collector frame, or None while the design does not resolve."""
        try:
            design = self._mandrel_resolved_design()
            lay = self._mandrel_axial_layout(design)
            return (float(lay.rod_u0), float(lay.rod_u1), float(design.radius))
        except Exception:  # noqa: BLE001 -- a half-typed design
            return None

    def _zero_pick_argv(self, kind, out):
        """argv for picking a custom zero, or a refusal string. The flags are
        the contract with the picker, so they are built where a test can read
        them without a window."""
        if kind == "mandrel":
            span = self._zero_rod_span()
            if span is None:
                return ("Finish the mandrel design first: the picker draws "
                        "the mandrel at its diameter and length.")
            u0, u1, r = span
            geo = ["--zero-rod", "%r,%r" % (2.0 * r, max(u1 - u0, 1e-3))]
            seed = self._zero_custom_point(kind)
            if seed is None:
                d = self._mandrel_resolved_design()
                seed = (r, self._mandrel_origin_u(d) - u0, 0.0)
        else:
            fp = self._plate_footprint_xy()
            try:
                t = float(self.plate_thickness.get())
            except (ValueError, tk.TclError):
                t = 0.0
            if fp is None or t <= 0:
                return ("Set the plate's size and thickness first, on "
                        "Setup's Collector card: the picker draws the plate "
                        "from them.")
            if self.plate_shape.get() == "circular":
                geo = ["--zero-disc", "%r,%r" % (fp[1] - fp[0], t)]
            else:
                geo = ["--zero-plate", "%r,%r,%r" % (fp[1] - fp[0],
                                                    fp[3] - fp[2], t)]
            seed = self._zero_custom_point(kind)
            if seed is None:
                x, y, z = self._origin_datum()
                seed = (z, x, y)
        return worker_cmd("worker:bedcal", "--pick", "zero", "--out", out,
                          *geo, "--datum", "{},{},{}".format(*seed),
                          unbuffered=False)

    def _calibrate_zero(self, kind):
        """Open the picker for a custom zero on the plate or the mandrel."""
        out = os.path.join(SESSION_DIR, "zero_%s.txt" % kind)
        try:
            os.remove(out)
        except OSError:
            pass
        argv = self._zero_pick_argv(kind, out)
        if isinstance(argv, str):
            self._zero_pick_abandoned(kind)
            self._refuse(argv)
            return
        try:
            proc = subprocess.Popen(argv, cwd=HERE)
        except Exception as exc:  # noqa: BLE001
            self._zero_pick_abandoned(kind)
            self._refuse('Could not open the 3-D picker: %s. Try again, or '
                         'restart the app if it keeps failing.' % exc)
            return
        self._poll_zero(proc, out, kind)

    def _zero_pick_abandoned(self, kind):
        """Nothing picked, and nothing ever was: put the dropdown back on the
        choice it showed before Custom, so the page never says Custom about a
        point that does not exist."""
        if self._zero_custom_point(kind) is not None:
            return
        prev = getattr(self, "_zero_last_choice", {}).get(kind, {}).get("value")
        choice, _point = self._zero_vars(kind)
        if prev and self._zero_is_custom(kind):
            choice.set(prev)

    def _poll_zero(self, proc, out, kind):
        if proc.poll() is None:
            self.root.after(400, lambda: self._poll_zero(proc, out, kind))
            return
        got = self._read_datum_triple(out)
        if got is None:
            self._zero_pick_abandoned(kind)
            return
        self._zero_vars(kind)[1].set("{:.4f},{:.4f},{:.4f}".format(*got))

    def _poll_bed_datum(self, proc, out):
        """Wait (non-blocking) for the calibrator to close, then load the datum it
        wrote into the Bed-datum fields. The user can pick mid-session and keep the
        window open; the fields update once it closes."""
        if proc.poll() is None:
            self.root.after(400, lambda: self._poll_bed_datum(proc, out))
            return
        got = self._read_datum_triple(out)
        if got is None:
            return                                  # closed without picking -> no change
        z, x, y = got
        self.bed_datum_z.set(f"{z:.4f}")
        self.bed_corner_x.set(f"{x:.4f}")
        self.bed_corner_y.set(f"{y:.4f}")
        self._sync_axis_datum_note()

    def _toggle_plate_shape(self):
        """Grey the dimension group that doesn't match the selected shape."""
        circ = self.plate_shape.get() == "circular"
        self._set_widgets_state(self.plate_rect_frame,
                                "disabled" if circ else "normal")
        self._set_widgets_state(self.plate_circ_frame,
                                "normal" if circ else "disabled")

    # -- derived geometry --------------------------------------------------- #
    def _plate_floor_z(self):
        """The plate's bottom Z. With the machine bounds box gone the floor is simply
        z=0 (the plate rests at the g-code floor); its top is floor + thickness."""
        return 0.0

    def _plate_top_z(self):
        """The plate's top surface Z (the deposition datum) = floor + thickness."""
        try:
            t = float(self.plate_thickness.get())
        except (ValueError, tk.TclError):
            t = 0.0
        return self._plate_floor_z() + t

    def _elevation_value(self):
        """The printhead-elevation value (mm) reused as the optional g-code Z bump;
        0.0 if blank or unparseable. Reads whichever field _standoff_var resolves to
        (the Printhead-sim tab's sim_standoff, via _SimMixin's override)."""
        try:
            return float(self._standoff_var().get())
        except (ValueError, tk.TclError):
            return 0.0

    def _origin_datum(self):
        """The (x, y, z) point the g-code origin (0,0,0) sits on.

        A corner or face-centre of the plate, on its top or bottom face, so the
        print is re-based onto the plate. None-ish fields read 0."""
        fp = self._plate_footprint_xy()
        top_z, bot_z = self._plate_top_z(), self._plate_floor_z()
        if fp is None:
            return (0.0, 0.0, 0.0)
        x0, x1, y0, y1 = fp
        anchor = self.origin_anchor.get().strip().lower()
        if anchor == "custom":
            # the picked point, already in this frame (`_zero_pick_argv`
            # draws the plate from this footprint); unpicked reads as the
            # bottom-left corner of the top face, the default zero
            p = self._zero_custom_point("plate")
            if p is not None:
                return (p[1], p[2], p[0])
            return (x0, y0, top_z)
        ax = {"bottom-left": x0, "top-left": x0, "bottom-right": x1,
              "top-right": x1, "center": 0.5 * (x0 + x1)}.get(anchor, x0)
        ay = {"bottom-left": y0, "bottom-right": y0, "top-left": y1,
              "top-right": y1, "center": 0.5 * (y0 + y1)}.get(anchor, y0)
        z = (bot_z if self.origin_face.get().strip().lower().startswith("bottom")
             else top_z)
        return (ax, ay, z)

    def _rotary_vars_make(self):
        """Create the rotary-collector vars, once, on demand.

        LAZY because they are read from OUTSIDE this page: `_MandrelMixin` asks where
        the rod is and which point of it reads zero, and the 4-axis designer's form can
        be built before this tab is, so making them here means they exist whichever
        page is built first. Idempotent, so both callers can call it unconditionally.
        """
        if hasattr(self, "rotary_on"):
            return
        # ROTARY COLLECTOR (4-axis). Off by default, so a page that never touches this
        # group writes exactly the profile it has always written. Only the MOUNT lives
        # here: the rod's diameter and length are the JOB's, set on the 4-axis designer,
        # because the same stage takes a 1 mm and a 6 mm rod. The axis' XY is the slide
        # offset above -- one datum for both collectors.
        self.rotary_on = tk.BooleanVar(value=False)
        # A preset SETS this; it does not click it, and a checkbutton's `command`
        # only speaks for clicks. Without the trace, loading a preset with the
        # stage ticked showed the tick over a greyed-out group -- and unticking
        # and re-ticking was the only way to reach the fields.
        self.rotary_on.trace_add("write", lambda *_a: self._sync_rotary_enable())
        self.rotary_word = tk.StringVar(value="A")
        # WHICH KIND of stage. Defaults to the mandrel, so a profile written by a
        # page that never opens this selector is byte-identical to the 4-axis one
        # it has always written.
        self.rotary_kind = tk.StringVar(value=_ROTARY_KIND_LABELS[0])
        self.rotary_kind.trace_add("write", lambda *_a: self._sync_rotary_enable())
        # THE FIVE-AXIS PAIR, collector-first: joint 0 carries the collector and
        # spins it, joint 1 is the tilt underneath. Each joint needs a WORD and an
        # AXIS and nothing else -- see this slice's design document for why there
        # are no pivot fields here.
        #
        # The defaults are Terranova 2024's words and the axes that go with them,
        # because those six files are the only real five-axis MEW g-code anyone
        # has and it costs a user nothing to change a label. A WORD is safe to
        # default this way where `zero_deg = 90` was not: a word is a name with no
        # geometric claim inside it, and `RotaryAxis`'s docstring objects to the
        # 90 because it asserts a rest pose the files do not support.
        # The pair keeps its OWN word variables rather than borrowing
        # `rotary_word`: one Tk variable meaning two things depending on a mode
        # selector is how a form goes stale on screen, and the mandrel's word is
        # restricted to A/B/C where a pair's is not.
        self.rotary1_word = tk.StringVar(value="V")
        self.rotary1_axis = tk.StringVar(value="z")
        self.rotary2_word = tk.StringVar(value="U")
        self.rotary2_axis = tk.StringVar(value="y")
        # THE TWO ROTARY BODIES. A mandrel needs no body slot of its own -- the
        # holder goes in the bed slot, because a 4-axis rig has no bed. A TABLE
        # is the opposite case: there IS a bed under it (the development rig's
        # Aerotech X plate) and there are TWO bodies above that bed, so they need two slots
        # and the bed slot goes back to meaning a bed.
        self.rotary1_stl = tk.StringVar(value="")
        self.rotary2_stl = tk.StringVar(value="")
        # WHERE EACH JOINT'S AXIS IS -- one "z,x,y" triple per joint, in that
        # joint's OWN STL frame, exactly as `bed.datum_z`/`bed.corner_x/y` are in
        # the bed STL's frame. Blank means NOT CALIBRATED, which the read-out
        # says out loud.
        #
        # ONE key per joint, not three, and NO Entry widget: the calibrator pick
        # is the only writer. Three free-typed numbers with no calibrator is what
        # cost `rotary.axis_height` its existence, and three keys where the
        # picker writes one triple is three chances to half-fill a datum or
        # transpose it. `_read_datum_triple` already names the z,x,y order once;
        # storing the triple whole means it is never re-ordered on the way in or
        # out.
        self.rotary1_pivot = tk.StringVar(value="")
        self.rotary2_pivot = tk.StringVar(value="")
        for _v in (self.rotary1_pivot, self.rotary2_pivot,
                   self.rotary1_word, self.rotary2_word):
            _v.trace_add("write", lambda *_a: self._sync_axis_datum_note())
        # THE G-CODE HOME -- INTERIM: for now the home is a point
        # picked on the C axis, until a proper homing method replaces it. One
        # "z,x,y" triple, in
        # JOINT 1's OWN STL frame (it is a point on joint 1's axis, picked on
        # joint 1's body), stored whole and written only by the calibrator --
        # the same rule the two pivots follow, for the same reasons.
        #
        # WHAT IT IS NOT, and this is the part to preserve until the real design
        # lands: it is not a datum LADDER like the 4-axis rod's surface/axis
        # choice, and it is not a second collector seat. It is one picked point
        # that says where the nozzle tip is at G0 X0 Y0 Z0. When a homing
        # model arrives, delete this variable, its row, its persist key, its
        # branch in `_rotary_pair_joints` and `bed_calibrator`'s `--pick home`.
        self.rotary_home = tk.StringVar(value="")
        self.rotary_home.trace_add("write",
                                   lambda *_a: self._sync_pair_home_note())
        # WHERE THE COLLECTOR STANDS -- one "z,x,y" triple in JOINT 1's own STL
        # frame, written only by the calibrator, exactly as the two pivots and
        # the home are. Every printing mode needs two datums, and
        # the first is where the collector (the thing being printed on) sits
        # relative to whatever holds it: the bed for 3 axis, the mandrel mount
        # for 4 axis, the C axis for 5 axis. This is that one, for five.
        #
        # IT IS NOT JOINT 1'S PIVOT, and keeping them apart is the whole point.
        # A pivot is a point on the axis of ROTATION; this is the point the part
        # STANDS on. Using the pivot as both puts a dome's centre of curvature
        # on the table and hangs its rim above it -- measured at 3.125 mm on an
        # R=10 dome, and the "floating a few
        # centimetres" photographed on a bigger one.
        self.rotary_seat = tk.StringVar(value="")
        self.rotary_seat.trace_add("write",
                                   lambda *_a: self._sync_pair_seat_note())
        # THE ROD ITSELF, once a stage is declared: how long it is and where its near
        # end sits along the axis. These are the rotary counterparts of the plate's size
        # and Corner X/Y -- collector PLACEMENT, which is why they go to the workers
        # rather than into machine.json. The DIAMETER stays on the 4-axis designer: it
        # is in the winding maths, so it is a path number and these two are not.
        # With the stage unticked both are ignored and the rod is derived from the print
        # as before. 30 mm is `RotaryAxis`'s own default rod, so a freshly ticked stage
        # describes a real mandrel rather than a blank one.
        self.rotary_rod_length = tk.StringVar(value="30")
        self.rotary_rod_x = tk.StringVar(value="0")
        # ... and where a tube program reads zero. See _ROTARY_ORIGIN_*.
        self.rotary_origin_axial = tk.StringVar(
            value=_ROTARY_ORIGIN_AXIAL_LABELS[0])
        self.rotary_origin_radial = tk.StringVar(
            value=_ROTARY_ORIGIN_RADIAL_LABELS[0])
        # ... and the point "Custom" means: one "z,x,y" triple picked on the
        # mandrel (`_calibrate_zero`), X along the rod from its near end. Only
        # X is read -- a tube program has no Y word and its Z is the Radially
        # choice. Blank = never picked.
        self.rotary_origin_custom = tk.StringVar(value="")
        # The rod's zero re-bases the tube's PROGRAM, never where the tube
        # sits on the rod (`_mandrel_origin_u`): the G-code page's text has
        # to be made again, and the pictures' zero triad moved. Nothing
        # traced them, so the page kept the old zero's numbers under a triad
        # at the new one.
        for _zv in (self.rotary_origin_axial, self.rotary_origin_radial,
                    self.rotary_origin_custom):
            _zv.trace_add("write", lambda *_a: self._rotary_zero_moved())
        # an older save says "Rod ..." or "(centre)"; show today's words for
        # the same point
        for var, pairs in ((self.rotary_origin_axial, _ROTARY_ORIGIN_AXIAL),
                           (self.rotary_origin_radial, _ROTARY_ORIGIN_RADIAL)):
            def _modern(*_a, v=var, pairs=pairs):
                old = v.get()
                new = old
                if new.startswith("Rod "):
                    new = "Mandrel " + new[4:]
                new = new.replace("(centre)", "(center)")
                if new != old and any(lbl == new for lbl, _k in pairs):
                    v.set(new)
            var.trace_add("write", _modern)
        # WHAT THE TWO JOINTS CAN PHYSICALLY DO, and the largest feed the rig
        # will take. Rig facts, exactly like the two pivots and the per-part
        # motion limits above -- which is why they are HERE and not on the
        # Curved-collector page, where the limiter that consumes them was
        # first written. This
        # page's preset kind is the one a rig is described by
        # (`gui_presets`: "which persist keys belong to 'machine' is decided
        # by _machine_persist_keys"), and preset ownership follows the TAB the
        # field lives on. Left on the designer they could not ride the 5 Axis
        # PhD Printer preset at all, so the number would have had to be
        # retyped per scaffold preset -- and a rig limit retyped per design is
        # a rig limit that is eventually wrong.
        #
        # The defaults and the persist keys both come from
        # `rotary_limits.RIG_LIMIT_FIELDS`, which is also what the verification
        # matrix generates with, so the suite's files are the files a user
        # gets. Only the spin and tilt ACCELERATION ship set, at the one
        # measured rig number; every rate and the feed ceiling ship at 0 (off)
        # because nobody has measured one and nobody is to go looking.
        import rotary_limits
        for key, attr, val in rotary_limits.RIG_LIMIT_FIELDS:
            setattr(self, attr, tk.StringVar(
                value=("" if not val else ("%g" % val))))

    def _rotary_kind_key(self):
        """`"mandrel"` or `"pair"` -- which collector table is declared.

        Reads the label back through `_ROTARY_KIND_KEY`, and answers `"mandrel"`
        for anything it does not recognise, so a preset written before this
        selector existed (or one carrying a stale label) lands on the 4-axis
        behaviour that preset was describing rather than on a five-axis pair
        nobody asked for."""
        self._rotary_vars_make()
        return _ROTARY_KIND_KEY.get(self.rotary_kind.get(), "mandrel")

    def _rotary_pair_joints(self):
        """The collector-first `"rotaries"` list for a five-axis table.

        Both joints carry an explicit `axis` because their WORDS are the rig's
        and need not be A/B/C. `radius`/`length` are the JOB's on a mandrel and
        meaningless on a curved collector, so neither is written.

        `stl` and `pivot` are written ONLY when the user has supplied them, and
        that is the difference between a rig this page can describe and two
        abstract joints. `pivot` is the calibrator's picked point VERBATIM, in
        that joint's own STL frame -- the same convention `bed_top_z` /
        `bed_corner` use for the bed, and `machine_model` does every conversion
        (every part STL is loaded into the machine
        frame by ONE rigid shift shared by all parts, so a point picked in the C
        body's frame is directly comparable to one picked in the A body's, on
        any rig). An unpicked joint writes no `pivot` at all rather than a zero
        one: `RotaryAxis` names a pivot silently taken as the origin as geometry
        that is "plausible and wrong everywhere except on the axis itself", and
        the page's blank field IS the uncalibrated switch, exactly as the bed
        datum's blank fields are the auto-detect switch."""
        role = _ROTARY_ROLE
        out = []
        for wvar, avar, svar, pvar in self._rotary_pair_vars():
            word = (wvar.get() or "").strip().upper()
            axis = (avar.get() or "").strip().lower()
            if not word or axis not in _ROTARY_AXES:
                return []           # incomplete -> emit nothing, not a half pair
            j = {"word": word, "role": role, "axis": axis}
            stl = svar.get().strip()
            if stl:
                j["stl"] = stl
            piv = self._parse_pivot(pvar.get())
            if piv is not None:
                z, x, y = piv                       # the file's z,x,y order
                j["pivot"] = [x, y, z]
            out.append(j)
        # THE COLLECTOR SEAT, on JOINT 1 -- the joint the collector is bolted
        # to, which is the same reason `home` sits there. Unpicked writes no key
        # at all, so a profile from a rig whose collector has not been located
        # is byte-identical to one written before this existed.
        if out:
            seat = self._parse_pivot(self.rotary_seat.get())
            if seat is not None:
                z, x, y = seat                      # the file's z,x,y order
                out[0]["seat"] = [x, y, z]
        # THE G-CODE HOME, on JOINT 1 -- INTERIM (see `self.rotary_home`).
        #
        # It goes beside `pivot` on the joint rather than at the top level of the
        # profile, and the reason is the frame: a picked point is stored VERBATIM
        # in the body's own STL frame and `machine_model` does every conversion
        # (as for `pivot`). A top-level key would need its own sentence saying which
        # STL frame it is in, and two frame statements is how two conventions
        # drift apart. Sitting next to `pivot`, it inherits that ruling and there
        # is nothing to state twice.
        #
        # Unpicked writes NO key at all, exactly as an unpicked pivot does, so a
        # profile from a rig that has not been homed is byte-identical to one
        # written before this existed.
        #
        # ...AND THE WORKING DISTANCE IT WAS PICKED AT, beside it. The pick is a
        # NOZZLE TIP position, made hovering one standoff (3.5 mm) up,
        # which is how a Z zero is set on any print, touch off and back off. Every
        # other datum in this program
        # is a SURFACE (the plate's top face, the mandrel's axis) and the
        # standoff is applied to it by the placement code, so the tip pick has
        # to be reduced to a surface point exactly once. That subtraction needs
        # a number this joint does not otherwise carry, and reading the live
        # standoff field inside `machine_model` would silently re-interpret an
        # old pick the moment the field was edited. Written here, the profile
        # says what the pick MEANS, and a saved rig keeps meaning it.
        if out:
            home = self._parse_pivot(self.rotary_home.get())
            if home is not None:
                z, x, y = home                      # the file's z,x,y order
                out[0]["home"] = [x, y, z]
                try:
                    out[0]["home_standoff"] = float(
                        self._standoff_var().get())
                except (ValueError, tk.TclError):
                    # a blank/garbage standoff field: write no key rather than
                    # a plausible zero. `machine_model` then reads the pick as
                    # the surface point it was before this existed, which is
                    # the same thing every other half-typed field on this page
                    # does -- the validator is what stops the run.
                    pass
        return out

    def _rotary_rod(self):
        """`(rod_length, rod_x)` of the DECLARED mandrel, or `(None, 0.0)`.

        `None` is the answer whenever the machine page has not declared a rotary stage,
        or its length field is blank -- and it means "derive the rod from the print",
        which is what every tube did before these fields existed. The caller is
        `gui_mandrel`, where the design and therefore the printed span lives."""
        self._rotary_vars_make()
        if not self.rotary_on.get():
            return (None, 0.0)
        try:
            length = float(self.rotary_rod_length.get())
        except (ValueError, tk.TclError):
            return (None, 0.0)
        if length <= 0.0:
            return (None, 0.0)
        try:
            x0 = float(self.rotary_rod_x.get())
        except (ValueError, tk.TclError):
            x0 = 0.0
        return (length, x0)

    def _rotary_origin_keys(self):
        """`(axial, radial)` datum keys -- one of near|far|mid, one of
        surface|axis. The combobox shows a sentence and persists it, exactly as
        the kinematics and mount pickers do; this maps it back."""
        self._rotary_vars_make()
        return (_ROTARY_ORIGIN_AXIAL_KEY.get(
                    self.rotary_origin_axial.get().strip(), "near"),
                _ROTARY_ORIGIN_RADIAL_KEY.get(
                    self.rotary_origin_radial.get().strip(), "surface"))

    def _rotary_surface_z(self, radius):
        """The g-code Z that means THE BARE MANDREL SURFACE, given the rod's radius --
        the radial half of the 4-axis datum, and the number the generator, the converter
        and the player all have to agree on.

        Zero (the default, and what the "Rod surface" datum gives) says a Z
        word is wall height above the surface. Zeroing on the rod's AXIS instead puts
        the surface at `R0`. `mew_frame.CylinderFrame` takes exactly this as
        `surface_z`, so nothing downstream has to know which datum produced it.

        Deliberately NOT part of the origin re-base: a Z rebase would be undone twice,
        once by the frame unrolling the file and once by the player putting the scene
        back in machine coordinates, and the tube would sit a radius off its own rod.
        """
        # The radial ladder is a COLLECTOR datum, and the origin is always the
        # collector since the World origin went.
        _axial, radial = self._rotary_origin_keys()
        z = float(radius) if radial == "axis" else 0.0
        if self.add_elevation_z.get():
            # the same opt-in the plate has: command the nozzle at its working
            # height, so a Z word is standoff + wall rather than wall.
            z += self._elevation_value()
        return z

    def _origin_offset(self):
        """The (dx, dy, dz) added to every machine-frame coordinate so the chosen origin
        datum (see _origin_datum) becomes (0,0,0) in the exported g-code -- the negative
        of that point. With "Add printhead elevation to Z" ticked, the elevation is
        ADDED to dz. The player undoes the whole re-base, so this only shifts the
        exported g-code / plate STL, never the 3D view."""
        px, py, pz = self._origin_datum()
        dx, dy, dz = -px, -py, -pz
        if self.add_elevation_z.get():
            dz += self._elevation_value()
        return (dx, dy, dz)

    def _plate_size_xy(self):
        """(size_x, size_y) of the plate footprint -- (d, d) for a circle -- or
        None if the dimension fields aren't parseable."""
        try:
            if self.plate_shape.get() == "circular":
                d = float(self.plate_diameter.get())
                return (d, d)
            return (float(self.plate_x.get()), float(self.plate_y.get()))
        except (ValueError, tk.TclError):
            return None

    def _plate_footprint_xy(self):
        """(x0, x1, y0, y1) of the plate footprint, anchored at (0, 0) by its
        bottom-left corner -- a circle uses its diameter-sized bounding square.
        None if the size fields aren't parseable."""
        size = self._plate_size_xy()
        if size is None:
            return None
        return (0.0, size[0], 0.0, size[1])

    def _plate_center_xy(self):
        """(center_x, center_y) of the plate = corner + half-size -- the form the
        workers take (their plate math is centre-based). None if unparseable."""
        fp = self._plate_footprint_xy()
        if fp is None:
            return None
        return ((fp[0] + fp[1]) / 2.0, (fp[2] + fp[3]) / 2.0)

    # -- validation --------------------------------------------------------- #
    def _validate_plate(self):
        """None if the plate fields are OK, else an error string. The plate datum
        always applies, so its dims are validated whether or not the plate is shown.
        The slide offset + motion limits are sim-only and parsed defensively in
        _machine_args, so they never block a run."""
        e = self._validate_origin()
        if e:
            return e
        # each refusal names the field by its label AND says where it is: it
        # is read on the Design page, two pages from the field ("Plate size X
        # must be a number." matched no label on the Collector card)
        where = " on Setup's Collector card (Printer & collector)"
        _, e = self._pos_float(self.plate_thickness.get(), "Thickness Z" + where)
        if e:
            return e
        if self.plate_shape.get() == "circular":
            _, e = self._pos_float(self.plate_diameter.get(), "Diameter" + where)
            if e:
                return e
        else:
            _, e = self._pos_float(self.plate_x.get(), "Size X" + where)
            if e:
                return e
            _, e = self._pos_float(self.plate_y.get(), "Size Y" + where)
            if e:
                return e
        return None

    # -- preset partition --------------------------------------------------- #
    def _machine_persist_keys(self):
        """The persist keys owned by the Machine and Collector tab -- the set a
        machine preset captures and a scaffold preset omits (see gui_presets).
        Defined here, next to the tab's fields, so the set stays in step with
        the tab."""
        keys = ["plate.shape", "plate.x", "plate.y", "plate.diameter",
                "plate.thickness", "plate.show",
                "origin.face", "origin.anchor", "origin.custom",
                "origin.add_elevation_z", "origin.no_layer_z",
                "origin.no_scaffold_z",
                "origin.custom_gcode", "origin.custom_gcode_after",
                "origin.gcode_flavor", "origin.gcode_flavor_custom",
                "origin.strip_comments_on_save", "origin.layer_markers",
                "origin.gcode_start", "origin.gcode_end",
                "origin.omit_start_end",
                "origin.gcode_rules", "origin.preflight_accepted",
                # Setup's output rows; the keys keep their old names, so a
                # preset saved before they moved here still reads
                "s.travel_as_g1", "s.gcode_convention", "s.wait_for_temp",
                "s.g2g3",
                "origin.feed_time_base", "origin.incremental",
                "origin.machine_x", "origin.machine_y", "origin.machine_z",
                "origin.machine_r1", "origin.machine_r2",
                # printer-model (3D simulation) parts + chain + alignment
                "machine.show", "machine.kinematics",
                "machine.hide_head", "machine.hide_gantry",
                "machine.hide_bed", "machine.hide_frame",
                "machine.x_stl", "machine.y_stl", "machine.z_stl",
                # NB: "machine.standoff" is intentionally absent -- elevation is
                # sim_standoff (the Printhead-sim tab) and rides the machine preset
                # via _machine_motion_keys' "sim.standoff", not the dead
                # machine.standoff var.
                "machine.static_stl", "machine.scale",
                # bed-anchored collector slide offset (XY) + manual bed datum
                "slide.offset_x", "slide.offset_y",
                "bed.datum_z", "bed.corner_x", "bed.corner_y",
                # custom-chain axes + head/bed parents (only used when kinematics=custom)
                "machine.head_parent", "machine.bed_parent",
                "machine.head_x", "machine.head_y", "machine.head_z",
                "machine.gantry_x", "machine.gantry_y", "machine.gantry_z",
                "machine.bed_x", "machine.bed_y", "machine.bed_z",
                # contact-point overrides
                "machine.tip_x", "machine.tip_y", "machine.tip_z",
                "machine.bed_nudge",
                # rotary (4-axis) collector MOUNT -- the rod's own size is a job
                # parameter and lives on the 4-axis designer (m.* keys).
                # "rotary.stl" is deliberately absent: the holder is the Bed STL
                # above, and a second key for the same body could disagree with it.
                "rotary.on", "rotary.word",
                # WHICH KIND of table, and the five-axis pair's two joints. A
                # word and an axis each, and no pivot -- see `_rotary_pair_joints`.
                "rotary.kind",
                "rotary.j1_word", "rotary.j1_axis",
                "rotary.j2_word", "rotary.j2_axis",
                # ... each joint's own BODY and its picked axis of rotation. The
                # body is a machine fact like every other part STL, and the pivot
                # is the calibrator's triple stored WHOLE ("z,x,y", blank =
                # uncalibrated) -- one key, because the picker writes one triple
                # and three keys is three chances to half-fill or transpose it.
                # Deliberately NOT the bed datum keys: on a pair the bed slot
                # holds a real bed, and two meanings on one key is the seam this
                # page has already been bitten by.
                "rotary.j1_stl", "rotary.j1_pivot",
                "rotary.j2_stl", "rotary.j2_pivot",
                # ... and the G-CODE HOME (INTERIM): where the nozzle tip sits
                # at G0 X0 Y0 Z0, picked on joint 1's body, in that body's own
                # STL frame. A machine fact like the two pivots and stored the
                # same way -- one "z,x,y" triple, blank = not picked, written
                # only by the calibrator. Goes when the real homing model does.
                "rotary.home",
                "rotary.j1_seat",
                # ... and the rod's own place + the tube datum, which ARE
                # machine facts: the rod is clamped where it is clamped, and
                # you zero on it the same way every print.
                "rotary.rod_length", "rotary.rod_x",
                "rotary.origin_axial", "rotary.origin_radial",
                "rotary.origin_custom"]
        # the five-axis pair's rotary rate / acceleration limits and the feed
        # ceiling -- rig facts, one list, `rotary_limits.RIG_LIMIT_FIELDS`
        import rotary_limits
        keys += [k for k, _a, _v in rotary_limits.RIG_LIMIT_FIELDS]
        # per-part motion limits (sim-only): machine.lim_{role}_{axis}_{min|max}
        for role in ("head", "gantry", "bed"):
            for ax in ("x", "y", "z"):
                keys.append(f"machine.lim_{role}_{ax}_min")
                keys.append(f"machine.lim_{role}_{ax}_max")
        # the accel-limited planner knobs (Max accel / Corner speed) add their
        # persist keys via _machine_motion_keys.
        keys.extend(self._machine_motion_keys())
        # ... and the process limits (pressure / speed / melt temperature)
        # Fiber target judges against: rig facts on the Printer model card
        keys.extend(getattr(self, "_ft_machine_keys", tuple)())
        return keys

    # -- CLI-arg builders --------------------------------------------------- #
    def _plate_args(self):
        """Plate datum argv for the workers (scaffold_gcode AND the converter understand
        the same fragment). plate_top_z is computed here so the floor formula lives in
        one place; `--plate` (draw the grey STL) is added only when "Show build plate"
        is on, but the geometry/datum args are always passed so the print still starts
        on the plate and the exit move clears it. The footprint is anchored at the
        g-code origin (0,0) and the workers are centre-based, so the centre is half the
        size."""
        cx, cy = self._plate_center_xy() or (0.0, 0.0)
        args = ["--plate-shape", self.plate_shape.get().strip(),
                "--plate-thickness", self.plate_thickness.get().strip(),
                "--plate-center-x", repr(cx),
                "--plate-center-y", repr(cy),
                "--plate-top-z", repr(self._plate_top_z())]
        if self.plate_shape.get() == "circular":
            args += ["--plate-diameter", self.plate_diameter.get().strip()]
        else:
            args += ["--plate-size-x", self.plate_x.get().strip(),
                     "--plate-size-y", self.plate_y.get().strip()]
        # g-code origin datum: shift the whole exported scene by this offset
        dx, dy, dz = self._origin_offset()
        args += ["--origin-offset", repr(dx), repr(dy), repr(dz)]
        if self.show_plate.get():
            args.append("--plate")
        return args

    def _scaffold_origin_xy(self):
        """(dx, dy) added to every scaffold / stabilization Start X/Y so the print is
        generated at the chosen g-code-origin datum + Start; the origin re-base then
        lands it at Start relative to that datum. So Start X/Y are measured from the
        collector origin POINT (the chosen corner / face-centre) and the print follows
        it. The XY mirror of how Start Z rides the plate top. The default (Bottom-left)
        gives (0,0) -> absolute, today's behaviour."""
        px, py, _pz = self._origin_datum()
        return (px, py)

    # -- a zero change keeps the scaffold where it is on the plate ---------- #
    # Start X/Y are g-code numbers, measured from zero (`_scaffold_origin_xy`).
    # So when zero moves on the plate -- the corner or centre, a picked Custom
    # point, or a plate size that moves a corner -- every plate Start X/Y is
    # rewritten by the same shift: the file's numbers change and the print
    # stays put. A load sets Start values and zero as one state, so
    # `_loading_preset` takes the new zero as it is -- History's Reproduce
    # restores both. A saved DESIGN preset does not hold the zero (the
    # printer does): it records the plate point its Start X/Y were measured
    # from, and `_zero_design_loaded` moves them onto this printer's zero. The
    # shift is path-independent (Bottom-left -> Top-right -> Bottom-left gives
    # the numbers back), so Undo needs nothing of its own. The mandrel needs
    # nothing either: a tube is placed on the rod (`_mandrel_axial_layout`)
    # and its zero only re-bases the program.
    _ZERO_PLATE_VARS = ("origin_anchor", "origin_custom", "plate_shape",
                        "plate_x", "plate_y", "plate_diameter")

    def _zero_follow_install(self):
        self._zero_last_xy = self._zero_plate_xy()
        for name in self._ZERO_PLATE_VARS:
            getattr(self, name).trace_add(
                "write", lambda *_a: self._zero_moved())

    def _rotary_zero_moved(self):
        """The rod's zero changed: every picture and the page's text follow
        (`_pv_all_dirty` reaches the G-code page; the tube's own preview
        redraws its triad)."""
        try:
            if hasattr(self, "_pv_states"):
                self._pv_all_dirty()
            fn = getattr(self, "_schedule_mandrel_preview", None)
            if callable(fn):
                fn()
        except (AttributeError, tk.TclError):
            pass        # a UI still being built: its first draw reads them

    #: where a saved design preset keeps the plate point (from the plate's
    #: bottom-left corner) that its Start X/Y are measured from
    DESIGN_ZERO_KEY = "_zero_on_plate"

    def _zero_on_plate(self):
        """Zero's (x, y) on the plate, from its bottom-left corner, while the
        collector is the plate; else None."""
        coll = getattr(self, "s_collector", None)
        if coll is None or coll.get() != "plate":
            return None
        return self._zero_plate_xy()

    #: ... and the zero's name as the header says it ("plate's centre"), so a
    #: load can say what the design was saved with
    DESIGN_ZERO_NAME_KEY = "_zero_name"

    def _zero_design_saved(self, data):
        """A design preset being saved records the zero its Start X/Y are
        measured from."""
        z = self._zero_on_plate()
        if z is not None:
            data[self.DESIGN_ZERO_KEY] = [round(z[0], 6), round(z[1], 6)]
            try:
                data[self.DESIGN_ZERO_NAME_KEY] = self._zero_name_now()
            except (AttributeError, ValueError, tk.TclError):
                pass

    def _zero_name_now(self):
        """This printer's plate zero in words, for a sentence: "the plate's
        centre", or a picked point by where it is."""
        name = self._gcode_zero_short()
        if name == "custom":
            z = self._zero_plate_xy() or (0.0, 0.0)
            return ("a point X%s Y%s from the plate's bottom-left corner"
                    % (_zero_mm(z[0]), _zero_mm(z[1])))
        return "the " + name

    def _zero_design_note(self):
        """The sentence after "Loaded design X." when that load moved its
        Start X/Y onto this printer's zero (`_zero_design_loaded`), else "".
        Read once: the next load starts with none."""
        note = getattr(self, "_zero_design_said", "") or ""
        self._zero_design_said = ""
        return note

    #: every plate Start X/Y a design preset holds: each scaffold's, the
    #: designer's and the G-code page's, and the stabilization base's
    #: (`_zero_start_vars` names the same fields)
    _DESIGN_START_KEY = re.compile(r"^(?:s\d*|gce\.s\d+)\.(?:stab_)?start_[xy]$")

    def _zero_design_loaded(self, data):
        """A saved design preset is about to load: its data, with the plate
        Start X/Y moved onto this printer's zero, so the scaffold lands where
        it was saved on the plate. The data is moved, not the fields after
        the load: moved afterwards, the G-code page drew the design at its
        old numbers first and kept the move as an edit of its own, so Undo
        took the design off its place and left the Start X/Y moved. Reported: with the zero at Center, every demo loaded
        with its Start X/Y measured from Center and ran off the plate. A
        preset saved before the zero was recorded was measured from the
        bottom-left corner (0, 0), the only zero there was then and the
        shipped printer's.

        When they move, `_zero_design_note` has the sentence that says so:
        the zero the design was saved with, this printer's, and that the
        design kept its place on the plate, so its numbers changed. The
        status line carries it, as it carries which physics a load prints
        with: the design is already right, so nothing waits for an answer."""
        self._zero_design_said = ""
        now = self._zero_on_plate()
        if now is None or not isinstance(data, dict):
            return data
        recorded = data.get(self.DESIGN_ZERO_KEY)
        try:
            sx, sy = (float(v) for v in recorded or (0.0, 0.0))
        except (TypeError, ValueError):
            return data
        dx, dy = now[0] - sx, now[1] - sy
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return data
        if recorded:
            then = data.get(self.DESIGN_ZERO_NAME_KEY) or (
                "a point X%s Y%s from the plate's bottom-left corner"
                % (_zero_mm(sx), _zero_mm(sy)))
        else:
            then = "the plate's bottom-left corner"
        try:
            here = self._zero_name_now()
        except (AttributeError, ValueError, tk.TclError):
            here = "elsewhere on the plate"
        moved = " ".join("%s%s%s" % (L, "+" if d < 0 else "", _zero_mm(-d))
                         for L, d in (("X", dx), ("Y", dy)) if abs(d) >= 1e-9)
        self._zero_design_said = (
            "It was saved with its zero at %s; this printer's zero is %s. "
            "It is in the same place on the plate, so its Start X/Y "
            "changed by %s mm." % (then, here, moved))
        out = dict(data)
        for key, value in data.items():
            if not self._DESIGN_START_KEY.match(key):
                continue
            d = dx if key.endswith("x") else dy
            if abs(d) < 1e-9:
                continue
            try:
                out[key] = _zero_mm(float(str(value).strip()) - d)
            except (TypeError, ValueError):
                continue
        return out

    def _zero_plate_xy(self):
        try:
            px, py, _pz = self._origin_datum()
        except (AttributeError, ValueError, tk.TclError):
            return None
        return (float(px), float(py))

    def _zero_start_vars(self):
        """Every plate Start X/Y pair: each designer scaffold's, each G-code
        page scaffold's, and the stabilization base's."""
        pairs = []
        for name in ("scaf_vars", "gce_scaf_vars"):
            for v in getattr(self, name, None) or ():
                if isinstance(v, dict) and "start_x" in v and "start_y" in v:
                    pairs.append((v["start_x"], v["start_y"]))
        sx, sy = (getattr(self, "stab_start_x", None),
                  getattr(self, "stab_start_y", None))
        if sx is not None and sy is not None:
            pairs.append((sx, sy))
        return pairs

    def _zero_moved(self):
        new = self._zero_plate_xy()
        old, self._zero_last_xy = getattr(self, "_zero_last_xy", None), new
        if (old is None or new is None
                or getattr(self, "_loading_preset", False)):
            return
        dx, dy = new[0] - old[0], new[1] - old[1]
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return
        for vx, vy in self._zero_start_vars():
            for var, d in ((vx, dx), (vy, dy)):
                if abs(d) < 1e-9:
                    continue
                try:
                    value = float(str(var.get()).strip())
                except (ValueError, tk.TclError):
                    continue        # a half-typed field keeps what it says
                var.set(_zero_mm(value - d))
        # the G-code page's own edits (a park move, a stitched control, a
        # pasted block) name plate places too: they stay put the same way
        carry = getattr(self, "_gce_zero_carry", None)
        if callable(carry):
            carry(-dx, -dy)

    def _viewer_origin_arg(self):
        """`--origin dx,dy,dz` (joined form) for the player: the g-code origin re-base
        it must UNDO so the rendered scene is invariant to the origin choice. The '='
        form survives a negative value (argparse won't read the leading '-' as a flag).
        No bounds/home args any more -- the machine-bounds box is gone."""
        dx, dy, dz = self._origin_offset()
        return ["--origin={},{},{}".format(repr(dx), repr(dy), repr(dz))]

    def _machine_field_error(self, pair_table):
        """The first printer-model number that does not parse, in words, or
        None. A silent fallback would be plausible and wrong: Position on the
        bed "abc" drawing the plate at the bed corner, STL scale "abc" taking 1,
        a typo in the bed datum dropping it for auto-detect."""
        def name(widget, fallback):
            try:
                return widget.cget("text").strip().rstrip(":") or fallback
            except Exception:  # noqa: BLE001
                return fallback

        def bad(var):
            try:
                float(var.get())
                return False
            except (ValueError, TypeError, tk.TclError):
                return True
        where = " It is on Setup's %s card (Printer & collector)."
        if not pair_table:
            pos = name(getattr(self, "_slide_offset_label", None),
                       "Position on the bed")
            for axis, var in (("X", self.slide_offset_x),
                              ("Y", self.slide_offset_y)):
                if bad(var):
                    return "%s %s must be a number (mm).%s" % (
                        pos, axis, where % "Collector")
        if bad(self.machine_scale):
            return "STL scale must be a number.%s" % (where % "Printer model")
        datum = (self.bed_datum_z, self.bed_corner_x, self.bed_corner_y)
        if all(v.get().strip() for v in datum):     # blank = auto-detect
            labels = getattr(self, "_bed_datum_field_labels", ()) or (None,) * 3
            for lbl, var in zip(labels, datum):
                if bad(var):
                    return ("%s must be a number, or leave all three blank "
                            "to detect it.%s" % (name(lbl, "Bed datum"),
                                                 where % "Calibrate"))
        return None

    def _machine_args(self):
        """`--machine <profile.json>` for the player when the printer model is on and a
        printhead STL is set, else []. The profile is written to the session scratch
        folder from the GUI fields, so the player just reads a path -- and only the
        player, not the converter, understands it. Off or no printhead -> [] -> the
        player keeps its single nozzle marker. Emits the role-keyed schema
        (head/gantry/bed) with the chosen kinematics chain, the bed-anchored slide
        offset, per-part motion limits and the nozzle-tip + bed contact overrides."""
        if not self.machine_show.get():
            return []
        xstl = self.machine_x_stl.get().strip()
        if not xstl:
            return []

        def _f(var, default=0.0):
            try:
                return float(var.get())
            except (ValueError, tk.TclError):
                return default

        kin = self._machine_kinematics_key()
        # A five-axis TABLE takes its collector XY from joint 1's picked axis, so
        # the slide offset is not a second opinion about the same number -- it is
        # zero, and its row is off the page (`_sync_rotary_enable`). Emitting the
        # var's stale value instead is precisely the silent-hidden-field seam
        # this page has been bitten by: the field is invisible, the number is
        # not.
        pair_table = (self.rotary_on.get()
                      and self._rotary_kind_key() == "pair")
        # ...and REFUSE a scaled model that carries a picked rotation centre,
        # rather than writing a profile `MachineProfile.from_dict` will raise on.
        # No printer model is a visible, recoverable state the user can read the
        # reason for; an exception out of the player is neither. Deliberately
        # NOT "drop the pivots and carry on": a pivot silently taken as the
        # origin is the plausible-and-wrong geometry this whole slice exists to
        # avoid.
        bad_scale = self._pivot_scale_conflict()
        if bad_scale is not None:
            # the whole reason goes to the log; the status line says the fix
            self.append_log(self._PIVOT_SCALE_MSG % bad_scale + "\n")
            self._refuse("Printer model not used: a rotation center needs STL "
                         "scale 1, and this model's is %s. Export the STLs in "
                         "mm and set STL scale to 1 (the Log page has why)."
                         % bad_scale, log=False)
            return []
        bad_field = self._machine_field_error(pair_table)
        if bad_field is not None:
            self._refuse("Printer model not used: " + bad_field, log=False)
            return []
        profile = {
            "name": "machine",
            "kinematics": kin,
            "parts": {
                "head": xstl,
                "gantry": self.machine_y_stl.get().strip() or None,
                "bed": self.machine_z_stl.get().strip() or None,
            },
            "static_stl": self.machine_static_stl.get().strip() or None,
            "standoff": _f(self._standoff_var(), 2.0),
            "scale": _f(self.machine_scale, 1.0),
            "slide_offset": ([0.0, 0.0] if pair_table else
                             [_f(self.slide_offset_x),
                              _f(self.slide_offset_y)]),
        }
        # manual bed datum: emitted only when all three are filled (else auto-detect)
        bd = [self.bed_datum_z.get().strip(), self.bed_corner_x.get().strip(),
              self.bed_corner_y.get().strip()]
        if all(bd):
            try:
                profile["bed_top_z"] = float(bd[0])
                profile["bed_corner"] = [float(bd[1]), float(bd[2])]
            except ValueError:
                pass
        # custom chain: per-part driven axes + which body the head/bed ride on (a
        # preset defines its own chain, so this is consulted only for "custom"). See
        # the parent-choice comment in the builder for why the bed-parent matters.
        if kin == "custom":
            profile["axes"] = {
                part: [a for a, v in zip("xyz", vs) if v.get()]
                for part, vs in self._machine_axis_vars.items()
            }
            profile["parents"] = {
                "head": self.machine_head_parent.get().strip() or "gantry",
                "gantry": "frame",
                "bed": self.machine_bed_parent.get().strip() or "frame",
            }
            # An orphaned driving body renders no motion on its axis; the tab already
            # shows a live warning, but catch it once more here so it can't slip into a
            # play/export unnoticed. Proceed after the warning (the player degrades to
            # the axes that ARE on a chain rather than crashing).
            orphans = self._machine_custom_orphans()
            if orphans:
                who = "; ".join(f"{r} drives {'/'.join(a).upper()}"
                                for r, a in orphans.items())
                self._refuse(f"With the current Custom kinematics, {who} but rides neither "
                    "the printhead nor the bed chain, so that axis will not move in "
                    "the printer view.\n\nSet 'Printhead rides: gantry' (or 'Bed "
                    "rides: gantry') on the Printer & collector page to make it "
                    "move.")
        # per-part motion limits: include an axis only when BOTH min/max parse and the
        # part actually drives it (so a stale value on a hidden axis is ignored).
        limits = {}
        for role in ("head", "gantry", "bed"):
            driven = self._driven_axes(role)
            axmap = {}
            for ax in driven:
                mn, mx = self.machine_limits[(role, ax)]
                smn, smx = mn.get().strip(), mx.get().strip()
                if smn and smx:
                    try:
                        axmap[ax] = [float(smn), float(smx)]
                    except ValueError:
                        pass
            if axmap:
                limits[role] = axmap
        if limits:
            profile["limits"] = limits
        # nozzle-tip override: only when all three are filled (else auto-detect)
        tip = [self.machine_tip_x.get().strip(), self.machine_tip_y.get().strip(),
               self.machine_tip_z.get().strip()]
        if all(tip):
            try:
                profile["tip"] = [float(v) for v in tip]
            except ValueError:
                pass
        # bed-top Z nudge, a manual bed-actor offset on top of the contact alignment
        nudge = _f(self.machine_bed_nudge, 0.0)
        if nudge:
            profile["part_offsets"] = {"bed": [0.0, 0.0, nudge]}
        # ROTARY COLLECTOR (4-axis): the MOUNT only. `radius`/`length` are the JOB's
        # and the player installs them from the tube being printed, so they are not
        # written here (machine_model.RotaryAxis). Omitted entirely when no stage is
        # fitted, which is what keeps a 3-axis profile byte-identical.
        if self.rotary_on.get() and self._rotary_kind_key() == "pair":
            # A FIVE-AXIS COLLECTOR TABLE: two joints under "rotaries", which is
            # what `--surface` requires and what `_rotaries_from_dict` reads in
            # preference to the single "rotary". Written INSTEAD of that key, not
            # beside it: a profile carrying both would resolve to the pair and
            # leave the mandrel key as a silent second opinion.
            pair = self._rotary_pair_joints()
            if pair:
                profile["rotaries"] = pair
                # The page says this too (`_sync_axis_datum_note`); the run log
                # says it because a machine.json outlives the screen it was
                # authored on, exactly as an uncalibrated mandrel axis is
                # reported by `gcode_player._log_placement`.
                log = getattr(self, "append_log", None)
                if callable(log):
                    log("[printer] five-axis collector table: %s about %s, then %s "
                        "about %s. UNCALIBRATED: collector assumed concentric "
                        "with the %s axis, and the %s tilt axis assumed through "
                        "the collector origin.\n"
                        % (pair[0]["word"], pair[0]["axis"].upper(),
                           pair[1]["word"], pair[1]["axis"].upper(),
                           pair[0]["word"], pair[1]["word"]))
        elif self.rotary_on.get():
            rot = {"word": (self.rotary_word.get().strip().upper() or "A"),
                   "role": _ROTARY_ROLE}
            # No "stl" key any more: the holder is the body in the BED slot
            # (`parts["bed"]`), not a decorative extra beside it. `RotaryAxis.stl`
            # survives for a machine.json written before this, and the player still
            # draws it.
            profile["rotary"] = rot
        # per-part visibility (sim-only): the roles ticked "Hide". A hidden printhead
        # is swapped for the nozzle frustum in the player so the tool still reads; the
        # rest simply aren't drawn. Omitted entirely when nothing is hidden.
        hidden = [role for role, var in (("head", self.machine_hide_head),
                                         ("gantry", self.machine_hide_gantry),
                                         ("bed", self.machine_hide_bed),
                                         ("frame", self.machine_hide_frame))
                  if var.get()]
        if hidden:
            profile["hidden_parts"] = hidden

        path = os.path.join(SESSION_DIR, "machine.json")
        try:
            with open(path, "w") as fh:
                json.dump(profile, fh, indent=2)
        except OSError:
            return []
        return ["--machine", path]
