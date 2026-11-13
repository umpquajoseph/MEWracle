#!/usr/bin/env python3
"""gui_presets.py -- Presets mixin: the named-preset stores, the persist map of
every saved Tk var, and snapshot/apply/load/save/rename/delete/import/export.

There are FOUR independent preset kinds, sharing one generic code path keyed by a
`kind` string. **The kind id, the folder and the user-visible label are three
different names, and two of them do not match** -- so this table is the
authority, not the id you happen to be reading:

  kind id       folder                constant                  label shown
  ----------------------------------------------------------------------------
  "scaffold"    presets/scaffold/     PRESETS_DIR               "preset"
  "machine"     presets/machine/      MACHINE_PRESETS_DIR       "machine preset"
  "calibration" presets/physics/      CALIBRATION_PRESETS_DIR   "Physics preset"
  "sweep:<tab>" presets/calibration/<tab>/  (per tab)           "Calibration preset"

All of them sit in the one `presets/` folder, with the pointer file
`presets/defaults.json`; gui_common.migrate_preset_layout moves an older
install's top-level stores (presets_machine/ and the rest, and the old
pointer file) in.

  * "scaffold"    -- the main preset: every tab EXCEPT the Machine and Collector
    tab. Its comparison grid has a child row per scaffold.
  * "machine"     -- the Machine and Collector tab alone (machine bounds, build
    plate, g-code origin). Flat grid, one row per preset.
  * "calibration" -- the PHYSICS profile: the rig/material anchors (cal.*) plus
    the process knobs (sim.*), saved by the Physics page. Flat grid.
  * "sweep:<tab>" -- the CALIBRATION page's print designs, ONE KIND PER TAB
    (<tab> is the calibration_procedures key: cts, cts4, viscosity,
    activation_energy, lag3, lag4, sag), each holding only that tab's sw.* keys
    (gui_sweeps._sweep_tab_keys) in its own subfolder. Flat grid. The bare id
    "sweep" is still a KEY GROUP -- every sw.* key, which the scaffold kind
    subtracts and Print History snapshots -- but no store and no folder.

The last two ids are historical: the physics profile's folder was renamed
presets_calibration/ -> presets_physics/ (now presets/physics/) so the Calibration page's own presets
could take the name that describes them. The ids were left alone because they
are read outside this file and are already written into users' pointer files,
so renaming an id breaks saved data while renaming a folder needs only the
one-time move in gui_common.migrate_preset_folders (run from _init_pstore).

Which persist keys belong to "machine" is decided by _machine_persist_keys()
(gui_buildplate); "scaffold" is everything else. The split is applied on BOTH
snapshot and apply, so a machine preset never disturbs the scaffold fields and
vice versa -- even for a legacy scaffold preset that still carries machine keys.

Per-kind mutable state lives in self._pstores[kind]; every kind's default-preset
pointer(s) share the one presets/defaults.json.

**"scaffold" and "machine" keep ONE DEFAULT PER COLLECTOR MODE**, because a
3-axis machine cannot print a 4-axis design: the pair that boots together has to
agree. So those two kinds have two pointers each -- see AXIS_MODES -- and "Set as
default" asks which mode it means. The 3-axis pointer IS the legacy "default" /
"machine_default" key, so an existing install boots exactly as it did; only the
"_4axis" keys are new. "calibration" and the "sweep:<tab>" kinds describe the rig
and its calibration prints, which do not change with the collector, so they keep a
single pointer each and their dialogue-free button.

Which default is LIVE is not a stored mode: startup always applies the 3-axis one
(_apply_default_preset), and the 4-axis pair loads when the user CLICKS the
Scaffold designer's Collector radio (_collector_mode_chosen). MEWracle Lite has no
mandrel collector at all, so its presets.js transcription keeps one default per
kind -- that is an absent mode, not a parity gap."""
from gui_common import *
import gui_prefs
import preset_store
import gui_form
import gui_tips
import gui_units
from gui_revert import _RevertMixin

#: a cell that holds a plain number (not "Yes", a name or a path)
_NUMERIC = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
#: a grid heading that ends in a speed or pressure unit
_HEAD_UNIT = re.compile(r"\((mm/min|mm/s|psi)\)\s*$")


def _in_module_card(widget):
    """True when `widget` sits inside a ModuleGrid card body (the modern
    shell), whose card already opens with its own intro line."""
    while widget is not None:
        if getattr(widget, "_form_card", False):
            return True
        widget = getattr(widget, "master", None)
    return False


# Columns of the scaffold "Compare presets" table: the per-scaffold persist keys
# (the suffix after each scaffold's s./sN. prefix) paired with a short heading.
# There are far more than fit the window, which is why the table scrolls sideways.
PRESET_TABLE_COLS = [
    ("width", "Width X"), ("height", "Length Y"),
    ("stepover", "Fiber spacing"), ("stepover_end", "Spacing, far edge"),
    ("grade_angle", "Grade dir."), ("stepover_top", "Spacing, top layer"),
    ("diameter", "Fiber Ø"), ("layers", "Layers"), ("speed", "Print speed"),
    ("loop_speed", "Loop speed"), ("start_x", "Start X"), ("start_y", "Start Y"),
    ("start_z", "Start Z"), ("start_angle", "Start angle"),
    ("rotation", "Layer rot."), ("arc_radius_mult", "Turnaround radius ×"),
    ("arc_segments", "Arc segs"), ("color", "Color"),
    ("angle_pattern_on", "Angle patt."), ("angle_pattern", "Angle list"),
    ("angle_stepover_pattern", "Angle stepovers"),
    ("angle_offset_pattern", "Angle offsets"),
    ("linear_on", "Linear patt."), ("linear_wavelength", "Wavelength"),
    ("linear_spacing", "Spacing"),
    ("linear_pattern", "Linear pts"),
    ("shape", "Shape"), ("shape_sides", "Sides"), ("shape_polygon", "Shape pts"),
    ("array_on", "Array"),
    ("array_nx", "Cols"), ("array_ny", "Rows"), ("array_nz", "Layers"),
    ("array_dx", "X spacing"), ("array_dy", "Y spacing"),
    ("array_dz", "Z spacing"), ("array_flip_x", "Flip X"),
    ("array_flip_y", "Flip Y"), ("array_travel", "Travel"),
]
# The PRESET_TABLE_COLS entries a 4-axis (mandrel) design actually uses: the
# SHARED scaffold fields the tab merge collapsed the mandrel's duplicates onto.
# Every other column is planar-only and is blanked for a tube.
MANDREL_SHARED_COLS = {"diameter", "layers", "speed", "color"}
# wider columns for the free-text / hex fields; the rest use PRESET_COL_DEFAULT
PRESET_COL_WIDTHS = {
    "angle_pattern": 130, "linear_pattern": 130, "shape_polygon": 130,
    "angle_stepover_pattern": 120, "angle_offset_pattern": 120,
    "color": 70, "rotation": 90,
}
PRESET_COL_DEFAULT = 80

# Headings for the machine-preset grid. The columns themselves are derived at
# runtime from _machine_persist_keys(); this just gives each key a short heading
# -- an unmapped key falls back to the raw key.
MACHINE_PRESET_COL_LABELS = {
    "plate.shape": "Plate shape", "plate.x": "Plate X", "plate.y": "Plate Y",
    "plate.diameter": "Plate Ø", "plate.thickness": "Thickness",
    "plate.show": "Show plate",
    "origin.face": "Zero face", "origin.anchor": "Zero corner",
    "origin.custom": "Custom zero", "rotary.origin_custom": "Custom rod zero",
    "origin.add_elevation_z": "Z+gap",
    "origin.no_layer_z": "No per-layer Z",
    "origin.no_scaffold_z": "No per-scaffold Z",
    "origin.custom_gcode": "Custom g-code",
    "origin.custom_gcode_after": "After line",
    "origin.gcode_flavor": "G-code flavor",
    "origin.gcode_flavor_custom": "Saved flavor (carried copy)",
    "origin.strip_comments_on_save": "Strip comments",
    "origin.layer_markers": "Mark each layer",
    "origin.gcode_start": "Start of print",
    "origin.gcode_end": "End of print",
    "origin.omit_start_end": "Leave out start and end",
    "origin.gcode_rules": "Automatic commands",
    "origin.preflight_accepted": "Save does not ask about",
    "origin.feed_time_base": "Feed time unit",
    "origin.incremental": "Incremental",
    "origin.machine_x": "Zero at printer X",
    "origin.machine_y": "Zero at printer Y",
    "origin.machine_z": "Zero at printer Z",
    "origin.machine_r1": "Zero at rotary 1",
    "origin.machine_r2": "Zero at rotary 2",
    "sim.max_accel": "Linear accel", "sim.scv": "Corner speed",
    "sim.rotary_accel": "Rotary accel",
    # printer model (3D simulation)
    "machine.show": "Show model", "machine.kinematics": "Kinematics",
    "machine.hide_head": "Hide head", "machine.hide_gantry": "Hide gantry",
    "machine.hide_bed": "Hide bed", "machine.hide_frame": "Hide frame",
    "machine.x_stl": "Head STL", "machine.y_stl": "Gantry STL",
    "machine.z_stl": "Bed STL", "machine.static_stl": "Frame STL",
    # "Elevation" heads whichever elevation key the machine preset carries: today
    # sim.standoff. Both are mapped (an unused entry is harmless).
    "machine.standoff": "Elevation", "sim.standoff": "Elevation",
    "machine.scale": "STL scale",
    "slide.offset_x": "Slide X", "slide.offset_y": "Slide Y",
    "bed.datum_z": "Bed Z", "bed.corner_x": "Bed cnr X",
    "bed.corner_y": "Bed cnr Y",
    "machine.head_parent": "Head rides", "machine.bed_parent": "Bed rides",
    "machine.head_x": "Head X", "machine.head_y": "Head Y",
    "machine.head_z": "Head Z", "machine.gantry_x": "Gantry X",
    "machine.gantry_y": "Gantry Y", "machine.gantry_z": "Gantry Z",
    "machine.bed_x": "Bed X", "machine.bed_y": "Bed Y", "machine.bed_z": "Bed Z",
    "machine.tip_x": "Nozzle X", "machine.tip_y": "Nozzle Y", "machine.tip_z": "Nozzle Z",
    "machine.bed_nudge": "Bed nudge",
    # the five-axis table's two bodies and their picked axes of rotation
    "rotary.j1_stl": "J1 body", "rotary.j1_pivot": "J1 axis",
    "rotary.j2_stl": "J2 body", "rotary.j2_pivot": "J2 axis",
    "rotary.home": "G-code zero",
    "rotary.j1_seat": "Collector seat",
}


# The collector modes the "scaffold" and "machine" preset kinds keep SEPARATE
# default pointers for: (s_collector value, pointer-key suffix, button label).
# The mode ids ARE s_collector's values, so there is no second vocabulary to keep
# in step. The 3-axis suffix is empty ON PURPOSE -- its pointer key stays the
# legacy "default" / "machine_default", so an install that predates this feature
# boots off exactly the key it already wrote.
AXIS_MODES = (
    ("plate",   "",       "Plate (3-axis)"),
    ("mandrel", "_4axis", "Mandrel (4-axis)"),
)
AXIS_LABEL = {m: label for m, _sfx, label in AXIS_MODES}
AXIS_SHORT = {m: label.split(" ")[0] for m, _sfx, label in AXIS_MODES}
# Shipped presets that changed name, old (lower-case) -> new. The machine is the
# MEWron 0.2; "MEWron (2)" and "MEWron 22G" were its earlier preset names, and a
# presets/defaults.json written before the rename still names them.
RENAMED_PRESETS = {
    "mewron (2)": "MEWron 0.2",
    "mewron 22g": "MEWron 0.2 22G",
}

# Startup applies this mode's defaults, always. There is no remembered mode: the
# other pair loads only from a Collector click, so a fresh boot is predictable.
BOOT_AXIS_MODE = "plate"


