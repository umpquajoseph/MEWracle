#!/usr/bin/env python3
"""gui_scaffold.py -- Scaffold mixin: the per-scaffold parameter tabs (core /
array / shape / linear / angle pattern), the stabilization + export global tabs,
and the toggles and array-spec helpers behind them. The validators and CLI-arg
builders that compile these fields into a run live in gui_args._ArgsMixin."""
import math

from gui_common import *

import gui_tips
import gui_units
import mew_gcode
import gui_widgets

# Curve quality -- display labels for `mew_deposition.CURVE_QUALITY`. The var holds
# the ENGINE token and the combobox the label, so a preset round-trip does not
# depend on wording.
CURVE_QUALITY_ORDER = ("draft", "standard", "fine")
CURVE_QUALITY_CHOICES = (
    ("draft", "Draft (0.25 Ø)"),
    ("standard", "Standard (0.1 Ø)"),
    ("fine", "Fine (0.03 Ø)"),
)
CURVE_QUALITY_VAL2LBL = {v: l for v, l in CURVE_QUALITY_CHOICES}
CURVE_QUALITY_LBL2VAL = {l: v for v, l in CURVE_QUALITY_CHOICES}

# Footprint-shape dropdown: (canonical value sent to the worker, display label).
# The canonical value persists and feeds --shape / the JSON spec, so it matches
# scaffold_gcode._SHAPES.
_SHAPE_CHOICES = [
    ("rectangle", "Rectangle"),
    ("circle", "Circle"),
    ("ellipse", "Ellipse"),
    ("polygon", "Regular polygon"),
    ("custom", "Custom (draw)"),
]
_SHAPE_VAL2LBL = {val: lbl for val, lbl in _SHAPE_CHOICES}
_SHAPE_LBL2VAL = {lbl: val for val, lbl in _SHAPE_CHOICES}

#: Every letter a rotary joint may be spelled with. ISO 6983 names A/B/C for
#: rotations about X/Y/Z and U/V/W for secondary axes, and a rig's words are a
#: property of the rig -- Terranova's own files carry U and V -- so nothing here
#: may assume A and C. The authority is the same one the parser uses:
#: `gcode_to_fibers._UNREAD_ROTARY_WORDS` (U, V, B, C) plus A, which that parser
#: reads by default because a four-axis mandrel file carries it. The literal
#: lives in `mew_gcode` (which never drags the converter in).
_ROTARY_WORDS = mew_gcode.ROTARY_WORDS


_CURVE_QUALITY_TIP = (
    "How far the DRAWN fiber may sit from the curve it is meant to "
    "follow, as a fraction of the fiber's own diameter.\n\n"
    "This is the setting to reach for when curves look blocky or "
    "faceted. It matters most on a mandrel, where nearly every point "
    "on the path is a junction, and it is what stops a 120° cell "
    "corner being drawn as a polygon.\n\n"
    "Draft: a quarter of a fiber diameter. The fastest.\n"
    "Standard: a tenth of a diameter, the same bound the viewer holds "
    "its own tube to. The default.\n"
    "Fine: three hundredths. For a final render or a figure. It can "
    "multiply the deposited point count several times.\n\n"
    "Measured on the five 4-axis cell families at 10 layers, against a "
    "16× denser reference: the worst family drew 1.6 fiber diameters "
    "off the curve before this existed, 0.45 at Draft, 0.16 at "
    "Standard and 0.08 at Fine.\n\n"
    "On a plate scaffold it applies only with Auto corner resolution "
    "ticked. Otherwise a plate uses the fixed Corner resolution grid.\n\n"
    "Only applies with Jet lag on (the Physics page). It refines "
    "the jet's catch-up, and there is none without it.")


class _RowTips:
    """Lets `_collector_text` re-word the TOOLTIPS of gui_form rows.

    That hook re-words a widget by calling ``configure(text=...)`` on it. Help
    whose words follow the collector is a tooltip, not a grey label, so this stands in
    for the label and re-points the rows' tips
    instead."""

    def __init__(self, *rows):
        self.rows = rows

    def configure(self, text):
        for rw in self.rows:
            rw.set_tip(text)