class _PresetsMixin(_RevertMixin):
    # -- per-kind store ----------------------------------------------------- #
    def _init_pstore(self, kind, directory, ptr_key, noun, per_axis=False):
        """Create (once) the mutable state bundle for a preset kind and load its
        files + default pointer(s) into it. Returns the store dict. Called from
        each kind's tab builder; the machine tab is built first, so its store
        registers first. Also the hook for the one-time moves into presets/
        (the old top-level layout, then calibration -> physics), which must run before the FIRST store of any
        kind reads its folder.

        `per_axis` splits the kind's default in two, one per AXIS_MODES entry.
        Either way the store carries `ptr_keys` (mode -> pointer key) and
        `defaults` (mode -> preset name or None), so every mutator below is
        written once; a single-default kind is just the map {None: key}."""
        # The layout move first, so seeding and the calibration -> physics
        # move both see presets/<kind>/. Seed BEFORE that second move:
        # seeding creates presets/physics/, which it reads as "already
        # migrated" and skips.
        migrate_preset_layout_once()
        migrate_preset_folders_once()
        migrate_user_presets_once()
        if per_axis:
            ptr_keys = {m: ptr_key + sfx for m, sfx, _label in AXIS_MODES}
        else:
            ptr_keys = {None: ptr_key}
        # `directory` is the USER folder (the only one written); its shipped
        # twin sits at the same place under SHIPPED_PRESETS_ROOT. A folder
        # outside PRESETS_ROOT (a test's scratch dir) has no shipped twin.
        shipped, folder = None, None
        try:
            rel = os.path.relpath(directory, PRESETS_ROOT)
        except ValueError:
            rel = os.pardir
        if not rel.startswith(os.pardir):
            folder = rel.replace(os.sep, "/")
            shipped = os.path.join(SHIPPED_PRESETS_ROOT, rel)
        store = {
            "kind": kind, "dir": directory, "ptr_keys": ptr_keys, "noun": noun,
            "shipped_dir": shipped, "folder": folder,
            "presets": [], "defaults": {m: None for m in ptr_keys},
            "table": None, "row_index": {}, "parent_iid": {},
        }
        self._pstores = getattr(self, "_pstores", {})
        self._pstores[kind] = store
        store["presets"], store["defaults"] = self._load_presets(kind)
        return store

    @staticmethod
    def _store_modes(st):
        """The mode keys of a store's default pointers: the AXIS_MODES ids for an
        axis-aware kind, or [None] for a single-default one."""
        return list(st["ptr_keys"])

    @classmethod
    def _is_per_axis(cls, st):
        return None not in st["ptr_keys"]

    # -- the Presets tabs (each kind's list + parameter preview) ------------ #
    def _build_presets_tab(self, parent):
        """The scaffold Presets tab: the comparison grid on the left IS the preset
        list -- each preset is a group-header row you select to act on, with its
        per-scaffold parameters as child rows -- and the management buttons sit on
        the right. Captures every tab EXCEPT the Machine and Collector tab, which
        has its own presets. Each preset is its own JSON file in PRESETS_DIR.
        Purely a convenience layer over _persist_map / _apply_preset_for_kind: it
        never touches the launch path."""
        self._init_pstore("scaffold", PRESETS_DIR, "default", "preset",
                          per_axis=True)
        self._build_one_presets_tab(
            parent, "scaffold",
            ("Save the current settings of every tab as a named preset, then "
             "load it back any time. The Printer and Collector tab has its own "
             "presets. Each preset’s per-scaffold parameters sit beneath its "
             "row. Scroll sideways for the columns past the right edge."))

    def _build_machine_presets_tab(self, parent):
        """The machine Presets tab. Looks and functions exactly like the scaffold
        Presets tab but captures ONLY the Machine and Collector fields (machine
        bounds, build plate, g-code origin), so a machine/collector setup can be
        reused across scaffold recipes. One file per preset in
        MACHINE_PRESETS_DIR."""
        self._init_pstore("machine", MACHINE_PRESETS_DIR, "machine_default",
                          "printer preset", per_axis=True)
        self._build_one_presets_tab(
            parent, "machine",
            ("Save the Printer and Collector tab as a named printer preset, "
             "then load it back any time. It covers the printer, the build "
             "plate or collector, and G-code zero, and is independent of the "
             "scaffold presets."))

    def _build_one_presets_tab(self, parent, kind, blurb, repeat_ok=False):
        """Shared layout for a preset kind's tab: the comparison grid (which IS the
        preset list) on the left, the management buttons on the right. The buttons
        all carry `kind`, so the same handlers drive every kind.

        `blurb` is the tab's intro line. Inside a modern ModuleGrid card the card
        already opens with its own intro saying the same thing, so the blurb is
        not drawn there unless `repeat_ok` says it carries something the card's
        does not (the calibration print's "Presets of the <tab> tab.")."""
        pad = self.pad
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)   # the grid takes the stretch
        # the card's one intro line, through the shared row builder
        if repeat_ok or not _in_module_card(parent):
            gui_form.Form(parent).intro(blurb, wrap=560)

        tablef = ttk.Frame(parent)
        tablef.grid(row=1, column=0, sticky="nsew", **pad)
        self._build_preset_table(tablef, kind)

        btns = ttk.Frame(parent)
        btns.grid(row=1, column=1, sticky="n", **pad)
        load = ttk.Button(btns, text="Load selected", width=20,
                          command=lambda: self._preset_load(kind))
        load.pack(fill="x", pady=2)
        gui_tips.add_tip(load, "Load the selected preset. Double-clicking its "
                               "row does the same.")
        ttk.Separator(btns, orient="horizontal").pack(fill="x", pady=6)
        ttk.Button(btns, text="Save current as new…", width=20,
                   command=lambda: self._preset_save_new(kind)).pack(
            fill="x", pady=2)
        ttk.Button(btns, text="Update selected", width=20,
                   command=lambda: self._preset_update(kind)).pack(
            fill="x", pady=2)
        ttk.Button(btns, text="Rename selected…", width=20,
                   command=lambda: self._preset_rename(kind)).pack(
            fill="x", pady=2)
        ttk.Button(btns, text="Delete selected", width=20,
                   command=lambda: self._preset_delete(kind)).pack(
            fill="x", pady=2)
        reset = ttk.Button(btns, text="Reset to shipped", width=20,
                           command=lambda: self._preset_reset(kind))
        reset.pack(fill="x", pady=2)
        gui_tips.add_tip(reset, "Put a preset that came with MEWracle back "
                                "the way it shipped, or bring back the ones "
                                "you deleted.")
        ttk.Separator(btns, orient="horizontal").pack(fill="x", pady=6)
        # share a single preset in/out as its own file
        # named from the data folder down, so a calibration tab's subfolder
        # reads "presets\calibration\cts" rather than a bare "cts"
        folder = os.path.relpath(self._pstores[kind]["dir"], USER_DATA_DIR)
        imp = ttk.Button(btns, text="Import…", width=20,
                         command=lambda: self._preset_import(kind))
        imp.pack(fill="x", pady=2)
        gui_tips.add_tip(imp, "Add a preset from a file someone shared.")
        exp = ttk.Button(btns, text="Export selected…", width=20,
                         command=lambda: self._preset_export(kind))
        exp.pack(fill="x", pady=2)
        gui_tips.add_tip(exp, "Save the selected preset as a file to share. "
                              "Each preset is already its own file in the "
                              "“%s” folder." % folder)
        ttk.Separator(btns, orient="horizontal").pack(fill="x", pady=6)
        # Marks the selected preset as a default; clearing it (re-pressing on the
        # current default) restores "no default = blank boot". For "scaffold" and
        # "machine" it first ASKS which collector the default is for, so 3-axis
        # and 4-axis work each boot a machine that can print the design.
        ttk.Button(btns, text="Set as default", width=20,
                   command=lambda: self._preset_set_default(kind)).pack(
            fill="x", pady=2)

        # pre-select the first preset so the buttons + parameter preview are
        # immediately meaningful (selecting never loads -- only the buttons do)
        self._refresh_preset_list(
            kind, select=0 if self._pstores[kind]["presets"] else None)

    # -- the preset grid ---------------------------------------------------- #
    def _preset_flat_keys(self, kind):
        """The column keys for a FLAT preset grid (one row per preset): the kind's
        persist keys for "machine" and "calibration", or None for "scaffold" (which
        uses the per-scaffold PRESET_TABLE_COLS grouped grid instead)."""
        if kind == "machine":
            return list(self._machine_persist_keys())
        if kind == "calibration":
            return list(self._calibration_persist_keys())
        if kind == "sweep":
            return list(self._sweep_persist_keys())
        if self._sweep_tab_of(kind):
            return list(self._sweep_tab_keys(self._sweep_tab_of(kind)))
        return None

    @staticmethod
    def _sweep_tab_of(kind):
        """The Calibration tab key of a per-tab kind "sweep:<tab>", else None."""
        if isinstance(kind, str) and kind.startswith(SWEEP_KIND_PREFIX):
            return kind[len(SWEEP_KIND_PREFIX):] or None
        return None

    def _preset_grid_labels(self, kind):
        """key -> short column heading for a flat grid; the calibration headings
        come from the mew_calibration registry (via the physics mixin's hook)."""
        if kind == "calibration":
            return self._calibration_grid_labels()
        if kind == "sweep" or self._sweep_tab_of(kind):
            return self._sweep_grid_labels()
        return MACHINE_PRESET_COL_LABELS

    def _build_preset_table(self, container, kind):
        """Build the comparison grid into `container` for `kind` and stash it on the
        kind's store. The scaffold grid has one group-header row per preset and one
        child row per scaffold it defines; the machine grid is flat. Either way the
        grid IS the preset list -- selecting any row picks that preset for the
        buttons, double-clicking loads it -- and it has its OWN horizontal
        scrollbar. (Re)filled by _refresh_preset_table."""
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        flat_keys = self._preset_flat_keys(kind)
        if flat_keys is not None:                  # machine / calibration: flat grid
            labels = self._preset_grid_labels(kind)
            col_specs = [(k, labels.get(k, k)) for k in flat_keys]
            # labels, not ids -- kind "calibration" IS the physics profile and kind
            # "sweep" IS the Calibration page's designs (see the table at the top)
            name_head = ("Printer preset" if kind == "machine"
                         else "Physics preset" if kind == "calibration"
                         else "Calibration preset")
        else:
            # design presets: the name only, at full width. The per-scaffold
            # value columns cut the names off and read wrong, so they went.
            col_specs = []
            name_head = "Preset"
        cols = [k for k, _h in col_specs]
        tree = ttk.Treeview(container, columns=cols, show="tree headings",
                            height=12, selectmode="browse")
        tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(container, orient="horizontal", command=tree.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        tree.heading("#0", text=name_head, anchor="w")
        tree.column("#0", width=200, minwidth=140, stretch=not col_specs,
                    anchor="w")
        for key, head in col_specs:
            tree.heading(key, text=head)
            tree.column(key, width=PRESET_COL_WIDTHS.get(key, PRESET_COL_DEFAULT),
                        minwidth=40, stretch=False, anchor="center")
            self._fit_heading(tree, key)
        # light shading marks each preset's row (the group-header row for the
        # scaffold grid; the single preset row for the flat machine grid)
        tree.tag_configure("preset", background=getattr(
            self, "_preset_accent", "#e8eef7"))     # the theme's (gui_theme)
        if not isinstance(getattr(self, "_preset_trees", None), list):
            self._preset_trees = []
        self._preset_trees.append(tree)
        # double-click a row = load that preset (and swallow the tree's default
        # expand/collapse so the group doesn't fold out from under the cursor)
        tree.bind("<Double-Button-1>",
                  lambda _e, k=kind: self._on_preset_activate(k))
        st = self._pstores[kind]
        st["table"] = tree
        # iid -> preset index for every row, and the reverse preset index ->
        # (header) iid; both rebuilt on each _refresh_preset_table.
        st["row_index"] = {}
        st["parent_iid"] = {}
        # (scaffold grid) header iid -> lowercased preset name, so a rebuild can
        # carry the user's expand/collapse state across by name. Empty when flat.
        st["iid_name"] = {}

    def _on_preset_activate(self, kind):
        """Double-click handler: load the selected preset, then return "break" so
        the Treeview's own double-click (expand/collapse) doesn't also fire."""
        if self._selected_preset_index(kind) is not None:
            self._preset_load(kind)
        return "break"

    @staticmethod
    def _scaf_key(i, key):
        """Persist-map key for scaffold index `i` (0-based) and per-scaffold field
        `key`: scaffold 1 keeps the bare "s." prefix, the rest are "s2.", "s3."...
        Mirrors the prefixes built in _persist_map."""
        return ("s." if i == 0 else f"s{i + 1}.") + key

    @staticmethod
    def _preset_scaffold_count(data):
        """How many scaffolds a stored preset defines (its "s.count"), clamped to
        1..5; a missing/garbled value reads as 1."""
        try:
            return max(1, int(float(data.get("s.count", 1))))
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _fmt_cell(val):
        """Render a stored value for a table cell: booleans as Yes/No, a missing
        key or empty string as blank, everything else as its string."""
        if val is None:
            return ""
        if isinstance(val, bool):
            return "Yes" if val else "No"
        # a number reads as the fields show it, five significant figures:
        # a fitted 228.16090808099523 filled its cell with digits. Display
        # only -- the stored preset is untouched.
        if isinstance(val, (int, float)) or (
                isinstance(val, str) and _NUMERIC.match(val.strip())):
            try:
                return gui_units.fmt_number(float(val))
            except (TypeError, ValueError):
                pass
        return str(val)

    @staticmethod
    def _unit_columns(tree, keys):
        """{column key: (quantity, canonical unit)} for the grid columns whose
        heading names a speed or pressure unit, re-headed in the display unit
        Settings chose -- so the Physics grid's CTS reads in mm/min like the
        card above it, not in the mm/s the preset stores."""
        out = {}
        for key in keys:
            try:
                head = str(tree.heading(key, "text"))
            except tk.TclError:
                continue
            base = getattr(tree, "_mew_heads", {}).get(key, head)
            m = _HEAD_UNIT.search(base)
            if not m:
                continue
            canon = m.group(1)
            q = gui_units.quantity_of(canon)
            if q is None:
                continue
            tree.__dict__.setdefault("_mew_heads", {})[key] = base
            shown = gui_units.shown(q, canon)
            tree.heading(key, text=base[:m.start(1)] + shown + base[m.end(1):])
            _PresetsMixin._fit_heading(tree, key)
            out[key] = (q, canon, shown)
        return out

    @staticmethod
    def _fit_heading(tree, key):
        """Widen column `key` so its whole heading shows: at a fixed 80 px the
        Physics grid read "Nozzle inne" and "CTS (mm/m", losing the unit.
        Never narrows a column."""
        import tkinter.font as tkfont
        try:
            spec = ttk.Style(tree).lookup("Treeview.Heading", "font")
            font = tkfont.Font(root=tree, font=spec or "TkHeadingFont")
            need = font.measure(str(tree.heading(key, "text"))) + 18
            if need > int(tree.column(key, "width")):
                tree.column(key, width=need)
        except (tk.TclError, RuntimeError):
            pass

    def _refresh_preset_table(self, kind):
        """Rebuild the kind's comparison grid from its store, and rebuild the
        iid<->preset-index maps that make the grid the selection surface. The
        default preset's row is tagged (per axis mode, for the kinds that split
        it -- see _default_badge). A no-op until the table widget exists.
        Scaffold: one group-header row per preset, one child per scaffold.
        Machine: one flat row per preset."""
        st = self._pstores.get(kind)
        if st is None or st["table"] is None:
            return
        tree = st["table"]
        # carry the user's expand/collapse state across the rebuild, keyed by
        # (case-insensitive) preset name so it survives index shifts from add or
        # delete. Presets NOT in the set come up collapsed, so a fresh grid and
        # every newly-saved preset default to collapsed.
        open_names = set()
        for iid, name in st.get("iid_name", {}).items():
            if tree.exists(iid) and tree.item(iid, "open"):
                open_names.add(name)
        tree.delete(*tree.get_children())
        st["row_index"] = {}
        st["parent_iid"] = {}
        st["iid_name"] = {}
        flat_keys = self._preset_flat_keys(kind)
        if flat_keys is not None:                  # machine / calibration: flat grid
            units = self._unit_columns(tree, flat_keys)
            for pi, p in enumerate(st["presets"]):
                data = p.get("data", {})
                text = p["name"] + self._default_badge(kind, p["name"])
                vals = []
                for k in flat_keys:
                    raw = data.get(k)
                    if k in units and isinstance(raw, (str, int, float)) \
                            and not isinstance(raw, bool):
                        q, canon, shown = units[k]
                        raw = gui_units.convert_text(str(raw), q, canon, shown)
                    vals.append(self._fmt_cell(raw))
                iid = tree.insert("", "end", text=text, values=vals,
                                  tags=("preset",))
                st["row_index"][iid] = pi
                st["parent_iid"][pi] = iid
            return
        for pi, p in enumerate(st["presets"]):
            data = p.get("data", {})
            # A 4-axis preset describes ONE tube, not a stack of planar scaffolds,
            # so it gets one row and the planar-only columns stay blank -- reading
            # a Width X off a mandrel preset would be a lie.
            tube = data.get("s.collector") == "mandrel"
            n = 1 if tube else self._preset_scaffold_count(data)
            text = p["name"]
            if tube:
                text += "   · 4-axis mandrel"
            text += self._default_badge(kind, p["name"])
            parent = tree.insert("", "end", text=text,
                                 open=(p["name"].lower() in open_names),
                                 tags=("preset",))
            st["row_index"][parent] = pi
            st["parent_iid"][pi] = parent
            st["iid_name"][parent] = p["name"].lower()
            if not tree["columns"]:     # name-only design list: no child rows
                continue
            for i in range(n):
                if tube:
                    vals = [(self._fmt_cell(data.get(self._scaf_key(0, key)))
                             if key in MANDREL_SHARED_COLS else "")
                            for key, _h in PRESET_TABLE_COLS]
                    label = "Mandrel tube"
                else:
                    vals = [self._fmt_cell(data.get(self._scaf_key(i, key)))
                            for key, _h in PRESET_TABLE_COLS]
                    label = f"Scaffold {i + 1}"
                child = tree.insert(parent, "end", text=label, values=vals)
                st["row_index"][child] = pi

    def _refresh_preset_list(self, kind, select=None):
        """Rebuild the kind's preset grid, then optionally select the row for
        preset index `select` (bringing it into view) so the buttons act on it and
        its parameters are on screen."""
        self._refresh_preset_table(kind)
        st = self._pstores[kind]
        if select is not None and 0 <= select < len(st["presets"]):
            iid = st["parent_iid"].get(select)
            if iid:
                st["table"].selection_set(iid)
                st["table"].focus(iid)
                st["table"].see(iid)

    def _selected_preset_index(self, kind):
        """Index into the kind's presets list of the selected row, or None.
        Selecting a preset's header row OR (scaffold grid) any of its scaffold
        child rows resolves to that preset, so the buttons work wherever in the
        group you clicked."""
        st = self._pstores.get(kind)
        if st is None or st["table"] is None:
            return None
        sel = st["table"].selection()
        if not sel:
            return None
        return st["row_index"].get(sel[0])

    def _preset_name_index(self, kind, name):
        """Index of an existing preset of this kind with this (case-insensitive)
        name, else None -- used to catch duplicate names and offer an overwrite."""
        low = name.strip().lower()
        for i, p in enumerate(self._pstores[kind]["presets"]):
            if p["name"].lower() == low:
                return i
        return None

    # -- store: one file per preset in the kind's folder -------------------- #
    def _load_presets(self, kind):
        """Load every preset file from the kind's folder plus its default
        pointer(s) -> (list of {"name", "data", "_file"}, {mode: default_name or
        None}). For the scaffold kind, first migrates a legacy inline
        presets/defaults.json into the folder. Tolerant: a missing folder or a corrupt
        file is skipped. A default is returned only if it matches a loaded preset
        (case-insensitive), so a stale pointer never dangles -- checked per mode,
        so a dangling 4-axis pointer cannot blank a live 3-axis one."""
        st = self._pstores[kind]
        directory = st["dir"]
        if kind == "scaffold":
            self._migrate_legacy_presets()
        elif self._sweep_tab_of(kind):
            self._migrate_legacy_sweep_presets()
        # The user's files first; then each shipped preset they have neither
        # edited (a user file of the same name IS their edit of it) nor
        # deleted (the pointer file's "hidden"). One entry per name, sorted
        # by file name across both folders as one folder was.
        presets, seen = [], set()
        shipped = self._shipped_names(kind)
        hidden = self._hidden_names(kind)
        rows = [(fn, directory, "user") for fn in self._list_json(directory)]
        if st.get("shipped_dir"):
            rows += [(fn, st["shipped_dir"], "shipped")
                     for fn in self._list_json(st["shipped_dir"])]
        rows.sort(key=lambda r: (r[0].lower(), r[2] != "user"))
        for src in ("user", "shipped"):
            for fn, folder, where in rows:
                if where != src:
                    continue
                try:
                    with open(os.path.join(folder, fn), "r") as fh:
                        raw = json.load(fh)
                except (OSError, ValueError):
                    continue
                if not isinstance(raw, dict) or \
                        not isinstance(raw.get("data"), dict):
                    continue
                name = raw.get("name")
                if not (isinstance(name, str) and name.strip()):
                    name = os.path.splitext(fn)[0]
                low = name.lower()
                if low in seen:   # first (sorted) file wins on a name clash
                    continue
                if where == "shipped" and low in hidden:
                    continue
                seen.add(low)
                presets.append({"name": name, "data": raw["data"],
                                "_file": fn, "_src": where,
                                "_sort": (fn.lower(), where != "user")})
        presets.sort(key=lambda p: p.pop("_sort"))
        for p in presets:
            p["_ships"] = p["name"].lower() in shipped
        names = {p["name"].lower() for p in presets}
        defaults = {}
        for mode, key in st["ptr_keys"].items():
            d = self._load_default_pointer(key)
            if d and d.lower() not in names:
                # A pointer naming a shipped preset by its OLD name follows the
                # rename, but only when the store holds the new one: a store
                # that still has the old file keeps pointing at it.
                new = RENAMED_PRESETS.get(d.lower())
                if new and new.lower() in names:
                    d = new
            defaults[mode] = d if d and d.lower() in names else None
        return presets, defaults

    @staticmethod
    def _list_json(directory):
        try:
            return [fn for fn in os.listdir(directory)
                    if fn.lower().endswith(".json")]
        except OSError:
            return []

    @staticmethod
    def _read_user_pointer():
        """The USER's pointer file (PRESETS_PATH) alone, or {}."""
        try:
            with open(PRESETS_PATH, "r") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _read_shipped_pointer():
        """The shipped pointer file, or {} -- also when PRESETS_PATH has been
        pointed somewhere else (a test's scratch store has no shipped twin)."""
        if os.path.normcase(os.path.dirname(os.path.abspath(PRESETS_PATH))) \
                != os.path.normcase(os.path.abspath(PRESETS_ROOT)):
            return {}
        try:
            with open(SHIPPED_PRESETS_PATH, "r") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    @classmethod
    def _read_pointer_file(cls):
        """The default pointers in force: the shipped ones, with every pointer
        the user set (a name, or null for "no default") over them."""
        merged = cls._read_shipped_pointer()
        merged.update(cls._read_user_pointer())
        return merged

    def _shipped_names(self, kind):
        """Lower-case names of the presets that ship in this kind's folder."""
        folder = self._pstores[kind].get("shipped_dir")
        return set(preset_store.folder_presets(folder)) if folder else set()

    def _hidden_names(self, kind):
        """Lower-case names of the shipped presets the user deleted."""
        st = self._pstores[kind]
        hidden = self._read_user_pointer().get(preset_store.HIDDEN_KEY)
        names = hidden.get(st.get("folder")) if isinstance(hidden, dict) \
            else None
        return {n.lower() for n in names or () if isinstance(n, str)}

    def _set_hidden(self, kind, name, hide=True):
        """Record (or clear) shipped preset `name` as deleted. True on success."""
        st = self._pstores[kind]
        if not st.get("folder") or (hide and name.lower()
                                    not in self._shipped_names(kind)):
            return True
        raw = self._read_user_pointer()
        hidden = raw.get(preset_store.HIDDEN_KEY)
        hidden = dict(hidden) if isinstance(hidden, dict) else {}
        names = [n for n in hidden.get(st["folder"], ())
                 if isinstance(n, str) and n.lower() != name.lower()]
        if hide:
            names.append(name)
        if names:
            hidden[st["folder"]] = names
        else:
            hidden.pop(st["folder"], None)
        return self._write_pointer({preset_store.HIDDEN_KEY: hidden})

    @classmethod
    def _load_default_pointer(cls, ptr_key="default"):
        """Read one kind's default-preset name from presets/defaults.json, or None
        (missing/corrupt file, or no default set for that kind)."""
        d = cls._read_pointer_file().get(ptr_key)
        return d if isinstance(d, str) else None

    @classmethod
    def _write_pointer(cls, updates):
        """Read-modify-write the shared default-pointer file, merging `updates`
        in. Read-modify-write so writing one kind's default never clobbers the
        other kind's. Returns True on success.

        Only the USER's file is written. A pointer equal to the shipped one is
        left out of it, so the user follows the shipped default until they
        choose another, and a release that changes it reaches them."""
        data = cls._read_user_pointer()
        shipped = cls._read_shipped_pointer()
        for k, v in updates.items():
            if (k in shipped and shipped[k] == v) or \
                    (k not in shipped and v in (None, {})):
                data.pop(k, None)
            else:
                data[k] = v
        try:
            os.makedirs(os.path.dirname(PRESETS_PATH), exist_ok=True)
            with open(PRESETS_PATH, "w") as fh:
                json.dump(data, fh, indent=2)
            return True
        except OSError:
            return False

    def _migrate_legacy_presets(self):
        """One-time: if presets/defaults.json still holds an inline `presets` list (the
        old single-file format), write each preset out as its own file in
        PRESETS_DIR -- without clobbering a file already there -- then rewrite
        presets/defaults.json down to just the pointer key(s). A preset whose file
        could not be written stays in the list for the next launch to retry.
        A no-op afterwards."""
        raw = self._read_user_pointer()
        if not isinstance(raw.get("presets"), list):
            return
        try:
            os.makedirs(PRESETS_DIR, exist_ok=True)
        except OSError:
            return
        try:
            taken = {fn.lower() for fn in os.listdir(PRESETS_DIR)}
        except OSError:
            taken = set()
        # The display NAMES already in the folder, too: the loader keeps the
        # first file of a name and drops the rest, so a legacy preset sharing
        # one would land on disk and never in the list. It takes the "(2)"
        # name an Import gives a clash instead.
        names = set()
        for fn in taken:
            try:
                with open(os.path.join(PRESETS_DIR, fn), "r") as fh:
                    n = json.load(fh).get("name")
            except (OSError, ValueError, AttributeError):
                continue
            names.add((n if isinstance(n, str) and n.strip()
                       else os.path.splitext(fn)[0]).lower())
        renamed, unwritten = {}, []
        for it in raw["presets"]:
            if not (isinstance(it, dict) and isinstance(it.get("name"), str)
                    and isinstance(it.get("data"), dict)):
                continue
            name, i = it["name"], 2
            while name.lower() in names:
                name, i = f"{it['name']} ({i})", i + 1
            names.add(name.lower())
            if name != it["name"]:
                renamed.setdefault(it["name"].lower(), name)
            fn = self._unique_filename(self._slug(name), taken)
            taken.add(fn.lower())
            try:
                with open(os.path.join(PRESETS_DIR, fn), "w") as fh:
                    json.dump({"name": name, "data": it["data"]},
                              fh, indent=2)
            except OSError:
                unwritten.append(it)        # the list is its only copy
        # drop the inline list -- all but what could not be written, which the
        # next launch tries again; keep the existing pointer key(s) intact
        if unwritten:
            raw["presets"] = unwritten
        else:
            raw.pop("presets", None)
        d = raw.get("default")
        # the legacy default meant the legacy preset, wherever it now lives
        raw["default"] = (renamed.get(d.lower(), d) if isinstance(d, str)
                          else None)
        try:
            with open(PRESETS_PATH, "w") as fh:
                json.dump(raw, fh, indent=2)
        except OSError:
            pass

    def _sweep_legacy_tab(self, data):
        """The ONE Calibration tab a whole-page preset (already brought up to
        the current keys) is split into: the tab it was saved on (`sw.cal_tab`),
        else the tab owning the print the bottom bar targeted (`sw.kind`), else
        the page's first tab.

        One tab, not every tab whose fields it carries: a whole-page preset
        carries EVERY tab's fields, mostly at their defaults, so splitting it
        everywhere would put seven copies of each old preset on the page."""
        tabs = self._sweep_preset_tabs()
        if not tabs:
            return None
        keys = [t for t, _p in tabs]
        cal = data.get("sw.cal_tab")
        if cal in keys:
            return cal
        kind = data.get("sw.kind")
        for tab, proto in tabs:
            if proto == kind:
                return tab
        return keys[0]

    def _migrate_legacy_sweep_presets(self):
        """One-time: split every whole-page Calibration preset -- a .json lying
        directly in SWEEP_PRESETS_DIR, from before presets went per tab -- into
        the subfolder of the tab it belongs to (`_sweep_legacy_tab`), keeping
        only that tab's keys, then move the original into `_legacy/` so nothing
        is lost and the split never runs twice. A preset that was the page's
        default becomes that tab's default.

        The data goes through `_migrate_preset_data`, the one key-migration
        chain, BEFORE the tab is chosen: an old one-tab CTS preset on a mandrel
        is a cts4 preset by then. A name that tab already holds is not written
        again (a split whose move failed last launch), and a file that cannot
        be read stays where it is. A no-op once the folder holds no loose
        .json."""
        root = SWEEP_PRESETS_DIR
        try:
            loose = [fn for fn in sorted(os.listdir(root))
                     if fn.lower().endswith(".json")
                     and os.path.isfile(os.path.join(root, fn))]
        except OSError:
            return
        if not loose or not self._sweep_preset_tabs():
            return
        old_default = self._read_pointer_file().get("sweep_default")
        pointers = {}
        legacy_dir = os.path.join(root, "_legacy")
        for fn in loose:
            src = os.path.join(root, fn)
            try:
                with open(src, "r") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
                continue
            # not a Calibration-page design (a physics profile the folder
            # rename left behind, say): leave it for a person to look at
            if not any(str(k).startswith("sw.") for k in raw["data"]):
                continue
            name = raw.get("name")
            if not (isinstance(name, str) and name.strip()):
                name = os.path.splitext(fn)[0]
            data = self._migrate_preset_data(dict(raw["data"]))
            tab = self._sweep_legacy_tab(data)
            keys = set(self._sweep_tab_keys(tab))
            dest = os.path.join(root, tab)
            try:
                os.makedirs(dest, exist_ok=True)
                taken = {f.lower() for f in os.listdir(dest)}
            except OSError:
                continue
            names = set()
            for f in taken:
                try:
                    with open(os.path.join(dest, f), "r") as fh:
                        n = json.load(fh).get("name")
                except (OSError, ValueError, AttributeError):
                    continue
                if isinstance(n, str):
                    names.add(n.lower())
            if name.lower() not in names:
                out = self._unique_filename(self._slug(name), taken)
                try:
                    with open(os.path.join(dest, out), "w") as fh:
                        json.dump({"name": name,
                                   "data": {k: v for k, v in data.items()
                                            if k in keys}}, fh, indent=2)
                except OSError:
                    continue                    # the original stays: retried
            if isinstance(old_default, str) and \
                    old_default.lower() == name.lower():
                pointers["sweep_default:" + tab] = name
            try:
                os.makedirs(legacy_dir, exist_ok=True)
                kept = {f.lower() for f in os.listdir(legacy_dir)}
                shutil.move(src, os.path.join(
                    legacy_dir, self._unique_filename(
                        os.path.splitext(fn)[0], kept)))
            except OSError:
                pass
        # a tab default the user has already set is never overwritten
        current = self._read_pointer_file()
        pointers = {k: v for k, v in pointers.items() if not current.get(k)}
        if pointers:
            self._write_pointer(pointers)

    @staticmethod
    def _slug(name):
        """Filename stem for a preset name: non-filename chars collapse to '_',
        edges trimmed; an empty result falls back to 'preset'."""
        s = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("._-")
        return s or "preset"

    @staticmethod
    def _unique_filename(base, taken):
        """`base`.json, suffixed _2/_3/... until it isn't in `taken` (a set of
        lowercased basenames). Keeps slugged names from colliding on disk."""
        fn = base + ".json"
        i = 2
        while fn.lower() in taken:
            fn = f"{base}_{i}.json"
            i += 1
        return fn

    def _write_preset_file(self, kind, preset):
        """Write one preset to its file in the kind's folder (creating the folder).
        A preset with no "_file" yet gets a slug-based unique basename stored back
        on the dict; an existing one keeps its basename, so its file is overwritten
        in place. Only "name"/"data" are serialized. Returns True on success."""
        st = self._pstores[kind]
        directory = st["dir"]
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            return False
        fn = preset.get("_file")
        if preset.get("_src") == "shipped":
            # the user's edit of a shipped preset: their own file of the same
            # name, under the shipped file name when that is free; the shipped
            # file is never written
            taken = {f.lower() for f in self._list_json(directory)}
            fn = fn if fn and fn.lower() not in taken else                 self._unique_filename(self._slug(preset["name"]), taken)
        elif not fn:
            taken = {p["_file"].lower() for p in st["presets"]
                     if p.get("_file") and p.get("_src") != "shipped"}
            taken |= {f.lower() for f in self._list_json(directory)}
            fn = self._unique_filename(self._slug(preset["name"]), taken)
        try:
            with open(os.path.join(directory, fn), "w") as fh:
                json.dump({"name": preset["name"], "data": preset["data"]},
                          fh, indent=2)
        except OSError:
            return False
        preset["_file"], preset["_src"] = fn, "user"
        preset["_ships"] = preset["name"].lower() in self._shipped_names(kind)
        return True

    def _delete_preset_file(self, kind, preset):
        """Remove a preset from the kind's list for good: the user's own file
        goes (best-effort; a missing file or IO error is ignored) and a
        shipped preset of that name is recorded as deleted, so it does not
        come back in its place. The shipped file itself is never touched."""
        fn = preset.get("_file")
        if fn and preset.get("_src") != "shipped":
            try:
                os.remove(os.path.join(self._pstores[kind]["dir"], fn))
            except OSError:
                pass
        self._set_hidden(kind, preset["name"])

    # -- snapshot / apply (split by kind) ----------------------------------- #
    def _keys_for_kind(self, kind):
        """The persist keys owned by `kind`. Four disjoint groups: "machine" (the
        Machine and Collector tab), "calibration" (the PHYSICS profile -- cal.* +
        sim.*), "sweep" (the CALIBRATION page's print designs -- sw.*), and
        "scaffold" (everything else). Each persist key belongs to exactly one
        group, so a preset of one kind never disturbs another's fields.

        A per-tab kind "sweep:<tab>" owns a SUBSET of the sweep group: that
        tab's print (`_sweep_tab_keys`). Two plate tabs share the base-pad and
        plate-margin keys, because both show those cards and both prints use
        them; every other key is one tab's alone."""
        machine = set(self._machine_persist_keys())
        calib = set(self._calibration_persist_keys())
        sweep = set(self._sweep_persist_keys())
        if kind == "machine":
            return machine
        if kind == "calibration":
            return calib
        if kind == "sweep":
            return sweep
        if self._sweep_tab_of(kind):
            return set(self._sweep_tab_keys(self._sweep_tab_of(kind)))
        return set(self._persist_map()) - machine - calib - sweep

    def _snapshot_for_kind(self, kind):
        """Snapshot the persisted vars that belong to `kind`. File-path boxes
        aren't in _persist_map, so they're excluded for free."""
        keys = self._keys_for_kind(kind)
        return {k: w.get() for k, w in self._persist_map().items() if k in keys}

    #: where a design (scaffold) preset keeps its copy of the Physics page
    DESIGN_PHYSICS_KEY = "_physics"

    def _preset_data_for_kind(self, kind):
        """What a SAVED preset of `kind` holds: its snapshot, and for a design
        the Physics page it was made with too. Reported: a design saved,
        closed and reopened came back exact while its CTS was back at the
        default, so the same preset printed a different scaffold. The g-code
        depends on the physics (a speed given as a multiple of CTS, the
        physics-driven fiber diameter), so a design without them cannot
        reproduce its print. Only a save writes this key: the History
        snapshot and the dirty checks use `_snapshot_for_kind`, which keeps
        the four kinds disjoint."""
        import gui_saved_choices
        # a closed-list setting is saved by its key, never its label
        data = {k: gui_saved_choices.to_saved(k, v)
                for k, v in self._snapshot_for_kind(kind).items()}
        saved = getattr(self, "_zero_design_saved", None)
        if kind == "scaffold" and callable(saved):
            saved(data)         # the zero its Start X/Y are measured from
        physics = getattr(self, "_cal_design_physics", None)
        if kind == "scaffold" and callable(physics):
            kept = physics()
            if kept:
                data[self.DESIGN_PHYSICS_KEY] = kept
        return data

    def _migrate_preset_data(self, data):
        """A saved preset's data brought up to the current keys -- every
        legacy rename and split, in the order they happened. The one chain,
        so a reader of preset data (`gui_sweeps._sw_rig_view`) sees exactly
        what loading it would set."""
        return self._migrate_saved_choices(
            self._migrate_legacy_physics_master(
            self._migrate_legacy_cts_collector(
            self._migrate_legacy_needle_radius(
            self._migrate_legacy_lag_amplitude(
            self._migrate_plate_corner(self._migrate_legacy_holder_stl(
            self._migrate_placement_key(self._migrate_legacy_end_margin(
            self._migrate_legacy_mandrel_mode(
            self._migrate_legacy_render_travel(
                self._migrate_legacy_no_sag(self._migrate_legacy_slide(
                    data)))))))))))))

    @staticmethod
    def _migrate_saved_choices(data):
        """A closed-list setting saved by its key (or, before 2026-11-13,
        by its label in any wording) as its var holds it
        (`gui_saved_choices.from_saved`). The LAST step, so the renames
        before it have run. An unreadable value is left as it is; the load
        names it and keeps the field's default (`_apply_preset_for_kind`)."""
        if not isinstance(data, dict):
            return data
        import gui_saved_choices
        out = None
        for k, v in data.items():
            new, _bad = gui_saved_choices.from_saved(k, v)
            if new is not v and new != v:
                out = dict(data) if out is None else out
                out[k] = new
        return data if out is None else out

    def _apply_preset_for_kind(self, kind, data):
        """Set the fields that belong to `kind`, so a preset of one kind never
        touches another's. A preset FULLY determines its kind's fields: every key
        of the kind is set to the preset's value, or to the field's factory default
        when the preset omits it. That makes loading deterministic -- a value never
        carries over from the previously-loaded preset. Then re-sync that kind's
        show/hide toggles."""
        data = self._migrate_preset_data(data)
        if kind == "machine" and "origin.gcode_rules" not in data:
            import mew_gcode
            data = dict(data)
            data["origin.gcode_rules"] = ""
            data.setdefault("origin.gcode_start", mew_gcode.DEFAULT_START_BLOCK)
            data.setdefault("origin.gcode_end", "")
        if kind == "machine" and "origin.strip_comments_on_save" not in data:
            # A printer saved before the default became Off carries the old
            # key, `origin.strip_comments`, written True whether or not anyone
            # ticked it: it is not read, so the printer loads with comments
            # kept, as a new one does.
            data = dict(data)
            data.pop("origin.strip_comments", None)
            data["origin.strip_comments_on_save"] = False
        if kind == "machine" and "origin.preflight_accepted" not in data:
            # a printer saved before Save asked: nothing accepted yet, rather
            # than the last printer's answers
            data = dict(data)
            data["origin.preflight_accepted"] = ""
        if kind == "machine":
            # A machine preset saved before the printer's output rows moved
            # to Setup does not have them: keep what the printer is set to,
            # rather than reset it to the factory spelling
            pm = self._persist_map()
            missing = [k for k in ("s.travel_as_g1", "s.gcode_convention",
                                   "s.wait_for_temp", "s.g2g3")
                       if k not in data and k in pm]
            if missing:
                data = dict(data)
                for k in missing:
                    data[k] = pm[k].get()
        if kind == "scaffold" and hasattr(self, "_scaf_grow"):
            # a preset of more scaffolds than there are slots: grow them
            # first, so their keys are in the persist map
            self._scaf_grow(self._preset_scaffold_count(data))
        keys = self._keys_for_kind(kind)
        defaults = getattr(self, "_field_defaults", {})
        # a closed-list value that names no choice is not set: the field
        # keeps its default, and the load says which and why
        import gui_saved_choices
        unreadable = []
        for k in [k for k in data if k in keys]:
            _v, bad = gui_saved_choices.from_saved(k, data[k])
            if bad:
                unreadable.append(bad)
                data = dict(data)
                data.pop(k)
        # A field's write-trace may DEPEND on another field also set by this same
        # preset (the physics-deposit trace restores a stashed diameter, stale once
        # a preset supplies its own). Loading is a bulk set, not user edits, so mute
        # such traces here and re-settle once at the end. Traces that must run
        # per-set check `_loading_preset` themselves.
        self._loading_preset = True
        try:
            for k, w in self._persist_map().items():
                if k not in keys:
                    continue
                value = data[k] if k in data else defaults.get(k)
                if value is None:                  # no saved value AND no default
                    continue
                try:
                    w.set(value)
                except Exception:  # noqa: BLE001  (a stale/garbled saved value)
                    pass
        finally:
            self._loading_preset = False
        # a mandrel value Calibrate measured is the printer's, not the
        # design's: a scaffold preset carrying its own must not undo it
        keep = getattr(self, "_cal_restore_measured", None)
        if callable(keep) and kind in ("calibration", "scaffold"):
            keep()
        # what this load set, so the Physics cards can say "from preset"
        note = getattr(self, "_cal_note_preset_loaded", None)
        if callable(note):
            note(kind)
        if kind == "machine":
            if hasattr(self, "_gce_library"):
                self._gce_library()
            self._toggle_plate_shape()
            self._toggle_kinematics_custom()
            self._refresh_motion_limits()
            self._sync_machine_hide_enable()
        elif kind == "calibration":
            self._after_calibration_preset_applied()
        elif kind == "sweep" or self._sweep_tab_of(kind):
            self._after_sweep_preset_applied()
        else:
            self._reset_editor_views()      # re-frame the graphs to the new data
            self._mark_scaffolds_inited()
            self._apply_all_toggles()
            self._after_scaffold_preset_applied()   # re-settle physics diameter mode
        if kind in ("scaffold", "calibration"):
            for values in (list(getattr(self, "scaf_vars", ())) +
                           list(getattr(self, "_gce_sv", ()))):
                settle = getattr(values.get("speed"), "_cts_ratio_settle", None)
                if settle is not None:
                    settle()
        # the loaded values ARE the kind's baseline: the ↺ marks measure edits from here
        self._revert_rebaseline(keys)
        if unreadable:
            self._preset_say("Loaded with a problem: " + " ".join(unreadable))

    def _apply_saved_preset(self, kind, data):
        """`_apply_preset_for_kind` for a SAVED preset (Load, the startup
        design, a collector switch): a design's plate Start X/Y move onto
        this printer's zero (`_zero_design_loaded`). A History snapshot
        restores its zero with its Start X/Y and goes straight through."""
        if kind == "scaffold":
            self._zero_design_said = ""     # `_zero_design_note`: this load's
            place = getattr(self, "_zero_design_loaded", None)
            if callable(place):
                data = place(data)
        return self._apply_preset_for_kind(kind, data)

    def _zero_design_tail(self, kind):
        """" It was saved with its zero at ..." when the design just loaded had
        its Start X/Y moved onto this printer's zero, else ""."""
        note = getattr(self, "_zero_design_note", None)
        if kind != "scaffold" or not callable(note):
            return ""
        said = note()
        return " " + said if said else ""

    # -- mutators (shared tail + the eight buttons, all kind-aware) --------- #
    def _commit_presets(self, kind, select, status):
        """Save the kind's default pointer, then refresh its grid (reselecting
        `select`) and set the status line. Shared tail of every preset mutator.
        The preset FILES are written/removed by the calling mutator (each preset
        is its own file); this persists only the default pointer(s) (read-modify-
        write, so the other kinds' defaults are preserved) and updates the UI.
        An axis-aware kind writes BOTH of its pointers every time -- they live in
        one dict and one file, and writing only the mode just touched is how a
        rename or a delete would leave the other mode dangling."""
        st = self._pstores[kind]
        if not self._write_pointer({key: st["defaults"][mode]
                                    for mode, key in st["ptr_keys"].items()}):
            self._preset_say("Not saved: could not write %s. Check that the "
                             "folder can be written to." % PRESETS_PATH)
            return
        self._refresh_preset_list(kind, select=select)
        self.status.set(status)

    def _preset_say(self, text):
        """A preset card's refusal, in the status line where every page says
        what it refused; a popup is kept for a decision (overwrite, delete)."""
        refuse = getattr(self, "_refuse", None)
        if refuse is not None:
            refuse(text, log=False)
        else:
            self.status.set(text)

    def _preset_file_error(self, kind):
        """Report a failed preset-file write (shared by the mutators)."""
        self._preset_say("Not saved: could not write the preset file in %s. "
                         "Check that the folder can be written to."
                         % self._pstores[kind]["dir"])

    def _preset_kept_physics(self, preset):
        """" It keeps the Physics page with it (CTS ...)." after a design is
        saved with its physics, else ""."""
        phrase = getattr(self, "_cal_cts_phrase", None)
        if self.DESIGN_PHYSICS_KEY not in preset.get("data", {})                 or not callable(phrase):
            return ""
        return (" It keeps the Physics page with it (%s), so it prints the "
                "same next time." % phrase())

    def _preset_load(self, kind):
        st = self._pstores[kind]
        idx = self._selected_preset_index(kind)
        if idx is None:
            self._preset_say("Pick a preset from the list first.")
            return
        preset = st["presets"][idx]
        # a design kept the physics it was made with: when the Physics page
        # now differs, the user picks which the print uses, and the status
        # line says which it is (`_cal_design_choice`)
        choose = getattr(self, "_cal_design_choice", None)
        choice = choose(preset) if kind == "scaffold" and callable(choose)             else None
        if choice == "cancel":
            self.status.set("Nothing loaded.")
            return False
        self._apply_saved_preset(kind, preset["data"])
        if choice == "design":
            self._apply_preset_for_kind(
                "calibration", preset["data"][self.DESIGN_PHYSICS_KEY])
        note = getattr(self, "_cal_design_note", None)
        tail = (" " + note(preset["data"], choice))             if kind == "scaffold" and callable(note) else ""
        self.status.set(f"Loaded {st['noun']} “{preset['name']}”."
                        f"{self._zero_design_tail(kind)}{tail}")
        return True

    def _default_badge(self, kind, name):
        """The "★ default" tag appended to a preset's row, or "" if it is nobody's
        default. An axis-aware kind NAMES the mode, and names both when one preset
        is the default for each -- an unlabelled star on two rows of the same grid
        would say nothing about which one boots."""
        st = self._pstores[kind]
        low = name.lower()
        hit = [m for m, d in st["defaults"].items() if d and d.lower() == low]
        if not hit:
            return ""
        if not self._is_per_axis(st):
            return "   ★ default"
        return "   ★ default: " + " + ".join(
            AXIS_SHORT[m] for m, _sfx, _label in AXIS_MODES if m in hit)

    def _ask_axis_mode(self, noun):
        """The "Set as default for:" chooser -> an s_collector value, or None if
        cancelled. Its own Toplevel rather than a messagebox because the answer
        NAMES one of two things; yes/no cannot. Split out from
        _preset_set_default so a test can supply the answer without a window."""
        top = tk.Toplevel(self.root)
        top.title("Set as default")
        top.transient(self.root)
        top.resizable(False, False)
        outer = ttk.Frame(top, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=f"Set as default {noun} for:",
                  style="Section.TLabel").pack(anchor="w")
        # "the 3-axis pair" read as jargon: say what loads, and when
        ttk.Label(outer, wraplength=430, justify="left", text=(
            f"A plate default loads when the app starts. A mandrel default "
            f"loads when you switch the Collector to Mandrel (4-axis). Pick "
            f"one; to use this {noun} for both, set it twice.")).pack(
            anchor="w", pady=(6, 12))
        choice = {"mode": None}

        def pick(mode):
            choice["mode"] = mode
            top.destroy()

        row = ttk.Frame(outer)
        row.pack(fill="x")
        for mode, _sfx, label in AXIS_MODES:
            ttk.Button(row, text=label, width=24,
                       command=lambda m=mode: pick(m)).pack(side="left",
                                                            padx=(0, 8))
        ttk.Button(row, text="Cancel", width=10,
                   command=top.destroy).pack(side="right")
        top.bind("<Escape>", lambda _e: top.destroy())
        top.update_idletasks()
        try:
            top.grab_set()
        except tk.TclError:            # no window manager (headless test runs)
            pass
        self.root.wait_window(top)
        return choice["mode"]

    def _preset_set_default(self, kind):
        """Mark the selected preset as a default. An axis-aware kind ASKS which
        collector the default is for (_ask_axis_mode) and writes only that mode's
        pointer, so the two never overwrite each other; every other kind has one
        default and asks nothing. Pressing it again on the current default of the
        mode you pick clears that one (back to a blank boot for that mode)."""
        st = self._pstores[kind]
        idx = self._selected_preset_index(kind)
        if idx is None:
            self._preset_say("Pick the preset to set as default first.")
            return
        if self._is_per_axis(st):
            mode = self._ask_axis_mode(st["noun"])
            if mode is None:                       # cancelled -- change nothing
                return
            where = f" for {AXIS_SHORT[mode]} work"
            when = (" on startup" if mode == BOOT_AXIS_MODE
                    else " when you switch the Collector to a mandrel")
        else:
            mode, where, when = None, "", " on startup"
        name = st["presets"][idx]["name"]
        current = st["defaults"].get(mode)
        if current and name.lower() == current.lower():
            st["defaults"][mode] = None
            self._commit_presets(
                kind, idx, f"“{name}” is no longer the default{where}.")
        else:
            st["defaults"][mode] = name
            self._commit_presets(
                kind, idx, f"“{name}” will load automatically{when}.")

    def _apply_default_preset(self):
        """Boot-time hook: load each kind's default preset (if set) into the form.
        Called once from __init__ after every tab -- and thus every persisted var
        and both preset stores -- exists. The two kinds touch disjoint key sets,
        so the order doesn't matter.

        Startup is ALWAYS 3-axis (BOOT_AXIS_MODE): there is no remembered mode, so
        a fresh boot is the same every time. The 4-axis defaults load only from a
        Collector click (_collector_mode_chosen)."""
        # Snapshot every field's factory default NOW, before any preset is applied --
        # _apply_preset_for_kind resets a key the preset omits to this default, so
        # loading a preset never leaves a stale value from the previously-loaded one.
        self._field_defaults = {k: w.get() for k, w in self._persist_map().items()}
        # the kinds touch disjoint key sets, so the order doesn't matter (two
        # plate tabs' defaults share the base-pad keys; the later tab's wins);
        # "calibration" / "sweep:<tab>" only exist in the physics build (else
        # .get returns None).
        sweeps = [k for k in self._pstores if self._sweep_tab_of(k)]
        for kind in ("machine", "calibration", *sweeps, "scaffold"):
            st = self._pstores.get(kind)
            if not st:
                continue
            name = st["defaults"].get(
                BOOT_AXIS_MODE if self._is_per_axis(st) else None)
            if not name:
                continue
            idx = self._preset_name_index(kind, name)
            if idx is None:
                continue
            preset = st["presets"][idx]
            self._apply_saved_preset(kind, preset["data"])
            tail = ""
            note = getattr(self, "_cal_design_note", None)
            if kind == "scaffold" and callable(note):
                # the startup design is a design load too: say which physics
                # it prints with, and let the window ask once it is up
                # (`_gw_boot_physics`) when they are not its own
                self._boot_design_preset = preset
                tail = " " + note(preset["data"], None)
            zero = self._zero_design_tail(kind)
            if kind == "scaffold":
                self._boot_zero_note = zero     # `_gw_boot_physics` restates it
            self.status.set(
                f"Loaded default {st['noun']} “{preset['name']}”.{zero}{tail}")
        # the user's own This-design defaults go on last, so they are what a
        # new design starts from (gui_settings._apply_design_defaults)
        apply_mine = getattr(self, "_apply_design_defaults", None)
        if apply_mine is not None:
            apply_mine()
        self._revert_install()

    # -- the axis-mode swap (the Collector radio's other half) -------------- #
    def _apply_axis_defaults(self, mode):
        """Load the default MACHINE preset and default SCAFFOLD preset for one
        collector mode, and return the names loaded (in load order).

        The pair moves together on purpose: swapping the design without the
        machine is the exact state this feature exists to prevent. Machine first,
        then scaffold -- the order boot uses; they touch disjoint keys, so it only
        decides which status line survives. A kind with no default set for `mode`
        is left alone rather than reset, so a half-configured install still
        switches the half it has configured."""
        loaded = []
        stores = getattr(self, "_pstores", {})       # a click can only land after
        for kind in ("machine", "scaffold"):         # boot, but stay total anyway
            st = stores.get(kind)
            if not st or not self._is_per_axis(st):
                continue
            name = st["defaults"].get(mode)
            if not name:
                continue
            idx = self._preset_name_index(kind, name)
            if idx is None:                        # pointer outlived its file
                continue
            self._apply_saved_preset(kind, st["presets"][idx]["data"])
            loaded.append(st["presets"][idx]["name"])
            if kind == "scaffold":
                self._axis_zero_note = self._zero_design_tail(kind)
        return loaded

    def _collector_mode_chosen(self):
        """The Collector radio was CLICKED: load that mode's default machine +
        scaffold presets.

        Wired to the radiobuttons' `command`, deliberately NOT to a trace on
        s_collector. A trace fires on every programmatic set -- including the one
        _apply_preset_for_kind performs while applying the very preset this loads
        -- which is unbounded re-entry; `command` fires only on a real click, and
        applying a preset is not one.

        Re-asserting the mode afterwards is not redundant. A preset FULLY
        determines its kind's fields, so a PLATE preset parked in the 4-axis slot
        would set s.collector straight back and the click would appear to do
        nothing at all. The click is the newer intent and wins."""
        mode = self.s_collector.get()
        self._axis_zero_note = ""
        loaded = self._apply_axis_defaults(mode)
        if self.s_collector.get() != mode:
            self.s_collector.set(mode)
        if loaded:
            # Said ONCE, where the click was: beside Setup's switch when that
            # is the page showing, else on the status line (Design's Collector
            # tool, the Scaffold designer). Both at once read as two events.
            names = ", ".join(f"“{n}”" for n in dict.fromkeys(loaded))
            said = "Loaded the %s printer and design: %s.%s" % (
                AXIS_SHORT[mode].lower(), names,
                getattr(self, "_axis_zero_note", ""))
            note = getattr(self, "_collector_loaded_note", None)
            label = getattr(self, "_collector_loaded_label", None)
            on_setup = False
            if note is not None and label is not None:
                try:
                    cur = str(self.nb.select() or "")
                    on_setup = bool(cur) and str(label).startswith(cur + ".")
                except Exception:  # noqa: BLE001 -- a message must not raise
                    on_setup = False
            if on_setup:
                note.set(said)
                self.status.set("")         # an older line would contradict it
            else:
                if note is not None:
                    note.set("")            # no stale note from an older click
                self.status.set(said)

    def _preset_save_new(self, kind):
        st = self._pstores[kind]
        import gui_widgets      # the app's own prompt, not Tk's stock one
        name = gui_widgets.ask_string(
            self.root, "Save preset", "Name for this preset:")
        if name is None:
            return
        name = name.strip()
        if not name:
            self._preset_say("A preset needs a name.")
            return
        existing = self._preset_name_index(kind, name)
        if existing is not None:
            # overwriting an existing name replaces its data in place (keeps its file)
            if not messagebox.askyesno(
                    "Overwrite preset",
                    f"A preset named “{name}” already exists. "
                    "Overwrite it with the current settings?"):
                return
            # keep the stored name (its original casing) and its file; replace
            # only the data
            st["presets"][existing]["data"] = self._preset_data_for_kind(kind)
            target = existing
        else:
            st["presets"].append({"name": name,
                                  "data": self._preset_data_for_kind(kind)})
            target = len(st["presets"]) - 1
        if not self._write_preset_file(kind, st["presets"][target]):
            self._preset_file_error(kind)
            return
        self._revert_rebaseline(self._keys_for_kind(kind))
        # saving never changes the startup default; only "Set as default" does
        said = ("" if kind not in ("machine", "calibration")
                or self._is_startup_default(kind, st["presets"][target]["name"])
                else "“Set as default” makes it load at every start.")
        self._commit_presets(
            kind, target, f"Saved {st['noun']} “{name}”."
            + self._preset_kept_physics(st["presets"][target])
            + (" " + said if said else ""))

    def _is_startup_default(self, kind, name):
        """True when `name` is the preset that loads at startup now (for a
        per-axis kind, in the boot collector mode or the current one)."""
        st = self._pstores[kind]
        modes = [None]
        if self._is_per_axis(st):
            cur = getattr(self, "s_collector", None)
            modes = {BOOT_AXIS_MODE, cur.get() if cur is not None else None}
        return any((st["defaults"].get(m) or "").lower() == name.lower()
                   for m in modes)

    def _preset_is_shipped(self, kind, name):
        """True when `name` is the example preset that came with the app: a
        printer preset flagged `_example`, or the shipped physics preset."""
        if not name:
            return False
        if kind == "calibration":
            return name == getattr(self, "SHIPPED_PHYSICS_PRESET", None)
        idx = self._preset_name_index(kind, name)
        if idx is None:
            return False
        data = self._pstores[kind]["presets"][idx].get("data")
        return isinstance(data, dict) and bool(data.get("_example"))

    def _preset_update(self, kind):
        st = self._pstores[kind]
        idx = self._selected_preset_index(kind)
        if idx is None:
            self._preset_say("Pick the preset to update first.")
            return
        name = st["presets"][idx]["name"]
        if not messagebox.askyesno(
                "Update preset",
                f"Replace the settings stored in “{name}” with the "
                "current inputs?"):
            return
        st["presets"][idx]["data"] = self._preset_data_for_kind(kind)
        if not self._write_preset_file(kind, st["presets"][idx]):
            self._preset_file_error(kind)
            return
        self._revert_rebaseline(self._keys_for_kind(kind))
        self._commit_presets(kind, idx, f"Updated {st['noun']} “{name}”."
                             + self._preset_kept_physics(st["presets"][idx]))

    def _preset_rename(self, kind):
        st = self._pstores[kind]
        idx = self._selected_preset_index(kind)
        if idx is None:
            self._preset_say("Pick the preset to rename first.")
            return
        old = st["presets"][idx]["name"]
        import gui_widgets
        name = gui_widgets.ask_string(
            self.root, "Rename preset", "New name:", initialvalue=old)
        if name is None:
            return
        name = name.strip()
        if not name:
            self._preset_say("A preset needs a name.")
            return
        clash = self._preset_name_index(kind, name)
        if clash is not None and clash != idx:
            self._preset_say(f"Another preset is already named “{name}”.")
            return
        # keep the default pointer(s) attached if we just renamed a default --
        # every mode, since one preset can be the default for both
        for mode, d in st["defaults"].items():
            if d and old.lower() == d.lower():
                st["defaults"][mode] = name
        # move the file so its name keeps matching the preset's: drop the old
        # file and clear "_file" so the write picks a fresh slug-based basename.
        self._delete_preset_file(kind, st["presets"][idx])
        st["presets"][idx]["name"] = name
        st["presets"][idx]["_file"] = None
        st["presets"][idx]["_src"] = "user"
        if not self._write_preset_file(kind, st["presets"][idx]):
            self._preset_file_error(kind)
            return
        self._commit_presets(kind, idx, f"Renamed {st['noun']} to “{name}”.")

    def _preset_delete(self, kind):
        st = self._pstores[kind]
        idx = self._selected_preset_index(kind)
        if idx is None:
            self._preset_say("Pick the preset to delete first.")
            return
        name = st["presets"][idx]["name"]
        if not messagebox.askyesno("Delete preset",
                                   f"Delete preset “{name}”?"):
            return
        # a deleted preset can't stay the default, in any mode
        for mode, d in st["defaults"].items():
            if d and name.lower() == d.lower():
                st["defaults"][mode] = None
        self._delete_preset_file(kind, st["presets"][idx])
        del st["presets"][idx]
        self._commit_presets(kind, min(idx, len(st["presets"]) - 1),
                             f"Deleted {st['noun']} “{name}”.")

    def _preset_reset(self, kind):
        """"Reset to shipped": the selected preset, when it is the user's edit
        of one that came with MEWracle, goes back to the shipped version (the
        user's file is deleted after asking; the shipped one shows again).
        Otherwise, when the user has deleted shipped presets, it offers to
        bring those back. The shipped files are only ever read."""
        st = self._pstores[kind]
        idx = self._selected_preset_index(kind)
        preset = st["presets"][idx] if idx is not None else None
        if preset is not None and preset.get("_src") == "user"                 and preset.get("_ships"):
            name = preset["name"]
            if not messagebox.askyesno(
                    "Reset to shipped",
                    f"Put “{name}” back to the version that came with "
                    "MEWracle? Your changes to it will be lost."):
                return
            try:
                os.remove(os.path.join(st["dir"], preset["_file"]))
            except OSError:
                self._preset_file_error(kind)
                return
            self._set_hidden(kind, name, hide=False)
            st["presets"], st["defaults"] = self._load_presets(kind)
            self._commit_presets(
                kind, self._preset_name_index(kind, name),
                f"“{name}” is back to the version that came with MEWracle.")
            return
        hidden = sorted(self._hidden_names(kind) & self._shipped_names(kind))
        if hidden:
            if not messagebox.askyesno(
                    "Reset to shipped",
                    "Bring back the %d preset%s that came with MEWracle and "
                    "that you deleted?" % (len(hidden),
                                          "" if len(hidden) == 1 else "s")):
                return
            ok = True
            for low in hidden:
                ok = self._set_hidden(kind, low, hide=False) and ok
            st["presets"], st["defaults"] = self._load_presets(kind)
            self._commit_presets(
                kind, idx, "Brought back %d shipped preset%s."
                % (len(hidden), "" if len(hidden) == 1 else "s"))
            return
        if preset is None:
            self._preset_say("Pick the preset to reset first.")
        elif preset.get("_src") == "shipped":
            self._preset_say(f"“{preset['name']}” is already the version "
                             "that came with MEWracle.")
        else:
            self._preset_say(f"“{preset['name']}” is your own preset: there "
                             "is no shipped version to go back to.")

    # -- import / export (share one preset as a single file) ---------------- #
    def _unique_preset_name(self, kind, name):
        """`name` if free, else `name (2)`, `name (3)`, ... -- the first display
        name not already taken (case-insensitive) for this kind. Lets an import
        keep a clashing preset instead of overwriting or refusing it."""
        if self._preset_name_index(kind, name) is None:
            return name
        i = 2
        while self._preset_name_index(kind, f"{name} ({i})") is not None:
            i += 1
        return f"{name} ({i})"

    def _preset_export(self, kind):
        """Write the selected preset to a file the user picks, as the same
        {"name", "data"} shape the loader reads -- so an exported file can be
        dropped straight into another user's folder or re-imported."""
        st = self._pstores[kind]
        idx = self._selected_preset_index(kind)
        if idx is None:
            self._preset_say("Pick the preset to export first.")
            return
        preset = st["presets"][idx]
        path = filedialog.asksaveasfilename(
            **gui_prefs.save_dir_kw(),
            parent=self.root, title="Export preset",
            defaultextension=".json",
            initialfile=self._slug(preset["name"]) + ".json",
            filetypes=[("Preset files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w") as fh:
                json.dump({"name": preset["name"], "data": preset["data"]},
                          fh, indent=2)
        except OSError:
            self._preset_say("Not exported: could not write %s. Pick another "
                             "folder." % path)
            return
        self.status.set(f"Exported {st['noun']} “{preset['name']}”.")

    def _preset_import(self, kind):
        """Copy one or more preset files into the kind's folder and add them to
        its list. Each file must be a {"data": {...}} object (a missing/blank
        "name" falls back to the filename stem); a name that clashes is kept under
        a "(2)"-style unique name rather than overwriting, so an import never
        destroys an existing preset. Reports anything skipped. (The folders are
        per-kind, so kind isn't enforced on the file contents -- on LOAD only the
        kind's keys are applied anyway.)"""
        st = self._pstores[kind]
        paths = filedialog.askopenfilenames(
            parent=self.root, title="Import preset file(s)",
            filetypes=[("Preset files", "*.json"), ("All files", "*.*")])
        if not paths:
            return
        added, bad, target = 0, 0, None
        for path in paths:
            try:
                with open(path, "r") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                bad += 1
                continue
            if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
                bad += 1
                continue
            name = raw.get("name")
            if not (isinstance(name, str) and name.strip()):
                name = os.path.splitext(os.path.basename(path))[0]
            # fresh "_file" (None) so _write_preset_file picks a slug from the
            # possibly-uniquified name rather than reusing the source filename.
            preset = {"name": self._unique_preset_name(kind, name.strip()),
                      "data": raw["data"], "_file": None}
            if not self._write_preset_file(kind, preset):
                self._preset_file_error(kind)
                continue
            st["presets"].append(preset)
            target = len(st["presets"]) - 1
            added += 1
        if added:
            self._commit_presets(
                kind, target,
                f"Imported {added} preset{'s' if added != 1 else ''}.")
        notes = []
        if bad:
            notes.append(f"{bad} file{'s' if bad != 1 else ''} skipped "
                         "(not a valid preset).")
        if notes:
            done = (f"Imported {added} preset{'s' if added != 1 else ''}. "
                    if added else "No presets were imported. ")
            self._preset_say(done + " ".join(notes))
        elif not added:
            self._preset_say("No presets were imported.")

    def _persist_map(self):
        m = {
            "g.diameter": self.diameter, "g.faces": self.faces,
            "g.sag_mult": self.sag_mult, "g.sag_mode": self.sag_mode,
            "g.mesh_tol": self.mesh_tol,
            "s.width": self.s_width, "s.height": self.s_height,
            "s.stepover": self.s_stepover, "s.diameter": self.s_diameter,
            "s.layers": self.s_layers, "s.speed": self.s_speed,
            "s.speed_unit": self.scaf_vars[0]["speed_unit"],
            "s.speed_ratio": self.scaf_vars[0]["speed_ratio"],
            "s.layer_speed_ratios": self.scaf_vars[0]["layer_speed_ratios"],
            "s.m_layer_speed_ratios": self.scaf_vars[0]["m_layer_speed_ratios"],
            "s.loop_speed": self.s_loop_speed,
            "s.start_x": self.s_start_x, "s.start_y": self.s_start_y,
            "s.start_z": self.s_start_z,
            "s.start_angle": self.s_start_angle,
            "s.footprint_angle": self.s_footprint_angle,
            "s.arc_radius_mult": self.s_arc_radius_mult,
            "s.arc_segments": self.s_arc_segments, "s.rotation": self.s_rotation,
            "s.angle_pattern_on": self.scaf_vars[0]["angle_pattern_on"],
            "s.angle_pattern": self.scaf_vars[0]["angle_pattern"],
            "s.angle_stepover_pattern":
                self.scaf_vars[0]["angle_stepover_pattern"],
            "s.angle_offset_pattern": self.scaf_vars[0]["angle_offset_pattern"],
            "s.layer_overrides": self.scaf_vars[0]["layer_overrides"],
            "s.stepover_end": self.scaf_vars[0]["stepover_end"],
            "s.grade_angle": self.scaf_vars[0]["grade_angle"],
            "s.stepover_top": self.scaf_vars[0]["stepover_top"],
            "s.linear_on": self.scaf_vars[0]["linear_on"],
            "s.linear_wavelength": self.scaf_vars[0]["linear_wavelength"],
            "s.linear_spacing": self.scaf_vars[0]["linear_spacing"],
            "s.linear_pattern": self.scaf_vars[0]["linear_pattern"],
            "s.linear_grid": self.scaf_vars[0]["linear_grid"],
            "s.shape": self.scaf_vars[0]["shape"],
            "s.shape_sides": self.scaf_vars[0]["shape_sides"],
            "s.shape_polygon": self.scaf_vars[0]["shape_polygon"],
            "s.shape_grid": self.scaf_vars[0]["shape_grid"],
            "s.link_loops_on": self.scaf_vars[0]["link_loops_on"],
            "s.link_loops_mode": self.scaf_vars[0]["link_loops_mode"],
            "s.link_loop_angle": self.scaf_vars[0]["link_loop_angle"],
            "s.link_loop_dist": self.scaf_vars[0]["link_loop_dist"],
            "s.link_loop_travel": self.scaf_vars[0]["link_loop_travel"],
            "s.perimeter_travel": self.scaf_vars[0]["perimeter_travel"],
            "s.perimeter_travel_offset":
                self.scaf_vars[0]["perimeter_travel_offset"],
            "s.g2g3": self.s_g2g3,
            "s.array_on": self.s_array_on, "s.array_nx": self.s_array_nx,
            "s.array_ny": self.s_array_ny, "s.array_nz": self.s_array_nz,
            "s.array_dx": self.s_array_dx,
            "s.array_dy": self.s_array_dy, "s.array_dz": self.s_array_dz,
            "s.array_flip_x": self.s_array_flip_x,
            "s.array_flip_y": self.s_array_flip_y,
            "s.array_travel": self.s_array_travel,
            "s.color": self.s_color, "s.count": self.s_count,
            # WHICH collector this design is for -- and therefore which half of
            # the Scaffold designer a saved preset describes.
            # An older preset predates the tab merge and simply omits it, so
            # _apply_preset_for_kind falls back to the factory default "plate",
            # which is what such a preset always was. The mandrel page's former
            # duplicate keys (m.layers / m.speed / m.fiber_diameter /
            # m.diam_override / m.sag_mode / m.sag_mult / m.faces) are no longer
            # in this map, so an old preset's copies of them are ignored on load
            # rather than transplanted over the plate values they would corrupt.
            "s.collector": self.s_collector,
            "s.exit_move": self.s_exit_move,
            "s.exit_margin": self.s_exit_margin,
            "s.stab_on": self.stab_on, "s.stab_width": self.stab_width,
            "s.stab_height": self.stab_height,
            "s.stab_stepover": self.stab_stepover,
            "s.stab_layers": self.stab_layers,
            "s.stab_arc_radius_mult": self.stab_arc_radius_mult,
            "s.stab_start_x": self.stab_start_x,
            "s.stab_start_y": self.stab_start_y,
            "s.make_stl": self.s_make_stl, "s.faces": self.s_faces,
            "s.sag_mult": self.s_sag_mult, "s.sag_mode": self.s_sag_mode,
            "s.sag_reset": self.s_sag_reset,
            "s.sag_reset_layers": self.s_sag_reset_layers,
            "s.sag_resolution": self.s_sag_resolution,
            "s.mesh_tol": self.s_mesh_tol,
            "s.fill_per_tau": self.s_fill_per_tau,
            "s.curve_quality": self.s_curve_quality,
            "s.auto_corner_grid": self.s_auto_corner_grid,
            "s.travel_in_stl": self.s_travel_in_stl,
            "s.travel_gcode_line": self.s_travel_gcode_line,
            "s.travel_as_g1": self.s_travel_as_g1,
            "s.gcode_convention": self.s_gcode_convention,
            "s.wait_for_temp": self.s_wait_for_temp,
            "s.deposition_order": self.s_deposition_order,
            # the build plate (the g-code datum)
            "plate.shape": self.plate_shape, "plate.x": self.plate_x,
            "plate.y": self.plate_y, "plate.diameter": self.plate_diameter,
            "plate.thickness": self.plate_thickness,
            "plate.show": self.show_plate,
            "origin.face": self.origin_face,
            "origin.anchor": self.origin_anchor,
            "origin.custom": self.origin_custom,
            "origin.add_elevation_z": self.add_elevation_z,
            "origin.no_layer_z": self.no_layer_z,
            "origin.no_scaffold_z": self.no_scaffold_z,
            "origin.custom_gcode": self.custom_gcode,
            "origin.custom_gcode_after": self.custom_gcode_after,
            "origin.gcode_flavor": self.gcode_flavor,
            "origin.gcode_flavor_custom": self.gcode_flavor_custom,
            "origin.strip_comments_on_save": self.machine_strip_comments,
            "origin.layer_markers": self.machine_layer_markers,
            "origin.gcode_start": self.machine_gcode_start,
            "origin.gcode_end": self.machine_gcode_end,
            "origin.omit_start_end": self.machine_omit_blocks,
            "origin.gcode_rules": self.machine_gcode_rules,
            "origin.preflight_accepted": self.preflight_accepted,
            "s.gcode_header_notes": self.gcode_header_notes,
            "origin.feed_time_base": self.feed_time_base,
            "origin.incremental": self.origin_incremental,
            "origin.machine_x": self.origin_machine_x,
            "origin.machine_y": self.origin_machine_y,
            "origin.machine_z": self.origin_machine_z,
            "origin.machine_r1": self.origin_machine_r1,
            "origin.machine_r2": self.origin_machine_r2,
            # printer model (3D simulation): part STLs + kinematics chain +
            # contacts + bed-anchored slide + motion limits, saved with the machine
            # preset (see _machine_persist_keys)
            "machine.show": self.machine_show,
            "machine.hide_head": self.machine_hide_head,
            "machine.hide_gantry": self.machine_hide_gantry,
            "machine.hide_bed": self.machine_hide_bed,
            "machine.hide_frame": self.machine_hide_frame,
            "machine.kinematics": self.machine_kinematics,
            "machine.x_stl": self.machine_x_stl,
            "machine.y_stl": self.machine_y_stl,
            "machine.z_stl": self.machine_z_stl,
            "machine.static_stl": self.machine_static_stl,
            # machine.standoff (the old Machine-tab elevation var) is intentionally NOT
            # persisted in the physics build: elevation is sim_standoff (persist key
            # sim.standoff), and since its field lives on the Physics tab it rides the
            # PHYSICS preset with the rest of that tab (see _PHYSICS_PROCESS_KEYS), not
            # the machine preset. The var still exists in gui_buildplate (the
            # unoverridden hook bodies resolve it) but is inert here.
            "machine.scale": self.machine_scale,
            "slide.offset_x": self.slide_offset_x,
            "slide.offset_y": self.slide_offset_y,
            "bed.datum_z": self.bed_datum_z,
            "bed.corner_x": self.bed_corner_x,
            "bed.corner_y": self.bed_corner_y,
            "machine.head_parent": self.machine_head_parent,
            "machine.bed_parent": self.machine_bed_parent,
            "machine.head_x": self.machine_head_x,
            "machine.head_y": self.machine_head_y,
            "machine.head_z": self.machine_head_z,
            "machine.gantry_x": self.machine_gantry_x,
            "machine.gantry_y": self.machine_gantry_y,
            "machine.gantry_z": self.machine_gantry_z,
            "machine.bed_x": self.machine_bed_x,
            "machine.bed_y": self.machine_bed_y,
            "machine.bed_z": self.machine_bed_z,
            "machine.tip_x": self.machine_tip_x,
            "machine.tip_y": self.machine_tip_y,
            "machine.tip_z": self.machine_tip_z,
            "machine.bed_nudge": self.machine_bed_nudge,
            # rotary (4-axis) collector MOUNT (see _machine_persist_keys). The
            # rod's diameter/length are NOT here -- they are the job's, and ride
            # the scaffold preset as m.* like every other designer field.
            "rotary.on": self.rotary_on,
            "rotary.word": self.rotary_word,
            # WHICH KIND of table, and the five-axis pair's two joints. Absent
            # from an older preset, and `_rotary_kind_key`
            # answers "mandrel" for a missing or unrecognised label, so such a
            # preset keeps describing the 4-axis rig it was describing.
            "rotary.kind": self.rotary_kind,
            "rotary.j1_word": self.rotary1_word,
            "rotary.j1_axis": self.rotary1_axis,
            "rotary.j2_word": self.rotary2_word,
            "rotary.j2_axis": self.rotary2_axis,
            # each joint's own body, and the axis of rotation picked ON that
            # body -- one "z,x,y" string per joint, written only by the
            # calibrator. Blank is the uncalibrated state and the page says so.
            "rotary.j1_stl": self.rotary1_stl,
            "rotary.j1_pivot": self.rotary1_pivot,
            "rotary.j2_stl": self.rotary2_stl,
            "rotary.j2_pivot": self.rotary2_pivot,
            # the G-CODE HOME (INTERIM): where the nozzle tip is at
            # G0 X0 Y0 Z0, picked on joint 1's body in that body's own frame.
            # Absent from a preset written before it existed, which reads back
            # blank -- the not-picked state the page already says out loud.
            "rotary.home": self.rotary_home,
            # THE COLLECTOR SEAT: where the collector's bottom edge rests on
            # joint 1's body, in that body's own frame. Absent from a preset
            # written before it existed, which reads back blank -- the
            # not-located state the page says out loud.
            "rotary.j1_seat": self.rotary_seat,
            # NB: no "rotary.stl" -- the holder is the Bed STL
            # ("machine.z_stl"); `_migrate_legacy_holder_stl` moves an old one.
            "rotary.rod_length": self.rotary_rod_length,
            "rotary.rod_x": self.rotary_rod_x,
            "rotary.origin_axial": self.rotary_origin_axial,
            "rotary.origin_radial": self.rotary_origin_radial,
            "rotary.origin_custom": self.rotary_origin_custom,
        }
        # per-part motion-limit min/max vars (machine.lim_{role}_{axis}_{min|max})
        for (role, ax), (mn, mx) in self.machine_limits.items():
            m[f"machine.lim_{role}_{ax}_min"] = mn
            m[f"machine.lim_{role}_{ax}_max"] = mx
        # _SimMixin adds the Printhead-sim fields (sim.*) here. Preset
        # ownership follows the TAB each field's widget lives on: sim.kinematics /
        # sim.max_accel / sim.rotary_accel / sim.scv (the acceleration limits, on the
        # Machine page) are
        # claimed by the MACHINE preset via _machine_motion_keys; EVERY other sim.*
        # field -- elevation, the process knobs, the deposition toggle + sub-options --
        # is on the Physics tab and rides the PHYSICS preset (the "calibration" kind,
        # via _calibration_persist_keys -> _PHYSICS_PROCESS_KEYS). None fall into the
        # scaffold kind anymore.
        m.update(self._sim_persist_entries())
        # the printer's process limits (machine.range_*, the Printer model
        # card; modern UI only), owned by the MACHINE preset
        m.update(getattr(self, "_ft_persist_entries", dict)())
        # _CalibrationMixin adds the rig-calibration fields (cal.*) here. The
        # cal.* keys are claimed by the "calibration" preset kind (see
        # _keys_for_kind), so they ride a Physics preset, not a scaffold one.
        m.update(self._calibration_persist_entries())
        # ... and the Experiment-sweeps page's sw.* fields (gui_sweeps, modern
        # shell only; the gui_core no-op returns {} in the classic App). They
        # ride a scaffold preset like any ungrouped field.
        m.update(self._sweeps_persist_entries())
        m.update(self._mandrel_persist_entries())
        # the scaffold designer's embedded-preview display toggles (pv.show_tubes /
        # pv.show_sag). Modern shell only (a no-op {} in classic); no kind prefix,
        # so they ride the SCAFFOLD preset (see _keys_for_kind).
        m.update(self._preview_persist_entries())
        # the G-code page's document (its chips, pieces and flavor):
        # modern shell only, scaffold kind -- it belongs to the design
        if hasattr(self, "_gce_persist_entries"):
            m.update(self._gce_persist_entries())
        # scaffolds 2.. (keys s2.*, s3.*...); scaffold 1 is the s.* entries
        # above. As many as there are slots (`_scaf_grow`).
        for idx in range(1, len(self.scaf_vars)):
            v = self.scaf_vars[idx]
            prefix = f"s{idx + 1}."
            for key in ("width", "height", "stepover", "diameter", "layers",
                        "speed", "speed_unit", "speed_ratio", "layer_speed_ratios",
                        "m_layer_speed_ratios", "loop_speed", "start_x", "start_y", "start_z",
                        "start_angle", "footprint_angle",
                        "arc_radius_mult", "arc_segments",
                        "rotation", "color", "angle_pattern_on",
                        "angle_pattern", "angle_stepover_pattern",
                        "angle_offset_pattern", "layer_overrides",
                        "stepover_end", "grade_angle", "stepover_top",
                        "linear_on", "linear_wavelength",
                        "linear_spacing", "linear_pattern", "linear_grid",
                        "shape", "shape_sides", "shape_polygon", "shape_grid",
                        "array_on", "array_nx", "array_ny", "array_nz",
                        "array_dx", "array_dy", "array_dz",
                        "array_flip_x", "array_flip_y",
                        "array_travel", "link_loops_on", "link_loops_mode",
                        "link_loop_angle", "link_loop_dist",
                        "link_loop_travel", "perimeter_travel",
                        "perimeter_travel_offset"):
                m[prefix + key] = v[key]
        return m

    @staticmethod
    def _migrate_legacy_slide(data):
        """Translate a pre-build-plate preset's slide.* keys into the new plate.*
        keys so old saved presets still load. The old slide was a rectangle with
        its lower-left corner at the origin, which is exactly the plate's corner
        anchor -> plate.corner = (0, 0); width/height were length strings
        ('3in' / '76.2') parsed to mm here. A no-op when the preset already carries
        plate.* keys or has no slide.* keys."""
        if not isinstance(data, dict):
            return data
        if "plate.shape" in data or not any(
                k in data for k in ("slide.show", "slide.width", "slide.height")):
            return data
        out = dict(data)
        out.setdefault("plate.shape", "rectangular")
        if "slide.show" in data:
            out.setdefault("plate.show", data["slide.show"])
        w = _parse_length_mm(str(data.get("slide.width", "")))[0]
        h = _parse_length_mm(str(data.get("slide.height", "")))[0]
        if w:
            out.setdefault("plate.x", f"{w:g}")
        if h:
            out.setdefault("plate.y", f"{h:g}")
        # the old slide sat with its lower-left corner at the g-code origin, which is
        # now the plate's implicit anchor (footprint at (0,0)), so nothing more to map.
        return out

    @staticmethod
    def _migrate_legacy_render_travel(data):
        """Translate a pre-split preset's `s.render_travel` into `s.travel_in_stl`.

        The one old checkbox meant "lay the travel as fiber, in everything": the
        solve, the .depo the player rides, and the exported STL. Travel fiber is
        now always solved and always in the .depo (the viewer's Show/Hide travel
        fiber is what turns it off), so the only part of that intent still needing
        a saved answer is the STL -- which is where a ticked old box maps.

        Without this the key is simply absent from the persist map and
        `_apply_preset_for_kind` falls through to the factory default, so someone
        who saved the box TICKED would quietly get a travel-free STL. A no-op once
        the preset carries the new key."""
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if "s.render_travel" in data and "s.travel_in_stl" not in data:
            out["s.travel_in_stl"] = bool(data["s.render_travel"])
        return out

    #: the one-tab CTS keys -> the CTS (4-axis) tab's own. An older preset's
    #: mandrel ladder shared the plate ladder's speeds and lines, behind a
    #: `sw.cts_collector` switch, and kept only its two layout lengths.
    _CTS4_FROM_LEGACY = (("sw.cts_start", "sw.cts4_start"),
                         ("sw.cts_stop", "sw.cts4_stop"),
                         ("sw.cts_step", "sw.cts4_step"),
                         ("sw.cts_reps", "sw.cts4_reps"),
                         ("sw.cts_pitch", "sw.cts4_pitch"),
                         ("sw.cts_m_line_len", "sw.cts4_line_len"),
                         ("sw.cts_m_gap", "sw.cts4_gap"))

    @classmethod
    def _migrate_legacy_cts_collector(cls, data):
        """Split a one-tab CTS preset into the two CTS tabs.

        A preset from before the split carries `sw.cts_collector` and the
        mandrel's `sw.cts_m_line_len` / `sw.cts_m_gap`. Its ladder was the
        mandrel's ladder too, so it seeds the CTS (4-axis) tab's own keys; the
        plate keys stay as they are, so a plate ladder prints byte-identically.
        A preset saved with the collector on the MANDREL reopens on the
        CTS (4-axis) tab and targets its print, which is what it was showing.
        Without this the old keys are dropped and the 4-axis tab opens at its
        factory ladder. A no-op on a preset with none of the old keys."""
        if not isinstance(data, dict) or not any(
                k in data for k in ("sw.cts_collector", "sw.cts_m_line_len",
                                    "sw.cts_m_gap")):
            return data
        out = dict(data)
        for old, new in cls._CTS4_FROM_LEGACY:
            if old in data and new not in data:
                out[new] = data[old]
        for old in ("sw.cts_collector", "sw.cts_m_line_len", "sw.cts_m_gap"):
            out.pop(old, None)
        if data.get("sw.cts_collector") == "mandrel":
            if out.get("sw.cal_tab", "cts") == "cts":
                out["sw.cal_tab"] = "cts4"
            if out.get("sw.kind", "cts") == "cts":
                out["sw.kind"] = "cts4"
        return out

    @staticmethod
    def _migrate_legacy_lag_amplitude(data):
        """Fold a pre-A physics preset's `cal.lag_amplitude` (the retired
        multiplier k) and `cal.lag_ceiling_angle_deg` (the angle whose tangent
        was the amplitude) into today's `cal.lag_amplitude`, which IS the
        amplitude A.

        Without this the ceiling key is simply absent from the persist map now,
        so it is dropped, and the stored k lands in the field that held a
        multiplier in older presets and holds A now. A preset saying k = 1 then opens the app at
        A = 1 against a shipped 2.8629 -- every corner under-lagged 2.9x, with
        nothing on screen but a Jet lag tile reading "A 1". The shipped
        `presets/physics/MEWron_0.2_22G.json` is exactly such a preset AND the boot
        default, which is how this was found: the rendered Physics page.

        The RULE lives in `mew_calibration.migrate_lag_amplitude` and is called,
        not copied -- the engine path (`models_from_profile`) and this one must
        not drift. A no-op once the preset carries A alone."""
        if not isinstance(data, dict):
            return data
        try:
            import mew_calibration
        except ImportError:
            return data
        return mew_calibration.migrate_lag_amplitude(data)

    @staticmethod
    def _migrate_legacy_needle_radius(data):
        """A preset saved when the needle was a bore RADIUS
        (`cal.noz_inner_radius`) loads as the same needle: the value becomes
        today's `cal.noz_inner_diameter`, doubled. The rule lives in
        `mew_calibration.migrate_inner_diameter`, which `models_from_profile`
        runs too, so the two paths cannot drift."""
        if not isinstance(data, dict):
            return data
        try:
            import mew_calibration
        except ImportError:
            return data
        return mew_calibration.migrate_inner_diameter(data)

    @staticmethod
    def _migrate_legacy_no_sag(data):
        """Translate a pre-Sag-solver preset's `g.no_sag` / `s.no_sag` booleans
        into the `g.sag_mode` / `s.sag_mode` combo labels that replaced them.

        Without this, loading an old preset that had "Disable sagging" TICKED
        would silently come back as Exact -- the key is simply absent from the
        persist map now, so `_apply_preset_for_kind` falls through to the factory
        default and the user's saved intent is inverted, quietly. A no-op once
        the preset carries the new key."""
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for old, new in (("g.no_sag", "g.sag_mode"), ("s.no_sag", "s.sag_mode")):
            if old in data and new not in data:
                out[new] = SAG_SOLVER_VAL2LBL[
                    "off" if data[old] else "exact"]
        return out

    @staticmethod
    def _migrate_legacy_physics_master(data):
        """A preset saved with the old master "Physics-deposited scaffold" switch
        OFF meant no physics at all. That key now carries only Jet lag, so the
        other three law switches -- Fiber sagging, Fiber fusion and Fiber
        diameter (stored as its inverse, the override) -- are turned off with it.
        A no-op once the preset carries `sim.sag_law`, which only the per-law
        build writes, or when the master was on."""
        if not isinstance(data, dict):
            return data
        if "sim.sag_law" in data or "sim.physics_deposit" not in data:
            return data
        if data["sim.physics_deposit"]:
            return data
        out = dict(data)
        out["sim.sag_law"] = False
        out["sim.physical_fusion"] = False
        out["sim.diam_override"] = True
        return out

    @staticmethod
    def _migrate_legacy_mandrel_mode(data):
        """A preset that pins a winding angle but names no design mode predates
        the outcome-first 4-axis page, and meant the ANGLE.

        `m.design_mode`'s factory default is "turn" (angle, pore and length
        all built, the mandrel turning at each end), so without this the loader falls
        through to that default and an old preset silently changes meaning --
        which is exactly what the `s.collector` note above warns about. A no-op
        once the preset carries the key.

        Such a preset's `m.eta` was TYPED -- nothing wrote it back then -- so a
        non-blank one also turns on `m.closure_override`, which is what keeps
        that pinned eta in charge now that the page resolves it by default."""
        if not isinstance(data, dict):
            return data
        if "m.angle" in data and "m.design_mode" not in data:
            extra = {"m.design_mode": "angle"}
            if str(data.get("m.eta", "")).strip() \
                    and "m.closure_override" not in data:
                extra["m.closure_override"] = True
            return dict(data, **extra)
        return data

    @staticmethod
    def _migrate_plate_corner(data):
        """Fold an older preset's Plate corner X/Y into its bed offset,
        and drop `origin.ref`.

        In the simulation the plate's bottom-left corner landed at the bed
        datum's corner + Plate corner + bed offset (`gcode_player.
        _compute_registration`, measured), so for a FLAT machine the sum is kept
        and the plate stays where it was drawn. In the exported g-code the corner
        cancelled under the Collector origin and so changes nothing there; a
        preset saved under the World origin now zeroes on the plate, which is
        the point of the removal.

        A ROTARY preset (`rotary.on`) keeps its bed offset unchanged: the corner
        never placed a rod or a table, so adding it would move one."""
        if not isinstance(data, dict) or not any(
                k in data for k in ("plate.corner_x", "plate.corner_y",
                                    "origin.ref")):
            return data
        out = {k: v for k, v in data.items()
               if k not in ("plate.corner_x", "plate.corner_y", "origin.ref")}
        if str(data.get("rotary.on")) not in ("True", "true", "1"):
            for axis in ("x", "y"):
                try:
                    c = float(data.get("plate.corner_" + axis, 0) or 0)
                    s = float(data.get("slide.offset_" + axis, 0) or 0)
                except (TypeError, ValueError):
                    continue
                if c:
                    out["slide.offset_" + axis] = repr(s + c)
        return out

    @staticmethod
    def _migrate_legacy_holder_stl(data):
        """`rotary.stl` was a second STL slot beside the bed one in older
        presets. A 4-axis rig has no bed, so the holder now goes IN the bed
        slot and that key is gone.

        Moved rather than dropped, because a preset that names a holder and no
        bed describes exactly the machine the new slot expects -- and dropped,
        the user's holder would simply vanish from the scene. It never
        overwrites a bed STL that is already there: a preset carrying both was
        written by a rig that had both, and the bed is the one the profile has
        always drawn."""
        if not isinstance(data, dict) or not data.get("rotary.stl"):
            return data
        if data.get("machine.z_stl"):
            return data
        out = dict(data)
        out["machine.z_stl"] = out.pop("rotary.stl")
        return out

    @staticmethod
    def _migrate_legacy_end_margin(data):
        """`m.end_margin` was the 4-axis page's placement in older presets, and
        it meant "this far in from the rod's left end" -- which is exactly the
        left rung of the ladder that replaced it.

        Dropped rather than migrated, an old tube preset would load with the
        offset at its factory 0 and print flush with the rod tip, silently
        losing the clearance the user saved it with. A no-op once the preset
        carries the new keys."""
        if not isinstance(data, dict) or "m.end_margin" not in data:
            return data
        if "m.place_offset" in data:
            return data
        return dict(data, **{"m.place_offset": data["m.end_margin"],
                             "m.place": "left"})

    @staticmethod
    def _migrate_placement_key(data):
        """A tube's placement (`m.place`, each G-code window scaffold's
        `gce.s<n>.m_place`, Calibrate's `sw.rod_place`) is saved by its KEY
        since 2026-11-13; before, the tube's was saved by its menu label,
        in whatever wording the menu had. An old label becomes its key
        (`gui_mandrel.placement_from_saved`), once, here. A value that is
        neither is left for the validator to name."""
        if not isinstance(data, dict):
            return data
        import gui_mandrel
        out = None
        for k, v in data.items():
            if k in ("m.place", "sw.rod_place") or re.fullmatch(
                    r"gce\.s\d+\.m_place", k):
                key = gui_mandrel.placement_from_saved(v)
                if key != v:
                    out = dict(data) if out is None else out
                    out[k] = key
        return data if out is None else out

    def _apply_all_toggles(self):
        """Re-sync every show/hide + enable/disable to its controlling checkbox
        after a bulk field change (e.g. loading a preset)."""
        self._toggle_sag()
        self._toggle_scaffold_sag()
        self._toggle_stabilization()
        self._toggle_g2g3()
        self._toggle_plate_shape()
        for v in self.scaf_vars:
            self._toggle_angle_pattern(v)
            self._toggle_linear_pattern(v)
            self._toggle_array(v)
            self._sync_shape_combo(v)
            self._toggle_shape(v)
        self._refresh_flip_buttons()
        self._sync_scaffold_count()