class _ScaffoldMixin:
    def _build_scaffold_tab(self, frm):
        """The Parameters -> Scaffold mode: create the vars, then lay out the
        containers. Split so the modern shell can re-container the SAME fields
        via its _build_scaffold_container override."""
        self._init_scaffold_vars()
        self._build_scaffold_container(frm)

    def _init_scaffold_vars(self):
        """Create every scaffold Tk variable (no widgets). Scaffold 1 keeps the
        original self.s_* vars (and "s." persist keys) for backward-compatible
        saved defaults; scaffolds 2..5 get their own dicts."""
        # The COLLECTOR the design is laid on, and therefore which half of this
        # page is shown: "plate" (3-axis serpentine) or "mandrel" (4-axis tube --
        # gui_mandrel). A real input mode, not a display filter: _active_mode()
        # reads it, so it also picks which worker argv the shared Generate button
        # builds. Persisted as "s.collector".
        self.s_collector = tk.StringVar(value="plate")
        self.s_width = tk.StringVar(value="30")
        self.s_height = tk.StringVar(value="30")
        self.s_stepover = tk.StringVar(value="0.5")
        self.s_diameter = tk.StringVar(value="0.02")
        self.s_layers = tk.StringVar(value="10")
        self.s_speed = tk.StringVar(value="600")
        self.s_loop_speed = tk.StringVar(value="")   # blank = inherit print speed
        self.s_start_x = tk.StringVar(value="40")
        self.s_start_y = tk.StringVar(value="10")
        self.s_start_z = tk.StringVar(value="0")     # z offset; 0 = on the plate top
        self.s_start_angle = tk.StringVar(value="0")
        self.s_footprint_angle = tk.StringVar(value="0")  # rotate the whole footprint
        self.s_arc_radius_mult = tk.StringVar(value="1.5")   # x stepover
        self.s_arc_segments = tk.StringVar(value="8")
        self.s_color = tk.StringVar(value=SCAFFOLD_COLORS[0])   # viewer colour
        # scaffold 1's rectangular-array vars (per-scaffold now; "s.array_*" keys)
        self.s_array_on = tk.BooleanVar(value=False)
        self.s_array_nx = tk.StringVar(value="2")
        self.s_array_ny = tk.StringVar(value="2")
        self.s_array_nz = tk.StringVar(value="1")       # Z levels (1 = no stacking)
        self.s_array_dx = tk.StringVar(value="35")
        self.s_array_dy = tk.StringVar(value="35")
        self.s_array_dz = tk.StringVar(value="0.2")     # Z spacing (default = the
        #                       10-layer x 0.02 mm default stack height: copies touch)
        self.s_array_flip_x = tk.BooleanVar(value=False)
        self.s_array_flip_y = tk.BooleanVar(value=False)
        self.s_array_travel = tk.StringVar(value="2")   # mm clear of each scaffold

        self.scaf_vars = [{
            "width": self.s_width, "height": self.s_height,
            "stepover": self.s_stepover, "diameter": self.s_diameter,
            "layers": self.s_layers, "speed": self.s_speed,
            "loop_speed": self.s_loop_speed, "start_x": self.s_start_x,
            "start_y": self.s_start_y, "start_z": self.s_start_z,
            "start_angle": self.s_start_angle,
            "footprint_angle": self.s_footprint_angle,
            "arc_radius_mult": self.s_arc_radius_mult,
            "arc_segments": self.s_arc_segments, "color": self.s_color,
            # per-layer angle pattern: a checkbox plus three parallel canonical
            # comma lists the multi-column table is a view over. Slots align
            # index-for-index; a blank Stepover/Offset slot inherits the scaffold
            # Stepover / applies no shift.
            "angle_pattern_on": tk.BooleanVar(value=False),
            "angle_pattern": tk.StringVar(value=""),
            "angle_stepover_pattern": tk.StringVar(value=""),
            "angle_offset_pattern": tk.StringVar(value=""),
            # per-layer parameters (the G-code page's Layer parameters table):
            # JSON {"<layer, 1-based>": {key: value}}, blank = none; read by
            # scaffold_gcode.parse_layer_overrides
            "layer_overrides": tk.StringVar(value=""),
            # an in-layer pore grade: the spacing at the far edge (mm, blank =
            # even spacing) and the direction it grades along (deg)
            "stepover_end": tk.StringVar(value=""),
            "grade_angle": tk.StringVar(value="0"),
            # a layer by layer grade: the top layer's spacing (mm, blank =
            # none); it writes each layer's stepover override
            # (gui_gcode_params.sync_layer_grade)
            "stepover_top": tk.StringVar(value=""),
            # linear pattern: wave each straight fiber through a shape.
            # linear_pattern is the canonical "x1,y1; x2,y2" string of ABSOLUTE
            # vertices the table and the preview graph are views over. The shape
            # repeats every wavelength + spacing mm.
            "linear_on": tk.BooleanVar(value=False),
            "linear_wavelength": tk.StringVar(value="1"),
            "linear_spacing": tk.StringVar(value="0"),
            "linear_pattern": tk.StringVar(value=""),
            # preview-only: how many periods the live graph lays out (1 or 2), the
            # snap-grid unit (mm), and the click mode ("add"/"remove")
            "linear_periods": tk.StringVar(value="1"),
            "linear_grid": tk.StringVar(value="0.1"),
            "linear_editmode": tk.StringVar(value="add"),
            # footprint shape: the canonical value that persists and feeds the
            # worker (matches scaffold_gcode._SHAPES). shape_sides is
            # circle/ellipse smoothness or the regular-polygon side count;
            # shape_polygon is the custom outline in linear_pattern's format.
            "shape": tk.StringVar(value="rectangle"),
            "shape_sides": tk.StringVar(value="64"),
            "shape_polygon": tk.StringVar(value=""),
            # preview-only: the custom-draw snap grid (mm) and click mode
            "shape_grid": tk.StringVar(value="1"),
            "shape_editmode": tk.StringVar(value="add"),
            # this scaffold's own rectangular array
            "array_on": self.s_array_on, "array_nx": self.s_array_nx,
            "array_ny": self.s_array_ny, "array_nz": self.s_array_nz,
            "array_dx": self.s_array_dx,
            "array_dy": self.s_array_dy, "array_dz": self.s_array_dz,
            "array_flip_x": self.s_array_flip_x,
            "array_flip_y": self.s_array_flip_y,
            "array_travel": self.s_array_travel,
            # linking loops: a per-scaffold lead-in/out loop at coincident square
            # corners. link_loops_on is the master toggle; _mode is auto/manual;
            # the thresholds gate Auto (dist also bounds "a corner" for Manual).
            "link_loops_on": tk.BooleanVar(value=True),
            "link_loops_mode": tk.StringVar(value="auto"),
            "link_loop_angle": tk.StringVar(value="25"),
            "link_loop_dist": tk.StringVar(value="0.5"),
            # travel-arrival loops: the hop between two layers that do not share
            # a free end is deposited fiber too.
            "link_loop_travel": tk.BooleanVar(value=False),
            # perimeter travel: route every automatic travel along the
            # scaffold's outline, `perimeter_travel_offset` mm clear of it
            "perimeter_travel": tk.BooleanVar(value=False),
            "perimeter_travel_offset": tk.StringVar(value="1"),
        }]
        for i in range(1, 5):
            self.scaf_vars.append(self._new_scaffold_vars(i))
        import gui_speed_ratio
        for values in self.scaf_vars:
            gui_speed_ratio.attach(self, values)
        # scaffolds whose fields are already populated (so revealing them must
        # NOT overwrite them with a fresh prefill); scaffold 1 always is.
        self._scaf_inited = {0}

    # ------------------------------------------------------------------ #
    # collector mode: 3-axis flat plate <-> 4-axis rotating mandrel
    # ------------------------------------------------------------------ #
    def _collector_register(self, widget, mode):
        """Record a widget that belongs to ONE collector mode -- or to several,
        passed as a tuple of modes. A tuple, never two calls: the walk below
        would show the widget on one pass and hide it on the next.

        Two modes: "plate" (3-axis) and "mandrel" (4-axis).

        Shared by both shells so the show/hide rule lives in one place. The
        mechanism is `grid_remove()`: Tk remembers the grid options, so `grid()`
        puts the widget back exactly where it was."""
        self._collector_widgets = getattr(self, "_collector_widgets", [])
        self._collector_widgets.append((widget, mode))

    def _collector_text(self, widget, plate, mandrel):
        """Record a widget whose LABEL changes with the collector.

        The end-of-print exit runs off a plate EDGE on 3-axis and off a tube END
        on 4-axis, and calling it either one on the other page is how a user
        concludes the option does not apply to them."""
        self._collector_texts = getattr(self, "_collector_texts", [])
        self._collector_texts.append((widget, {"plate": plate,
                                               "mandrel": mandrel}))

    def _toggle_exit_move(self, *_):
        """Grey the mm field when the exit move itself is off."""
        e = getattr(self, "s_exit_margin_entry", None)
        if e is not None:
            try:
                e.configure(state=("normal" if self.s_exit_move.get()
                                  else "disabled"))
            except tk.TclError:
                pass

    def _collector_register_tab(self, notebook, frame, mode):
        """Same, for a NOTEBOOK TAB (the classic UI's global settings).

        A tab is not gridded, so ttk's per-tab `state="hidden"` is what hides
        one; unlike `hide()`+`add()` it keeps the tab in its original position."""
        self._collector_tabs = getattr(self, "_collector_tabs", [])
        self._collector_tabs.append((notebook, frame, mode))

    def _build_collector_toggle(self, parent, modern):
        """The 3-axis / 4-axis mode toggle that heads the Scaffold designer.

        TWO SHAPES. The modern shell draws a gui_widgets.SegmentedBar reading
        "3 axis / 4 axis" -- one joined bar, no leading caption, because radios
        plus a "Collector:" caption read as separate controls above a page
        whose every other group is one surface. The classic developer UI
        keeps the labelled radio row it has always had, where the long names
        ("Rotating mandrel -- 4-axis") still fit the wider pane.

        A radio ROW, not a combobox: the choice is small and every option
        should be readable without opening anything.

        `command` is what loads that mode's default machine preset and default
        design (gui_presets._collector_mode_chosen) -- a 3-axis machine cannot
        print a 4-axis design, so the two travel together. It is on the radios'
        `command` and NOT on a trace of s_collector on purpose: `command` fires
        only for a real click, while a trace also fires for the programmatic set
        _apply_preset_for_kind does while applying the very preset being loaded,
        which re-enters without bound. The show/hide half of the switch is the
        separate `_apply_collector_mode` trace, which must run on both."""
        self._mandrel_vars_make()
        outer = ttk.Frame(parent)
        head = ttk.Frame(outer)
        head.pack(side="top", anchor="w")
        if modern:
            # The words are the AXIS COUNT alone. The long collector names plus
            # a leading "Collector:" wanted about 570 px in a 545 px pane and
            # clipped the third label; naming the axes says the same thing in a
            # third of the width, and the page title already says "Scaffold
            # designer", so the caption above the bar was saying nothing the
            # tooltip beside it does not say better.
            self._collector_bar = gui_widgets.SegmentedBar(
                head,
                options=(("plate", "Plate (3-axis)"), ("mandrel", "Mandrel (4-axis)")),
                variable=self.s_collector,
                command=self._collector_mode_chosen)
            # No caption is drawn in front of the bar (see the note above), so
            # the Ctrl+F finder is given the name to search it by. A searchable
            # name only -- nothing new appears on screen.
            self._collector_bar._finder_label = "Collector"
            self._collector_bar.pack(side="left")
        else:
            ttk.Label(head, text="Collector:").pack(side="left")
            ttk.Radiobutton(head, text="Plate — 3-axis", value="plate",
                            variable=self.s_collector,
                            command=self._collector_mode_chosen).pack(
                side="left", padx=(8, 0))
            ttk.Radiobutton(head, text="Rotating mandrel — 4-axis",
                            value="mandrel", variable=self.s_collector,
                            command=self._collector_mode_chosen).pack(
                side="left", padx=(12, 0))
        if modern:
            import gui_form
            import gui_tips
            # KEPT as an ⓘ, not folded into a tooltip: it is the help for the
            # page's mode switch, not for a parameter, and at five paragraphs
            # it is the kind of long story the pattern keeps a dot for.
            gui_form.exempt(gui_tips.info_dot(
                head,
                "3-axis is the plate: the planar serpentine scaffold, a "
                "footprint, a fiber spacing and layer rotation.\n\n"
                "4-axis is the rotating mandrel: a closing helical tube on a "
                "cylindrical collector, driven by X plus rotary A moves. It "
                "has no footprint, no turnaround and no linking loops.\n\n"
                "Layers, speed, fiber diameter, the sag solver, tube facets, the "
                "preview color, the export options and the presets are the SAME "
                "settings in every mode; only the geometry sections swap.",
                title="Collector"),
                "help for the collector mode switch (the page's mode, not a "
                "parameter)").pack(side="left", padx=(8, 0))
        return outer

    def _apply_collector_mode(self, *_):
        """Show the sections this collector mode uses, hide the rest, and hand
        the shared preview panel to whichever half now owns it."""
        mode = self.s_collector.get()
        if mode not in ("plate", "mandrel"):
            # A preset or a remembered state from a build that had a third
            # collector: there is no such mode here, so it opens flat.
            self.s_collector.set("plate")
            return
        for widget, want in getattr(self, "_collector_widgets", []):
            try:
                if (mode in want) if isinstance(want, tuple) else want == mode:
                    widget.grid()
                else:
                    widget.grid_remove()
            except tk.TclError:          # a widget destroyed by a page rebuild
                continue
        for widget, words in getattr(self, "_collector_texts", []):
            try:
                widget.configure(text=words[mode])
            except (tk.TclError, KeyError):
                continue
        for notebook, frame, want in getattr(self, "_collector_tabs", []):
            try:
                notebook.tab(frame,
                             state=("normal" if want == mode else "hidden"))
            except tk.TclError:
                continue
        # A tube is a single continuous part: the multi-scaffold stack has no
        # meaning on it, so pin the count rather than leaving a stale 3 behind.
        if mode == "mandrel" \
                and getattr(self, "s_count", None) is not None:
            if self.s_count.get() != "1":
                self.s_count.set("1")
        # The toggle changes the input mode WITHOUT a tab change, so the
        # remembered target must follow it here -- `_on_tab_changed` never fires.
        # Only when the remembered mode is one of this page's two, so flipping
        # the toggle cannot steal the G-code page's target.
        if getattr(self, "_last_input_mode", None) in ("scaffold", "mandrel"):
            self._last_input_mode = (mode if mode == "mandrel"
                                     else "scaffold")
        # The action bar is built AFTER the pages, so the first call (from the
        # form builder) has no button to relabel yet.
        relabel = getattr(self, "_refresh_result_controls", None)
        if callable(relabel) and getattr(self, "run_btn", None) is not None:
            relabel()
        kick = getattr(self, "_collector_preview_switch", None)
        if callable(kick):
            kick()

    def _build_scaffold_container(self, frm):
        """CLASSIC layout of the scaffold mode (the modern shell overrides
        this): the count spinner over two notebooks --

          scaf_nb   : one tab per scaffold index, each an inner Core / Shape /
                      Array / Linear notebook
          global_nb : Stabilization / Export / Presets -- settings that apply
                      ONCE to the whole continuous-fiber file, not per scaffold"""
        pad = self.pad
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(2, weight=1)

        self._build_collector_toggle(frm, modern=False).grid(
            row=0, column=0, sticky="w", pady=(0, 6))

        cnt = ttk.Frame(frm)
        cnt.grid(row=1, column=0, sticky="w", pady=(0, 4))
        self._collector_register(cnt, "plate")
        ttk.Label(cnt, text="Number of scaffolds:").pack(side="left")
        self.s_count = tk.StringVar(value="1")
        # no upper limit in the design: the bound is the Spinbox's own need
        ttk.Spinbox(cnt, from_=1, to=SCAFFOLD_SPIN_MAX, width=4,
                    textvariable=self.s_count,
                    state="readonly",
                    command=self._sync_scaffold_count).pack(side="left", padx=(4, 0))
        ttk.Label(cnt, text="(each stacks in Z on the previous by default; "
                            "every field stays editable)",
                  foreground="#777").pack(side="left", padx=(8, 0))

        # --- per-scaffold notebook: each index has inner Core / Array tabs ---
        # (The build plate / machine bounds are gui_core's own top-level "Machine
        # and Collector" tab: they apply to every pipeline, not just this one.)
        self.scaf_nb = ttk.Notebook(frm)
        self.scaf_nb.grid(row=2, column=0, sticky="nsew")
        self._collector_register(self.scaf_nb, "plate")
        for i in range(len(self.scaf_vars)):
            self._scaf_nb_tab(i)
        # back-compat alias so existing self.s_rotation references keep working
        self.s_rotation = self.scaf_vars[0]["rotation"]

        # --- the 4-axis half, in the SAME grid cell (the two are exclusive) --
        self.mandrel_nb = ttk.Notebook(frm)
        self.mandrel_nb.grid(row=2, column=0, sticky="nsew")
        self._collector_register(self.mandrel_nb, "mandrel")
        for text, builder in (
                ("  Collector  ", self._build_mandrel_collector_fields),
                ("  Tube geometry  ", self._build_mandrel_geometry_fields),
                ("  Advanced  ", self._build_mandrel_closure_fields),
                # Always present in the developer UI rather than family-gated:
                # a ttk.Notebook tab is hidden with `hide()`, not `grid_remove`,
                # so it cannot ride `_mandrel_family_register` the way the
                # modern shell's disclosure does.
                ("  Custom cell  ", self._build_mandrel_cell_editor_fields)):
            tabf = ttk.Frame(self.mandrel_nb, padding=6)
            self.mandrel_nb.add(tabf, text=text)
            builder(tabf)

        # --- global settings (apply once to the whole file): tabs ---
        self.global_nb = ttk.Notebook(frm)
        self.global_nb.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        stab_tab = ttk.Frame(self.global_nb, padding=8)
        export_tab = ttk.Frame(self.global_nb, padding=8)
        preset_tab = ttk.Frame(self.global_nb, padding=8)
        self.global_nb.add(stab_tab, text="  Stabilization  ")
        # PLATE ONLY, exactly as in the modern shell: the stabilization base is a
        # dense pad printed FIRST on the plate, and scaffold_gcode's --mandrel
        # branch returns before any --stabilization flag is read.
        self._collector_register_tab(self.global_nb, stab_tab, "plate")
        # The print slide lives on the global "Build plate" tab. _SimMixin inserts
        # a "Printhead sim" tab here; the unoverridden hook leaves it unchanged.
        self._build_sim_tab(self.global_nb)
        self.global_nb.add(export_tab, text="  Export  ")
        self.global_nb.add(preset_tab, text="  Presets  ")
        self._build_stabilization_tab(stab_tab)
        self._build_export_tab(export_tab)
        self._build_presets_tab(preset_tab)

        # show only the active scaffold tabs (count defaults to 1)
        self._sync_scaffold_count()
        # ... then hide whichever collector half is not selected
        self.s_collector.trace_add("write", self._apply_collector_mode)
        self._apply_collector_mode()

    def _build_stabilization_tab(self, parent):
        """Global stabilization base: a checkbox over its parameter fields. The
        tab always exists; unticking greys the fields rather than hiding them, so
        the tab stays a discoverable home for the feature."""
        import gui_form
        self.stab_on = tk.BooleanVar(value=False)
        parent.columnconfigure(0, weight=0)   # see _build_quality_tab
        F = gui_form.Form(parent)
        F.check("Add a stabilization scaffold" if hasattr(self, "_gce_page_scope")
                else "Add a stabilization base", self.stab_on,
                "A small dense pad printed first, so the jet is steady before "
                "the real part starts. Its fiber diameter, print speed, "
                "interlayer linking loops and viewer color are the main "
                "scaffold's, and it is always a square grid (90 deg "
                "rotation).",
                command=self._toggle_stabilization)

        # the rows that belong to the toggle, in one flush block it greys
        self.stab_frame, G = gui_form.framed(parent, F.row)

        self.stab_width = tk.StringVar(value="15")
        self.stab_height = tk.StringVar(value="15")
        self.stab_stepover = tk.StringVar(value="1.7")
        self.stab_layers = tk.StringVar(value="10")
        self.stab_arc_radius_mult = tk.StringVar(value="")   # blank = 1.5
        self.stab_start_x = tk.StringVar(value="10")
        self.stab_start_y = tk.StringVar(value="10")

        G.entry("Width X", self.stab_width, "The pad's size along X.",
                unit="mm", indent=1)
        G.entry("Length Y", self.stab_height, "The pad's size along Y.",
                unit="mm", indent=1)
        G.entry("Fiber spacing", self.stab_stepover, gui_tips.GLOSSARY["stepover"],
                unit="mm", indent=1)
        G.entry("Layers", self.stab_layers, "How many layers the pad stacks.",
                indent=1)
        G.entry("Turnaround radius", self.stab_arc_radius_mult,
                "Corner-loop radius as a multiple of the fiber spacing. "
                "Blank = 1.5.",
                unit="× spacing", indent=1, placeholder="1.5")
        G.entry("Start X", self.stab_start_x,
                "X of the pad's footprint corner.", unit="mm", indent=1)
        G.entry("Start Y", self.stab_start_y,
                "Y of the pad's footprint corner.", unit="mm", indent=1)
        self._toggle_stabilization()

    def _spelling_vars_make(self):
        """The printer's two spelling settings, made once: travels as G1 and
        GX / G0X. Calibrate's form shows the same two (`gui_sweeps.
        _sw_spelling_vars`), so whichever page builds first makes them."""
        if hasattr(self, "s_travel_as_g1"):
            return
        self.s_travel_as_g1 = tk.BooleanVar(value=True)
        self.s_gcode_convention = tk.StringVar(value="GX")   # "GX" | "G0X"
        # re-render the g-code display when either flips (display/export only; the
        # geometry pipeline ignores both)
        for _tv in (self.s_travel_as_g1, self.s_gcode_convention):
            _tv.trace_add("write",
                          lambda *_a: self._refresh_gcode_display_on_toggle())

    def _g2g3_var_make(self):
        """"Write curves as G2/G3 arcs", made once: whether the printer
        interpolates arcs is one setting, and Calibrate's form shows it
        too (`gui_sweeps._sw_link_g2g3`), so whichever page builds first
        makes it."""
        if not hasattr(self, "s_g2g3"):
            self.s_g2g3 = tk.BooleanVar(value=False)

    def _build_g2g3_row(self, F):
        """"Write curves as G2/G3 arcs": whether the controller interpolates
        arcs. Setup shows it (`gui_buildplate._build_machine_gcode_fields`)
        and the Machine preset carries it; Calibrate's form shows the same
        setting (`gui_sweeps._sw_link_g2g3`)."""
        self._g2g3_var_make()
        g2 = F.check(
            "Write curves as G2/G3 arcs",
            self.s_g2g3,
            "Writes each turnaround and linking loop as one true circular arc "
            "(G2/G3) instead of a fan of straight G1 chords, for printers that "
            "interpolate arcs (Klipper needs [gcode_arcs]).\n\n"
            "The STL is always a faceted preview polyline (the exact arc only "
            "lives on the printer). With G2/G3 on, the STL re-expands every arc "
            "at DOUBLE the \"Arc segments\" density, so an arc-mode preview reads "
            "visibly smoother than the equivalent chord-mode one. \"Arc segments\" "
            "still sets the base fidelity in both modes.\n\n"
            "Plate prints only: a tube is written in G1.",
            command=self._toggle_g2g3)
        # PLATE ONLY: a tube carries no turnaround arcs, and G2/G3 are refused
        # under a rotary frame (scaffold_gcode.py:2966).
        for w in g2.widgets:
            self._collector_register(w, "plate")

    def _wait_temp_var_make(self):
        """"Wait to print until target temperature is reached", made once,
        whichever page builds its row first."""
        if hasattr(self, "s_wait_for_temp"):
            return
        self.s_wait_for_temp = tk.BooleanVar(value=False)
        self.s_wait_for_temp.trace_add(
            "write", lambda *_a: self._refresh_gcode_display_on_toggle())
        self._wait_temp_follow_temperature()

    def _wait_temp_follow_temperature(self):
        """A Temperature edit re-shows the g-code only while the wait line
        is in it. Attached once, as soon as the Physics page's Temperature
        exists, which may be after Setup made the wait box."""
        t_var = getattr(self, "sim_temperature", None)
        if t_var is None or getattr(self, "_wait_temp_traced", False):
            return
        self._wait_temp_traced = True
        t_var.trace_add("write", lambda *_a: (
            self.s_wait_for_temp.get()
            and self._refresh_gcode_display_on_toggle()))

    def _build_printer_output_rows(self, F):
        """The printer's three output rows: travels as G1, the GX / G0X
        spelling and the heat wait. Settings of the printer, not of a
        design: Setup shows them (`gui_buildplate._build_machine_gcode_
        fields`) and the Machine preset carries them; every print, a
        design's or a calibration's, reads them."""
        # Two INDEPENDENT user-facing g-code emission options -- "Travels as G1"
        # and the GX / G0X spelling. Both rewrite ONLY the g-code the user sees
        # and exports; the scratch file the converter, the preview and the player
        # read stays G0/G1/G2, so the deposited geometry and every on-screen view
        # are byte-identical whatever these are set to. They compose, and only
        # the spelling changes: see _rewrite_gcode_lines for the rule.
        self._spelling_vars_make()
        self._wait_temp_var_make()
        F.check(
            "Emit travel (G0) moves as G1", self.s_travel_as_g1,
            "In the g-code MEWracle SAVES and shows in the g-code tab, every travel "
            "move is written as a G1 printing move instead of G0. Travels are the "
            "repositioning hops between passes, parts and layers.\n\n"
            "This changes NOTHING on screen or in the STL: the scaffold, the "
            "preview, the sagged fibers and the simulation are all computed from the "
            "same travels either way. Only the exported text differs. It reflects "
            "that in melt-electrowriting the jet keeps depositing while it travels, "
            "so a travel is really a printing move. On a printer where G0 is an "
            "alias of G1 (sticky feedrate) the motion is identical.\n\n"
            "Untick to keep travels as G0 in the exported g-code.")
        conv_tip = (
            "How the single-digit motion commands are spelled in the exported "
            "g-code:\n\n"
            "GX: bare, as G0, G1 and the G2/G3 turnaround arcs.\n"
            "G0X: zero-padded, as G00, G01, G02, G03. Some controllers "
            "require it.\n\n"
            "They are the SAME commands, so nothing changes on screen, in the STL "
            "or on the printer. Only the exported text differs. This is independent of the "
            "travels-as-G1 box above: with G0X the arcs pad to G02/G03 whether or "
            "not the travels are rewritten.")
        cv = F.group("G-code convention", conv_tip)
        cv.add_radio("GX", self.s_gcode_convention, "GX", conv_tip)
        cv.add_radio("G0X", self.s_gcode_convention, "G0X", conv_tip)

        # A heat-and-wait line at the top of the exported g-code, for a rig
        # that starts printing from a cold or cooling nozzle. Like the two
        # spellings above it is user-facing only: see _gcode_wait_temp_line.
        F.check(
            "Wait to print until target temperature is reached",
            self.s_wait_for_temp,
            "Adds the flavor's heat wait (M109 S<temperature> on Klipper and "
            "Marlin) to the g-code MEWracle saves and shows: where the start "
            "block has [[temperature_command]], else as the first line. The "
            "printer heats the nozzle to that temperature and holds every "
            "move until it gets there. Unticked, no flavor waits.\n\n"
            "The temperature is the Temperature field on the Physics page, read "
            "when you save. Calibration prints wait too; each slide of a "
            "temperature ladder waits at its own temperature.\n\n"
            "Nothing on screen or in the STL changes.")

    def _build_export_tab(self, parent, quality_into=None):
        """Global output settings: the end-of-print exit move, whether to render
        an STL, travel rendering and the multi-scaffold deposition order (plus,
        in the classic UI only, the printer's output rows and the G2/G3 arc
        capability, which are Setup's in the modern one). The g-code/STL are
        written into the scratch session folder automatically, so there are no
        output-path fields -- the user names a copy only via "Save ... as".

        `quality_into` is where the fidelity knobs go. Left None (the classic
        developer UI) they are gridded at the bottom of this same tab under a
        separator, exactly as before; the modern shell passes its own Quality
        card's body and gets them there instead."""
        import gui_form
        parent.columnconfigure(0, weight=0)   # see _build_quality_tab
        F = gui_form.Form(parent)
        self._export_form = F     # the modern shell adds the Z rows below it
        self.s_gcode_out = tk.StringVar()
        self.s_stl_out = tk.StringVar()

        # End-of-print exit move: after the last deposition the polymer keeps
        # flowing, so left over the part it piles onto the finished scaffold.
        self.s_exit_move = tk.BooleanVar(value=True)
        self.s_exit_margin = tk.StringVar(value="5")
        # BOTH collectors: a tube has two ends and an axis to leave along, and
        # the trailing polymer ruins a wall as it ruins a scaffold. Only the
        # WORDS change -- see _collector_text below.
        # The explanation was a grey line under the row whose words followed
        # the collector; it is the rows' tooltip now, and still follows it.
        # `_collector_text` calls configure(text=...), so it is handed an
        # adapter that re-points the tooltips instead of a label.
        exit_tips = (
            "A final travel this far past the nearest plate edge, so "
            "trailing polymer flow misses the scaffold.",
            "A final travel this far past whichever END of the tube the print "
            "finishes on, along the mandrel axis, so trailing polymer flow "
            "misses the wall.")
        exit_chk_row = F.check(
            "Run nozzle off the plate when done", self.s_exit_move,
            exit_tips[0], command=self._toggle_exit_move, inline=True)
        self._collector_text(exit_chk_row.field,
                             plate="Run nozzle off the plate when done",
                             mandrel="Run nozzle off the end of the tube when done")
        # the distance rides the toggle's own line: a child row under it read
        # as the checkbox's label wrapping onto a second line
        self.s_exit_margin_entry = exit_chk_row.add_entry(
            "Exit distance", self.s_exit_margin, exit_tips[0], unit="mm",
            width="small")
        self._collector_text(_RowTips(exit_chk_row),
                             plate=exit_tips[0], mandrel=exit_tips[1])
        self._toggle_exit_move()

        # G2/G3 true-arc emission: a controller capability, so Setup's in the
        # modern UI (beside the printer's other output rows); the classic
        # developer UI, which has no such page, keeps it here
        self._g2g3_var_make()
        if not hasattr(self, "_machine_grid"):
            self._build_g2g3_row(F)

        self.s_make_stl = tk.BooleanVar(value=True)
        F.check("Also render STL", self.s_make_stl,
                "Build the deposited-fiber STL as well as the g-code.")

        # the printer's output rows (travels as G1, GX / G0X, the heat wait)
        # are Setup's in the modern UI, beside Strip comments; the classic
        # developer UI, which has no such page, keeps them here. Their vars
        # exist either way: every print reads them.
        self._spelling_vars_make()
        self._wait_temp_var_make()
        self._wait_temp_follow_temperature()
        if not hasattr(self, "_machine_grid"):
            self._build_printer_output_rows(F)
        # the user's own g-code line (a machine setting; gui_buildplate)
        if hasattr(self, "custom_gcode") and not hasattr(self, "_machine_grid"):
            self._build_custom_gcode_fields(F)

        # Travel (G0) deposition. OFF by default, so an untouched scaffold renders
        # exactly as before. It is a GEOMETRY choice, not a physics one -- the jet
        # lays material while travelling whether or not the physics deposition
        # transform is installed -- so it belongs with the other render knobs.
        # With physics deposition ON the travel is additionally laid at the jet's
        # trailing touchdown and the fiber after it inherits the lag state.
        self.s_travel_in_stl = tk.BooleanVar(value=False)
        self.s_travel_gcode_line = tk.BooleanVar(value=False)
        # Flipping this changes the DEPOSITED geometry, so an already-generated
        # STL/.depo no longer matches the form: invalidate the cached result so
        # "Save STL as" cannot export a stale travel mesh. The g-code itself is
        # untouched (render-travel is a convert-time choice), so last_gcode is
        # kept. Preset loads are exempt -- see the method.
        self.s_travel_in_stl.trace_add(
            "write", lambda *_a: self._invalidate_deposited_result())
        # BOTH collectors: `scaffold_gcode`'s mandrel branch forwards
        # --render-travel / --travel-in-stl, so the box cuts a tube's travel fiber
        # out of the STL as it cuts a plate's. On a tube it cuts the approach from
        # the g-code datum to the print start; the per-layer radial hop is a pure
        # Z move at one (u, v) and deposits nothing visible.
        F.check(
            "Render travel (G0) fiber in the exported STL",
            self.s_travel_in_stl,
            "In melt-electrowriting the jet never stops. It keeps depositing "
            "while the head travels between parts and between layers. That fiber "
            "is always laid and always solved, on a plate and on a mandrel alike: "
            "the entry/exit tails at every part, and the linking fiber laid during "
            "a layer change. You see it in the viewer, whose “Hide travel "
            "fiber” button turns it off there.\n\n"
            "This box decides one thing only: whether that fiber reaches the "
            "EXPORTED STL. Off (the default), the mesh holds the print alone, so a "
            "travel crossing open space does not arrive as a strut in a file you "
            "print, measure or send to someone. Ticked, it is meshed with the rest.\n\n"
            "Either way the travel is real material in the sag solve, and the "
            "scaffold rests on it where the two touch. Unticking this removes the "
            "travel from the mesh, not from the physics. The exported scaffold "
            "is therefore not the same as one generated with no travel fiber at all.\n\n"
            "With Jet lag on (the Physics page), the travel is laid "
            "at the jet's trailing touchdown point. The fiber after it starts "
            "already carrying that lag, in magnitude and direction, instead of "
            "from rest. That is what really happens, and it matters most where "
            "the lag is a large fraction of the feature size.")
        F.check(
            "Render travel (G0) as a G-code line instead of fiber",
            self.s_travel_gcode_line,
            "The simulation always lays the travel as real fiber. Tick this to "
            "draw the commanded G0 moves as plain g-code lines INSTEAD, which is "
            "the quickest way to see where the travels actually run without a tube "
            "drawn along the same path hiding them.\n\n"
            "The viewer opens that way, and its \u201cShow travel fibers\u201d button "
            "brings the fiber back. You can have either, both or neither "
            "without regenerating.\n\n"
            "It is a drawing choice only: it changes nothing about the deposited "
            "fiber, the solved geometry, the STL or the g-code.")

        # Deposition order across scaffolds. Only bites a multi-scaffold stack.
        # "By scaffold number" (default) emits all of scaffold 1 -- every Z-array
        # copy -- then all of scaffold 2, and the viewer rests each scaffold on
        # top of the last (the converter's --layer-by-deposition). "By layer
        # height" stable-sorts every copy by its Z so the scaffolds interleave by
        # height, in the g-code and in the sim viewer alike. Changing it re-orders
        # the emitted g-code and the deposited geometry: invalidate a cached run.
        self.s_deposition_order = tk.StringVar(value="scaffold")  # "scaffold"|"layer"
        self.s_deposition_order.trace_add(
            "write", lambda *_a: self._invalidate_order_result())
        order_tip = (
            "The order the scaffolds in a multi-scaffold stack are printed in (it "
            "has no effect on a single scaffold).\n\n"
            "By scaffold number (the default): all of scaffold 1 is laid down "
            "first, including every Z-array copy, then all of scaffold 2, and so "
            "on. The simulation rests each scaffold on top of the previous one, "
            "matching that print order.\n\n"
            "By layer height across scaffolds: every scaffold copy is sorted by "
            "its height, so the scaffolds interleave. Take two 2-layer scaffolds "
            "each arrayed up in Z, one at 0° and one at 45°, offset half a step "
            "up. They print as 2 layers at 0°, 2 at 45°, 2 at 0°, and so on, a "
            "true sandwich laid in that order. The simulation shows the same "
            "interleaving.\n\n"
            "Pick 'by layer height' when scaffold 2's layers are meant to sit "
            "between scaffold 1's, not entirely on top.")
        # One option per line: side by side, the second one was clipped at the
        # form pane's default width. The second line has no label of its own
        # and starts on the same field edge.
        do = F.group("Deposition order", order_tip)
        do.add_radio("By scaffold number", self.s_deposition_order, "scaffold",
                     order_tip)
        do2 = F.group(None, order_tip)
        do2.add_radio("By layer height across scaffolds",
                      self.s_deposition_order, "layer", order_tip)
        # PLATE ONLY: a tube is one continuous part, and --layer-by-deposition is
        # not forwarded under --mandrel.
        for w in do.widgets + do2.widgets:
            self._collector_register(w, "plate")

        # -- QUALITY ----------------------------------------------------- #
        # Its own module card in the modern shell, still the bottom
        # of the Export tab in the classic developer UI -- which is what
        # `quality_into` is for: one builder, mounted wherever the shell has
        # room for it.
        if quality_into is None:
            F.section("Quality")
            quality_into, _Q = gui_form.framed(parent, F.row)
        self._build_quality_tab(quality_into)

    def _build_quality_tab(self, parent):
        """How much the EXPORT spends on fidelity: how round the tube is, which
        sag solver runs and how hard it droops, how finely the mesh and the
        physics-deposited curves are sampled.

        Separate from _build_export_tab because Export is about WHAT files
        come out and what is in them; this is about how good they are, which is
        the question a user asks at a different moment. Nothing moved between
        variables -- every `self.s_*` here is the one that was here before, so no
        preset key changes."""
        import gui_form
        # a card body arrives with column 0 weighted (ModuleGrid.add), and
        # column 0 is the Form's label column: unweighted, or the fields
        # drift ~100 px right of every other card's
        parent.columnconfigure(0, weight=0)
        F = gui_form.Form(parent)

        self.s_faces = tk.StringVar(value="12")
        self.s_sag_mult = tk.StringVar(value="1.0")

        F.entry("Tube facets", self.s_faces,
                "How many flat sides approximate the round fiber in the STL. "
                "More gives a smoother surface and a larger file. Each scaffold sags and "
                "renders at its own fiber diameter.")
        # NO "Sag multiplier" box. The var stays (every saved preset carries
        # `s.sag_mult`, and `--sag-multiplier` is still a worker argument) but it
        # is pinned at 1 and no longer editable: it scaled `sag_coeff`, a
        # calibrated per-rig coefficient that lives on the Physics page's Fiber
        # sagging card, from a page the user was not looking at. Two knobs on one
        # number, and the one shown beside the law was not the one the export
        # used. Scale the droop by editing c on that card instead.
        # Sag solver. Combo holds the LABEL (the RENDER_CHOICES idiom);
        # _sag_solver_value maps it back for argv and validation.
        # Approximate by default -- see the note on `sag_mode` in gui_core.py.
        self.s_sag_mode = tk.StringVar(value=SAG_SOLVER_VAL2LBL["approximate"])
        self.s_sag_mode_combo = F.combo(
            "Sag solver", self.s_sag_mode,
            [lbl for lbl, _ in SAG_SOLVER_CHOICES],
            "Which solver computes the deposited fibers' sag for the EXPORTED "
            "files (the STL, and the geometry the simulation rides).\n\n"
            "Exact: the full contact solve. Every fiber is tested against the "
            "structure below it at half-diameter spacing. This is the reference "
            "the other two are measured against. Use it for a final export.\n\n"
            "Approximate (fast): the same physics and the same sag law, with "
            "contacts found analytically between fiber pairs instead of by "
            "sampling. Roughly 5x faster, and its fibers land within a fraction "
            "of a fiber diameter of Exact. Good for iterating.\n\n"
            "Two things it cannot do, both because it sags at ONE diameter: a "
            "multi-scaffold print whose parts differ in fiber diameter is "
            "sagged at the finest one, and with the physics diameter varying "
            "along the fiber (Physics page) the stack height departs from Exact "
            "by about a quarter of a fiber diameter. The log says so when that "
            "combination runs.\n\n"
            "Off: lay every fiber straight, as commanded. Fastest, and not "
            "physical.\n\n"
            "This is the EXPORT solver. The live preview on the Scaffold page has "
            "its own, much faster approximation on its own toggle. Nothing here "
            "changes it.").field
        self.s_sag_mode_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._toggle_scaffold_sag())

        # Periodic sag reset. Global, next to the other sag knobs, because the
        # solve is: layers are Z-clusters across every stacked scaffold, so
        # "every N layers" cannot be resolved per-scaffold (apply_sag/_layer_ids).
        # OFF by default so an untouched scaffold renders as it did before the
        # feature; the entry still prefills the typical 20.
        self.s_sag_reset = tk.BooleanVar(value=False)
        self.s_sag_reset_layers = tk.StringVar(value="20")
        reset_tip = (
            "Real prints show the deposited fibers lying straight again after a "
            "number of layers, with sagging then starting over. Every N layers the "
            "model forgets the structure below, lays that layer flat on top of "
            "what is already there, and lets sag build up again. A tall "
            "scaffold then shows repeating bands rather than one uniform sag.\n\n"
            "The mechanism is not known, so this is a period you set, not "
            "something the model derives.\n\n"
            "Sag is not endless. It deepens over roughly the first 5–8 layers "
            "and then stays at full depth, so a period shorter than that never "
            "lets it get there. Scaffold height is unaffected either way: a reset "
            "layer rests on the peaks below, exactly where it would have sat.\n\n"
            "Off by default. Left unticked, sagging accumulates and stays at "
            "full depth. 20 layers is a typical period to start from.")
        self.s_sag_reset_check = F.check(
            "Reset sagging periodically", self.s_sag_reset, reset_tip,
            command=self._toggle_scaffold_sag).field
        self.s_sag_reset_entry = F.entry(
            "Reset sagging every", self.s_sag_reset_layers, reset_tip,
            unit="layers",
            width="small", indent=1).field

        # Sag resolution. With the sag knobs, not with Mesh detail: it changes the
        # SOLVE's sampling and so the .depo cache key, which Mesh detail never does.
        # Holds the LABEL, like Mesh detail; `_sag_solver_args` maps it.
        self.s_sag_resolution = tk.StringVar(value=SAG_RESOLUTION_VAL2LBL["1"])
        self.s_sag_resolution_combo = F.combo(
            "Sag resolution", self.s_sag_resolution,
            [lbl for lbl, _ in SAG_RESOLUTION_CHOICES],
            SAG_RESOLUTION_TIP).field

        # Mesh detail. Sits with Tube facets rather than with the sag knobs: both
        # decide how many triangles the STL spends, and neither touches the solve.
        self.s_mesh_tol = tk.StringVar(value=MESH_DETAIL_VAL2LBL["1.0"])
        self.s_mesh_tol_combo = F.combo(
            "Mesh detail", self.s_mesh_tol,
            [lbl for lbl, _ in MESH_DETAIL_CHOICES],
            "How finely the deposited centerline is sampled into tube rings for "
            "the STL. It changes the FILE, never the solve. The simulation and "
            "the deviation view read the full-resolution "
            "geometry either way.\n\n"
            "The sag solver samples each sagging span for its own contact "
            "search, which is finer than a faceted tube can express. Standard "
            "drops the samples a tube of this many facets cannot represent "
            "anyway, and halves the file: a 30x30 x 10-layer export goes from "
            "824 MB to 409 MB and opens in the viewer in 1.7 s instead of 3.9 s.\n\n"
            "Standard: the default. Every dropped point stays within the tube's "
            "own faceting error, and rings are kept through turnaround loops "
            "where a tube on a tight bend moves further than its centerline.\n\n"
            "Smaller file: about a third smaller again. Measurably less "
            "faithful, and indistinguishable on screen. Good for viewing.\n\n"
            "Full: every sample gets a ring. Use it if you need the file to "
            "match an older export byte for byte.").field

        # -- Curve quality: the ONE dial for how round a drawn curve is ----- #
        # Above Corner resolution on purpose: that one is spelled in relaxation
        # times, while this asks the same question in the unit the rest of the
        # pipeline answers it in (fiber diameters of allowed error). Corner
        # resolution stays for reproducing an old export.
        self.s_curve_quality = tk.StringVar(value="standard")
        self._s_curve_quality_label = tk.StringVar(
            value=CURVE_QUALITY_VAL2LBL["standard"])
        cq_combo = F.combo(
            "Curve quality", self._s_curve_quality_label,
            [CURVE_QUALITY_VAL2LBL[v] for v in CURVE_QUALITY_ORDER],
            _CURVE_QUALITY_TIP).field
        cq_combo.bind("<<ComboboxSelected>>", self._curve_quality_from_label)

        # Corner resolution -- the physics deposition's refine grid
        # (mew_deposition `fill_per_tau`). Greyed unless "Physics-deposited
        # scaffold" is on, because without the transform there is no transient
        # window to refine.
        self.s_fill_per_tau = tk.StringVar(value="6")
        self.s_fill_per_tau_entry = F.entry(
            "Corner resolution", self.s_fill_per_tau,
            "How finely the physics-deposited fiber is sampled through a "
            "turnaround or a fiber start. Those are the places where the jet is "
            "still catching up with the nozzle and the laid path bends.\n\n"
            "The straights need almost no samples, so this only shows at the "
            "crest of a turnaround, which is the tightest bend on the whole "
            "path. Too few and the crest is drawn as a few long chords, which "
            "reads as a blocky or faceted corner.\n\n"
            "6: the default, and what earlier exports used.\n"
            "12: halves the worst corner angle. Doubles the points and the "
            "deposition time.\n"
            "24: halves it again. Slow enough on a large scaffold that the "
            "live preview will lag.\n\n"
            "Measured on a 10×10 scaffold with the turnaround radius at 0, "
            "printing at 500 mm/min with a 3.5 mm nozzle–collector gap, the sharpest laid "
            "turn is 38° at 6, 23° at 12 and 12° at 24. Raise it if turnarounds "
            "look faceted; leave it at 6 otherwise.\n\n"
            "Only applies with Jet lag on (the Physics page). It refines the "
            "jet's catch-up, and there is none without it. The same value drives "
            "the export, the simulation and the live preview, so they cannot "
            "disagree.\n\nThe value is samples per lag time.",
            unit="per lag time", width="small").field

        # Auto corner resolution -- turns the adaptive corner grid (the sampler
        # Curve quality tunes, `mew_deposition.auto_corner_grid`) on for a FLAT
        # scaffold. The tube already runs it
        # unconditionally. On by default; unticked, a plate keeps the fixed grid
        # and its export is byte-identical to the build before this box. Greyed
        # with Corner resolution: both refine the jet-lag relay, and there is
        # none without it.
        self.s_auto_corner_grid = tk.BooleanVar(value=True)
        self.s_auto_corner_grid_check = F.check(
            "Auto corner resolution", self.s_auto_corner_grid,
            "Adds samples wherever the drawn fiber would cut a corner, and "
            "nowhere else. A curved wave (a sine, say) is a "
            "corner at almost every point, and at a Corner resolution of 6 "
            "its crests are drawn as long straight chords. Straight passes "
            "barely bend, so they gain few points.\n\n"
            "How close the drawn fiber is held to the true curve is the "
            "Curve quality setting above.\n\n"
            "Measured on a 0.3 mm sine pattern: the crest sat about one "
            "fiber diameter off the curve at Corner resolution 6. With this "
            "on, it sat a fifth of a diameter off, with fewer points than "
            "setting Corner resolution to 20.\n\n"
            "The live preview always refines at Draft, to stay fast. The "
            "export uses the Curve quality setting.\n\n"
            "Corner resolution still sets the starting grid this refines. "
            "Plate scaffolds only. A mandrel always samples "
            "this way. Only applies with Jet lag on (the Physics "
            "page).").field

    def _curve_quality_from_label(self, _e=None):
        """Combobox label -> the engine token in `s_curve_quality`.

        The var is what is persisted and what reaches a worker, so it must never
        hold a display string: a preset saved today has to load after the wording
        changes."""
        self.s_curve_quality.set(
            CURVE_QUALITY_LBL2VAL.get(self._s_curve_quality_label.get(),
                                      "standard"))

    def _sync_curve_quality_label(self):
        """...and back, for a preset load or a persist restore, which write the
        TOKEN. Without this the combobox keeps showing whatever it showed."""
        label = getattr(self, "_s_curve_quality_label", None)
        if label is not None:
            label.set(CURVE_QUALITY_VAL2LBL.get(
                self.s_curve_quality.get().strip().lower(),
                CURVE_QUALITY_VAL2LBL["standard"]))
        if hasattr(self, "pv_curve_quality"):
            self._sync_pv_curve_quality_label()

    def _pv_curve_quality_from_label(self, _e=None):
        """The preview's Curve-quality combobox label -> its engine token."""
        self.pv_curve_quality.set(
            CURVE_QUALITY_LBL2VAL.get(self._pv_curve_quality_label.get(),
                                      "draft"))

    def _sync_pv_curve_quality_label(self):
        """...and back, after a preset load writes the token (as the export's
        `_sync_curve_quality_label`)."""
        label = getattr(self, "_pv_curve_quality_label", None)
        if label is not None:
            label.set(CURVE_QUALITY_VAL2LBL.get(
                self.pv_curve_quality.get().strip().lower(),
                CURVE_QUALITY_VAL2LBL["draft"]))

    # -- g-code emission rewrite (user-facing g-code only) ----------------- #
    # The text passes live in `mew_gcode` (no Tk), which the G-code page's
    # document renders through; these names stay for every existing caller.
    # The methods below read the page's vars and hand the engine one
    # `mew_gcode.OutputOptions`.
    _split_z_word = staticmethod(mew_gcode.split_z_word)
    _is_five_axis_gcode = staticmethod(mew_gcode.is_five_axis_gcode)
    _rewrite_gcode_lines = staticmethod(mew_gcode.rewrite_gcode_lines)

    def _gcode_emit_opts(self):
        """(travel_as_g1, padded) for the USER-FACING g-code rewrite, but ONLY when
        the g-code in hand is the scaffold generator's own output. An imported file
        is never rewritten, so (False, False) -- its commands survive verbatim.
        A calibration slide is spelled by the Calibration page's own Export
        card (gui_sweeps `_sw_emit_opts`)."""
        if getattr(self, "_last_gcode_is_calibration", False):
            return self._sw_emit_opts()[:2]
        if not getattr(self, "_last_gcode_is_scaffold", False):
            return (False, False)
        g1 = bool(getattr(self, "s_travel_as_g1", None)
                  and self.s_travel_as_g1.get())
        conv = getattr(self, "s_gcode_convention", None)
        padded = bool(conv is not None and conv.get() == "G0X")
        return (g1, padded)

    def _flat_z_boundaries(self):
        """``(ordinal, height)`` for every scaffold's first layer AFTER the
        first -- the steps "Don't adjust height per layer" still names.

        THE ORDINAL COUNTS Z-BEARING MOTION LINES, which both emitters make
        exactly one of per layer, plus one for the plate lead-in
        (`generate_multi_scaffold_gcode`'s Z-only `G0` when the print starts
        above zero). So scaffold k's first one sits at

            lead-in? + stabilization layers + every earlier scaffold's layers

        and that is what this counts. A height alone could not do it: a
        scaffold's own ladder climbs THROUGH a later scaffold's start Z whenever
        the parts are stacked, which is the common case.

        Read live (like the other emission options) so the box can be flipped
        without regenerating. `_expanded_specs_and_sources` is the same call that
        built the run's `--scaffolds-json`, so the order here IS the order the
        worker emitted, including the by-layer-height deposition sort.

        The stabilization base is not a boundary: it inherits scaffold 1's start
        Z, so the program's first Z word already covers it -- but its layers
        still occupy ordinals, so they are counted. A TUBE is one part with one
        datum and has no boundaries at all."""
        if getattr(self, "s_collector", None) is not None \
                and self.s_collector.get() == "mandrel":
            return []
        try:
            specs, _sources = self._expanded_specs_and_sources()
            if len(specs) < 2:
                return []
            plate_top = self._plate_top_z()
            base = plate_top + self._origin_offset()[2]
            # the lead-in fires on the PRE-rebase height, exactly as the emitter
            # tests it (`first_z > 0.0`, before `translate_gcode` runs)
            n = 1 if float(specs[0].get("start_z", 0.0)) + plate_top > 0.0 else 0
            if getattr(self, "stab_on", None) is not None and self.stab_on.get():
                n += max(1, int(float(self.stab_layers.get())))
            out = []
            for sp in specs[:-1]:
                n += max(1, int(float(sp.get("layers", 1))))
                out.append((n, 0.0))        # ordinal now, height below
            return [(o, float(sp.get("start_z", 0.0)) + base)
                    for (o, _z), sp in zip(out, specs[1:])]
        except Exception:  # noqa: BLE001 -- an unparseable field is not a reason
            return []      # to refuse the export; fall back to one Z word

    def _gcode_z_opts(self):
        """(drop_layer_z, boundary_zs) for the USER-FACING rewrite. Gated on the
        same `_last_gcode_is_scaffold` the other options are: an imported file's
        own height ladder is its author's, not ours.

        A five-axis FILE is declined by `_rewrite_gcode_lines` itself."""
        if getattr(self, "_last_gcode_is_calibration", False):
            return (self._sw_emit_opts()[2], ())    # one part, one Z word
        if not getattr(self, "_last_gcode_is_scaffold", False):
            return (False, ())
        v = getattr(self, "no_layer_z", None)
        if not (v is not None and v.get()):
            return (False, ())
        per_scaffold = getattr(self, "no_scaffold_z", None)
        if per_scaffold is not None and per_scaffold.get():
            return (True, ())               # one Z word for the whole program
        return (True, tuple(self._flat_z_boundaries()))

    def _gcode_wait_temp_line(self):
        """``"M109 S<T>\\n"`` for the top of the USER-FACING g-code while "Wait to
        print until target temperature is reached" is ticked, else "". T is the
        Physics page's Temperature (`sim_temperature`, degC), read live.

        Gated like the other emission options: scaffold output only, so an
        imported file's own heat-up is never doubled, and a calibration slide
        is the Calibration page's to spell. An unparseable or non-positive
        temperature writes no line rather than a guessed one; the g-code tab
        then shows the file without it."""
        return mew_gcode.wait_temp_line(self._gcode_wait_temp())

    def _gcode_wait_temp(self):
        """The heat-and-wait temperature as the page holds it (a string,
        validated by `mew_gcode.wait_temp_line`), or None for no line."""
        if getattr(self, "_last_gcode_is_calibration", False) \
                or not getattr(self, "_last_gcode_is_scaffold", False):
            return None
        v = getattr(self, "s_wait_for_temp", None)
        t_var = getattr(self, "sim_temperature", None)
        if v is None or not v.get() or t_var is None:
            return None
        return t_var.get()

    def _custom_gcode_spec(self):
        """``(line, after)`` for the Export card's custom g-code line, or None
        when there is nothing to insert: the line is empty, or "After line" is
        not a whole number >= 0 (no line rather than a guessed position). A
        machine setting, so it is read the same for every page."""
        v = getattr(self, "custom_gcode", None)
        a = getattr(self, "custom_gcode_after", None)
        if v is None or a is None:
            return None
        line = v.get().strip()
        if not line:
            return None
        try:
            after = int(str(a.get()).strip())
        except (TypeError, ValueError):
            return None
        if after < 0:
            return None
        return line, after

    def _insert_custom_gcode(self, text):
        """`text` with the custom g-code line inserted after its line `after`
        (0 = the top; past the end = the last line). Run LAST on user-facing
        g-code, so the count is lines of the file the user saves and sees,
        M109 header included."""
        spec = self._custom_gcode_spec()
        if spec is None:
            return text
        return mew_gcode.insert_line_after(text, *spec)

    def _gcode_custom_applies(self):
        """The custom line goes into MEWracle's own g-code (a scaffold or a
        calibration slide), never into an imported file."""
        return (getattr(self, "_last_gcode_is_calibration", False)
                or getattr(self, "_last_gcode_is_scaffold", False))

    def _gcode_output_options(self):
        """Every output option, read live off the page's vars, as the ONE
        value the engine takes (`mew_gcode.OutputOptions`). Each part keeps
        its own gate above: an imported file gets all-off, a calibration
        slide gets the Calibration page's options."""
        if getattr(self, "_last_gcode_is_calibration", False) \
                and hasattr(self, "_sw_output_options"):
            # the very options Calibrate's Save and page text are written by
            return self._sw_output_options(filename=getattr(
                self, "_gcode_export_filename", None))
        g1, padded = self._gcode_emit_opts()
        drop_z, boundaries = self._gcode_z_opts()
        custom = (self._custom_gcode_spec() if self._gcode_custom_applies()
                  else None)
        inc, origin, rot = (False, (), "")
        if self._gcode_is_designer_output() and                 hasattr(self, "_origin_output_fields"):
            inc, origin, rot = self._origin_output_fields()
        machine_blocks = (hasattr(self, "_machine_grid")
                          and self._gcode_is_designer_output())
        wait_temp = self._gcode_wait_temp()
        start, end = (self._machine_blocks() if machine_blocks else ("", ""))
        if machine_blocks and mew_gcode.start_block_waits(start):
            wait_temp = None  # the start block places the wait itself
        return mew_gcode.OutputOptions(
            travel_as_g1=g1, padded=padded, drop_layer_z=drop_z,
            boundaries=tuple(boundaries), wait_temp=wait_temp,
            custom_line=custom, incremental=inc, origin=origin,
            rotary_axes=rot,
            start_block=start, end_block=end,
            context=(self._machine_gcode_context()
                     if machine_blocks else {}),
            rig_recipes=(self._machine_recipes()
                         if machine_blocks else []),
            layer_markers=bool(machine_blocks
                               and self.machine_layer_markers.get()))

    def _gcode_is_designer_output(self):
        """The g-code in hand is a generator's (a scaffold or a calibration
        slide), so the Machine page's spelling applies: an imported file
        is written as it came."""
        return bool(getattr(self, "_last_gcode_is_scaffold", False)
                    or getattr(self, "_last_gcode_is_calibration", False))

    def _gcode_export_flavor(self):
        """The flavor the user-facing g-code is spelled in: the Machine
        page's for designer output, G-code's own (Klipper) otherwise."""
        if self._gcode_is_designer_output() and                 hasattr(self, "_machine_flavor_name"):
            return self._machine_flavor_name()
        return "klipper"

    def _gcode_export_active(self):
        """True when the user-facing g-code would differ from the scratch file, so
        the export must filter rather than byte-copy."""
        return self._gcode_output_options().active(
            self._gcode_export_flavor())

    def _gcode_for_display_export(self, text):
        """Apply the emission rewrite to user-facing g-code (the embedded viewer
        text and Save-g-code-as); `mew_gcode.apply_output_options` is the
        whole rule, and with every option off it returns `text` unchanged."""
        text = mew_gcode.apply_output_options(
            text, self._gcode_output_options(), self._gcode_export_flavor())
        strip = getattr(self, "machine_strip_comments", None)
        if strip is not None and strip.get():
            text = mew_gcode.strip_export_comments(text, self._gcode_export_flavor())
        return text

    def _new_scaffold_vars(self, idx):
        """A fresh field-var dict for scaffold `idx` (0-based; 1.. here), with
        the same build-time defaults as scaffold 1 plus its rainbow colour. The
        rotation widget is added later by _build_scaffold_fields. Scaffold 1
        (idx 0) instead defaults to SINGLE_SCAFFOLD_COLOR -- see
        _apply_scaffold1_default_color."""
        return {
            "width": tk.StringVar(value="30"),
            "height": tk.StringVar(value="30"),
            "stepover": tk.StringVar(value="0.5"),
            "diameter": tk.StringVar(value="0.02"),
            "layers": tk.StringVar(value="10"),
            "speed": tk.StringVar(value="600"),
            "loop_speed": tk.StringVar(value=""),
            "start_x": tk.StringVar(value="40"),
            "start_y": tk.StringVar(value="10"),
            "start_z": tk.StringVar(value="0"),
            "start_angle": tk.StringVar(value="0"),
            "footprint_angle": tk.StringVar(value="0"),
            "arc_radius_mult": tk.StringVar(value="1.5"),
            "arc_segments": tk.StringVar(value="8"),
            "color": tk.StringVar(value=(SINGLE_SCAFFOLD_COLOR if idx == 0
                     else SCAFFOLD_COLORS[idx % len(SCAFFOLD_COLORS)])),
            "angle_pattern_on": tk.BooleanVar(value=False),
            "angle_pattern": tk.StringVar(value=""),
            "angle_stepover_pattern": tk.StringVar(value=""),
            "angle_offset_pattern": tk.StringVar(value=""),
            # per-layer parameters (the G-code page's Layer parameters table):
            # JSON {"<layer, 1-based>": {key: value}}, blank = none; read by
            # scaffold_gcode.parse_layer_overrides
            "layer_overrides": tk.StringVar(value=""),
            # an in-layer pore grade: the spacing at the far edge (mm, blank =
            # even spacing) and the direction it grades along (deg)
            "stepover_end": tk.StringVar(value=""),
            "grade_angle": tk.StringVar(value="0"),
            # a layer by layer grade: the top layer's spacing (mm, blank =
            # none); it writes each layer's stepover override
            # (gui_gcode_params.sync_layer_grade)
            "stepover_top": tk.StringVar(value=""),
            "linear_on": tk.BooleanVar(value=False),
            "linear_wavelength": tk.StringVar(value="1"),
            "linear_spacing": tk.StringVar(value="0"),
            "linear_pattern": tk.StringVar(value=""),
            "linear_periods": tk.StringVar(value="1"),
            "linear_grid": tk.StringVar(value="0.1"),
            "linear_editmode": tk.StringVar(value="add"),
            "shape": tk.StringVar(value="rectangle"),
            "shape_sides": tk.StringVar(value="64"),
            "shape_polygon": tk.StringVar(value=""),
            "shape_grid": tk.StringVar(value="1"),
            "shape_editmode": tk.StringVar(value="add"),
            "array_on": tk.BooleanVar(value=False),
            "array_nx": tk.StringVar(value="2"),
            "array_ny": tk.StringVar(value="2"),
            "array_nz": tk.StringVar(value="1"),
            "array_dx": tk.StringVar(value="35"),
            "array_dy": tk.StringVar(value="35"),
            "array_dz": tk.StringVar(value="0.2"),
            "array_flip_x": tk.BooleanVar(value=False),
            "array_flip_y": tk.BooleanVar(value=False),
            "array_travel": tk.StringVar(value="2"),
            "link_loops_on": tk.BooleanVar(value=True),   # ON by default
            "link_loops_mode": tk.StringVar(value="auto"),
            "link_loop_angle": tk.StringVar(value="25"),
            "link_loop_dist": tk.StringVar(value="0.5"),
            # travel-arrival loops: the hop between two layers that do not share
            # a free end is deposited fiber too.
            "link_loop_travel": tk.BooleanVar(value=False),
            "perimeter_travel": tk.BooleanVar(value=False),
            "perimeter_travel_offset": tk.StringVar(value="1"),
        }

    def _build_scaffold_fields(self, parent, v):
        """One scaffold index's panel: an inner Core / Array notebook. Core holds
        the geometry; Array holds this scaffold's own rectangular-array settings."""
        inner = ttk.Notebook(parent)
        inner.pack(fill="both", expand=True)
        core = ttk.Frame(inner, padding=8)
        shp = ttk.Frame(inner, padding=8)
        arr = ttk.Frame(inner, padding=8)
        lin = ttk.Frame(inner, padding=8)
        inner.add(core, text="  Core  ")
        inner.add(shp, text="  Shape  ")
        inner.add(arr, text="  Array  ")
        inner.add(lin, text="  Wave  ")
        self._build_scaffold_core_fields(core, v)
        self._build_scaffold_shape_fields(shp, v)
        self._build_scaffold_array_fields(arr, v)
        self._build_scaffold_linear_fields(lin, v)

    def _build_scaffold_core_fields(self, parent, v):
        """CLASSIC Core tab for one scaffold: the geometry grid, the linking-loops
        LabelFrame beneath it and the per-layer angle-pattern LabelFrame in its own
        right-hand column. The modern shell composes the same three body builders
        into its sectioned layout instead."""
        F = self._build_scaffold_geometry_fields(parent, v)

        # linking loops: a lead-in/out loop at coincident square corners
        self._build_scaffold_link_loop_fields(parent, v, row=F.row)
        self._build_scaffold_perimeter_fields(parent, v, row=F.row + 1)

        # --- multiple angle rotations (per-layer pattern) ------------------- #
        # Its own right-hand column, with the table in a fixed-height scroll area,
        # so adding layers grows the table internally instead of pushing the Core
        # tab and the global settings notebook below it off-screen.
        apf = ttk.LabelFrame(parent, text="Layer angles", padding=6)
        apf.grid(row=0, column=4, rowspan=F.row + 1, sticky="n", padx=(16, 0),
                 pady=2)
        self._build_scaffold_angle_pattern_body(apf, v)

    def _build_scaffold_geometry_fields(self, parent, v, F=None):
        """Lay one scaffold's geometry rows into `parent` through a gui_form
        Form (`F`, or a new one on `parent`), bound to the var dict `v`.
        Creates the rotation combobox and stores it under v['rotation'].
        Returns the Form, so a caller can keep adding rows to the same grid."""
        import gui_form
        if F is None:
            F = gui_form.Form(parent)

        def field(label, var, tip, unit=None):
            return F.entry(label, var, tip, unit=unit).field

        rot_tip = ("Layer rotation turns each layer by this much from the one "
                   "below (90 = a square grid). Pick a preset or type any "
                   "angle in degrees. Start angle sets the first layer's "
                   "direction; Footprint angle rotates the whole outline on "
                   "the plate.")

        # -- size (width/height/diameter entries are kept so the Shape and the
        #    physics-deposit toggles can grey them out) --------------------- #
        v["width_entry"] = field("Width X", v["width"],
                                 "The footprint's size along X.", "mm")
        v["height_entry"] = field("Length Y", v["height"],
                                  "The footprint's size along Y.", "mm")
        field("Fiber spacing", v["stepover"], gui_tips.GLOSSARY["stepover"], "mm")
        v["diameter_entry"] = field(
            "Fiber diameter", v["diameter"],
            "The deposited fiber's diameter. It also sets the layer height: "
            "each layer sits one fiber diameter above the last.", "mm")
        # the "From Physics" jump (gui_sim); absent on a stub or the classic UI
        link = getattr(self, "_diam_link", None)
        if link is not None:
            link(v["diameter_entry"])
        field("Layers", v["layers"], "How many layers the scaffold stacks.")
        if gui_units.enabled():
            import gui_speed_ratio
            gui_speed_ratio.form_rows(self, F, v)
        else:
            field("Print speed", v["speed"],
                  "The feed rate the fiber is laid at.", "mm/min")

        # -- start position + rotation cluster ---------------------------- #
        field("Start X", v["start_x"],
              "X of the footprint corner the print starts from.", "mm")
        field("Start Y", v["start_y"],
              "Y of the footprint corner the print starts from.", "mm")
        field("Start Z", v["start_z"],
              "Z 0 sits on the plate top. Raise Z to print higher.", "mm")
        combo = F.combo("Layer rotation", None, ROTATION_PRESETS,
                        gui_tips.pic("layer_rotation", rot_tip),
                        unit="deg", readonly=False).field
        combo.set(ROTATION_PRESETS[0])
        v["rotation"] = combo
        v["start_angle_entry"] = field("Start angle", v["start_angle"],
                                       rot_tip, "deg")
        v["footprint_angle_entry"] = field("Footprint angle",
                                           v["footprint_angle"], rot_tip,
                                           "deg")

        # -- corner-loop / render knobs ----------------------------------- #
        field("Turnaround radius", v["arc_radius_mult"], gui_tips.pic(
              "arc_radius",
              "Corner-loop radius as a multiple of the fiber spacing. 1.5 "
              "= default; 0 = sharp, straight turns."), "× spacing")
        v["arc_segments_entry"] = field(
            "Arc segments", v["arc_segments"],
            "How many straight segments approximate each corner loop in the "
            "STL preview.")
        field("Loop speed", v["loop_speed"],
              "Feed rate for the corner linking loops. Leave blank to use the "
              "print speed.", "mm/min")
        # The modern shell has its OWN colour control -- swatch, hex entry and
        # picker -- under the Geometry module's three collector grids
        # (`gui_pages._build_scaffold_color_row`), so this plain hex field is
        # classic-only. Both edit the same `v["color"]`.
        #
        # A guard pointing at the wrong home would leave the modern shell with
        # no colour control at all, and nothing could see it: the var would
        # still persist and still reach the workers, so every persist-key guard
        # would stay green.
        if getattr(self, "_build_scaffold_preview", None) is None:
            field("Viewer color", v["color"],
                  "STL-viewer color for this scaffold, such as #ff0000. "
                  "Viewer only: it never reaches the STL.", "hex")
        return F

    def _build_scaffold_angle_pattern_body(self, apf, v):
        """The per-layer angle-pattern controls, laid into `apf` (the classic
        LabelFrame or the modern disclosure body). When ticked, a table of
        per-layer fiber angles appears and OVERRIDES the single "Layer rotation" +
        "Start angle" (which grey out). The table edits the scaffold's
        `angle_pattern` string, the source of truth that persists and is sent to
        the worker; it cycles when there are more layers than listed angles."""
        import gui_form
        F = gui_form.Form(apf)
        F.check("Give each layer its own angle", v["angle_pattern_on"],
                "Give each layer its own absolute fiber angle instead of one "
                "repeating rotation. The list cycles when there are more "
                "layers than angles, and overrides Layer rotation and Start "
                "angle.",
                command=lambda vv=v: self._toggle_angle_pattern(vv))

        # The whole Scaffold tab scrolls (gui_common.VScrollFrame), so the table
        # needs no inner scroll area: angle_scroll is the holder the toggle
        # shows/hides and angle_table is where the rows are rendered. It is an
        # EDITOR (rows added and removed at run time), placed as one slot on
        # the form's grid and exempt inside.
        table = gui_form.exempt(
            ttk.Frame(apf), "per-layer angle table: an editor whose rows are "
                            "added and removed at run time")
        F.slot(None, table, sticky="nw", indent=1)
        v["angle_scroll"] = table
        v["angle_table"] = table
        self._toggle_angle_pattern(v)   # build/hide the table per the checkbox

    def _build_scaffold_array_fields(self, parent, v):
        """This scaffold's rectangular array: a checkbox over its parameter
        fields. When ticked, the scaffold is tiled into a Columns x Rows grid
        (start_x/start_y offset by the spacing) -- for whichever scaffold tab is
        active when you Generate, and "Number of scaffolds" is ignored then.
        Unticking greys the fields rather than hiding them. The flip buttons
        reverse the array direction on that axis."""
        import gui_form
        F = gui_form.Form(parent)
        F.check("Tile this scaffold into a grid",
                v["array_on"],
                "Tile this scaffold into a Columns x Rows x Layers grid. Each Z "
                "layer is a full copy stacked Z spacing higher. Set that to "
                "the scaffold's own height to stack directly on top. It applies "
                "to whichever scaffold is being edited when you Generate, and "
                "Number of scaffolds is ignored then.",
                command=lambda vv=v: self._array_ticked(vv))

        # the rows that belong to the toggle: their own flush block, so the
        # toggle can grey the whole of it (`_toggle_array`)
        fields, G = gui_form.framed(parent, F.row)
        v["array_fields"] = fields
        G.entry("Columns (X)", v["array_nx"], "Copies along X.", indent=1)
        G.entry("Rows (Y)", v["array_ny"], "Copies along Y.", indent=1)
        G.entry("Layers (Z)", v["array_nz"],
                "Copies stacked in Z, each a full scaffold.", indent=1)
        flip = "The button beside it reverses the array's direction on this axis."
        rx = G.entry("X spacing", v["array_dx"],
                     "Distance between neighboring copies along X. " + flip,
                     unit="mm", indent=1)
        v["flip_x_btn"] = rx.add_button(
            "", lambda vv=v: self._flip_axis(vv, "X"), flip, width=4)
        ry = G.entry("Y spacing", v["array_dy"],
                     "Distance between neighboring copies along Y. " + flip,
                     unit="mm", indent=1)
        v["flip_y_btn"] = ry.add_button(
            "", lambda vv=v: self._flip_axis(vv, "Y"), flip, width=4)
        G.entry("Z spacing", v["array_dz"],
                "Height between stacked copies. Set it to the scaffold's own "
                "height to stack directly on top.", unit="mm", indent=1)
        G.entry("Travel offset", v["array_travel"],
                "Run the nozzle this far clear of each scaffold before "
                "traveling to the next, so the continuous flow doesn't drag "
                "across it.", unit="mm", indent=1)
        self._refresh_flip_buttons(v)
        self._toggle_array(v)

    # ===================== footprint shape (Shape tab) ===================== #
    _SHAPE_TIP = ("The plan-view outline the scaffold fills. It must be "
                  "convex.")
    _SHAPE_HINTS = {
        "rectangle": "A Width X x Length Y rectangle (the original footprint).",
        "circle": "A circle of diameter = Width X (Length Y tracks it). "
                  "Sides sets the outline smoothness.",
        "ellipse": "An ellipse with axes Width X x Length Y. Sides sets the "
                   "outline smoothness.",
        "polygon": "A regular polygon with 'Sides' sides, diameter = Width X "
                   "(Length Y tracks it).",
        "custom": "Draw a CONVEX outline below: click to add a point, drag to "
                  "move one, switch to Remove to delete. Width X / Length Y "
                  "follow the drawing's bounding box. Concave outlines are not "
                  "supported yet.",
    }

    def _build_scaffold_shape_fields(self, parent, v):
        """This scaffold's FOOTPRINT SHAPE tab: a shape-type dropdown, a Sides
        field (circle/ellipse smoothness or regular-polygon side count) and, for a
        custom outline, a clickable draw graph plus a numeric vertex table.

        The footprint is the outline the serpentine fills, and a non-rectangle one
        must be CONVEX. Width X / Length Y (Core tab) stay the bounding box for
        every shape, so the rest of the pipeline (travel routing, exclusion boxes)
        needs no shape awareness. shape_polygon shares linear_pattern's
        'x,y; x,y' string format and parser."""
        import gui_form
        F = gui_form.Form(parent)
        # the shape's own description rides the combo's tooltip, re-set per
        # shape by `_toggle_shape` -- it was grey text under the row
        v["shape_row"] = F.combo(
            "Shape", None, [lbl for _val, lbl in _SHAPE_CHOICES],
            self._SHAPE_TIP)
        combo = v["shape_row"].field
        combo.bind("<<ComboboxSelected>>",
                   lambda _e, vv=v: self._on_shape_combo(vv))
        v["shape_combo"] = combo
        sides = F.entry(
            "Sides", v["shape_sides"],
            "Circle and ellipse: how many straight sides draw the outline "
            "(its smoothness). Regular polygon: its side count.",
            width="small")
        v["shape_sides_entry"] = sides.field
        # greys with its field, as every other row's label does
        v["shape_sides_label"] = sides.label

        # --- custom-outline draw panel (shown only for shape = custom) -------- #
        # ONE slot on the form's label edge, not padded in from the rows above.
        # Its inside is an editor (a vertex table
        # and a drawing canvas with its own toolbar), exempt from the row guard.
        body = gui_form.exempt(
            ttk.Frame(parent), "custom-outline editor: a vertex table and a "
                               "drawing canvas with its own toolbar")
        F.slot(None, body, sticky="nsew")
        body.columnconfigure(1, weight=1)
        v["shape_canvas_holder"] = body

        # left: a two-column (x, y) vertex table (the same idiom as the linear tab)
        tcol = ttk.Frame(body)
        tcol.grid(row=0, column=0, sticky="nw")
        hdr = ttk.Frame(tcol)
        hdr.grid(row=0, column=0, sticky="w", pady=(0, 2))
        ttk.Label(hdr, text="X (mm)", width=10).grid(row=0, column=1)
        ttk.Label(hdr, text="Y (mm)", width=10).grid(row=0, column=2)
        ttk.Button(hdr, text="+ Add point",
                   command=lambda vv=v: self._add_shape_row(vv)).grid(
            row=0, column=3, sticky="w", padx=(8, 0))
        table = ttk.Frame(tcol)
        table.grid(row=1, column=0, sticky="nw")
        v["shape_table"] = table

        # right: the live, clickable draw graph
        self._build_shape_canvas(body, v)

        # keep Length Y mirrored to Width X for round/regular shapes, and the bbox
        # synced to a custom drawing, as the relevant field changes
        v["width"].trace_add("write", lambda *_a, vv=v: self._mirror_shape_width(vv))
        v["shape_polygon"].trace_add(
            "write", lambda *_a, vv=v: self._on_shape_polygon_changed(vv))

        self._sync_shape_combo(v)        # set the combo label from v["shape"]
        self._toggle_shape(v)            # show/hide + grey per the chosen shape

    def _on_shape_combo(self, v):
        """Combo selection -> canonical v['shape'] value -> refresh the tab."""
        v["shape"].set(_SHAPE_LBL2VAL.get(v["shape_combo"].get(), "rectangle"))
        self._toggle_shape(v)

    def _sync_shape_combo(self, v):
        """Set the combo's label from the canonical v['shape'] (e.g. after load)."""
        combo = v.get("shape_combo")
        if combo is not None:
            combo.set(_SHAPE_VAL2LBL.get(v["shape"].get(), "Rectangle"))

    def _toggle_shape(self, v):
        """Show the fields relevant to the chosen shape and grey the rest: Sides is
        live only for circle/ellipse/polygon; Length Y is driven by Width X for
        circle/polygon and by the drawing for custom; the draw panel appears only
        for a custom outline."""
        shape = v["shape"].get()
        if v.get("shape_row") is not None:
            v["shape_row"].set_tip(self._SHAPE_TIP + "\n\n"
                                   + self._SHAPE_HINTS.get(shape, ""))
        if v.get("shape_sides_entry") is not None:
            state = ("normal" if shape in ("circle", "ellipse", "polygon")
                     else "disabled")
            v["shape_sides_entry"].configure(state=state)
            if v.get("shape_sides_label") is not None:
                v["shape_sides_label"].configure(state=state)
        we, he = v.get("width_entry"), v.get("height_entry")
        if shape in ("circle", "polygon"):
            if we is not None:
                we.configure(state="normal")
            if he is not None:
                he.configure(state="disabled")
            v["height"].set(v["width"].get())          # square bbox -> round/regular
        elif shape == "custom":
            if we is not None:
                we.configure(state="disabled")
            if he is not None:
                he.configure(state="disabled")
            self._sync_shape_bbox(v)                    # bbox follows the drawing
        else:                                          # rectangle, ellipse
            if we is not None:
                we.configure(state="normal")
            if he is not None:
                he.configure(state="normal")
        holder = v.get("shape_canvas_holder")
        if holder is not None:
            if shape == "custom":
                self._render_shape_table(v)
                holder.grid()
                self._redraw_shape_canvas(v)
            else:
                holder.grid_remove()

    def _mirror_shape_width(self, v):
        """Keep Length Y = Width X while a circle/polygon is selected (their bbox
        is square); a no-op for the other shapes."""
        if v["shape"].get() in ("circle", "polygon"):
            if v["height"].get() != v["width"].get():
                v["height"].set(v["width"].get())

    def _on_shape_polygon_changed(self, v):
        """A custom outline edit (table, drag, or load): redraw the graph and, when
        custom is active, refresh the Width/Length bounding box."""
        self._redraw_shape_canvas(v)
        if v["shape"].get() == "custom":
            self._sync_shape_bbox(v)

    def _sync_shape_bbox(self, v):
        """Set Width X / Length Y to the custom outline's bounding box -- the
        footprint bbox the travel/exclusion routing uses."""
        pairs, _e = self._parse_linear_pattern(v["shape_polygon"].get())
        if not pairs:
            return
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if w > 0:
            v["width"].set(f"{w:g}")
        if h > 0:
            v["height"].set(f"{h:g}")

    @staticmethod
    def _shape_is_convex(verts):
        """Convexity via the worker's own test, so the GUI flags exactly what the
        validator will reject. Fewer than 3 points reads as 'not yet concave'."""
        if len(verts) < 3:
            return True
        try:
            import scaffold_gcode
            return scaffold_gcode._polygon_is_convex(list(verts))
        except Exception:  # noqa: BLE001
            return True

    # ------- table view <-> canonical string: staleness bookkeeping ---------- #
    # Each of the three editors (custom outline, per-layer angles, linear pattern)
    # is a VIEW: _render_*_table draws rows from a canonical StringVar and the row
    # entries write back through _sync_*_pattern. That write-back is only valid
    # while the rows still mirror the string. A BULK set -- a preset load, a
    # settings restore, a scaffold prefill -- replaces the string under a table
    # that has not re-rendered, so a write-back in that window rejoins the OLD
    # rows over the freshly loaded value and silently erases it; mid-drag is the
    # same window, since the canvas defers its re-render to release.
    # So _render_*_table STAMPS the string it drew from and _sync_*_pattern
    # refuses to write while that stamp is out of date. Only a render stamps.

    def _stamp_rows(self, v, key):
        """Record the canonical string `key`'s just-rendered table now mirrors."""
        v[key + "_rows_src"] = v[key].get()

    def _rows_stale(self, v, key):
        """True when `key`'s string changed under its table since the last render,
        so the rows are no longer a faithful view and must not write back. Also
        true before the first render (no stamp), so a table that was never built
        cannot erase a loaded pattern with its empty rows."""
        return v.get(key + "_rows_src") != v[key].get()

    def _write_rows_back(self, v, key, text):
        """Commit a _sync_*_pattern rejoin. The rows mirror `text` by construction,
        so stamp it BEFORE the set, whose traces may re-enter and re-check."""
        v[key + "_rows_src"] = text
        # a rewrite of the same text fires every trace on the var (the G-code
        # page's regenerate, the preview) for nothing; see _sync_angle_pattern
        if v[key].get() != text:
            v[key].set(text)

    # ------- numeric (x, y) vertex table for a custom outline ---------------- #
    def _render_shape_table(self, v):
        """(Re)draw the custom outline's (x, y) table from the canonical
        shape_polygon string; row edits write straight back to it (the source of
        truth that persists and feeds the worker)."""
        table = v.get("shape_table")
        if table is None:
            return
        for w in table.winfo_children():
            w.destroy()
        pairs, _e = self._parse_linear_pattern(v["shape_polygon"].get())
        self._stamp_rows(v, "shape_polygon")   # the rows now mirror this string
        pairs = pairs or []
        rows = []
        for i, (a, p) in enumerate(pairs):
            avar = tk.StringVar(value=f"{a:g}")
            pvar = tk.StringVar(value=f"{p:g}")
            avar.trace_add("write", lambda *_a, vv=v: self._sync_shape_pattern(vv))
            pvar.trace_add("write", lambda *_a, vv=v: self._sync_shape_pattern(vv))
            ttk.Label(table, text=f"Pt {i + 1}:").grid(
                row=i, column=0, sticky="e", padx=(0, 4), pady=1)
            ttk.Entry(table, textvariable=avar, width=10).grid(
                row=i, column=1, sticky="w", pady=1, padx=(0, 4))
            ttk.Entry(table, textvariable=pvar, width=10).grid(
                row=i, column=2, sticky="w", pady=1)
            ttk.Button(table, text="X", width=3,
                       command=lambda idx=i, vv=v: self._remove_shape_row(vv, idx)
                       ).grid(row=i, column=3, sticky="w", padx=(4, 0), pady=1)
            rows.append((avar, pvar))
        v["shape_rows"] = rows

    def _sync_shape_pattern(self, v):
        """Rejoin the live table rows into the canonical shape_polygon string
        (skipping fully-blank rows; a blank in one column reads as 0). A no-op
        while the rows are a stale view of the string (see _rows_stale)."""
        if self._rows_stale(v, "shape_polygon"):
            return
        rows = v.get("shape_rows") or []
        parts = []
        for avar, pvar in rows:
            a, p = avar.get().strip(), pvar.get().strip()
            if not a and not p:
                continue
            parts.append(f"{a or 0},{p or 0}")
        self._write_rows_back(v, "shape_polygon", "; ".join(parts))

    def _add_shape_row(self, v):
        self._sync_shape_pattern(v)
        cur = v["shape_polygon"].get().strip()
        v["shape_polygon"].set(cur + "; 0,0" if cur else "0,0")
        self._render_shape_table(v)

    def _remove_shape_row(self, v, idx):
        rows = v.get("shape_rows") or []
        parts = []
        for j, (avar, pvar) in enumerate(rows):
            if j == idx:
                continue
            a, p = avar.get().strip(), pvar.get().strip()
            if not a and not p:
                continue
            parts.append(f"{a or 0},{p or 0}")
        v["shape_polygon"].set("; ".join(parts))
        self._render_shape_table(v)

    def _clear_shape(self, v):
        v["shape_polygon"].set("")
        self._render_shape_table(v)

    # ------- the clickable draw graph ---------------------------------------- #
    def _build_shape_canvas(self, parent, v):
        """The clickable outline editor to the right of the vertex table: click to
        drop a snapped vertex, drag one to move it, switch to Remove to delete.
        The outline is drawn closed and coloured green (convex) or red (concave --
        which the worker rejects)."""
        panel = ttk.Frame(parent)
        panel.grid(row=0, column=1, sticky="nw", padx=(14, 0))
        head = ttk.Frame(panel)
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="Draw outline").grid(row=0, column=0, sticky="w")
        ttk.Label(head, text="Grid (mm):").grid(
            row=0, column=1, sticky="e", padx=(14, 2))
        ttk.Entry(head, width=5, textvariable=v["shape_grid"]).grid(
            row=0, column=2, sticky="w")
        v["shape_status"] = ttk.Label(head, text="", font=("TkDefaultFont", 7))
        v["shape_status"].grid(row=0, column=3, sticky="e", padx=(10, 0))

        mode = ttk.Frame(panel)
        mode.grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(mode, text="Click to:").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(mode, text="Add", value="add",
                        variable=v["shape_editmode"]).grid(
            row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Radiobutton(mode, text="Remove", value="remove",
                        variable=v["shape_editmode"]).grid(
            row=0, column=2, sticky="w", padx=(6, 0))
        ttk.Button(mode, text="Clear", width=6,
                   command=lambda vv=v: self._clear_shape(vv)).grid(
            row=0, column=3, sticky="w", padx=(10, 0))
        # manual zoom (the graph no longer autoscales as points move)
        ttk.Label(mode, text="Zoom:").grid(row=0, column=4, padx=(12, 2))
        ttk.Button(mode, text="−", width=2,
                   command=lambda vv=v: self._zoom_view(
                       vv, "shape_view", self._VIEW_ZOOM_STEP,
                       self._redraw_shape_canvas)).grid(row=0, column=5)
        ttk.Button(mode, text="+", width=2,
                   command=lambda vv=v: self._zoom_view(
                       vv, "shape_view", 1.0 / self._VIEW_ZOOM_STEP,
                       self._redraw_shape_canvas)).grid(row=0, column=6)
        ttk.Button(mode, text="Fit", width=4,
                   command=lambda vv=v: self._fit_view(
                       vv, "shape_view",
                       self._redraw_shape_canvas)).grid(row=0, column=7,
                                                         padx=(4, 0))

        canvas = tk.Canvas(panel, width=300, height=300, background="white",
                           highlightthickness=1, highlightbackground="#cccccc",
                           takefocus=0)
        canvas.grid(row=2, column=0, sticky="nw", pady=(3, 0))
        v["shape_preview_canvas"] = canvas
        canvas.bind("<Configure>",
                    lambda _e, vv=v: self._redraw_shape_canvas(vv))
        canvas.bind("<Button-1>", lambda e, vv=v: self._shape_canvas_press(vv, e))
        canvas.bind("<B1-Motion>", lambda e, vv=v: self._shape_canvas_drag(vv, e))
        canvas.bind("<ButtonRelease-1>",
                    lambda e, vv=v: self._shape_canvas_release(vv, e))
        v["shape_grid"].trace_add(
            "write", lambda *_a, vv=v: self._redraw_shape_canvas(vv))
        return panel

    def _build_shape_editor(self, parent, v, stacked=False):
        """The custom-outline editor as one body: the draw graph with its
        toolbar (Click to Add/Remove, Clear, Zoom -/+/Fit, Grid) and the
        numeric vertex table with + Add point. The Footprint tab and the
        Subsection tool both build it, over their own dict `v` holding
        `shape` ("custom" while it is live), `shape_polygon`, `shape_grid`
        and `shape_editmode`. `stacked` puts the graph above the table for a
        narrow form. Returns the body frame; its owner shows and hides it."""
        body = ttk.Frame(parent)
        body.columnconfigure(1, weight=1)
        tcol = ttk.Frame(body)
        hdr = ttk.Frame(tcol)
        hdr.grid(row=0, column=0, sticky="w", pady=(0, 2))
        ttk.Label(hdr, text="X (mm)", width=10).grid(row=0, column=1)
        ttk.Label(hdr, text="Y (mm)", width=10).grid(row=0, column=2)
        ttk.Button(hdr, text="+ Add point",
                   command=lambda vv=v: self._add_shape_row(vv)).grid(
            row=0, column=3, sticky="w", padx=(8, 0))
        table = ttk.Frame(tcol)
        table.grid(row=1, column=0, sticky="nw")
        v["shape_table"] = table
        panel = self._build_shape_canvas(body, v)
        if stacked:
            panel.grid_configure(row=0, column=0, columnspan=2, padx=0)
            tcol.grid(row=1, column=0, columnspan=2, sticky="nw", pady=(6, 0))
        else:
            tcol.grid(row=0, column=0, sticky="nw")
        return body

    def _shape_pairs(self, v):
        pairs, _e = self._parse_linear_pattern(v["shape_polygon"].get())
        return list(pairs or [])

    def _shape_write_pairs(self, v, pairs, render_table=True):
        v["shape_polygon"].set("; ".join(f"{x:g},{y:g}" for x, y in pairs))
        if render_table:
            self._render_shape_table(v)

    def _shape_grid_unit(self, v):
        try:
            g = float(v["shape_grid"].get())
        except (ValueError, tk.TclError):
            return 1.0
        return g if g > 0 else 1.0

    def _shape_px_to_data(self, v, px, py):
        xf = v.get("shape_xform")
        if not xf:
            return None
        x0, y0, sc, dleft, dtop = xf
        if sc <= 0:
            return None
        g = self._shape_grid_unit(v)
        dx = dleft + (px - x0) / sc
        dy = dtop - (py - y0) / sc
        return (round(dx / g) * g, round(dy / g) * g)

    def _shape_hit_vertex(self, v, px, py, thresh=8.0):
        xf = v.get("shape_xform")
        pairs = self._shape_pairs(v)
        if not xf or not pairs:
            return None
        x0, y0, sc, dleft, dtop = xf
        best, bestd = None, thresh * thresh
        for i, (vx, vy) in enumerate(pairs):
            sx, sy = x0 + (vx - dleft) * sc, y0 + (dtop - vy) * sc
            d = (sx - px) ** 2 + (sy - py) ** 2
            if d <= bestd:
                best, bestd = i, d
        return best

    def _shape_canvas_press(self, v, e):
        if v["shape"].get() != "custom":
            return
        hit = self._shape_hit_vertex(v, e.x, e.y)
        if v["shape_editmode"].get() == "remove":
            if hit is not None:
                pairs = self._shape_pairs(v)
                del pairs[hit]
                self._shape_write_pairs(v, pairs)
            return
        if hit is not None:
            v["shape_drag_idx"] = hit
            return
        pt = self._shape_px_to_data(v, e.x, e.y)
        if pt is not None:
            pairs = self._shape_pairs(v)
            pairs.append(pt)
            self._shape_write_pairs(v, pairs)

    def _shape_canvas_drag(self, v, e):
        idx = v.get("shape_drag_idx")
        if idx is None:
            return
        pt = self._shape_px_to_data(v, e.x, e.y)
        pairs = self._shape_pairs(v)
        if pt is not None and 0 <= idx < len(pairs):
            pairs[idx] = pt
            self._shape_write_pairs(v, pairs, render_table=False)

    def _shape_canvas_release(self, v, _e):
        if v.get("shape_drag_idx") is not None:
            v["shape_drag_idx"] = None
            self._render_shape_table(v)

    def _redraw_shape_canvas(self, v):
        """Redraw the custom-outline editor: an equal-aspect graph with a snap
        grid, the CLOSED outline (green convex / red concave) and a draggable
        marker on each vertex. The frame is FROZEN (manual Zoom/Fit). Stashes the
        pixel<->mm transform on `v` for the click handlers."""
        canvas = v.get("shape_preview_canvas")
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        W, H = canvas.winfo_width(), canvas.winfo_height()
        if W < 20 or H < 20:
            return
        verts = self._shape_pairs(v)
        g = self._shape_grid_unit(v)
        xs, ys = [0.0], [0.0]
        for x, y in verts:
            xs.append(x)
            ys.append(y)
        pad = 2.0 * g
        xmin, xmax = min(xs) - pad, max(xs) + pad
        ymin, ymax = min(ys) - pad, max(ys) + pad
        cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        half = max((xmax - xmin) / 2.0, (ymax - ymin) / 2.0, 5.0 * g)
        half = math.ceil(half / g - 1e-9) * g
        # freeze the frame: use the stored (zoomed) view, seeding it from the fit
        # above on first draw. No autoscale as points move -- Zoom/Fit only.
        cx, cy, half = self._resolve_view(v, "shape_view", cx, cy, half)
        left, right, top, bottom = 36, 10, 8, 20
        side = min(W - left - right, H - top - bottom)
        if side < 20:
            return
        x0 = left + (W - left - right - side) / 2.0
        y0 = top + (H - top - bottom - side) / 2.0
        x1, y1 = x0 + side, y0 + side
        sc = side / (2.0 * half)
        dleft, dtop = cx - half, cy + half
        v["shape_xform"] = (x0, y0, sc, dleft, dtop)
        sx = lambda xx: x0 + (xx - dleft) * sc          # noqa: E731
        sy = lambda yy: y0 + (dtop - yy) * sc           # noqa: E731

        px_per_unit = sc * g
        draw_k = max(1, int(math.ceil(6.0 / px_per_unit))) if px_per_unit > 0 else 1
        gstep = g * draw_k
        label_k = (max(1, int(math.ceil(38.0 / (px_per_unit * draw_k))))
                   if px_per_unit > 0 else 1)
        m = int(math.ceil((cx - half) / gstep - 1e-9))
        while m * gstep <= cx + half + 1e-9:
            val = m * gstep
            gx = sx(val)
            is_axis = (abs(val) < 1e-9)
            canvas.create_line(gx, y0, gx, y1,
                               fill="#9a9a9a" if is_axis else "#ececec")
            if is_axis or m % label_k == 0:
                canvas.create_text(gx, y1 + 9, text=self._fmt_tick(val),
                                   fill="#666666", font=("TkDefaultFont", 6))
            m += 1
        m = int(math.ceil((cy - half) / gstep - 1e-9))
        while m * gstep <= cy + half + 1e-9:
            val = m * gstep
            gy = sy(val)
            is_axis = (abs(val) < 1e-9)
            canvas.create_line(x0, gy, x1, gy,
                               fill="#9a9a9a" if is_axis else "#ececec")
            if is_axis or m % label_k == 0:
                canvas.create_text(x0 - 4, gy, text=self._fmt_tick(val),
                                   anchor="e", fill="#666666",
                                   font=("TkDefaultFont", 6))
            m += 1

        convex = self._shape_is_convex(verts)
        if len(verts) >= 2:
            coords = []
            for x, y in verts:
                coords += [sx(x), sy(y)]
            coords += [sx(verts[0][0]), sy(verts[0][1])]    # close the outline
            canvas.create_line(*coords, fill="#2ca02c" if convex else "#d62728",
                               width=2)
        for vx, vy in verts:
            mx, my = sx(vx), sy(vy)
            r = 4.0
            canvas.create_oval(mx - r, my - r, mx + r, my + r,
                               fill="#1f77b4", outline="white", width=1)

        status = v.get("shape_status")
        if status is not None:
            if len(verts) < 3:
                status.configure(text="add 3+ points", foreground="#888888")
            elif convex:
                status.configure(text="convex", foreground="#2ca02c")
            else:
                status.configure(text="concave - not allowed",
                                 foreground="#d62728")

    def _build_scaffold_linear_fields(self, parent, v):
        """This scaffold's LINEAR PATTERN tab: a checkbox over wavelength +
        period-spacing fields, a two-column point table, and a clickable preview
        graph. When enabled, each straight fiber traces the shape given by the
        table's ABSOLUTE (x, y) vertices (x along the fiber, y perpendicular),
        repeating every `wavelength + spacing` mm. Unticking greys the fields
        rather than hiding them. The table AND the preview are views over the
        canonical `linear_pattern` string, the source of truth that persists and
        feeds the worker, so points can be authored by typing or by clicking."""
        import gui_form
        F = gui_form.Form(parent)
        F.check("Wave each straight fiber",
                v["linear_on"],
                "Replace each straight fiber with a repeating drawn shape "
                "along its length.",
                command=lambda vv=v: self._toggle_linear_pattern(vv))

        # the greyable group (the toggle disables it): its own flush block
        lf, G = gui_form.framed(parent, F.row, stretch=True)
        v["linear_fields"] = lf

        G.entry("Wavelength", v["linear_wavelength"],
                "Length of one repeat along the fiber.", unit="mm", indent=1)
        G.entry("Period spacing", v["linear_spacing"],
                "Flat gap added after each repeat, so the shapes don't "
                "touch.", unit="mm", indent=1)

        # the point table (left) and the live cartesian preview (right) sit side
        # by side; toggling the feature shows/hides this whole body. An EDITOR,
        # placed as one slot and exempt inside; how to write its points is its
        # tooltip (it was a grey line and an ⓘ above it).
        body = gui_form.exempt(
            ttk.Frame(lf), "linear-pattern editor: a point table and a "
                           "clickable preview graph with its own toolbar")
        G.slot(None, body,
               "Points are ABSOLUTE mm in the fiber frame: X runs with the "
               "fiber, Y sideways. A square = (0,0) (0,1) (1,1) (1,0) with "
               "wavelength 1; add period spacing so squares tile with a gap. "
               "Zig-zag = (0,1) (0.5,-1) with wavelength 1. Every layer of "
               "the same angle gets the same shape (so 0/90/0/90 stacks); a "
               "layer at a different angle deposits the same shape rigidly "
               "rotated to that angle.", sticky="nsew", indent=1)
        body.columnconfigure(1, weight=1)
        v["linear_scroll"] = body

        # left column: the headers + the row table (rendered by _render_linear_table)
        tcol = ttk.Frame(body)
        tcol.grid(row=0, column=0, sticky="nw")
        hdr = ttk.Frame(tcol)
        hdr.grid(row=0, column=0, sticky="w", pady=(0, 2))
        ttk.Label(hdr, text="X (mm)", width=10).grid(row=0, column=1)
        ttk.Label(hdr, text="Y (mm)", width=10).grid(row=0, column=2)
        ttk.Button(hdr, text="+ Add point",
                   command=lambda vv=v: self._add_linear_row(vv)).grid(
            row=0, column=3, sticky="w", padx=(8, 0))
        table = ttk.Frame(tcol)
        table.grid(row=1, column=0, sticky="nw")
        v["linear_table"] = table

        # right column: the live, autoscaling cartesian preview
        self._build_linear_preview(body, v)

        self._toggle_linear_pattern(v)   # build/hide the table per the checkbox

    def _scaffold_count(self):
        """Active scaffold count, at least 1 and as many as asked for (a
        garbled value -> 1). The slots behind it are grown on demand
        (`_scaf_grow`)."""
        try:
            return max(1, int(float(self.s_count.get())))
        except (ValueError, tk.TclError, OverflowError):
            return 1

    def _scaf_grow(self, n):
        """At least `n` designer scaffold slots: a design holds as many
        scaffolds as you like. Five are made at start (their persist keys and
        factory defaults are the ones every saved preset already knows); each
        one past them is made here, vars (`_new_scaffold_vars`) and widgets
        (`_scaf_slot_built`) both, the first time a count or a preset asks
        for it. A slot is never destroyed, as the first five never are."""
        while len(self.scaf_vars) < n:
            i = len(self.scaf_vars)
            self.scaf_vars.append(self._new_scaffold_vars(i))
            import gui_speed_ratio
            gui_speed_ratio.attach(self, self.scaf_vars[-1])
            self._scaf_slot_built(i)

    def _scaf_slot_built(self, i):
        """Slot i's widgets and per-scaffold state, once its vars exist. The
        classic notebook's tab here; the modern shell overrides it for its
        module cards and pill (`gui_pages`)."""
        if getattr(self, "scaf_nb", None) is not None:
            self._scaf_nb_tab(i)
        states = getattr(self, "_pv_states", None)
        if isinstance(states, list):
            while len(states) <= i:
                states.append(self._pv_new_state())
        diam = getattr(self, "_apply_diam_mode", None)
        if diam is not None:
            try:
                diam()
            except (tk.TclError, AttributeError, KeyError):
                pass

    def _scaf_nb_tab(self, i):
        """The classic notebook's tab for scaffold i."""
        tabf = ttk.Frame(self.scaf_nb, padding=4)
        self.scaf_nb.add(tabf, text=f" Scaffold {i + 1} ")
        self._build_scaffold_fields(tabf, self.scaf_vars[i])

    def _active_scaffold_index(self):
        """0-based index of the scaffold currently selected, clamped to the
        active count (so a hidden/garbled selection falls back into range)."""
        try:
            i = self._scaffold_selector_index()
        except (tk.TclError, ValueError):
            i = 0
        return max(0, min(i, self._scaffold_count() - 1))

    # -- the scaffold-selector seam: the ONLY notebook-identity glue. The
    #    classic UI answers from scaf_nb; the modern shell overrides both with
    #    its pill rail, under the same contract (0-based index / show N and
    #    clamp). ------------------------------------------------------------ #
    def _scaffold_selector_index(self):
        """0-based index of the selected scaffold in the classic notebook."""
        return self.scaf_nb.index(self.scaf_nb.select())

    def _scaffold_selector_show(self, n):
        """Display exactly the first `n` scaffolds and clamp the selection into
        range (classic: tab state hidden/normal + re-select)."""
        for i in range(len(self.scaf_vars)):
            try:
                self.scaf_nb.tab(i, state=("normal" if i < n else "hidden"))
            except tk.TclError:
                pass
        try:
            if self.scaf_nb.index(self.scaf_nb.select()) >= n:
                self.scaf_nb.select(n - 1)
        except (tk.TclError, ValueError):
            pass

    @staticmethod
    def _set_widgets_state(container, state):
        """Recursively set `state` ('normal'/'disabled') on every child widget of
        `container` that supports it, to grey out a feature's fields when its
        enable checkbox -- which lives OUTSIDE the container -- is off."""
        for child in container.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                pass
            _ScaffoldMixin._set_widgets_state(child, state)

    def _toggle_array(self, v):
        """Grey/enable scaffold `v`'s array fields as its checkbox flips."""
        self._set_widgets_state(
            v["array_fields"], "normal" if v["array_on"].get() else "disabled")

    def _flip_axis(self, v, which):
        """Flip scaffold `v`'s array direction on one axis (its +X/-X, +Y/-Y
        buttons)."""
        key = "array_flip_x" if which == "X" else "array_flip_y"
        v[key].set(not v[key].get())
        self._refresh_flip_buttons(v)

    def _refresh_flip_buttons(self, v=None):
        """Relabel flip buttons to mirror their state (re-sync after a load). With
        no `v`, refresh every scaffold's buttons."""
        targets = [v] if v is not None else self.scaf_vars
        for vv in targets:
            if "flip_x_btn" in vv:
                vv["flip_x_btn"].configure(
                    text=("-X" if vv["array_flip_x"].get() else "+X"))
                vv["flip_y_btn"].configure(
                    text=("-Y" if vv["array_flip_y"].get() else "+Y"))

    def _array_specs(self, i=None):
        """If scaffold `i`'s array box is ticked (default the ACTIVE scaffold),
        return the list of specs that tiles it into a Columns x Rows x Layers grid:
        each copy's start_x/start_y is offset by the X/Y spacing (negated on a
        flipped axis) and its start_z by the Z spacing. Copies are emitted Z level
        outermost, so every lower level is fully printed before the one stacking on
        it, and each level row-by-row in a serpentine order that keeps consecutive
        copies adjacent and the dragged continuous fiber short. Returns None when
        the box is off, so the caller takes the single-copy path."""
        if i is None:
            i = self._active_scaffold_index()
        v = self.scaf_vars[i]
        if not v["array_on"].get():
            return None
        base = self._scaffold_spec(i)
        nx = max(1, int(float(v["array_nx"].get())))
        ny = max(1, int(float(v["array_ny"].get())))
        nz = max(1, int(float(v["array_nz"].get())))
        dx = float(v["array_dx"].get() or 0) * (
            -1.0 if v["array_flip_x"].get() else 1.0)
        dy = float(v["array_dy"].get() or 0) * (
            -1.0 if v["array_flip_y"].get() else 1.0)
        dz = float(v["array_dz"].get() or 0)
        bx, by = base.get("start_x", 0.0), base.get("start_y", 0.0)
        bz = base.get("start_z", 0.0)
        specs = []
        for k in range(nz):                       # Z level: lower levels print first
            for r in range(ny):
                cols = range(nx) if r % 2 == 0 else range(nx - 1, -1, -1)
                for c in cols:
                    s = dict(base)                # base values are immutable / RO
                    s["start_x"] = bx + c * dx
                    s["start_y"] = by + r * dy
                    s["start_z"] = bz + k * dz
                    specs.append(s)
        return specs

    def _build_scaffold_link_loop_fields(self, parent, v, row):
        """Linking-loop controls for one scaffold: a master checkbox, an
        Auto/Manual mode radio, and the Auto detection thresholds (min turn angle,
        coincidence distance). At a square corner one layer exits and the next
        enters at the same point; a linking loop replaces that in-place turn with a
        gentle OUTBOARD loop so the jet rounds it off the part. Auto loops only a
        coincident corner turning at least the angle threshold; Manual loops every
        coincident corner."""
        pad = self.pad
        lf = ttk.LabelFrame(parent, text="Linking loops", padding=6)
        lf.grid(row=row, column=0, columnspan=4, sticky="we", **pad)
        self._build_scaffold_link_loop_body(lf, v)

    def _build_scaffold_link_loop_body(self, lf, v, F=None):
        """The linking-loop controls, laid into `lf` (the classic LabelFrame)
        or onto the rows of an existing gui_form Form `F` (the modern Geometry
        card, which carries them under its own section heading). Everything
        below the master toggle belongs to it, so it is indented one step."""
        import gui_form
        if F is None:
            F = gui_form.Form(lf)
        toggle = (lambda vv=v: self._toggle_link_loops(vv))
        F.check("Add a lead-in loop at square corners", v["link_loops_on"],
                gui_tips.pic(
                "linking_loops",
                "At a square corner one layer exits and the next enters at "
                "the same point. A linking loop replaces that in-place turn "
                "with a gentle outboard loop, so the jet rounds it off the "
                "part."), command=toggle)
        mode = F.group("Detect corners",
                       "Auto loops a corner only if it turns at least the "
                       "minimum turn angle. Manual loops every corner.",
                       indent=1)
        v["link_loops_auto_rb"] = mode.add_radio(
            "Auto-detect", v["link_loops_mode"], "auto", None, command=toggle)
        v["link_loops_manual_rb"] = mode.add_radio(
            "Manual (every corner)", v["link_loops_mode"], "manual", None,
            command=toggle)
        v["link_loop_angle_entry"] = F.entry(
            "Min turn angle", v["link_loop_angle"],
            "Auto only: loop a corner only if it turns at least this much.",
            unit="deg", indent=1).field
        v["link_loop_dist_entry"] = F.entry(
            "Look-back distance", v["link_loop_dist"],
            "How far back along the path the loop's lead-in aims. With "
            "travel loops off, it is instead the widest gap counted as one "
            "corner.", unit="mm", indent=1).field
        # Where consecutive layers do not share a free end the head hops between
        # them, and in continuous-flow MEW that hop is deposited fiber -- so it
        # makes a real corner at BOTH ends, and both are rounded.
        v["link_loop_travel_cb"] = F.check(
            "Also loop layer changes joined by a travel fiber",
            v["link_loop_travel"],
            "Where consecutive layers do not share a free end the head hops "
            "between them. In continuous flow that hop is deposited fiber, "
            "with a real corner at both ends. This rounds both.",
            indent=1).field
        self._toggle_link_loops(v)        # set the initial enabled/disabled state
        return F

    def _build_scaffold_perimeter_fields(self, parent, v, row):
        """CLASSIC Core tab: the perimeter-travel LabelFrame, one row under the
        linking loops."""
        lf = ttk.LabelFrame(parent, text="Travel", padding=6)
        lf.grid(row=row, column=0, columnspan=4, sticky="we", **self.pad)
        self._build_scaffold_perimeter_body(lf, v)

    def _build_scaffold_perimeter_body(self, lf, v, F=None):
        """The perimeter-travel controls, laid into `lf` (the classic
        LabelFrame) or onto the rows of a gui_form Form `F` (the modern
        Geometry card). Off, the automatic travel runs round the scaffold's
        bounding box as it always has; on, it follows the outline."""
        import gui_form
        if F is None:
            F = gui_form.Form(lf)
        toggle = (lambda vv=v: self._toggle_perimeter_travel(vv))
        F.check("Run travel along the perimeter", v["perimeter_travel"],
                "Every automatic travel (the lead-in, the hop between layers, "
                "the hop to the next scaffold and the exit) follows the "
                "scaffold's outline at the distance below, instead of going "
                "round its bounding box with 90 degree corners.",
                command=toggle)
        v["perimeter_travel_entry"] = F.entry(
            "Distance from the part", v["perimeter_travel_offset"],
            "How far outside the outermost fiber the travel runs.",
            unit="mm", indent=1).field
        self._toggle_perimeter_travel(v)
        return F

    def _toggle_perimeter_travel(self, v):
        """The distance is editable only while the travel follows the perimeter."""
        ent = v.get("perimeter_travel_entry")
        if ent is not None:
            ent.configure(
                state="normal" if v["perimeter_travel"].get() else "disabled")

    def _toggle_link_loops(self, v):
        """Enable the mode radio whenever the master box is ticked; enable the
        angle threshold only in Auto mode (Manual ignores the angle). The
        coincidence distance stays editable in both modes -- it defines what counts
        as a corner either way."""
        on = v["link_loops_on"].get()
        st = "normal" if on else "disabled"
        v["link_loops_auto_rb"].configure(state=st)
        v["link_loops_manual_rb"].configure(state=st)
        v["link_loop_dist_entry"].configure(state=st)
        v["link_loop_travel_cb"].configure(state=st)
        auto = on and v["link_loops_mode"].get() == "auto"
        v["link_loop_angle_entry"].configure(state="normal" if auto else "disabled")

    def _apply_scaffold1_default_color(self, n):
        """Scaffold 1's DEFAULT colour depends on whether it stands alone:
        SINGLE_SCAFFOLD_COLOR on its own, SCAFFOLD_COLORS[0] once a second
        scaffold joins, so a stack's layers read as distinct. Swaps ONLY when the
        field still holds one of the two defaults AND the count crosses the
        lone<->stack boundary, so a chosen colour is never overwritten."""
        prev = getattr(self, "_prev_scaffold_count", None)
        self._prev_scaffold_count = n
        # a preset apply restores colours from saved data -- never second-guess it
        if getattr(self, "_loading_preset", False) or prev is None:
            return
        if (prev <= 1) == (n <= 1):          # didn't cross the boundary
            return
        cur = self.scaf_vars[0]["color"].get().strip().lower()
        single, rainbow0 = SINGLE_SCAFFOLD_COLOR.lower(), SCAFFOLD_COLORS[0].lower()
        if n > 1 and cur == single:
            self.scaf_vars[0]["color"].set(SCAFFOLD_COLORS[0])
        elif n <= 1 and cur == rainbow0:
            self.scaf_vars[0]["color"].set(SINGLE_SCAFFOLD_COLOR)

    def _sync_scaffold_count(self, *_):
        """Show exactly the first N scaffolds (the rest hidden, their vars
        retained), prefill any newly revealed scaffold from its predecessor, and
        move off a selection that just got hidden (the clamp lives in
        _scaffold_selector_show)."""
        n = self._scaffold_count()
        self._scaf_grow(n)
        self._apply_scaffold1_default_color(n)
        for i in range(1, n):
            if i not in self._scaf_inited:
                self._prefill_scaffold(i)
                self._scaf_inited.add(i)
        self._scaffold_selector_show(n)

    def _prefill_scaffold(self, i):
        """Seed scaffold i's fields from scaffold i-1, then stand it on top: its
        Start Z = the previous scaffold's start_z + layers * fiber diameter. Only
        ever runs on first reveal, so later edits are never clobbered."""
        src, dst = self.scaf_vars[i - 1], self.scaf_vars[i]
        for key in ("width", "height", "stepover", "diameter", "layers", "speed",
                    "speed_unit", "speed_ratio", "layer_speed_ratios", "m_layer_speed_ratios",
                    "loop_speed", "start_x", "start_y", "start_angle",
                    "arc_radius_mult", "arc_segments", "rotation",
                    "link_loops_on", "link_loops_mode", "link_loop_angle",
                    "link_loop_dist", "link_loop_travel",
                    "perimeter_travel", "perimeter_travel_offset",
                    "angle_pattern_on", "angle_pattern",
                    "angle_stepover_pattern", "angle_offset_pattern",
                    "layer_overrides", "stepover_end", "grade_angle", "stepover_top",
                    "linear_on", "linear_wavelength", "linear_spacing",
                    "linear_pattern", "linear_periods", "linear_grid",
                    "shape", "shape_sides", "shape_polygon", "shape_grid",
                    "array_on", "array_nx", "array_ny", "array_nz",
                    "array_dx", "array_dy", "array_dz",
                    "array_flip_x", "array_flip_y", "array_travel"):
            try:
                dst[key].set(src[key].get())
            except Exception:  # noqa: BLE001
                pass
        try:
            z = (float(src["start_z"].get())
                 + int(float(src["layers"].get())) * float(src["diameter"].get()))
            dst["start_z"].set(f"{z:g}")
        except (ValueError, tk.TclError):
            dst["start_z"].set(src["start_z"].get())
        # reflect the copied pattern/array/shape: render rows + grey the right fields
        self._toggle_angle_pattern(dst)
        self._toggle_linear_pattern(dst)
        self._toggle_array(dst)
        self._sync_shape_combo(dst)
        self._toggle_shape(dst)
        self._refresh_flip_buttons(dst)

    #: the array spacing every new scaffold starts with (`_init_scaffold_vars`)
    _ARRAY_DEFAULT_SPACING = "35"
    #: the gap a seeded array leaves between neighbouring copies, mm
    _ARRAY_SEED_GAP = 5.0

    def _array_ticked(self, v):
        """The user ticked or unticked the array. Ticked on with the spacing
        still at the untouched default, it is seeded from the scaffold."""
        if v["array_on"].get():
            self._seed_array_spacing(v)
        self._toggle_array(v)

    def _seed_array_spacing(self, v):
        """An array switched on with the spacing still at the untouched 35 mm
        default gets a spacing from the scaffold itself: its own size plus a
        5 mm gap. 35 mm put a 10 mm scaffold's 2 x 2 copies off a 76 x 51 mm
        slide. A spacing the user typed is left alone. Called only from a
        user's switch, never from a preset load, so a saved 35 stays 35."""
        for key, size in (("array_dx", "width"), ("array_dy", "height")):
            if v[key].get().strip() != self._ARRAY_DEFAULT_SPACING:
                continue
            try:
                s = float(v[size].get())
            except (ValueError, tk.TclError):
                continue
            if s > 0:
                v[key].set("%g" % round(s + self._ARRAY_SEED_GAP, 3))

    def _mark_scaffolds_inited(self):
        """Treat the currently-counted scaffolds as already populated, so a
        following _sync_scaffold_count won't prefill over them. Called after a bulk
        field change (loading saved defaults, Reset) where the shown scaffolds'
        values come from the restored data."""
        self._scaf_inited = set(range(self._scaffold_count()))

    @staticmethod
    def _parse_angle_pattern(text):
        """Parse a per-layer angle pattern string into (list_of_floats, error).
        Accepts commas and/or whitespace and tolerates surrounding brackets; a
        blank string is ([], None). Mirrors scaffold_gcode.parse_angle_pattern so
        validation here matches what the worker will accept."""
        s = (text or "").strip().strip("()[]{}").strip()
        if not s:
            return [], None
        out = []
        for tok in re.split(r"[,\s]+", s):
            if not tok:
                continue
            try:
                out.append(float(tok))
            except ValueError:
                return None, f"'{tok}' is not a number"
        return out, None

    @staticmethod
    def _parse_layer_value_list(text):
        """Parse a per-layer stepover/offset string into (list, error), entries
        float or None (a blank slot). Split on COMMAS ONLY so empty slots keep
        their position, aligned index-for-index with the angle list; a blank
        string is ([], None). Mirrors scaffold_gcode.parse_layer_value_list."""
        s = (text or "").strip().strip("()[]{}").strip()
        if not s:
            return [], None
        out = []
        for tok in s.split(","):
            tok = tok.strip()
            if not tok:
                out.append(None)
                continue
            try:
                out.append(float(tok))
            except ValueError:
                return None, f"'{tok}' is not a number"
        return out, None

    @staticmethod
    def _angle_stamp(v):
        """Composite staleness stamp for the angle table, which edits THREE
        canonical strings: the single-key `_rows_stale` guard watches only
        angle_pattern and so misses a load where the angles are unchanged but the
        Stepover/Offset columns differ."""
        return "\x00".join((v["angle_pattern"].get(),
                            v["angle_stepover_pattern"].get(),
                            v["angle_offset_pattern"].get()))

    @staticmethod
    def _join_layer_columns(angles, steps, offs):
        """Rejoin parallel Angle/Stepover/Offset cell-string lists into the three
        canonical strings, keeping only slots with a non-blank ANGLE (aligned) and
        collapsing an all-blank Stepover/Offset column to "" so it stays inert.
        Returns (angle_str, step_str, off_str)."""
        ka, ks, ko = [], [], []
        for i, a in enumerate(angles):
            if not a:
                continue                     # blank angle -> drop the whole layer
            ka.append(a)
            ks.append(steps[i] if i < len(steps) else "")
            ko.append(offs[i] if i < len(offs) else "")
        step_str = "" if all(not s for s in ks) else ", ".join(ks)
        off_str = "" if all(not o for o in ko) else ", ".join(ko)
        return ", ".join(ka), step_str, off_str

    def _toggle_angle_pattern(self, v):
        """Show the per-layer angle table (and grey out the single rotation /
        start-angle, which it replaces) when the checkbox is on; hide it when
        off. Re-renders the table from the canonical angle_pattern string so a
        just-loaded pattern shows its rows."""
        on = bool(v["angle_pattern_on"].get())
        # the G-code page's vars carry `rotation` as a plain StringVar and no
        # entry widgets, so only a widget is greyed
        for key in ("rotation", "start_angle_entry"):
            w = v.get(key)
            if hasattr(w, "configure"):
                w.configure(state=("disabled" if on else "normal"))
        table = v.get("angle_table")
        if table is None:
            return
        # show/hide the table holder so no empty frame lingers when off
        holder = v.get("angle_scroll", table)
        if on:
            self._render_angle_table(v)
            holder.grid()
        else:
            holder.grid_remove()

    def _render_angle_table(self, v):
        """(Re)draw scaffold `v`'s per-layer angle table from its three canonical
        strings: `angle_pattern` plus the OPTIONAL `angle_stepover_pattern` /
        `angle_offset_pattern` (per-layer stepover + period offset, mm). Each row
        is one layer. A blank Stepover/Offset cell means "use the scaffold
        Stepover / no shift" for that layer. Row edits write straight back to the
        three StringVars, so the table is just a view."""
        table = v.get("angle_table")
        if table is None:
            return
        for w in table.winfo_children():
            w.destroy()
        angles, _ea = self._parse_angle_pattern(v["angle_pattern"].get())
        steps, _es = self._parse_layer_value_list(
            v["angle_stepover_pattern"].get())
        offs, _eo = self._parse_layer_value_list(v["angle_offset_pattern"].get())
        v["angle_pattern_rows_src"] = self._angle_stamp(v)  # rows mirror all three
        angles, steps, offs = angles or [], steps or [], offs or []

        for c, txt in enumerate(("Layer", "Angle°", "Spacing", "Offset")):
            ttk.Label(table, text=txt, foreground="#777").grid(
                row=0, column=c, sticky="w", padx=(0, 6), pady=(0, 2))

        a_rows, s_rows, o_rows = [], [], []
        for i, a in enumerate(angles):
            sv = steps[i] if i < len(steps) and steps[i] is not None else None
            ov = offs[i] if i < len(offs) and offs[i] is not None else None
            avar = tk.StringVar(value=f"{a:g}")
            svar = tk.StringVar(value=("" if sv is None else f"{sv:g}"))
            ovar = tk.StringVar(value=("" if ov is None else f"{ov:g}"))
            for var in (avar, svar, ovar):
                var.trace_add(
                    "write", lambda *_a, vv=v: self._sync_angle_pattern(vv))
            ttk.Label(table, text=f"{i + 1}:").grid(
                row=i + 1, column=0, sticky="e", padx=(0, 4), pady=1)
            ttk.Entry(table, textvariable=avar, width=7).grid(
                row=i + 1, column=1, sticky="w", pady=1)
            ttk.Entry(table, textvariable=svar, width=7).grid(
                row=i + 1, column=2, sticky="w", padx=(4, 0), pady=1)
            ttk.Entry(table, textvariable=ovar, width=7).grid(
                row=i + 1, column=3, sticky="w", padx=(4, 0), pady=1)
            ttk.Button(table, text="X", width=3,
                       command=lambda idx=i, vv=v: self._remove_angle_row(vv, idx)
                       ).grid(row=i + 1, column=4, sticky="w", padx=(4, 0), pady=1)
            a_rows.append(avar)
            s_rows.append(svar)
            o_rows.append(ovar)
        v["angle_rows"] = a_rows
        v["angle_step_rows"] = s_rows
        v["angle_off_rows"] = o_rows
        ttk.Button(table, text="+ Add layer",
                   command=lambda vv=v: self._add_angle_row(vv)).grid(
            row=len(angles) + 1, column=0, columnspan=3, sticky="w", pady=(2, 0))

    def _sync_angle_pattern(self, v):
        """Rejoin the live row entries back into the three canonical strings
        (dropping any layer whose Angle is blank; Stepover/Offset slots stay
        aligned to the surviving angles). A no-op while the rows are a stale view
        of the three strings -- see _angle_stamp."""
        if v.get("angle_pattern_rows_src") != self._angle_stamp(v):
            return
        a_rows = v.get("angle_rows") or []
        s_rows = v.get("angle_step_rows") or []
        o_rows = v.get("angle_off_rows") or []
        angles = [r.get().strip() for r in a_rows]
        steps = [r.get().strip() for r in s_rows]
        offs = [r.get().strip() for r in o_rows]
        a_str, s_str, o_str = self._join_layer_columns(angles, steps, offs)
        # Stamp the composite BEFORE the sets so a trace re-entering mid-write
        # reads the rows as faithful (not stale) and stays idempotent.
        v["angle_pattern_rows_src"] = "\x00".join((a_str, s_str, o_str))
        # only a real change is written: each write fires every trace on the
        # var (the G-code page's regenerate, the preview), and rewriting the
        # same strings kept re-arming them several times a second
        for key, val in (("angle_stepover_pattern", s_str),
                         ("angle_offset_pattern", o_str),
                         ("angle_pattern", a_str)):
            if v[key].get() != val:
                v[key].set(val)

    def _add_angle_row(self, v):
        self._sync_angle_pattern(v)
        cur = v["angle_pattern"].get().strip()
        v["angle_pattern"].set(cur + ", 0" if cur else "0")
        self._render_angle_table(v)   # new row gets blank Stepover/Offset

    def _remove_angle_row(self, v, idx):
        angles = [r.get().strip() for r in (v.get("angle_rows") or [])]
        steps = [r.get().strip() for r in (v.get("angle_step_rows") or [])]
        offs = [r.get().strip() for r in (v.get("angle_off_rows") or [])]
        if 0 <= idx < len(angles):
            del angles[idx]
            if idx < len(steps):
                del steps[idx]
            if idx < len(offs):
                del offs[idx]
        a_str, s_str, o_str = self._join_layer_columns(angles, steps, offs)
        v["angle_stepover_pattern"].set(s_str)
        v["angle_offset_pattern"].set(o_str)
        v["angle_pattern"].set(a_str)
        self._render_angle_table(v)

    @staticmethod
    def _parse_linear_pattern(text):
        """Parse a linear-pattern string into (list_of_(x, y), error).
        Pairs separated by ';'/newlines, the two numbers by comma/whitespace; a
        blank string is ([], None). Mirrors scaffold_gcode.parse_linear_pattern so
        validation here matches what the worker will accept."""
        s = (text or "").strip().strip("()[]{}").strip()
        if not s:
            return [], None
        out = []
        for chunk in re.split(r"[;\n]+", s):
            chunk = chunk.strip().strip("()[]{}").strip()
            if not chunk:
                continue
            toks = [t for t in re.split(r"[,\s]+", chunk) if t]
            if len(toks) != 2:
                return None, f"point '{chunk}' must be two numbers (x, y)"
            try:
                out.append((float(toks[0]), float(toks[1])))
            except ValueError:
                return None, f"'{chunk}' has a non-number"
        return out, None

    def _toggle_linear_pattern(self, v):
        """Grey/enable scaffold `v`'s linear-pattern fields as its checkbox flips,
        and show/hide the point-table + preview scroll area. Re-renders the table
        from the canonical linear_pattern string so a just-loaded pattern shows its
        rows."""
        on = bool(v["linear_on"].get())
        fields = v.get("linear_fields")
        if fields is not None:
            self._set_widgets_state(fields, "normal" if on else "disabled")
        # the Periods dropdown is readonly when live, not a free-text "normal"
        combo = v.get("linear_periods_combo")
        if combo is not None:
            combo.configure(state="readonly" if on else "disabled")
        holder = v.get("linear_scroll")
        if v.get("linear_table") is None or holder is None:
            return
        if on:
            self._render_linear_table(v)
            holder.grid()
            self._redraw_linear_preview(v)
        else:
            holder.grid_remove()

    def _render_linear_table(self, v):
        """(Re)draw scaffold `v`'s two-column (x, y) point table from its canonical
        linear_pattern string. Each row is one vertex: a label, an X entry, a Y
        entry and a remove button. Row edits write straight back to the
        linear_pattern StringVar (the single source of truth that persists and
        feeds the worker)."""
        table = v.get("linear_table")
        if table is None:
            return
        for w in table.winfo_children():
            w.destroy()
        pairs, _e = self._parse_linear_pattern(v["linear_pattern"].get())
        self._stamp_rows(v, "linear_pattern")  # the rows now mirror this string
        pairs = pairs or []
        rows = []
        for i, (a, p) in enumerate(pairs):
            avar = tk.StringVar(value=f"{a:g}")
            pvar = tk.StringVar(value=f"{p:g}")
            avar.trace_add("write",
                           lambda *_a, vv=v: self._sync_linear_pattern(vv))
            pvar.trace_add("write",
                           lambda *_a, vv=v: self._sync_linear_pattern(vv))
            ttk.Label(table, text=f"Pt {i + 1}:").grid(
                row=i, column=0, sticky="e", padx=(0, 4), pady=1)
            ttk.Entry(table, textvariable=avar, width=10).grid(
                row=i, column=1, sticky="w", pady=1, padx=(0, 4))
            ttk.Entry(table, textvariable=pvar, width=10).grid(
                row=i, column=2, sticky="w", pady=1)
            ttk.Button(table, text="X", width=3,
                       command=lambda idx=i, vv=v: self._remove_linear_row(vv, idx)
                       ).grid(row=i, column=3, sticky="w", padx=(4, 0), pady=1)
            rows.append((avar, pvar))
        v["linear_rows"] = rows

    def _sync_linear_pattern(self, v):
        """Rejoin the live row entries back into the canonical linear_pattern
        string (skipping fully-blank rows; a blank in one column reads as 0).
        Called on every row edit via a trace; a no-op while the rows are a stale
        view of the string (see _rows_stale)."""
        if self._rows_stale(v, "linear_pattern"):
            return
        rows = v.get("linear_rows") or []
        parts = []
        for avar, pvar in rows:
            a, p = avar.get().strip(), pvar.get().strip()
            if not a and not p:
                continue
            parts.append(f"{a or 0},{p or 0}")
        self._write_rows_back(v, "linear_pattern", "; ".join(parts))

    def _add_linear_row(self, v):
        self._sync_linear_pattern(v)
        cur = v["linear_pattern"].get().strip()
        v["linear_pattern"].set(cur + "; 0,0" if cur else "0,0")
        self._render_linear_table(v)

    def _remove_linear_row(self, v, idx):
        rows = v.get("linear_rows") or []
        parts = []
        for j, (avar, pvar) in enumerate(rows):
            if j == idx:
                continue
            a, p = avar.get().strip(), pvar.get().strip()
            if not a and not p:
                continue
            parts.append(f"{a or 0},{p or 0}")
        v["linear_pattern"].set("; ".join(parts))
        self._render_linear_table(v)

    def _build_linear_preview(self, parent, v):
        """The clickable cartesian preview to the RIGHT of the point table: an
        equal-aspect graph of the shape, redrawn on every edit to the pattern,
        wavelength, spacing, grid or period count. The frame does NOT autoscale as
        points move -- Zoom in / Zoom out / Fit set it. Click to drop a snapped
        vertex, drag one to a new intersection, or flip to Remove to delete. The
        Periods dropdown (1 or 2) lays out that many repeats."""
        panel = ttk.Frame(parent)
        panel.grid(row=0, column=1, sticky="nw", padx=(14, 0))
        head = ttk.Frame(panel)
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="Live preview").grid(row=0, column=0, sticky="w")
        ttk.Label(head, text="Periods:").grid(
            row=0, column=1, sticky="e", padx=(14, 2))
        combo = ttk.Combobox(head, width=3, state="readonly", values=("1", "2"),
                             textvariable=v["linear_periods"])
        combo.grid(row=0, column=2, sticky="w")
        v["linear_periods_combo"] = combo
        ttk.Label(head, text="Grid (mm):").grid(
            row=0, column=3, sticky="e", padx=(14, 2))
        ttk.Entry(head, width=5, textvariable=v["linear_grid"]).grid(
            row=0, column=4, sticky="w")

        # click-mode toggle: Add drops/moves vertices, Remove deletes them
        mode = ttk.Frame(panel)
        mode.grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(mode, text="Click to:").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(mode, text="Add", value="add",
                        variable=v["linear_editmode"]).grid(
            row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Radiobutton(mode, text="Remove", value="remove",
                        variable=v["linear_editmode"]).grid(
            row=0, column=2, sticky="w", padx=(6, 0))
        # manual zoom (the graph no longer autoscales as points move)
        ttk.Label(mode, text="Zoom:").grid(row=0, column=3, padx=(12, 2))
        ttk.Button(mode, text="−", width=2,
                   command=lambda vv=v: self._zoom_view(
                       vv, "linear_view", self._VIEW_ZOOM_STEP,
                       self._redraw_linear_preview)).grid(row=0, column=4)
        ttk.Button(mode, text="+", width=2,
                   command=lambda vv=v: self._zoom_view(
                       vv, "linear_view", 1.0 / self._VIEW_ZOOM_STEP,
                       self._redraw_linear_preview)).grid(row=0, column=5)
        ttk.Button(mode, text="Fit", width=4,
                   command=lambda vv=v: self._fit_view(
                       vv, "linear_view",
                       self._redraw_linear_preview)).grid(row=0, column=6,
                                                           padx=(4, 0))

        # An explicit square: inside the scrolling tab body there is no vertical
        # stretch to fill, so the graph must request a definite size of its own.
        canvas = tk.Canvas(panel, width=300, height=300, background="white",
                           highlightthickness=1, highlightbackground="#cccccc",
                           takefocus=0)
        canvas.grid(row=2, column=0, sticky="nw", pady=(3, 0))
        v["linear_preview_canvas"] = canvas
        canvas.bind("<Configure>",
                    lambda _e, vv=v: self._redraw_linear_preview(vv))
        canvas.bind("<Button-1>", lambda e, vv=v: self._linear_canvas_press(vv, e))
        canvas.bind("<B1-Motion>", lambda e, vv=v: self._linear_canvas_drag(vv, e))
        canvas.bind("<ButtonRelease-1>",
                    lambda e, vv=v: self._linear_canvas_release(vv, e))
        # redraw live whenever the canonical pattern / wavelength / spacing / grid /
        # period changes
        for key in ("linear_pattern", "linear_wavelength", "linear_spacing",
                    "linear_periods", "linear_grid"):
            v[key].trace_add(
                "write", lambda *_a, vv=v: self._redraw_linear_preview(vv))

    @staticmethod
    def _fmt_tick(val):
        """Compact tick label; collapses a tiny float to a clean '0'."""
        return "0" if abs(val) < 1e-9 else f"{val:g}"

    # ------- editor-graph view (manual zoom, no autoscale) ------------------- #
    # One "Zoom in" / "Zoom out" press scales the visible half-range by this.
    _VIEW_ZOOM_STEP = 1.4

    def _resolve_view(self, v, key, fit_cx, fit_cy, fit_half):
        """Return the (cx, cy, half) an editor graph should draw with. The view is
        FROZEN at the first fit and thereafter changed only by Zoom in / Zoom out /
        Fit, because re-framing as points were dragged was disorienting. First
        draw -- or after Fit clears it -- seeds it from the data-fit values."""
        view = v.get(key)
        if not view:
            view = (fit_cx, fit_cy, fit_half)
            v[key] = view
        return view

    def _zoom_view(self, v, key, factor, redraw):
        """Scale a frozen editor view's half-range about its own centre (Zoom out:
        factor > 1; Zoom in: factor < 1), then redraw. No-op until the first draw
        has seeded the view. Zoom-in is floored at one snap-grid cell
        (`_view_min_half`): finer would collapse every click onto a single node,
        stacking new points at one undraggable spot."""
        view = v.get(key)
        if not view:
            return
        cx, cy, half = view
        v[key] = (cx, cy, max(half * factor, self._view_min_half(v, key)))
        redraw(v)

    def _view_min_half(self, v, key):
        """The tightest half-range an editor graph may zoom to: one snap-grid cell
        of the matching editor. Below a cell the canvas spans no distinct grid
        node, so every click snaps to the same point and becomes immovable."""
        g = (self._shape_grid_unit(v) if key == "shape_view"
             else self._linear_grid_unit(v))
        return g

    def _fit_view(self, v, key, redraw):
        """Drop the frozen view so the next redraw re-seeds it from the data
        fit (the old autoscale, on demand), then redraw."""
        v[key] = None
        redraw(v)

    def _reset_editor_views(self):
        """Drop every scaffold's frozen graph-editor views (linear-pattern and
        footprint shape) so the next redraw re-fits each to the freshly loaded
        geometry. Called on preset apply: a loaded preset must FRAME its own
        pattern/footprint, or a leftover zoomed-in frame survives the load and
        traps new clicks at one grid node."""
        for v in getattr(self, "scaf_vars", []):
            v["linear_view"] = None
            v["shape_view"] = None

    def _linear_grid_unit(self, v):
        """Scaffold `v`'s snap-grid unit (mm), clamped to a positive value (a
        garbled / non-positive entry falls back to 0.1)."""
        try:
            g = float(v["linear_grid"].get())
        except (ValueError, tk.TclError):
            return 0.1
        return g if g > 0 else 0.1

    def _linear_period(self, v):
        """Scaffold `v`'s full repeat period (wavelength + spacing) in mm, or None
        if the wavelength is missing/non-positive (the wave is then off)."""
        try:
            wl = float(v["linear_wavelength"].get())
        except (ValueError, tk.TclError):
            return None
        if wl <= 0:
            return None
        try:
            sp = float(v["linear_spacing"].get())
        except (ValueError, tk.TclError):
            sp = 0.0
        return wl + max(0.0, sp)

    def _linear_wave_points(self, v):
        """The waved-fiber polyline for the live preview: `periods` (1 or 2)
        repeats of scaffold `v`'s shape laid along +x from the origin via the exact
        worker geometry (scaffold_gcode._wave_pass), in real shape coordinates so a
        click maps straight to a vertex. `pin_tips=False`, so the line runs THROUGH
        the authored tip vertices and connects to every marker -- tip pinning is a
        footprint-edge effect this abstract preview has no edge for. None when the
        pattern is empty/invalid or the period is non-positive."""
        pairs, err = self._parse_linear_pattern(v["linear_pattern"].get())
        if err or not pairs:
            return None
        period = self._linear_period(v)
        if period is None or period <= 0:
            return None
        try:
            periods = int(float(v["linear_periods"].get()))
        except (ValueError, tk.TclError):
            periods = 1
        periods = 2 if periods >= 2 else 1
        try:
            import scaffold_gcode  # lazy: keep GUI start-up independent of it
            pts = scaffold_gcode._wave_pass(
                (0.0, 0.0), (period * periods, 0.0), pairs, period, (1.0, 0.0),
                pin_tips=False)
        except Exception:  # noqa: BLE001
            return None
        return pts if len(pts) >= 2 else None

    # ------- click/drag point editing on the preview graph ------------------- #
    def _linear_px_to_data(self, v, px, py):
        """Invert a canvas pixel to snapped data (x, y) mm using the transform the
        last redraw stashed on `v`; None if no transform yet. Snaps to the grid
        unit so dropped/dragged points land on intersections."""
        xf = v.get("linear_xform")
        if not xf:
            return None
        x0, y0, sc, dleft, dtop = xf
        if sc <= 0:
            return None
        g = self._linear_grid_unit(v)
        dx = dleft + (px - x0) / sc
        dy = dtop - (py - y0) / sc
        return (round(dx / g) * g, round(dy / g) * g)

    def _linear_hit_vertex(self, v, px, py, thresh=8.0):
        """Index of the existing vertex whose marker is within `thresh` px of
        (px, py), or None. Used to pick a point to drag or remove."""
        xf = v.get("linear_xform")
        pairs, _e = self._parse_linear_pattern(v["linear_pattern"].get())
        if not xf or not pairs:
            return None
        x0, y0, sc, dleft, dtop = xf
        best, bestd = None, thresh * thresh
        for i, (vx, vy) in enumerate(pairs):
            sx, sy = x0 + (vx - dleft) * sc, y0 + (dtop - vy) * sc
            d = (sx - px) ** 2 + (sy - py) ** 2
            if d <= bestd:
                best, bestd = i, d
        return best

    def _linear_pairs(self, v):
        """The current vertex list parsed from the canonical string (or [])."""
        pairs, _e = self._parse_linear_pattern(v["linear_pattern"].get())
        return list(pairs or [])

    def _linear_write_pairs(self, v, pairs, render_table=True):
        """Write `pairs` back to the canonical linear_pattern string (the single
        source of truth); its trace redraws the preview. Re-render the numeric
        table too unless told otherwise (skipped mid-drag for cheapness)."""
        v["linear_pattern"].set("; ".join(f"{x:g},{y:g}" for x, y in pairs))
        if render_table:
            self._render_linear_table(v)

    def _linear_canvas_press(self, v, e):
        """Graph mouse-down. Remove mode: delete the clicked vertex. Add mode:
        clicking an existing vertex begins a drag, clicking empty space drops a new
        snapped vertex at the end of the shape."""
        if not v["linear_on"].get():
            return
        hit = self._linear_hit_vertex(v, e.x, e.y)
        if v["linear_editmode"].get() == "remove":
            if hit is not None:
                pairs = self._linear_pairs(v)
                del pairs[hit]
                self._linear_write_pairs(v, pairs)
            return
        if hit is not None:
            v["linear_drag_idx"] = hit           # grab it to drag
            return
        pt = self._linear_px_to_data(v, e.x, e.y)
        if pt is not None:
            pairs = self._linear_pairs(v)
            pairs.append(pt)
            self._linear_write_pairs(v, pairs)

    def _linear_canvas_drag(self, v, e):
        """Graph mouse-drag: move the grabbed vertex to the snapped cursor. Defer
        the numeric-table re-render to release so motion stays light."""
        idx = v.get("linear_drag_idx")
        if idx is None:
            return
        pt = self._linear_px_to_data(v, e.x, e.y)
        pairs = self._linear_pairs(v)
        if pt is not None and 0 <= idx < len(pairs):
            pairs[idx] = pt
            self._linear_write_pairs(v, pairs, render_table=False)

    def _linear_canvas_release(self, v, _e):
        """Graph mouse-up: end any drag and refresh the numeric table once."""
        if v.get("linear_drag_idx") is not None:
            v["linear_drag_idx"] = None
            self._render_linear_table(v)

    def _redraw_linear_preview(self, v):
        """Redraw scaffold `v`'s live linear-pattern preview: an equal-aspect
        cartesian graph of the shape and its tiling, in real shape coordinates with
        a snap grid at the grid unit and a draggable marker on every vertex. The
        frame is FROZEN (manual Zoom/Fit). Stashes the pixel<->mm transform on `v`
        so the click handlers can invert it. Bound to every pattern/wavelength/
        spacing/grid/period edit and to canvas resize."""
        canvas = v.get("linear_preview_canvas")
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        W, H = canvas.winfo_width(), canvas.winfo_height()
        if W < 20 or H < 20:
            return

        pts = self._linear_wave_points(v)
        verts = self._linear_pairs(v)
        g = self._linear_grid_unit(v)

        # data bounding box: the tiled fiber + every vertex + the origin (so the
        # axes are always in view), padded by a couple grid cells so there is room
        # to click beyond the shape.
        xs, ys = [0.0], [0.0]
        for seq in (pts or [], verts):
            for x, y in seq:
                xs.append(x)
                ys.append(y)
        pad = 2.0 * g
        xmin, xmax = min(xs) - pad, max(xs) + pad
        ymin, ymax = min(ys) - pad, max(ys) + pad
        cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        # one shared half-range (equal aspect), at least a few cells, snapped up to
        # a whole number of grid units so gridlines land on round values.
        half = max((xmax - xmin) / 2.0, (ymax - ymin) / 2.0, 5.0 * g)
        half = math.ceil(half / g - 1e-9) * g
        # freeze the frame: use the stored (zoomed) view, seeding it from the fit
        # above on first draw. No autoscale as points move -- Zoom/Fit only.
        cx, cy, half = self._resolve_view(v, "linear_view", cx, cy, half)

        left, right, top, bottom = 36, 10, 8, 20
        side = min(W - left - right, H - top - bottom)
        if side < 20:
            return
        x0 = left + (W - left - right - side) / 2.0
        y0 = top + (H - top - bottom - side) / 2.0
        x1, y1 = x0 + side, y0 + side
        sc = side / (2.0 * half)                        # px per mm, same both axes
        dleft, dtop = cx - half, cy + half              # data at the plot's top-left
        v["linear_xform"] = (x0, y0, sc, dleft, dtop)
        sx = lambda xx: x0 + (xx - dleft) * sc          # noqa: E731
        sy = lambda yy: y0 + (dtop - yy) * sc           # noqa: E731 (y grows down)

        # gridlines at the snap unit, thinned so they never crowd below ~6 px, and
        # labelled sparsely (~every 38 px). Snapping itself always uses `g`.
        px_per_unit = sc * g
        draw_k = max(1, int(math.ceil(6.0 / px_per_unit))) if px_per_unit > 0 else 1
        gstep = g * draw_k
        label_k = (max(1, int(math.ceil(38.0 / (px_per_unit * draw_k))))
                   if px_per_unit > 0 else 1)
        m = int(math.ceil((cx - half) / gstep - 1e-9))
        while m * gstep <= cx + half + 1e-9:
            val = m * gstep
            gx = sx(val)
            is_axis = (abs(val) < 1e-9)
            canvas.create_line(gx, y0, gx, y1,
                               fill="#9a9a9a" if is_axis else "#ececec")
            if is_axis or m % label_k == 0:
                canvas.create_text(gx, y1 + 9, text=self._fmt_tick(val),
                                   fill="#666666", font=("TkDefaultFont", 6))
            m += 1
        m = int(math.ceil((cy - half) / gstep - 1e-9))
        while m * gstep <= cy + half + 1e-9:
            val = m * gstep
            gy = sy(val)
            is_axis = (abs(val) < 1e-9)
            canvas.create_line(x0, gy, x1, gy,
                               fill="#9a9a9a" if is_axis else "#ececec")
            if is_axis or m % label_k == 0:
                canvas.create_text(x0 - 4, gy, text=self._fmt_tick(val),
                                   anchor="e", fill="#666666",
                                   font=("TkDefaultFont", 6))
            m += 1

        # the tiled fiber path (faint), then a marker on each editable vertex
        if pts:
            coords = []
            for x, y in pts:
                coords += [sx(x), sy(y)]
            canvas.create_line(*coords, fill="#1f77b4", width=2)
        for i, (vx, vy) in enumerate(verts):
            mx, my = sx(vx), sy(vy)
            r = 4.0
            canvas.create_oval(mx - r, my - r, mx + r, my + r,
                               fill="#d62728" if i else "#2ca02c",
                               outline="white", width=1)

        wl = v["linear_wavelength"].get().strip()
        sp = v["linear_spacing"].get().strip()
        cap = f"λ={wl or '?'} mm   gap={sp or '0'} mm   grid={g:g} mm"
        canvas.create_text(x0 + 2, top, anchor="nw", text=cap,
                           fill="#333333", font=("TkDefaultFont", 7))

    @staticmethod
    def _sag_solver_value(var, default="exact"):
        """The internal sag-solver value ("exact"/"approximate"/"off") behind a
        Sag-solver combo's LABEL. One place, because both panels' argv, validation
        and greying read it and a stale label must never silently mean "exact"
        somewhere and "off" elsewhere."""
        if var is None:
            return default
        return SAG_SOLVER_LBL2VAL.get(var.get(), default)

    def _toggle_sag(self):
        """Nothing left to grey on the classic G-code panel -- the Sag-multiplier
        entry it switched is gone. Kept because `_build_convert_tab` and the
        solver combo both call it, and a future sag knob belongs here."""

    def _toggle_scaffold_sag(self):
        """Grey the scaffold's sag knobs as its two switches flip. Sag solver "Off"
        turns the whole group off; the reset's own tickbox then gates just its
        period entry, so an unticked reset reads as clearly inert as a disabled
        one."""
        sag_on = self._sag_solver_value(self.s_sag_mode) != "off"
        state = "normal" if sag_on else "disabled"
        self.s_sag_reset_check.configure(state=state)
        self.s_sag_reset_entry.configure(
            state="normal" if (sag_on and self.s_sag_reset.get()) else "disabled")
        combo = getattr(self, "s_sag_resolution_combo", None)
        if combo is not None:
            combo.configure(state="readonly" if sag_on else "disabled")

    def _toggle_stabilization(self):
        """Grey/enable the stabilization fields as its checkbox flips. The tab and
        its fields stay in place (the panel is a persistent home), only their
        editability changes."""
        self._set_widgets_state(
            self.stab_frame, "normal" if self.stab_on.get() else "disabled")

    def _toggle_g2g3(self):
        """Arc segments stays editable in BOTH modes, so it is never greyed out.
        In G1 mode it sets the number of straight chords per loop; in G2/G3 mode
        the g-code is true smooth arcs but the STL still approximates them, and the
        field sets that preview fidelity (recorded in the g-code header for the
        converter)."""
        for v in self.scaf_vars:
            entry = v.get("arc_segments_entry")
            if entry is not None:
                entry.configure(state="normal")
