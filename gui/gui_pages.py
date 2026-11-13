#!/usr/bin/env python3
"""gui_pages.py -- MODERN-UI ONLY: the notebook-free page layouts.

_ModernPagesMixin overrides the CONTAINER seams the classic builders expose
(_build_scaffold_container, the _scaffold_selector_* pair, and in the calibration
case the whole tab hook) and re-lays the SAME field builders -- every Tk var,
trace, validator, widget-dict handle and preset key is the classic one. Only
presentation and navigation change; that is the no-drop contract test_ui_shell
guards.

Layout language: one calm scrolling surface per page. Overline section headers +
hairlines carry the grouping; secondary features live in gui_widgets.Disclosure
rows (collapsed while off, badge tracks the enable var); the scaffolds (as many as
asked for) are a gui_widgets.PillRail -- a selector, not tabs -- implementing the scaf_nb glue
contract.

Sits between _ShellMixin and _HomeMixin in ModernApp's MRO (mewracle_gui).
"""

import math
import tkinter as tk
from tkinter import ttk

from gui_common import SCAFFOLD_SPIN_MAX, VScrollFrame, _field

import gui_units
import gui_fonts
import gui_form
import gui_fusion_input
import gui_lawedit
import gui_mandrel
import gui_math
import gui_physics_diagrams
import gui_tips
import gui_widgets


class _ModernPagesMixin:
    # ------------------------------------------------------------------ #
    # scaffold designer: one scrolling surface
    # ------------------------------------------------------------------ #
    def _build_scaffold_container(self, frm):
        """Modern override of the classic two-notebook layout. `frm` is the
        scaffold page's VScrollFrame body.

        The page is a gui_widgets.ModuleGrid: two columns of module tiles, and
        clicking one replaces the grid with that module's fields across the whole
        pane. Every field builder below is the classic one, called with the same
        arguments it has always been called with -- only its parent changed."""
        import gui_form
        GAP = gui_fonts.GAP_SECTION
        frm.columnconfigure(0, weight=1)
        self._page_disclosures = getattr(self, "_page_disclosures", [])
        self._scaf_module_cards = []
        row = 0

        head = ttk.Frame(frm)
        head.grid(row=row, column=0, sticky="w")
        ttk.Label(head, text="Scaffold designer",
                  style="Title.TLabel").pack(side="left")
        if hasattr(self, "_gce_update_designer_from_page"):
            # the G-code page has its own design (gui_gcode_twodesigns):
            # this pulls it in, on demand, as that page's link pulls this
            up = ttk.Button(head, text="Update from G-code window",
                            style="Link.TButton",
                            command=self._gce_update_designer_from_page)
            up.pack(side="left", padx=(gui_fonts.SP["m"], 0))
            gui_tips.add_tip(up, "Copy the G-code page's design onto this "
                             "page. The two are separate designs: neither "
                             "changes the other until you ask.")
        row += 1
        # wraplength, not a bare label: this pane is 582 px and the sash makes it
        # narrower still. Without it the hint is CLIPPED mid-word ("... pick a
        # mo") rather than wrapped -- Tk labels do not wrap on their own. The
        # pane's width, not the label's: the label's own width IS its
        # wraplength once set, so measuring it feeds itself.
        intro = gui_form.page_intro(
            frm, "Design layered fiber scaffolds on a plate or a rotating "
                 "mandrel.",
            row=row, wrap=540)
        hint = intro.winfo_children()[0]
        frm.bind("<Configure>",
                 lambda e, w=hint: e.width > 120
                 and w.configure(wraplength=e.width - 40), add="+")
        row += 1

        # -- COLLECTOR: the 3-axis / 4-axis mode toggle, above
        #    everything it switches. Every half shares this page's
        #    Layers/speed/diameter, sag solver, facets, colour, export options
        #    and presets. ---------------------------------------------------- #
        self._build_collector_toggle(frm, modern=True).grid(
            row=row, column=0, sticky="w", pady=(0, GAP))
        row += 1

        # -- SCAFFOLDS: count + the pill rail (3-axis only: a tube is one
        #    continuous part, so there is no stack to select).
        #    NO OVERLINE HEADER. "Number of scaffolds" already names the group,
        #    and a one-row group under its own header read as a section with a
        #    title twice its own weight; the row now sits directly under the
        #    axis toggle it belongs to. ------------------------------------- #
        cnt, F = gui_form.framed(frm, row)
        cnt.grid_configure(pady=(0, gui_fonts.SP["m"]))
        self._collector_register(cnt, "plate")
        row += 1
        self.s_count = tk.StringVar(value="1")
        F.spin("Number of scaffolds", self.s_count,
               "How many scaffolds stack in Z. Each prints on top of the "
               "one before.", 1, SCAFFOLD_SPIN_MAX, width="small",
               state="readonly",
               command=self._sync_scaffold_count)
        ed = F.group("Editing",
                     "Which scaffold the modules below are editing. Every "
                     "scaffold's fields stay editable; the pill only picks "
                     "which one is on screen.")
        self._scaf_rail = gui_widgets.PillRail(
            ed.host(), count=len(self.scaf_vars),
            command=self._show_scaffold_frame, bound=frm)
        ed.add_widget(self._scaf_rail, kind="pills")

        # -- THE MODULE GRID ------------------------------------------------ #
        # Registration order IS reading order: the two collector-specific
        # collectors first (each sets the radius the geometry below is measured
        # against), then geometry, then the features that modify it, then the
        # whole-print output settings, then the full-width Presets bar.
        grid = gui_widgets.ModuleGrid(frm, columns=2)
        grid.grid(row=row, column=0, sticky="nsew")
        frm.rowconfigure(row, weight=1)
        self._scaf_grid = grid
        row += 1

        # COLLECTOR (4-axis only): the mandrel itself, above the geometry it
        # sets the radius for.
        coll = grid.add("collector", "Collector", info=(
            "The cylindrical collector the tube is wound onto. Its diameter is "
            "the radius both the generator and the preview wrap around."))
        self._collector_register(coll, "mandrel")
        self._build_mandrel_collector_fields(coll.body, tips=gui_tips)

        # GEOMETRY -- per scaffold, and the one card that holds BOTH
        # collector geometries. They share the body and are gridded exclusively,
        # because the card shows whichever collector is live; a separate card per
        # collector would leave the other one empty rather than absent.
        self._scaf_module(grid, "geometry", "Geometry",
                          self._build_geometry_frame, info=(
            "The core shape and print settings for this design.\n\n"
            "Plate: size, fiber spacing, layers, print speed, start position, "
            "the three rotation angles, and the corner-loop knobs.\n\n"
            "Rotating mandrel: the pattern, the scaffold length, the outcome the "
            "pattern is designed to (a pore size for the diamond helix, a "
            "cell size for the unit-cell families), and the same layers, "
            "speed and fiber diameter. What the software resolved is "
            "tabulated under the grid; the winding angle and the closure "
            "ladder are overrides, in Advanced.\n\n"
            "The interlayer linking loops live at the bottom of the flat-plate "
            "half: they are a property of the corner the serpentine turns, "
            "which is the grid above them."))

        # PREVIEW OPTIONS have no tile. They are a row across the preview
        # panel's own header -- beside the picture they change, since none of them reaches an
        # exported file. Only the VARS are made here, and early, because
        # _install_preview_traces reads them.
        if hasattr(self, "_pv_ensure_option_vars"):
            self._pv_ensure_option_vars()

        # STABILIZATION BASE -- whole print, plate only.
        stab = grid.add("stabilization", "Stabilization scaffold", info=gui_tips.pic(
            "stabilization",
            "A small dense pad printed first, in the same continuous fiber, so "
            "the jet is steady before the real part starts."))
        self._collector_register(stab, "plate")
        self._build_stabilization_tab(stab.body)
        stab.bind_status(self.stab_on)      # var is created by the builder

        # The per-scaffold feature modules, plate only.
        self._scaf_module(grid, "angle_pattern", "Layer angles",
                          self._build_scaffold_angle_pattern_body,
                          status_key="angle_pattern_on", mode="plate",
                          info=gui_tips.pic("angle_pattern",
            "Give each layer its own absolute fiber angle instead of one "
            "repeating rotation. The list cycles if there are more layers than "
            "angles, and overrides Layer rotation and Start angle. Each layer may "
            "also set its own Spacing (blank = the scaffold's Fiber spacing) and a period "
            "Offset in mm that slides that layer's fiber grid sideways "
            "(perpendicular to its fibers; blank = no shift)."))

        self._scaf_module(grid, "footprint", "Footprint",
                          self._build_scaffold_shape_fields,
                          status_key="shape", mode="plate", info=(
            "The plan-view outline the scaffold fills: rectangle, circle, "
            "ellipse, regular polygon, or a shape you draw yourself."))

        self._scaf_module(grid, "array", "Array",
                          self._build_scaffold_array_fields,
                          status_key="array_on", mode="plate", info=(
            "Tile this scaffold into a grid of identical copies. When on, the copy "
            "count replaces the multi-scaffold stack (‘Number of scaffolds’ is "
            "ignored)."))

        self._scaf_module(grid, "linear", "Wave",
                          self._build_scaffold_linear_fields,
                          status_key="linear_on", mode="plate", info=(
            "Replace each straight fiber with a repeating drawn shape (for example "
            "a wave or a square wave) along its length."))

        # the physics printhead-sim hook, at the same relative point as the
        # classic container (the shell override ignores the arg and mounts the
        # panel on its own sidebar page)
        self._build_sim_tab(None)

        # TUBE CLOSURE (4-axis only): the closure ladder, the speed policy and the
        # optional calibrated placement radii. Those stay on this page rather than
        # the Physics page because they describe a cylindrical collector, which is
        # why mew_calibration.py:314 excludes them from the Physics registry.
        closure = grid.add("advanced", "Advanced", info=(
            "Overrides. Nothing in here is needed to design a tube: the "
            "Tube card's Design by row chooses between asking for a pore and "
            "typing a winding angle. The closure ladder (η and Rm) is worked "
            "out for you either way; \u201cSet η and Rm myself\u201d types it "
            "instead. It belongs to the diamond helix and is hidden for the "
            "unit-cell families, "
            "which close by tiling a whole number of cells instead. The "
            "speed policy and the calibrated placement radii apply to every "
            "family."))
        self._collector_register(closure, "mandrel")
        self._build_mandrel_closure_fields(closure.body, tips=gui_tips)

        # THE DRAWN CELL (4-axis, "custom" family only). Gated on BOTH axes:
        # _collector_register hides it on a flat plate and _mandrel_family_register
        # hides it for every family whose geometry is named rather than drawn.
        drawn = grid.add("custom_cell", "Custom cell", info=(
            "Draw the unit cell that tiles around the mandrel and along "
            "it. Only the ‘Custom cell (drawn)’ pattern uses "
            "this; every other family's geometry comes from its "
            "literature definition and a cell size."))
        self._collector_register(drawn, "mandrel")
        self._mandrel_family_register((gui_mandrel.MANDREL_DRAWN_FAMILY,), drawn)
        self._build_mandrel_cell_editor_fields(drawn.body, tips=gui_tips)

        export = grid.add("export", "Export", info=(
            "What comes out and what is in it: the end-of-print exit move, "
            "whether to also build the STL, how travels are drawn, and the "
            "order a multi-scaffold stack is laid in. How the printer's g-code "
            "is spelled is on Setup."))
        quality = grid.add("quality", "Quality", info=(
            "How much fidelity the export spends: how round the fiber is in the "
            "STL, which sag solver runs and how hard it sags, and how finely "
            "the mesh and the physics-deposited curves are sampled. None of it "
            "touches the g-code."))
        # ONE builder, two homes: the classic developer UI still grids the
        # quality block at the bottom of its Export tab (quality_into=None).
        self._build_export_tab(export.body, quality_into=quality.body)
        # how the exported g-code spells HEIGHT is Setup's (its G-code card):
        # `origin.*` keys in the MACHINE preset (gui_buildplate)
        EF = self._export_form
        # how many G-code-page changes ride this design's export, with the
        # way to them (gui_gcode_changes). Empty and hidden when none.
        if hasattr(self, "_gce_build_badge"):
            self._gce_build_badge(export.body, EF.row + 1)

        # Presets is the one WIDE tile: its table wants the full pane, and it is
        # the last thing you reach for rather than one of a pair.
        presets = grid.add("presets", "Presets", wide=True, info=(
            "Save every scaffold setting on this page as a named preset and "
            "reload it any time. (The Printer & collector page has its own "
            "presets.)"))
        self._build_presets_tab(presets.body)
        tree = self._pstores.get("scaffold", {}).get("table")
        if tree is not None:
            gui_widgets.scroll_guard(tree)

        # The Design summary, under the tiles: what the design adds up to, read
        # off the preview's own build. Flat plate and mandrel only.
        self._ds_panel = grid.add_footer(
            lambda parent: gui_form.exempt(gui_widgets.DesignSummary(
                parent, "Design summary", self._DS_METRICS),
                "the Design summary: read-only readouts, no parameter rows"))

        # show only the active scaffolds (count defaults to 1) ...
        self._sync_scaffold_count()
        # ... then hide whichever collector half is not selected
        self.s_collector.trace_add("write", self._apply_collector_mode)
        self._apply_collector_mode()
        # ... and last, the tile summaries. AFTER _apply_collector_mode, because
        # the geometry summary reads s_collector to decide which of the three
        # geometries it is describing.
        self._install_summary_traces()
        self._refresh_module_summaries()

    # -- the per-scaffold module shape ------------------------------------- #
    def _scaf_module(self, grid, key, title, builder, status_key=None,
                     mode=None, info=None):
        """Register ONE card for a per-scaffold module, whose body holds the five
        scaffolds' frames stacked in a single grid cell.

        One card, not five: the tile says what THIS module is, and the pill rail
        already says which scaffold you are editing. `_show_scaffold_frame` raises
        the matching frame and re-points the badge at that scaffold's status var.

        `builder` is the classic field builder, called (frame, vars) -- or
        (frame, vars, i) if it takes the scaffold index, which only Geometry does.
        `mode` registers the card with the collector gate; None means every
        collector shows it. Registering under two modes would NOT work and is not
        a gap: _apply_collector_mode walks every (widget, mode) pair, so a card
        listed twice is shown by one pass and hidden by the next."""
        card = grid.add(key, title, info=info)
        wants_index = builder.__code__.co_argcount > 3   # self, frame, vars, i
        card._scaf_frames = []
        card._scaf_builder = (builder, wants_index)
        for i in range(len(self.scaf_vars)):
            self._scaf_card_frame(card, i)
        card._scaf_frames[0].grid()
        card._status_key = status_key
        if status_key:
            card.bind_status(self.scaf_vars[0][status_key])
        if mode:
            self._collector_register(card, mode)
        self._scaf_module_cards.append(card)
        # back-compat alias so existing self.s_rotation references keep working
        if key == "geometry":
            self.s_rotation = self.scaf_vars[0]["rotation"]
        return card

    def _build_geometry_frame(self, f, v, i):
        """Geometry's per-scaffold frame: the plate grid always, plus -- on
        scaffold 1 only -- the mandrel grid, gridded exclusively in the same
        cell.

        Layers, speed and fiber diameter appear in more than one of those grids as
        two widgets over ONE variable, so switching collector carries their values
        rather than resetting them. Only scaffold 1 gets the tube half
        because the mandrel pins the scaffold count to 1."""
        import gui_form
        # Each collector's rows are one gui_form block (`framed`): a flush
        # sub-frame that shows and hides as one, whose label and field edges
        # match every other block's because every Form shares one label-column
        # minimum.
        # Column 3 weighted: the lead-in-loop checkbox spans all four columns
        # and is wider than they are, and with no column weighted Tk shares
        # that overflow among them -- 9 px of it landed on the label column
        # and shifted every field in this block right of every other card's.
        # The weighted stretch column takes it instead. NOT framed(stretch=
        # True): that pins the block to the pane's width and clips the
        # checkbox's text, whose wording Lite shares.
        plate, F = gui_form.framed(f, 0)
        plate.columnconfigure(3, weight=1)
        self._collector_register(plate, "plate")
        self._build_scaffold_geometry_fields(plate, v, F=F)
        # Linking loops, folded in here rather than given a tile of their own.
        # They describe what happens at the corners of the grid
        # directly above, and a tile whose whole content is one checkbox and two
        # thresholds was a tile spent on a footnote. Same Form, under its own
        # heading, so it rides `plate`'s collector registration: there are no
        # square corners to round on a tube.
        F.section("Interlayer linking loops")
        self._build_scaffold_link_loop_body(plate, v, F=F)
        # Travel, under its own heading on the same Form: where the head goes
        # between layers and scaffolds, so it rides `plate`'s collector too.
        F.section("Travel")
        self._build_scaffold_perimeter_body(plate, v, F=F)
        if i == 0:
            tube = ttk.Frame(f)
            tube.grid(row=1, column=0, sticky="w")
            self._collector_register(tube, "mandrel")
            self._build_mandrel_geometry_fields(tube, tips=gui_tips)
        # VIEWER COLOUR, under both collector grids and registered to neither,
        # because both want it and both read the SAME var. `v["color"]` drives
        # the live preview (`_pv_on_color`, `_mandrel_on_color`), the STL
        # viewer, and -- via `--scaffold-groups` on a plate or `--fiber-color`
        # on a tube -- the player. One field, one colour, everywhere the design is drawn.
        #
        # It has to be built HERE rather than inside a collector's grid. The
        # classic developer UI keeps its own "Viewer colour (hex):" in
        # `_build_scaffold_geometry_fields`, guarded to classic-only because
        # this row is the modern shell's. Without it the modern shell would have
        # no colour control at all -- and since the var persists and is sent to
        # the workers, no persist-key test could see it go.
        self._build_scaffold_color_row(f, v)

    def _build_scaffold_color_row(self, f, v):
        """The swatch + hex entry + picker for one scaffold's viewer colour."""
        from tkinter import colorchooser
        import gui_form

        # ITS OWN HEADING. Without one the row sits directly under the flat
        # plate's "Interlayer linking loops" block and reads as another linking-
        # loop field -- seen in the render, not reasoned about. The heading also
        # gives the 4-axis grid, which has no loops block, a break
        # between the geometry numbers and this.
        _fr, F = gui_form.framed(f, 3)
        # first=False: this Form is not the top of the card, so the heading
        # gets the same air above it as every other section heading
        F.section("Appearance", first=False)
        tip = ("The color this design is drawn in: the live preview, the STL "
               "viewer and the simulation's deposited fibers. It is never "
               "written into the exported STL or the g-code.")
        row = F.group("Viewer color", tip)
        sw = gui_form.mark(tk.Label(row.host(), width=3, relief="solid",
                                    borderwidth=1), "swatch")
        row.add_widget(sw, tip, kind="swatch")
        row.add_entry("", v["color"], tip, width="num")

        def _sync_sw(*_a):
            try:
                sw.configure(background=(v["color"].get().strip() or "#000000"))
            except tk.TclError:             # a half-typed hex is not a colour
                pass

        def _pick():
            _rgb, hx = colorchooser.askcolor(color=(v["color"].get() or None),
                                             parent=f)
            if hx:
                v["color"].set(hx)

        row.add_button("Pick…", _pick, "Choose the color in a color picker.",
                       width=6)
        v["color"].trace_add("write", _sync_sw)
        _sync_sw()

    # ------------------------------------------------------------------ #
    # module summaries: the one line each tile shows under its title
    # ------------------------------------------------------------------ #
    # This is the only genuinely NEW state the module grid introduces. A
    # Disclosure's badge could say On/Off and nothing else, so "how is this
    # design actually set up" meant opening every row in turn. A tile has room
    # for an answer, and the grid is only worth the swap if it gives one.
    #
    # All of it is DERIVED -- nothing here is persisted, nothing here is read
    # back by the generator. If a formatter raises (a half-typed entry, a var a
    # collector does not own) that module simply shows no summary; a tile is
    # never allowed to take the page down over a cosmetic line.

    @staticmethod
    def _num(var, fmt="%g", default=""):
        """A var's value as a number, formatted -- or `default` while the user is
        mid-keystroke. Entry validation runs on commit, so a summary that assumed
        parseable text would raise on every partially-typed field."""
        try:
            return fmt % float(str(var.get()).strip())
        except (ValueError, AttributeError, TypeError):
            return default

    @staticmethod
    def _txt(var, default=""):
        try:
            v = str(var.get()).strip()
        except Exception:  # noqa: BLE001
            return default
        return v or default

    def _join(self, *parts):
        return " · ".join(p for p in parts if p)

    def _summary_geometry(self, v, loops=True):
        mode = self._txt(self.s_collector, "plate")
        if mode == "mandrel":
            fam = self._txt(getattr(self, "m_family", None) or tk.StringVar())
            return self._join(
                fam or "tube",
                "%s mm long" % self._num(self.m_length) if self._num(self.m_length) else "",
                "%s layers" % self._num(v["layers"]) if self._num(v["layers"]) else "")
        w, h = self._num(v["width"], "%.4g"), self._num(v["height"], "%.4g")
        # "linking loops" only when they are ON. The loops have no tile of their
        # own, so this line is the only place the grid says they are running;
        # naming them when they are off would spend the flat-plate summary's last
        # slot on a feature nobody switched on. The sidebar's Current design
        # block passes loops=False and never names them.
        tag = ""
        try:
            if loops and bool(v["link_loops_on"].get()):
                tag = "linking loops"
        except (tk.TclError, KeyError):
            pass
        return self._join(
            "%s × %s mm" % (w, h) if w and h else "",
            "%s mm stepover" % self._num(v["stepover"], "%.4g"),
            "%s layers" % self._num(v["layers"]),
            tag)

    def _summary_collector(self):
        d = self._num(self.m_diameter, "%.3g")
        return self._join("Ø %s mm" % d if d else "",
                          "%s mm mandrel" % self._num(self.m_length, "%.4g"))

    def _summary_stabilization(self):
        if not bool(self.stab_on.get()):
            return "Not printed"
        return self._join("%s layers" % self._num(self.stab_layers),
                          "%s mm stepover" % self._num(self.stab_stepover, "%.4g"))

    def _summary_angle(self, v):
        if not bool(v["angle_pattern_on"].get()):
            return "One repeating rotation"
        pat = self._txt(v["angle_pattern"])
        return ("%s°" % pat) if pat else "No angles listed"

    def _summary_footprint(self, v):
        shape = self._txt(v["shape"], "rectangle").capitalize()
        if shape.lower().startswith("polygon"):
            n = self._num(v["shape_sides"])
            return self._join(shape, "%s sides" % n if n else "")
        return shape

    def _summary_array(self, v):
        if not bool(v["array_on"].get()):
            return "Single scaffold"
        nx, ny = self._num(v["array_nx"]), self._num(v["array_ny"])
        return self._join("%s × %s copies" % (nx, ny) if nx and ny else "",
                          "%s mm pitch" % self._num(v["array_dx"], "%.4g"))

    def _summary_linear(self, v):
        if not bool(v["linear_on"].get()):
            return "Straight fibers"
        return self._join(self._txt(v["linear_pattern"]).capitalize(),
                          "λ %s mm" % self._num(v["linear_wavelength"], "%.4g"))

    def _summary_export(self):
        parts = ["G-code"]
        try:
            if bool(self.s_make_stl.get()):
                parts[0] = "G-code + STL"
        except Exception:  # noqa: BLE001
            pass
        out = [parts[0]]
        try:
            if bool(self.s_exit_move.get()):
                out.append("exit %s mm" % self._num(self.s_exit_margin, "%.4g"))
        except Exception:  # noqa: BLE001
            pass
        try:
            if bool(self.s_g2g3.get()):
                out.append("G2/G3 arcs")
        except Exception:  # noqa: BLE001
            pass
        return self._join(*out)

    def _summary_quality(self):
        """Quality's tile line: the sag solver, the tube's roundness, and the sag
        multiplier only when it is not 1. A multiplier of 1 is the default and
        saying so on every tile is a word that never varies."""
        out = []
        solver = self._txt(getattr(self, "s_sag_mode", None) or tk.StringVar())
        if solver:
            out.append("Sag: %s" % solver.split("(")[0].strip().lower())
        faces = self._num(getattr(self, "s_faces", None) or tk.StringVar())
        if faces:
            out.append("%s facets" % faces)
        mult = self._num(getattr(self, "s_sag_mult", None) or tk.StringVar(),
                         "%.4g")
        if mult and mult not in ("1", "1.0"):
            out.append("×%s sag" % mult)
        return self._join(*out)

    def _summary_advanced(self):
        # the speed mode's stored value is a bare word ("surface"), which reads
        # as a collector rather than a speed policy. Name what it is.
        speed = self._txt(getattr(self, "m_speed_mode", None) or tk.StringVar())
        mode = self._txt(getattr(self, "m_design_mode", None)
                         or tk.StringVar())
        return self._join(
            {"turn": "Turns at the ends", "pore": "Length closes, pore picked",
             "angle": "Length closes, angle picked"}.get(
                 mode, "%s-driven" % mode.capitalize() if mode else ""),
            "constant %s speed" % speed if speed else "")

    def _summary_custom_cell(self):
        w = self._num(getattr(self, "m_cell_w", None) or tk.StringVar(), "%.4g")
        h = self._num(getattr(self, "m_cell_h", None) or tk.StringVar(), "%.4g")
        return "%s × %s mm cell" % (w, h) if w and h else ""

    def _summary_presets(self):
        store = self._pstores.get("scaffold", {}) if hasattr(self, "_pstores") else {}
        name = ""
        try:
            table = store.get("table")
            if table is not None:
                rows = table.get_children()
                name = "%d saved" % len(rows)
        except Exception:  # noqa: BLE001
            name = ""
        return name

    def _refresh_module_summaries(self, *_a):
        """Recompute every tile's summary from the live vars.

        Coalesced through after_idle: a single keystroke in Width X writes one
        var, but a preset LOAD writes several hundred, and formatting thirteen
        lines per write is the difference between free and visible."""
        if getattr(self, "_summ_pending", False):
            return
        grid = getattr(self, "_scaf_grid", None)
        if grid is None:
            return
        self._summ_pending = True

        def _run():
            self._summ_pending = False
            g = getattr(self, "_scaf_grid", None)
            if g is None:
                return
            i = 0
            try:
                i = self._active_scaffold_index()
            except Exception:  # noqa: BLE001
                pass
            v = self.scaf_vars[min(i, len(self.scaf_vars) - 1)]
            for key, fn in (
                    ("geometry", lambda: self._summary_geometry(v)),
                    ("collector", self._summary_collector),
                    ("stabilization", self._summary_stabilization),
                    ("angle_pattern", lambda: self._summary_angle(v)),
                    ("quality", self._summary_quality),
                    ("footprint", lambda: self._summary_footprint(v)),
                    ("array", lambda: self._summary_array(v)),
                    ("linear", lambda: self._summary_linear(v)),
                    ("advanced", self._summary_advanced),
                    ("custom_cell", self._summary_custom_cell),
                    ("export", self._summary_export),
                    ("presets", self._summary_presets)):
                card = g.card(key)
                if card is None:
                    continue
                try:
                    card.set_summary(fn())
                except Exception:  # noqa: BLE001
                    # a cosmetic line must never take the page down
                    card.set_summary("")
            self._ds_refresh()

        try:
            self.root.after_idle(_run)
        except (tk.TclError, AttributeError):
            _run()

    def _install_summary_traces(self):
        """Watch every var a summary reads. Broad on purpose: the alternative is a
        per-module watch list that goes stale the moment a formatter is edited,
        and the refresh is coalesced anyway."""
        seen = set()

        def watch(var):
            if var is None or id(var) in seen:
                return
            seen.add(id(var))
            try:
                var.trace_add("write", self._refresh_module_summaries)
            except (AttributeError, tk.TclError):
                pass

        for sv in getattr(self, "scaf_vars", []):
            for var in sv.values():
                watch(var)
        for attr in ("s_collector", "s_make_stl", "s_exit_move", "s_exit_margin",
                     "s_g2g3", "stab_on", "stab_layers", "stab_stepover",
                     "m_diameter", "m_length", "m_family", "m_design_mode",
                     "m_speed_mode", "m_cell_w", "m_cell_h",
                     "pv_show_tubes", "pv_show_sag", "pv_show_fusion",
                     "pv_show_lag", "sim_physics_deposit"):
            watch(getattr(self, attr, None))

    # ------------------------------------------------------------------ #
    # the Design summary under the tiles
    # ------------------------------------------------------------------ #
    # Nothing here computes a design, and nothing waits for the preview.
    # The fiber diameter, stepover and layer count are the page's own vars. The
    # CTS verdict is taken when the preview snapshots an edit (`cts_now`), and
    # the flat plate's print time and fiber length come from the quick job
    # beside the preview (`gui_preview.design_stats`), so a large scaffold's
    # slow build delays neither. Only the plate check and the tube's time and
    # length are read off a finished build. Every number is in the Settings
    # panel's display unit.
    _DS_METRICS = (("time", "Print time"), ("length", "Fiber length"),
                   ("volume", "Deposited volume"), ("pore", "Pore size"),
                   ("height", "Scaffold height"), ("diameter", "Fiber diameter"))

    def _ds_refresh(self, *_a):
        panel = getattr(self, "_ds_panel", None)
        if panel is None:
            return
        try:
            mode = self._txt(self.s_collector, "plate")
            if mode not in ("plate", "mandrel"):
                panel.grid_remove()
                return
            panel.grid()
            self._ds_fill(panel, mode)
        except Exception:  # noqa: BLE001
            # a read-only readout must never take the page down
            pass

    @staticmethod
    def _ds_float(var):
        try:
            return float(str(var.get()).strip())
        except (ValueError, AttributeError, TypeError, tk.TclError):
            return None

    def _ds_fill(self, panel, mode):
        U = gui_units
        off = bool(getattr(self, "_pv_preview_disabled", lambda: False)())
        if mode == "mandrel":
            v = self.scaf_vars[0]
            state = getattr(self, "_mandrel_preview_state", None) or {}
            meta = state.get("meta") or {}
            # a refused or switched-off build has no g-code for these fields
            if off or state.get("below_cts") is not None:
                meta = {}
            stats = meta
            hint = "Tube"
        else:
            i = self._active_scaffold_index()
            v = self.scaf_vars[i]
            states = getattr(self, "_pv_states", None)
            state = states[i] if states else {}
            meta = {} if off else (state.get("meta") or {})
            stats = state.get("ds_stats") or {}
            hint = "Scaffold %d" % (i + 1)
            try:
                if bool(v["array_on"].get()):
                    hint += " · one array copy"
            except (KeyError, tk.TclError):
                pass
        panel.set_hint(hint)

        d = self._ds_float(v["diameter"])
        layers = self._ds_float(v["layers"])
        built = meta
        t = stats.get("print_time_s")
        length = stats.get("fiber_length_mm")
        panel.set_metric("time", U.readout("print_time", t, "s")
                         if t is not None else "")
        panel.set_metric("length", U.readout("fiber_len", length, "mm")
                         if length is not None else "")
        panel.set_metric("volume", U.readout(
            "volume", length * math.pi * d ** 2 / 4.0, "mm³")
            if length is not None and d else "")
        panel.set_metric("diameter", U.readout("fiber_d", d, "mm")
                         if d else "")

        pore = None
        notes, ok = [], []
        if mode == "mandrel":
            # the resolved helix's pore, as the Geometry card's own readout
            # takes it (gui_mandrel._mandrel_render_helix); the unit-cell
            # families have no single pore, so they show none
            area = getattr(built.get("design"), "pore_area", None)
            pore = math.sqrt(area) if area else None
            panel.set_metric("height", U.readout("pore", layers * d, "mm")
                             if d and layers else "", label="Wall thickness")
            panel.set_metric("pore", U.readout("pore", pore, "mm")
                             if pore else "")
        else:
            step = self._ds_float(v["stepover"])
            pore = step - d if step is not None and d else None
            panel.set_metric("height", U.readout("pore", layers * d, "mm")
                             if d and layers else "", label="Scaffold height")
            panel.set_metric("pore", "" if pore is None else
                             "Closed" if pore <= 0 else
                             U.readout("pore", pore, "mm"))
            if pore is not None:
                if pore <= 0:
                    notes.append(("warn", "The fiber spacing is no wider than the "
                                          "fiber, so the pores close."))
                else:
                    ok.append("Pores stay open.")
            fits = built.get("off_plate")
            if fits is True:
                notes.append(("warn", "Part of the print falls off the build "
                                      "plate."))
            elif fits is False:
                ok.append("Fits the plate.")

        cts = state.get("cts_now")
        if cts is not None:
            spd, crit = cts
            if spd < crit:
                notes.append(("warn", "Print speed %s is below the critical "
                              "translation speed (%s). The jet will coil "
                              "instead of laying a straight fiber."
                              % (U.readout("speed", spd * 60.0, "mm/min"),
                                 U.readout("speed", crit * 60.0, "mm/min"))))
            else:
                ok.append("Print speed clears the critical translation speed.")
        if ok:
            notes.append(("ok", " ".join(ok)))
        panel.set_notes(notes)

    # -- the scaffold-selector seam (the pill rail answers) ---------------- #
    def _show_scaffold_frame(self, i):
        """PillRail callback: raise scaffold i's frame inside every per-scaffold
        module, and re-point each of those cards' badges at scaffold i's own
        status var. Identity is preserved -- the other frames are grid_remove'd,
        never destroyed.

        **NO paint fence, and that is the fix for a BLACK FLASH.** A fence here
        was measured to turn the whole module grid black for a frame on every
        single swap: photographing just the grid at ~120 fps, the darkest frame
        after the click came back at 116.8 against the page's own 247 -- a 53%
        drop, in 5 of 5 reps -- and with the fence off, 0 of 5 and no drop at
        all (the probe's control, a deliberate one-paint-cycle flash, was
        caught 3 times in 3). Compositing
        presents the redirection surface before the background has been drawn
        into it. Seen on screen as a black flash before the fields appear."""
        for card in getattr(self, "_scaf_module_cards", []):
            for j, f in enumerate(getattr(card, "_scaf_frames", [])):
                if j == i:
                    f.grid()
                else:
                    f.grid_remove()
            key = getattr(card, "_status_key", None)
            if key:
                try:
                    card.bind_status(self.scaf_vars[i][key])
                except (KeyError, tk.TclError):
                    pass
        # the summaries describe the SELECTED scaffold, so they move with it
        if hasattr(self, "_refresh_module_summaries"):
            self._refresh_module_summaries()

    def _scaf_card_frame(self, card, i):
        """Card `card`'s frame for scaffold i, built hidden."""
        builder, wants_index = card._scaf_builder
        f = ttk.Frame(card.body)
        f.grid(row=0, column=0, sticky="new")
        # The spare width goes to column 3, gui_form's empty stretch
        # column. Weighting column 0 -- the label column of every Form
        # gridded straight into `f` -- pushed those cards' fields 100 px
        # right of Geometry's.
        f.columnconfigure(3, weight=1)
        if wants_index:
            builder(f, self.scaf_vars[i], i)
        else:
            builder(f, self.scaf_vars[i])
        card._scaf_frames.append(f)
        f.grid_remove()

    def _scaf_slot_built(self, i):
        """A scaffold past the first five: its frame on every module card and
        its pill, then what every shell does (`gui_scaffold`)."""
        for card in getattr(self, "_scaf_module_cards", []):
            if len(card._scaf_frames) <= i:
                self._scaf_card_frame(card, i)
        rail = getattr(self, "_scaf_rail", None)
        if rail is not None:
            rail.add_pill()
        super()._scaf_slot_built(i)

    def _scaffold_selector_index(self):
        return self._scaf_rail.index()

    def _scaffold_selector_show(self, n):
        self._scaf_rail.show_n(n)
        self._show_scaffold_frame(self._scaf_rail.index())

    # ------------------------------------------------------------------ #
    # the Physics page's module grid
    # ------------------------------------------------------------------ #
    # The off-by-default fitting hooks the page does NOT render. Every one is a
    # zero exponent (or the reference a zero exponent pivots on), so the law it
    # belongs to asserts nothing with the hook left alone -- which is exactly why
    # none of them has literature backing to show a user. They stay in
    # CALIBRATION_FIELDS, in the Calibration dataclass and in the laws, so the
    # persist map, every saved preset, Lite's mirrored field table and the
    # classic developer UI's "Rig & material" tab are all untouched; a user who
    # measures the dependence still has somewhere to enter it, and the engine
    # still computes with it. This tuple is the ONLY thing that hides them, and
    # it hides them from the modern Physics page alone.
    #
    # Do NOT "simplify" this into a group filter. `advanced` is not the same set:
    # sag_span_exp (2.0, the viscous catenary), every lag_* constant (Hrynevich
    # 2020 Eq. 4) and fusion_max_overlap (0.9, a clamp) are all `advanced` AND
    # all fitted or grounded, so they are rendered. The one `advanced` field that
    # is NOT rendered for a different reason is contact_damping_zeta -- see
    # _PHYS_RETIRED below, which is a separate set on purpose.
    _PHYS_HIDDEN_HOOKS = frozenset({
        # CTS power-law scaling: CTS = cts_ref * (V/V_ref)^a * (gap/gap_ref)^b
        #                              * (Q/Q_ref)^c * (eta/eta_ref)^d
        "cts_voltage_ref", "cts_voltage_exp", "cts_gap_ref", "cts_gap_exp",
        "cts_flow_ref", "cts_flow_exp", "cts_visc_ref", "cts_visc_exp",
        # the electrostatic tip-enhancement term (0.0 = the uniform plate field)
        "field_tip_strength",
        # sag's diameter and thermal hooks (the span exponent is NOT here)
        "sag_diam_exp", "sag_thermal_visc_exp", "sag_temp_ref",
        # fusion's diameter and thermal hooks (the clamp is NOT here)
        "fusion_diam_exp", "fusion_diam_ref", "fusion_thermal_visc_exp",
        "fusion_temp_ref",
    })

    # RETIRED, which is a different thing from hidden: a hidden hook is off but
    # LIVE -- set its exponent and the law changes. These reach no geometry at
    # all, so offering them is not conservative, it is wrong.
    #
    # `contact_damping_zeta` damped `integrate_contact_path_2nd`, the second-order
    # contact filter that `integrate_contact_path_ray` replaced.
    # The shipped tracker has no damping term -- `integrate_contact_path_ray`
    # (mew_physics.py:1173) takes lag_ceiling / lag_floor / saturation_exp and
    # nothing else -- and `mew_deposition.laid_fibers` states in its own docstring
    # that zeta is "accepted, ignored, reaching no geometry", kept only so old
    # profiles and `mew_export --zeta` keep loading. A spinbox for it told the
    # user the number did something.
    #
    # Same treatment as the hooks and for the same reason: NOT rendered here, and
    # left everywhere else -- the registry, the dataclass, every saved preset,
    # Lite's mirrored field table (lite/lite_params.py hand-mirrors the registry
    # by hand) and the classic UI.
    # Deleting the FieldSpec is a separate, wider change.
    _PHYS_RETIRED = frozenset({"contact_damping_zeta"})

    def _phys_fields(self, *sections):
        """The registry fields of `sections`, in registry order, minus the hidden
        hooks and the retired knobs. Filtered by SECTION rather than group,
        because the page is now one card per physical law and each card carries
        its own measured and fitted fields together."""
        import dataclasses
        import mew_calibration
        want = set(sections)
        drop = self._PHYS_HIDDEN_HOOKS | self._PHYS_RETIRED
        labels = {
            "inner_radius": "Inner diameter, D",
            "length": "Needle length, L",
            "temperature_ref": "Anchor temperature, T_ref",
            "viscosity_ref": "Viscosity at anchor, η_ref",
            "activation_energy": "Temperature sensitivity, Ea",
            "cts_ref": "Measured CTS",
            "lag_amplitude": "Lag limit, A",
            "lag_amplitude_gap": "Gap A was measured at",
            "lag_angle_cts_deg": "Lag angle at CTS",
            "lag_saturation_exp": "Lag climb rate, b",
            "stable_window": "Stable window ends at",
            "sag_coeff": "Depth of a 1 mm span, c",
            "sag_span_exp": "Span exponent, p",
            "fusion_max_overlap": "Sink limit, φ_max",
            "cone_knee_frac": "Cone knee height",
        }
        units = {"sag_coeff": "mm", "lag_amplitude": "× gap",
                 "stable_window": "× CTS", "cone_knee_frac": "× gap"}
        return [dataclasses.replace(
                    fs, label=labels.get(fs.attr, fs.label),
                    unit=units.get(fs.attr, fs.unit),
                    desc=self._PHYS_DESCS.get(fs.shown_as or fs.attr,
                                              fs.desc))
                for fs in mew_calibration.CALIBRATION_FIELDS
                if fs.section in want and fs.attr not in drop]

    # Plain-words hover help for the modern Physics cards, replacing the
    # registry's developer one-liners where those lead with derivation rather
    # than with what the number does. The registry text stays for the classic
    # UI and Lite.
    _PHYS_DESCS = {
        "density": ("Melt mass per unit volume. The default laws do not use "
                    "this value; a custom law can use it."),
        "sag_coeff": (
            "How deep every span sags. c is the depth, in mm, of a span 1 mm "
            "long; twice c makes every span twice as deep.\n\n"
            "The Sag multiplier on the Scaffold designer page scales it per "
            "scaffold. The default value is a visual match to one print, not "
            "a fit to sag measurements: measure yours with Measure sag."),
        "sag_span_exp": (
            "How much more a long span sags than a short one. Twice the span "
            "gives 2^p times the depth: 4 times at p = 2.\n\n"
            "2 is the viscous catenary, the physics of a hot fiber hanging "
            "between two supports, so leave it unless your own sag "
            "measurements say otherwise."),
        "inner_diameter": (
            "The inner diameter of the needle, the hole the melt comes out "
            "of. Flow goes as its fourth power, so a small error here is a "
            "large error in flow: measure yours (a 21 G regular-wall needle "
            "is 0.514 mm). The flow law uses half of it, the radius r."),
        "length": (
            "The length of the narrow metal needle the melt is pushed "
            "through, not the syringe barrel. A longer needle lets less melt "
            "through."),
        "temperature_ref": (
            "The melt temperature of the run where you measured η_ref. The "
            "viscosity at every other temperature is worked out from here."),
        "viscosity_ref": (
            "How thick the melt is at the anchor temperature. Fit it so the "
            "model reproduces one fiber diameter you measured."),
        "activation_energy": (
            "How fast the melt thins as it gets hotter: the larger Ea, the "
            "more a change in temperature changes the flow. 31300 is neat "
            "PCL; fit yours from a temperature sweep."),
        "cts_ref": (
            "The slowest print speed that still lays a straight fiber, "
            "measured on your printer. The most important number on this page "
            "to measure."),
        "lag_amplitude": (
            "The most the fiber can trail the nozzle, as a multiple of the "
            "gap: at high speed the lag levels off at A × gap.\n\n"
            "The number to fit for your printer. 2.8629 is Hrynevich 2020's "
            "own value."),
        "lag_amplitude_gap": (
            "The gap the lag limit A was measured at. A record only: nothing "
            "reads it, but an A means nothing without it."),
        "lag_angle_cts_deg": (
            "The lag right at CTS, as an angle. 0 means the jet hangs "
            "straight down at CTS. Change it only if your own sweep shows a "
            "lag there."),
        "lag_saturation_exp": (
            "How quickly the lag climbs to its limit as the speed rises past "
            "CTS. Larger b gets there sooner.\n\n"
            "Hrynevich's own value. Leave it fixed while you fit A: the "
            "calibration prints cannot tell b apart from a shifted CTS."),
        "stable_window": (
            "The speed, as a multiple of CTS, above which a print is marked "
            "as overstretched. A label only: it never changes the fiber."),
        "cone_knee_frac": (
            "How tall the drawn Taylor cone looks: the height below the "
            "nozzle where it has narrowed to half its width, as a fraction "
            "of the gap. Drawing only: it never changes the fiber."),
    }

    # The mandrel block's typeset laws, one row each (the paper's Eqs. 4 and 7).
    # R_ax is the planar law with the SURFACE speed in epsilon; R_cir is the
    # exact arc -- NOT r/(r+H), which was its superseded small-angle form. The
    # mandrel radius is r_m because `r` is the needle bore everywhere else on
    # this page (`mew_lawlang` law `rcir`).
    _MANDREL_LAW_ROWS = (
        (None, r"R_{ax} = H\, A \left( 1 - \varepsilon^{-b} \right)"),
        (None, r"\varepsilon = \frac{v_{surf}}{CTS}"),
        ("rcir", r"R_{cir} = r_{m} \left( \theta - \arcsin\left( "
                 r"\frac{r_{m} \sin \theta}{r_{m} + H} \right) \right)"),
    )

    def _phys_mandrel_lag(self, parent, card=None):
        """The Jet lag card's MANDREL block: what the 4-axis placement runs on.

        The two mandrel laws typeset (`_MANDREL_LAW_ROWS`), four rows bound to
        the SAME vars the Scaffold designer's calibrated placement and the
        Calibration page's two mandrel Applies use
        (`gui_mandrel._MANDREL_CAL_VARS`) -- one state, shown here too -- and a
        live line saying the values IN USE at the designer's surface speed and
        where each comes from: calibrated (from a tube or a mandrel CTS sweep),
        the user's own R_cir law, or the default (R_ax from the planar lag law,
        R_cir from the default law, the flat CTS). None of these reaches a flat
        print (`mew_lag_fit.mandrel_calibration`).

        With `card`, the R_cir law also gets its "Try a different form" panel
        in the card's law host. R_ax does not: it IS the planar lag, whose
        closed-form inverse the tracker integrates."""
        if not hasattr(self, "_mandrel_vars_make"):
            return
        self._mandrel_vars_make()
        cols, rows = parent.grid_size()
        F = gui_form.Form(parent, row=rows)
        F.section("Mandrel (4-axis)")
        strip = gui_math.MathStrip(
            parent, r" \\ ".join(m for _k, m in self._MANDREL_LAW_ROWS))
        F.slot(None, strip)
        self._phys_mandrel_strip = strip
        gui_tips.add_tip(
            strip,
            "R_ax is the planar lag law above, with ε taken from the speed of "
            "the surface under the nozzle rather than the commanded feed. "
            "R_cir is the circumferential lag: how far round the mandrel the "
            "fibre lands behind the nozzle, as an arc on the surface. The "
            "default law assumes the jet leans by the same angle θ from the "
            "mandrel's surface normal as it does from a flat collector, where "
            "tan θ = R_ax / H, and follows that leaning jet down to the curved "
            "surface. r_m is the mandrel radius. "
            "A measured pair from a "
            "calibration tube overrides both.")
        F.entry("Mandrel CTS", self.m_cts,
                "The CTS measured on the mandrel. Calibrate's “CTS (mandrel)” "
                "type writes it here, converted from mm/min. Blank uses the "
                "flat CTS above. Only a mandrel print reads it.", unit="mm/s")
        F.entry("Mandrel R_ax", self.m_lag_axial,
                "The axial lag radius measured on a calibration tube. Calibrate's "
                "“Jet lag (mandrel)” type writes it here. Blank uses the "
                "plate lag law. Only a mandrel print reads it.", unit="mm")
        F.entry("Mandrel R_cir", self.m_lag_circ,
                "The circumferential lag radius from the same tube. Blank uses "
                "the R_cir law above (the default, or your own form). Held at "
                "its fraction of R_ax at every speed.", unit="mm")
        F.entry("Radii measured at", self.m_lag_speed,
                "The surface speed the two radii were measured at, written by "
                "the same Apply. With it, R_ax follows the lag law to other "
                "speeds from that point. Blank, only their ratio is used and "
                "the size comes from the flat amplitude A.", unit="mm/s")
        note = tk.StringVar(value="")
        self._phys_mandrel_note = note
        F.live(None, textvariable=note, wrap=420)
        if card is not None:
            self._phys_mandrel_law_editor(card)
        import mew_calibration
        watched = [self.m_cts, self.m_lag_axial, self.m_lag_circ,
                   self.m_lag_speed, getattr(self, "m_diameter", None),
                   getattr(self, "sim_standoff", None),
                   getattr(self, "cal_vars", {}).get(
                       mew_calibration.LAW_SOURCE_KEY)]
        try:
            watched.append(self.scaf_vars[0]["speed"])
        except (AttributeError, IndexError, KeyError):
            pass
        for var in watched:
            if var is not None:
                var.trace_add("write",
                              lambda *_a: self._phys_mandrel_refresh())
        self._phys_mandrel_refresh()

    def _phys_mandrel_law_editor(self, card):
        """The R_cir law's panel, in the Jet lag card's law host.

        The card's own strip draws the PLANAR lag law, which stays uneditable,
        so this panel re-typesets the mandrel block's strip instead -- and only
        its R_cir row, the other rows being fixed (`_MANDREL_LAW_ROWS`). Same
        recomposition as `_phys_law_editors_multi`, over rows that are not all
        editable."""
        disc = gui_lawedit.build(self, card._law_host, "rcir", named=True,
                                 on_applied=self._phys_mandrel_recompose)
        if disc is None:
            return None
        disc.grid(row=0, column=0, sticky="ew")
        self._phys_law_editors = getattr(self, "_phys_law_editors", {})
        self._phys_law_editors["rcir"] = disc
        self._phys_mandrel_recompose()
        return disc

    def _phys_mandrel_recompose(self):
        """Redraw the mandrel strip from the rows, putting the user's R_cir law
        in place of the default R_cir law when one is applied. Called on Apply / Reset and
        whenever the law var changes (a preset load)."""
        strip = getattr(self, "_phys_mandrel_strip", None)
        if strip is None:
            return
        parts = []
        for key, shipped in self._MANDREL_LAW_ROWS:
            src = gui_lawedit.law_source(self, key) if key else ""
            markup = (gui_math.from_expression(src, gui_lawedit.symbol(key))
                      if src else None)
            parts.append(markup or shipped)
        try:
            strip.label.set_source(r" \\ ".join(parts))
        except (AttributeError, tk.TclError):
            pass

    def _phys_mandrel_refresh(self):
        """Recompute the mandrel block's in-use line. Never raises: a half-typed
        field says so on the line instead."""
        self._phys_mandrel_recompose()
        note = getattr(self, "_phys_mandrel_note", None)
        if note is None:
            return
        try:
            import dataclasses
            import mew_lag_fit
            from mew_physics import Calibration, MEWModel
            speed = float(self.scaf_vars[0]["speed"].get()) / 60.0
            dia = float(self.m_diameter.get())
            gap = float(self.sim_standoff.get())
            models = self._calibration_models()
            cal = models[2] if models is not None else Calibration()
            extras = self._mandrel_cal_extras()
            cal = dataclasses.replace(cal, **extras)
            cts = float(extras.get("mandrel_cts") or cal.cts_ref)
            # the rig's own material and nozzle, so a user R_cir law that names
            # one of their constants sees the value the lay will see
            model = (MEWModel(material=models[0], nozzle=models[1],
                              working_distance=gap, calibration=cal)
                     if models is not None else None)
            R_ax, R_cir = mew_lag_fit.predict_radii(
                radius=0.5 * dia, gap=gap, speed=speed, cts=cts,
                calibration=cal, model=model)
        except Exception:  # noqa: BLE001 -- say so, never raise from a trace
            note.set("In use: cannot compute yet (check the mandrel diameter, "
                     "the designer's speed and the gap, or your R_cir law).")
            return
        # One line per value, each saying CALIBRATED (and on which tab) or
        # DEFAULT (and what the default is), in the tabs' own words.
        pair = bool(extras.get("mandrel_lag_axial")
                    and extras.get("mandrel_lag_circ"))
        if pair and extras.get("mandrel_lag_speed"):
            src_ax = ("measured with Calibrate, “Jet lag (mandrel)”: %.3g mm at "
                      "%.3g mm/s, carried to this speed by the lag law"
                      % (extras["mandrel_lag_axial"],
                         extras["mandrel_lag_speed"]))
        elif pair:
            src_ax = ("default size from the flat amplitude A; the measured "
                      "pair has no speed, so only its ratio is used")
        else:
            src_ax = "default: the planar lag law"
        if pair:
            src_cir = ("measured with Calibrate, “Jet lag (mandrel)”, at %.3g of "
                       "R_ax" % (extras["mandrel_lag_circ"]
                                 / extras["mandrel_lag_axial"]))
        elif gui_lawedit.law_source(self, "rcir"):
            src_cir = "your own R_cir law"
        else:
            src_cir = "the default R_cir law"
        src_cts = ("measured with Calibrate, “CTS (mandrel)”"
                   if extras.get("mandrel_cts")
                   else "default: the flat CTS, as no mandrel CTS is measured")
        note.set("A 4-axis print at the Scaffold designer's %.4g mm/min uses:\n"
                 "• R_ax %.3f mm, %s.\n"
                 "• R_cir %.3f mm, %s.\n"
                 "• CTS %.4g mm/min (%.3g mm/s), %s."
                 % (speed * 60.0, R_ax, src_ax, R_cir, src_cir,
                    cts * 60.0, cts, src_cts))

    def _phys_card(self, grid, key, title, law=None, info=None, wide=False):
        """Register one Physics-page module card and lay its body out the same way
        every time, so nine cards cannot each invent a shape.

        Returns `(card, head, fields, footer)`:
          * `law`   -- the law this module implements, typeset by `gui_math` in
                       a tinted strip above everything. `gui_math`'s own markup
                       (a small TeX subset), NOT a pre-spelled string: the
                       renderer owns the italics, the raised exponents, the
                       division bars and the operator spacing, so two cards
                       cannot disagree about how a law is written.
          * `head`  -- the module's ON/OFF, when it has one. A frame either way, so
                       a builder that runs LATER (every sim.* switch does) has a
                       parent waiting for it.
          * `fields`-- where `_render_cal_fields` puts the parameters.
          * `footer`-- the Calibrate... jumps and any extra switches.
        """
        card = grid.add(key, title, info=info, wide=wide)
        body = card.body
        body.columnconfigure(0, weight=1)
        r = 0
        card._law_strip = None
        card._law_shipped_markup = law
        if law:
            # A full-width strip with its own air, not a bare line: the law was a
            # 9-px monospace label sitting directly under the prose blurb at the
            # same weight, and it read as a third sentence rather than as the
            # law. `gui_math` does the typesetting inside it, and the strip is
            # re-sourced in place when the user applies a law of their own
            # (gui_lawedit.retypeset), so an edited card stays typeset.
            card._law_strip = gui_math.MathStrip(body, law)
            card._law_strip.grid(row=r, column=0, columnspan=2, sticky="w",
                                 pady=(0, gui_fonts.SP["m"]))
            r += 1
        head = ttk.Frame(body)
        head.grid(row=r, column=0, sticky="ew")
        fields = ttk.Frame(body)
        fields.grid(row=r + 1, column=0, sticky="ew")
        footer = ttk.Frame(body)
        footer.grid(row=r + 2, column=0, sticky="w",
                    pady=(gui_fonts.GAP_SECTION, 0))
        # Where `gui_lawedit` hangs the "Try a different form" panel, below the
        # Calibrate jumps. A frame either way, so the four editable cards and the
        # six that are not laid out identically.
        card._law_host = ttk.Frame(body)
        card._law_host.grid(row=r + 3, column=0, sticky="ew",
                            pady=(gui_fonts.GAP_SECTION, 0))
        card._law_host.columnconfigure(0, weight=1)
        # The card's picture, in column 1 beside everything above (or under it
        # on a narrow window); `gui_physics_diagrams` owns the sizing.
        card._figure = gui_physics_diagrams.mount(body, key, r, r + 3, r + 4)
        return card, head, fields, footer

    def _phys_law_editor(self, card, law_key):
        """Hang `gui_lawedit`'s panel on a card, wired to that card's typeset
        strip so an applied law re-renders in place instead of leaving the
        SHIPPED equation on display above a law that is no longer running.

        Silent no-op when the physics modules are absent, like every other
        physics builder in this shell."""
        disc = gui_lawedit.build(
            self, card._law_host, law_key, strip=card._law_strip,
            shipped_markup=card._law_shipped_markup)
        if disc is not None:
            disc.grid(row=0, column=0, sticky="ew")
            self._phys_law_editors = getattr(self, "_phys_law_editors", {})
            self._phys_law_editors[law_key] = disc
        return disc

    def _phys_law_editors_multi(self, card, rows):
        """Two or more editors on ONE card, for a card whose typeset strip draws a
        law per row (the Material card: Arrhenius over Hagen-Poiseuille).

        The strip is a single multi-row drawing, so the per-editor retypeset would
        wipe the rows it does not own. `rows` is [(law_key, that row's shipped
        markup), ...] and the recomposition below redraws EVERY row from whichever
        of them the user has overridden -- which is also why each panel is named
        after the symbol it produces."""
        def recompose():
            parts = []
            for key, shipped in rows:
                src = gui_lawedit.law_source(self, key)
                markup = (gui_math.from_expression(src, gui_lawedit.symbol(key))
                          if src else None)
                parts.append(markup or shipped)
            try:
                card._law_strip.label.set_source(r" \\ ".join(parts))
            except (AttributeError, tk.TclError):
                pass

        self._phys_law_editors = getattr(self, "_phys_law_editors", {})
        host = card._law_host
        for i, (key, _markup) in enumerate(rows):
            disc = gui_lawedit.build(self, host, key, named=True,
                                     on_applied=recompose)
            if disc is None:
                continue
            disc.grid(row=i, column=0, sticky="ew",
                      pady=(0 if i == 0 else gui_fonts.GAP_SECTION, 0))
            self._phys_law_editors[key] = disc
        card._law_rows = list(rows)

    def _phys_calibrate_button(self, parent, procedure, label):
        """A card's own jump to the Calibration page, pre-selected on the procedure
        that MEASURES this module's value. The generic 'Calibrate...' button this
        replaces landed on whichever card the page happened to be showing."""
        ttk.Button(parent, text=label,
                   command=lambda p=procedure: self._goto_calibration_page(p)).pack(
            side="left", padx=(0, gui_fonts.SP["s"]))

    def _phys_reset_button(self, parent, title, sections=(), laws=()):
        """A card's own 'Reset to defaults', beside its Calibrate jumps.

        The Advanced card's reset puts the WHOLE page back, which is the only
        thing that existed: a rig that wanted its sag coefficient back had to
        throw away its fitted viscosity with it. This restores one law's
        constants and, if the card lets you replace its law, that law too.

        WHAT IT RESTORES, and why it is not the widgets on screen. Every
        registry field in the card's `sections`, including the ones
        `_phys_fields` hides -- the folded-away zero-exponent hooks. A preset can
        set those, and a button that says "defaults" while a hook from the last
        preset is still multiplying the answer would be lying about the card.
        What it does NOT touch is the module's ON/OFF and the machine values
        (the Visualisation card's motion limits belong to the MACHINE preset),
        because those are not calibration.
        """
        import mew_calibration

        def reset():
            want = set(sections)
            n = 0
            done = []
            for fs in mew_calibration.CALIBRATION_FIELDS:
                if fs.section not in want:
                    continue
                var = self.cal_vars.get(fs.key)
                if var is None:
                    continue
                done.append(fs.key)
                # the shipped preset's value, the one the app boots with --
                # not the registry's, which describes a different needle
                var.set(self._cal_default_text(fs))
                n += 1
                if fs.gate_label:
                    gate = self.cal_vars.get(fs.gate_key)
                    if gate is not None:
                        gate.set(self._cal_gate_default(fs))
            # A user-replaced law is a calibrated value like any other, so the
            # card's reset has to hand it back too -- `_law_reset` is the panel's
            # own button, which also re-seeds its box and re-typesets the strip.
            for law_key in laws:
                disc = getattr(self, "_phys_law_editors", {}).get(law_key)
                fn = getattr(disc, "_law_reset", None)
                if fn is not None:
                    fn()
            unmark = getattr(self, "_cal_unmark", None)
            if callable(unmark):
                unmark(done)
            self._after_calibration_preset_applied()
            self.status.set("%s: %d value(s) back to the defaults."
                            % (title, n))

        ttk.Button(parent, text="Reset to defaults", command=reset).pack(
            side="left", padx=(0, gui_fonts.SP["s"]))
        return reset

    def _build_calibration_section(self, grid):
        """The Physics page below its title: one `ModuleCard` per physical law, in
        the shared `ModuleGrid` the page owns.

        `grid` already carries the "process" card (built by `gui_shell`, so that the
        per-print knobs read first); this registers the rest in physical reading
        order -- what the jet is (CTS, material), what it does on the way down (lag),
        what it does once it lands (sag, fusion), what comes out (diameter), then the
        two non-law cards and Presets.

        Three of these cards are filled by the printhead-sim hook, which runs LATER
        (it needs `self.scaf_vars`), so their hosts are stashed in `_sim_hosts` and
        `gui_shell._build_sim_tab` hands them to `_populate_sim_controls`. That is the
        same arrangement the single `_sim_host` had before the grid existed.

        Builds nothing but the Process card when the physics modules are missing,
        exactly like the classic hook."""
        if not self._init_calibration_vars():
            return
        self._phys_grid = grid
        hosts = self._sim_hosts = getattr(self, "_sim_hosts", {})

        # -- CRITICAL TRANSLATION SPEED ---------------------------------- #
        # One field, and that is the finding rather than an omission: CTS is a
        # measured per-session anchor. The scaling hooks that could make voltage,
        # gap, flow or viscosity move it are all at exponent 0 and hidden.
        card, _h, fields, footer = self._phys_card(
            grid, "cts", "Critical translation speed",
            law=r"CTS = \frac{4Q}{\pi d_{CTS}^{2}}",
            info=gui_tips.pic("cts", "The slowest stage speed that still lays "
                  "a straight fiber. Slower than this, the fiber coils.\n\n"
                  "Measure it on your printer each session: the model does not "
                  "predict it. By mass balance CTS = 4Q/(π·d_CTS²), so "
                  "predicting it would mean predicting the diameter where the "
                  "jet lands, and no formula gives that.\n\n"
                  "Print speed is set per scaffold under Parameters on the "
                  "Design page. Below CTS the straight-fiber model no longer holds, "
                  "and the Coiling wave card marks where."))
        self._render_cal_fields(fields, self._phys_fields(
            "Critical translation speed"), headers=False)
        # one jump per CTS tab, as the Jet lag card has one per lag tab; the
        # mandrel's CTS is shown and edited on the Jet lag card's mandrel block
        self._phys_calibrate_button(footer, "cts", "Measure on a plate…")
        self._phys_calibrate_button(footer, "cts4", "Measure on a mandrel…")
        self._phys_reset_button(footer, "Critical translation speed",
                                ("Critical translation speed",), ("cts",))
        self._phys_law_editor(card, "cts")

        # -- MATERIAL + NOZZLE ------------------------------------------- #
        # The CONSTANTS of the melt and the bore: eta_ref, Ea, T_ref, r, L. One
        # card, not two, because only the lumped group pi*r^4/(8*eta_ref*L) is
        # identifiable from flow data -- r, L and eta_ref trade off, so splitting
        # them across two cards would invite fitting one without the other.
        #
        # ONE law is typeset here: eta(T). The FLOW law lives on the Fiber
        # diameter card, where its output Q is consumed -- this card carries r
        # and L, but a reader looking for "what sets my fibre thickness" should
        # not be sent to a card called Material and machine parameters.
        _eta_row = (r"\eta(T) = \eta_{ref}\, \exp\left[ \frac{E_a}{R}"
                    r"\left( \frac{1}{T} - \frac{1}{T_{ref}} \right) \right]")
        _flow_row = r"Q = \frac{\pi r^{4} \Delta P}{8\, \eta(T)\, L}"
        _diam_row = r"d = \sqrt{ \frac{4Q}{\pi v} }"
        card, _h, fields, footer = self._phys_card(
            grid, "material", "Material and printer parameters",
            law=_eta_row,
            info=("Your melt and your nozzle: the numbers the flow and "
                  "diameter laws are built from.\n\n"
                  "D is the needle's inner diameter and L its length; "
                  "the flow law uses the radius r = D/2. η_ref is the melt's "
                  "viscosity at the anchor temperature T_ref, and Ea is how "
                  "fast that viscosity falls as the melt gets hotter.\n\n"
                  "Ea alone sets how temperature changes the print: an error "
                  "in η_ref does not change it. Measure Ea on your own polymer "
                  "before you claim a temperature effect.\n\n"
                  "Flow data pins down only the combination πr⁴/(8·η_ref·L): "
                  "r, L and η_ref trade off against each other.\n\n"
                  "The flow law these feed is on the Fiber diameter card."))
        self._render_cal_fields(fields, self._phys_fields(
            "Nozzle (measured)", "Material (melt)"))
        # The JET, in the same grid so its entries line up with the bore's: the
        # drawn Taylor cone's switch (built into the reserved row by
        # gui_sim._sim_cone_field, from `hosts["jet"]`), its knee height, and
        # the base, which is not a setting -- the needle's inner diameter
        # pins it
        # (mew_physics JetState.cone_base_radius). The path of the drawn jet
        # has no setting either: its catenary is solved from the gap and the
        # lag (gcode_player._jet_arc_points). The tip-field hook stays hidden.
        # The polymer's NAME: a label the g-code header carries ([[polymer]]);
        # it changes no physics. It sits with eta_ref and Ea because it names
        # the melt they describe.
        if getattr(self, "sim_polymer", None) is None:
            self.sim_polymer = tk.StringVar(value="")   # gui_sim keeps it
        P = gui_form.Form(fields, row=fields.grid_size()[1])
        P.section("Polymer", first=False)
        P.entry("Polymer name", self.sim_polymer,
                "What the material is called, e.g. PCL. It is written into "
                "the g-code header ([[polymer]]) and Print History. It does "
                "not change the physics: the melt constants above do.",
                width="text")
        J = gui_form.Form(fields, row=fields.grid_size()[1])
        J.section("Jet", first=False)
        self._phys_jet_row = J.row
        J.row += 1
        for fs in self._phys_fields("Taylor cone (render only)"):
            J.entry(fs.label, self.cal_vars[fs.key], fs.desc, unit=fs.unit)
        J.live("Cone base", text="the needle's inner diameter D, above",
               tip="The drawn cone starts as wide as the hole in the needle, "
                   "so its base is the needle's inner diameter D. It is not "
                   "set separately.")
        hosts["jet"] = fields
        self._phys_calibrate_button(footer, "viscosity", "Measure η…")
        self._phys_calibrate_button(footer, "activation_energy", "Measure Ea…")
        self._phys_reset_button(footer, "Material and printer parameters",
                                ("Nozzle (measured)", "Material (melt)",
                                 "Taylor cone (render only)"),
                                ("viscosity",))
        self._phys_law_editor(card, "viscosity")

        # -- JET LAG ------------------------------------------------------ #
        # The law's generation/export ON/OFF goes in `head`, built later by
        # gui_sim._sim_lag_switch -- the Fiber-fusion card's arrangement.
        card, head, fields, footer = self._phys_card(
            grid, "lag", "Jet lag",
            # The law as PUBLISHED, in LAG DISTANCE (mm), which is what the
            # tracker and `lag_ray_constants` actually work in -- H is in it,
            # because the lag scales with the working distance. It was typeset
            # as a chord ANGLE led by `k`, which put an internal per-rig
            # multiplier at the head of the equation and dropped H entirely.
            # A and b are the fields below; A is now the STORED amplitude
            # rather than a tangent of a stored angle, so the letter the strip
            # prints and the letter the entry is labelled with are the same one.
            law=(r"lag = H\, A \left( 1 - \varepsilon^{-b} \right)"
                 r" \\ "
                 r"\varepsilon = \frac{v}{CTS} \ge 1"),
            info=gui_tips.pic("jet_lag", "The jet trails the moving nozzle, "
                  "so the fiber lands behind the head and every corner comes "
                  "out rounded.\n\n"
                  "ε is the print speed over CTS. The lag grows with ε and "
                  "levels off at A times the gap H, so it never runs away; b "
                  "sets how quickly it gets there. This is Hrynevich 2020's "
                  "Eq. 4, in mm.\n\n"
                  "A is the number to measure on your printer. The default 2.8629 "
                  "is Hrynevich's own 10.02/3.5, so a fitted A compares "
                  "directly with the published one. The gap A was measured at "
                  "is kept beside it, because A means nothing without it.\n\n"
                  "The lag angle at CTS is a separate floor, for printers whose "
                  "jet is not vertical at CTS. Below CTS the lag stays at that "
                  "floor.\n\n"
                  "The exported fiber and the live deposition both use this "
                  "law's exact inverse, so its form is fixed while its numbers "
                  "are yours."))
        hosts["lag"] = head
        self._phys_lag_card = card
        self._render_cal_fields(fields, self._phys_fields("Jet lag"),
                                headers=False)
        self._phys_mandrel_lag(fields, card)
        self._phys_calibrate_button(footer, "lag3", "Measure on a plate…")
        self._phys_calibrate_button(footer, "lag4", "Measure on a mandrel…")
        # The planar lag has no expression editor; the mandrel's R_cir does, so
        # the card's reset hands that law back too.
        self._phys_reset_button(footer, "Jet lag", ("Jet lag",), ("rcir",))
        # The one law with NO expression editor, and the card says so rather than
        # leaving a user hunting for it. `mew_lawlang.LAWS` has no "lag" entry and
        # it must stay that way; the reason is in that module's docstring and repeated here in the
        # words a user needs.
        # A tooltip on the typeset law, not a grey line under the card: it is
        # help about the law, and the one-pattern rule puts help on hover.
        gui_tips.add_tip(
            card._law_strip,
            "No ‘Try a different form’ for this law: the exported centerline "
            "and the live deposition integrate its closed-form inverse rather "
            "than evaluating it, so a typed replacement would leave the STL "
            "following one law and this card showing another. The mandrel's "
            "R_cir law, further down, is evaluated directly and can be "
            "replaced.")

        # -- BRIDGE SAG --------------------------------------------------- #
        # ON/OFF in `head` (gui_sim._sim_sag_switch), as on the Jet lag card.
        card, head, fields, footer = self._phys_card(
            grid, "sag", "Sagging",
            # Written exactly as `gui_math.from_expression` renders the law's own
            # seeded text `c * span**p`, so applying it unchanged cannot move a
            # pixel. The card and the box are the same alphabet now
            # (`mew_lawlang.SYMBOLS`), and this is where that has to be visible.
            law=r"depth = c \cdot span^{p}",
            info=gui_tips.pic("sag", "How far a fiber sags between the two crossings that hold "
                  "it up.\n\n"
                  "c is the depth of a 1 mm span, and the one number to set "
                  "for your printer. p says how much more a long span sags than a "
                  "short one. It is 2 from the physics of a hot fiber hanging "
                  "between supports, not from a fit.\n\n"
                  "The default c is a visual match to one print, not a fit to "
                  "sag measurements. Only spans with nothing beneath them sag "
                  "visibly."))
        hosts["sag"] = head
        self._phys_sag_card = card
        self._render_cal_fields(fields, self._phys_fields("Bridge sag"),
                                headers=False)
        self._phys_calibrate_button(footer, "sag", "Measure sag…")
        self._phys_reset_button(footer, "Sagging", ("Bridge sag",),
                                ("sag",))
        self._phys_law_editor(card, "sag")

        # -- FUSION ------------------------------------------------------- #
        # The ON/OFF goes in `head`, above the magnitudes it switches on; the
        # widget itself is built later by gui_sim._sim_fusion_switch, which is
        # also what the classic panel calls, so the two shells cannot word it
        # differently.
        card, head, fields, footer = self._phys_card(
            grid, "fusion", "Fusion",
            law=r"\Delta = \varphi\, d \quad \varphi \le \varphi_{max}",
            info=("A fiber laid on a still-soft one sinks into it instead of "
                  "stacking at full diameter, so a tall scaffold ends up "
                  "shorter than layers × diameter.\n\n"
                  "φ is how far one fiber sinks into the one below, as a "
                  "fraction of the fiber diameter: Δ = φd, and 0 is no sink. "
                  "φ_max caps it.\n\n"
                  "Straight spans, where fibers lie along each other, can "
                  "have their own sink; unticked, they use the junction "
                  "value. Spans with nothing under them never fuse.\n\n"
                  "The default φ was matched by eye to prints on an unheated "
                  "collector. Collector temperature is not in the model, so "
                  "do not carry φ to a heated collector or another printer."))
        hosts["fusion"] = head
        self._phys_fusion_card = card
        self._phys_fusion_input = gui_fusion_input.build(self, fields)
        self._phys_calibrate_button(footer, "fusion", "Measure fusion…")
        self._phys_reset_button(footer, "Fusion", ("Fusion",),
                                ("fusion",))
        self._phys_law_editor(card, "fusion")

        # -- FIBER DIAMETER ---------------------------------------------- #
        # TWO laws, because the chain that sets the diameter IS the pair: Q from
        # Hagen-Poiseuille, then d from mass conservation over it. The flow row
        # is here, not on the Material card beside the bore dimensions it names,
        # so a reader asking "what sets my fibre thickness" finds the whole
        # answer on one card.
        # The CONSTANTS stay there (r, L, eta_ref, Ea, T_ref) -- this card still
        # owns none of them. What it owns is the MODE (physics-predetermined vs
        # hand-typed), the live readout, and now both laws.
        card, head, _f, footer = self._phys_card(
            grid, "diameter", "Fiber diameter",
            law=_diam_row + r" \\ " + _flow_row,
            info=("The fiber's diameter, set by how much melt flows out and "
                  "how fast the stage carries it away.\n\n"
                  "The flow Q comes from the pressure, the needle and the "
                  "viscosity. Laid down at stage speed v, that flow makes a "
                  "fiber of diameter d: twice the speed gives a fiber 0.71 "
                  "times as thick.\n\n"
                  "Checked on PCL: d ∝ v^−0.5 (R² = 0.974) and d ∝ P^0.5 "
                  "through Q ∝ P (R² = 0.990). Voltage is not in it, which "
                  "matches the finding that voltage barely moves the "
                  "diameter. Straight fiber only (v ≥ CTS).\n\n"
                  "No numbers live on this card: pressure and temperature are "
                  "on Process parameters, the needle and melt on Material and "
                  "printer parameters, and the speed on each scaffold under "
                  "Parameters on the Design page."))
        hosts["diameter"] = head
        self._phys_diameter_card = card
        # No registry fields of its own -- the constants live on Material and
        # machine parameters -- so this reset hands back the two LAWS only.
        self._phys_reset_button(footer, "Fiber diameter", (),
                                ("diameter", "flow"))
        self._phys_law_editors_multi(card, [("diameter", _diam_row),
                                            ("flow", _flow_row)])

        # -- FIBER TARGET ------------------------------------------------- #
        # Not a law: the laws above run backwards (gui_fiber_target). It sits
        # beside Fiber diameter because it answers that card's question the
        # other way round -- "which settings give the fiber I want".
        if hasattr(self, "_ft_build_physics_card"):
            self._ft_build_physics_card(grid)

        # -- EXPORT / VISUALISATION --------------------------------------- #
        # The coiling wave. There is no master switch here any more: each law
        # card carries its own ON/OFF, and the Taylor cone is in the Material
        # card's Jet section. The widgets are gui_sim's (see
        # _sim_visual_fields). The acceleration limits are on the Machine
        # page's Machine card: they are the machine's.
        card, head, fields, footer = self._phys_card(
            grid, "visual", "Coiling wave",
            info=("Where the print runs slower than the critical translation "
                  "speed, the jet buckles. This draws it there as a tight "
                  "wave, so you can see where CTS was crossed.\n\n"
                  "The wave is part of the jet-lag laydown, "
                  "so it reaches the STL and switches off with Jet lag. The "
                  "Taylor cone is set in the Jet section of Material and "
                  "printer parameters.\n\n"
                  "Whether each law is applied at all is the switch at the top "
                  "of its own card: Jet lag, Sagging, Fusion and "
                  "Fiber diameter.\n\n"
                  "How the simulated head speeds up and slows down is set by "
                  "the acceleration limits on the Printer & collector page."))
        hosts["visual"] = head
        self._phys_visual_card = card

        # -- ADVANCED ----------------------------------------------------- #
        # What is left once every law has its own card: the mandrel composition
        # switch (deliberately not a registry field) and the reset. The
        # "Contact dynamics" section is still asked for below and still returns
        # NOTHING today -- `contact_damping_zeta` is its only field and
        # _PHYS_RETIRED drops it. The call stays so a future contact-solver
        # parameter lands here rather than nowhere.
        #
        # This card does NOT write `cal_show_advanced`, and checking showed there
        # is nothing to preserve: that var is not in `_persist_map`, and its only
        # readers are the CLASSIC "Show advanced parameters" tickbox and
        # `_toggle_calibration_advanced` beside it (gui_calibration.py:178, :312).
        # It was the modern page's disclosure open-state mirror; with one card per
        # law there is no single "advanced" section left for it to describe.
        card, _h, fields, footer = self._phys_card(
            grid, "advanced", "Advanced",
            info=("The settings that are not one law's parameters: the 4-axis "
                  "per-layer lag re-registration switch, and a reset of every "
                  "calibrated value on this page back to its "
                  "default.\n\n"
                  "Every law card carries its own “Reset to defaults” for just "
                  "that law's constants. Use this one only when you want the "
                  "whole physics preset back. It throws away a fitted viscosity "
                  "and a fitted lag amplitude together."))
        self._phys_advanced_card = card
        self._render_cal_fields(fields, self._phys_fields("Contact dynamics"),
                                headers=False)
        # Same builder both shells call, so the mandrel switch cannot be worded
        # two ways. It grids with columnspan=3 into whatever it is given.
        self._build_advanced_switches(fields)
        ttk.Button(footer, text="Reset every card to defaults",
                   command=self._reset_calibration_defaults).pack(side="left")

        # -- PRESETS: the one WIDE tile, and the ONE box on this kind ----- #
        # The rig + material profile's only preset box, because this is where
        # those fields live. A second box on the same kind silently orphans this
        # one: `_init_pstore` overwrites `self._pstores[kind]` and
        # `_build_preset_table` overwrites `st["table"]`, so the box built FIRST
        # keeps a dead table.
        # The Calibration page's own box is a different kind -- the print design,
        # labelled "Calibration presets".
        card, _h, fields, _ft = self._phys_card(
            grid, "presets", "Physics presets", wide=True,
            info=("Save every field on this page, the whole printer and material "
                  "calibration, as a named preset and reload it any time, "
                  "independent of the scaffold, printer and calibration-print "
                  "presets."))
        self._build_calibration_presets_tab(fields)
        tree = self._pstores.get("calibration", {}).get("table")
        if tree is not None:
            gui_widgets.scroll_guard(tree)

        # The grid answers .sync() exactly as a Disclosure does, so the shared
        # post-preset re-sync needs no special case for this page.
        self._page_disclosures = getattr(self, "_page_disclosures", [])
        self._page_disclosures.append(grid)
        self._install_phys_summary_traces()
        self._refresh_phys_summaries()

    # -- the Physics tiles' one-line summaries ----------------------------- #
    @staticmethod
    def _phys_num(txt, digits=4):
        """A field's text, shortened for a ONE-LINE tile summary.

        The entries hold full precision on purpose -- `viscosity_ref` round-trips
        a fitted 228.16090808099523 and `lag_amplitude` a published
        2.8628571428571425 -- and printed verbatim either one eats the whole
        summary line and pushes the rest off the tile. Trimmed to `digits` significant
        figures HERE and nowhere else: the entry, the profile and the model all
        still see every digit.

        Anything that is not a number (a blank, a mode word) comes back
        untouched."""
        try:
            val = float(txt)
        except (TypeError, ValueError):
            return txt
        out = "%.*g" % (digits, val)
        # %g gives exponent form for the small anchors (1.09e-03); a fiber
        # diameter or a melt density reads better in plain decimal at this size
        if "e" in out and 1e-6 <= abs(val) < 1e7:
            out = ("%.*f" % (digits + 2, val)).rstrip("0").rstrip(".")
        return out

    def _phys_cal(self, key, default="", digits=4):
        """One calibration var's current text, trimmed for a summary line, or
        `default` when the field is blank or absent (a blank optional field means
        'unset', not zero)."""
        var = getattr(self, "cal_vars", {}).get("cal." + key)
        if var is None:
            return default
        try:
            txt = str(var.get()).strip()
        except tk.TclError:
            return default
        return self._phys_num(txt, digits) if txt else default

    def _phys_law(self, law_key):
        """" · your own law" when this law has been replaced, "" otherwise. A
        module tile has to say so: the whole point of the editor is that the
        number the build uses is no longer the one the docs describe."""
        try:
            note = gui_lawedit.summary(self, law_key)
        except Exception:                                # noqa: BLE001
            return ""
        return "" if note in ("", "default law") else " · " + note

    def _phys_sim(self, attr, default=""):
        var = getattr(self, attr, None)
        if var is None:
            return default
        try:
            return var.get()
        except tk.TclError:
            return default

    def _summary_phys_process(self):
        return " · ".join((
            "Gap %s mm" % self._phys_sim("sim_standoff", "?"),
            "%s kV" % self._phys_sim("sim_voltage", "?"),
            gui_units.show("pressure", self._phys_sim("sim_pressure", "?"),
                           "psi"),
            "%s °C" % self._phys_sim("sim_temperature", "?")))

    PHYS_INTRO = ("Open a card to see its law, edit its constants or measure "
                  "them on your printer.")

    #: the values the intro's default sentence speaks for: one per card that
    #: shows "(default)", the numbers a stranger's first print rides on
    _PHYS_INTRO_KEYS = ("cts_ref", "mat_viscosity_ref", "mat_activation_energy",
                        "noz_inner_diameter", "noz_length", "lag_amplitude",
                        "sag_coeff", "fusion_overlap")

    def _phys_intro_text(self):
        """The Physics page's intro. While any value is still the one the app
        ships with, it also says whose values those are (direction page §3: the
        shipped preset is "labelled as the lab's default, measured on the lab's
        printer, and Calibrate is where the user replaces it")."""
        fn = getattr(self, "_cal_provenance", None)
        name = getattr(self, "_shipped_physics_name", lambda: "")()
        if fn is None or not name:
            return self.PHYS_INTRO
        try:
            shipped = any(fn("cal." + k) == "default"
                          for k in self._PHYS_INTRO_KEYS)
        except Exception:                                # noqa: BLE001
            return self.PHYS_INTRO
        if not shipped:
            return self.PHYS_INTRO
        return (self.PHYS_INTRO + " Values marked (default) are the lab's "
                "“%s” preset, measured on the lab's own printer and needle, "
                "so they are a starting point for yours: Calibrate measures "
                "your printer's and replaces them." % name)

    def _phys_prov(self, *names):
        """" (measured)", " (default)", " (from preset)" or " (edited)": where
        these values came from (`_cal_provenance`)."""
        fn = getattr(self, "_cal_provenance", None)
        if fn is None:
            return ""
        try:
            # the mandrel's values are App vars (`m_cts`), not `cal.*` keys
            return " (%s)" % fn(*(n if n.startswith("m_") else "cal." + n
                                  for n in names))
        except Exception:                                # noqa: BLE001
            return ""

    def _phys_mandrel_val(self, name):
        """The mandrel value `name` (an App var such as `m_cts`) as typed, or
        "" when blank: blank means the flat value is used."""
        var = getattr(self, name, None)
        try:
            return str(var.get()).strip() if var is not None else ""
        except tk.TclError:
            return ""

    @staticmethod
    def _phys_short(text):
        """A fitted value as a summary shows it: 3.14811 reads 3.15."""
        try:
            return "%.3g" % float(text)
        except ValueError:
            return text

    def _summary_phys_cts(self):
        text = (gui_units.show("speed", self._phys_cal("cts_ref", "?", digits=12),
                               "mm/s") + self._phys_prov("cts_ref")
                + self._phys_law("cts"))
        # A mandrel CTS lives on the Jet lag card's Mandrel rows, but it is
        # a CTS: without it here, a mandrel user who applied one read only
        # "480 mm/min (default)" on this card.
        m = self._phys_mandrel_val("m_cts")
        if m:
            text += " · mandrel %s%s" % (gui_units.show("speed", m, "mm/s"),
                                         self._phys_prov("m_cts"))
        return text

    def _summary_phys_material(self):
        name = str(self._phys_sim("sim_polymer", "")).strip()
        return (name + " · " if name else "") + "Viscosity %s Pa·s at %s °C%s · temperature sensitivity Ea %s J/mol%s" % (
            self._phys_cal("mat_viscosity_ref", "?"),
            self._phys_cal("mat_temperature_ref", "?"),
            self._phys_prov("mat_viscosity_ref"),
            self._phys_cal("mat_activation_energy", "?"),
            self._phys_prov("mat_activation_energy"))

    def _summary_phys_lag(self):
        if not self._phys_sim("sim_physics_deposit", True):
            return "Off: fibers keep the commanded path"
        return "Lag limit A %s× gap%s · climb rate b %s · angle at CTS %s°" % (
            self._phys_cal("lag_amplitude", "?"),
            self._phys_prov("lag_amplitude"),
            self._phys_cal("lag_saturation_exp", "?"),
            self._phys_cal("lag_angle_cts_deg", "?")) + (
            " · your own R_cir law" if self._phys_law("rcir") else "") + (
            " · mandrel R_ax %s mm%s" % (
                self._phys_short(self._phys_mandrel_val("m_lag_axial")),
                self._phys_prov("m_lag_axial", "m_lag_circ"))
            if self._phys_mandrel_val("m_lag_axial") else "")

    def _summary_phys_sag(self):
        if not self._phys_sim("sim_sag_law", True):
            return "Off: fibers stay at their commanded height"
        return ("A 1 mm span sags %s mm · span exponent p %s" % (
            self._phys_cal("sag_coeff", "?"),
            self._phys_cal("sag_span_exp", "?")) + self._phys_prov("sag_coeff")
            + self._phys_law("sag"))

    def _summary_phys_fusion(self):
        if not self._phys_sim("sim_physical_fusion", False):
            return "Off: fibers stack at full diameter"
        wall = self._phys_cal("fusion_wall_overlap")
        # the straight-span overlap is a GATED field: blank/unticked means it
        # inherits the junction value, so only report it when its tickbox is on
        try:
            gv = getattr(self, "cal_vars", {}).get("cal.fusion_wall_overlap.on")
            gate = bool(gv.get()) if gv is not None else False
        except (AttributeError, tk.TclError):
            gate = False
        txt = "Junction sink φ %s%s" % (self._phys_cal("fusion_overlap", "?"),
                          self._phys_prov("fusion_overlap"))
        if gate and wall:
            txt += " · straight spans φ %s%s" % (wall,
                                        self._phys_prov("fusion_wall_overlap"))
        return txt + self._phys_law("fusion")

    def _summary_phys_diameter(self):
        if self._phys_sim("sim_diam_override", False):
            return "Off: diameter typed by hand"
        return "Computed from the physics" + self._phys_law("diameter")

    def _summary_phys_visual(self):
        return ("Drawn where the print is below CTS"
                if self._phys_sim("sim_coil_wave", False) else "Off")

    def _summary_phys_advanced(self):
        # ζ is gone from this card (see _PHYS_RETIRED), so the summary cannot
        # report it: a tile saying "ζ 1.0" advertises a knob that is not there
        # and does not reach geometry.
        return "Per-layer lag re-registration %s" % (
            "on" if self._phys_sim("sim_mandrel_per_layer", False) else "off")

    def _summary_phys_presets(self):
        # counted off the table, the same way _summary_presets counts the
        # scaffold store -- the store dict holds no separate name list
        store = getattr(self, "_pstores", {}).get("calibration", {})
        try:
            table = store.get("table")
            if table is None:
                return ""
            n = len(table.get_children())
        except Exception:  # noqa: BLE001
            return ""
        return "%d saved" % n   # as the Setup and Scaffold preset cards say

    def _refresh_phys_summaries(self, *_a):
        """Recompute every Physics tile's summary from the live vars. Coalesced
        through after_idle for the same reason the scaffold grid's is: one
        keystroke writes one var, a preset load writes hundreds."""
        if getattr(self, "_phys_summ_pending", False):
            return
        if getattr(self, "_phys_grid", None) is None:
            return
        self._phys_summ_pending = True

        def _run():
            self._phys_summ_pending = False
            g = getattr(self, "_phys_grid", None)
            if g is None:
                return
            for key, fn in (
                    ("process", self._summary_phys_process),
                    ("cts", self._summary_phys_cts),
                    ("material", self._summary_phys_material),
                    ("lag", self._summary_phys_lag),
                    ("sag", self._summary_phys_sag),
                    ("fusion", self._summary_phys_fusion),
                    ("diameter", self._summary_phys_diameter),
                    ("target", getattr(self, "_summary_phys_target",
                                       lambda: "")),
                    ("visual", self._summary_phys_visual),
                    ("advanced", self._summary_phys_advanced),
                    ("presets", self._summary_phys_presets)):
                card = g.card(key)
                if card is None:
                    continue
                try:
                    card.set_summary(fn())
                except Exception:  # noqa: BLE001
                    # a cosmetic line must never take the page down
                    card.set_summary("")
            lbl = getattr(self, "_phys_intro_label", None)
            if lbl is not None:
                try:
                    lbl.configure(text=self._phys_intro_text())
                except Exception:  # noqa: BLE001
                    pass

        try:
            self.root.after_idle(_run)
        except (tk.TclError, AttributeError):
            _run()

    def _install_phys_summary_traces(self):
        """Watch every var a Physics summary reads. Broad on purpose, the same
        bargain the scaffold grid makes: a per-card watch list goes stale the
        moment a formatter is edited, and the refresh is coalesced anyway.

        The sim.* vars mostly do not exist yet (_build_sim_tab runs later), so
        gui_shell re-calls this once the panel is populated."""
        seen = getattr(self, "_phys_watched", None)
        if seen is None:
            seen = self._phys_watched = set()

        def watch(var):
            if var is None or id(var) in seen:
                return
            seen.add(id(var))
            try:
                var.trace_add("write", self._refresh_phys_summaries)
            except (AttributeError, tk.TclError):
                pass

        for var in getattr(self, "cal_vars", {}).values():
            watch(var)
        for attr in ("sim_standoff", "sim_voltage", "sim_pressure",
                     "sim_temperature", "sim_physics_deposit",
                     "sim_diam_override", "sim_physical_fusion",
                     "sim_sag_law", "sim_taylor_cone", "sim_coil_wave",
                     "sim_mandrel_per_layer",
                     # the mandrel's own CTS and lag pair, which the CTS and
                     # Jet lag summaries name (`_phys_mandrel_val`)
                     "m_cts", "m_lag_axial", "m_lag_circ"):
            watch(getattr(self, attr, None))
        # the Fiber target card's line reads its own inputs too
        for var in getattr(self, "_ft_summary_vars", tuple)():
            watch(var)

    def _goto_calibration_page(self, procedure=None):
        """Jump to Calibrate (where every value on this page is measured and
        fitted). Modern shell only; a status hint in a build without it, e.g.
        the classic developer UI.

        `procedure` is a `calibration_procedures.PROCEDURES` key, and every
        Physics card passes the one that measures ITS value -- so the jump lands
        on that quantity's type rather than on whichever type Calibrate
        happened to be showing. `_cal_show_tab` ignores a key it does not
        have, so an unknown one degrades to the plain jump."""
        nav = getattr(self, "_nav_select", None)
        if callable(nav) and getattr(self, "_cal_tools", None) is not None:
            nav("calibration")
            show = getattr(self, "_cal_show_tab", None)
            if procedure and callable(show):
                try:
                    show(procedure)
                except Exception:  # noqa: BLE001
                    pass
        else:
            self.status.set("Open Calibrate to measure these values.")

    # -- preset-load re-sync ------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # the Machine & collector page: the module grid, one card per question
    # ------------------------------------------------------------------ #
    # The classic tab stacks five labelled groups (plate, origin, printer model,
    # motion limits, rotary) and asks "is a rotary stage installed?" with its own
    # tick and kind radios. Here the same section builders are split across four
    # cards by the question they answer, and the page carries the Scaffold
    # designer's own 3/4-axis toggle -- the SAME variable, so there is one mode
    # for the whole app. Each card then shows only the rows its mode uses.
    #
    # THE TOGGLE IS THE STAGE. On this page nothing else says whether a rotary
    # stage exists: `_machine_mode_sync` sets `rotary_on` and `rotary_kind` from
    # the mode, after every mode change and after every machine preset load. So a
    # four-axis rig's preset loaded for a flat print writes a plain 3-axis
    # profile, and saving it in that mode saves it that way. The persist keys are
    # unchanged, so every existing preset still loads in both UIs.

    #: collector mode -> (rotary stage on, rotary kind key)
    _MACHINE_MODE_ROTARY = {"plate": (False, None),
                            "mandrel": (True, "mandrel")}

    def _build_machine_grid(self, mbody):
        """Lay the Machine page out as a ModuleGrid: Instructions, Machine,
        Calibrate, Collector, G-code, Presets.

        A FIRST-TIMER'S PAGE. Each card answers one question in the order you
        set a printer up -- what the printer is made of, where its key points
        are, what is mounted and where zero is -- and the Instructions tile
        walks those steps with a status per item from `machine_setup`, the one
        source the tile summaries and the per-row chips also read.

        Called before the Scaffold designer exists, so the mode toggle and the
        mode sync wait for `_finish_machine_page`; the collector registrations
        made here are applied by the scaffold page's own first
        `_apply_collector_mode`."""
        import gui_buildplate
        pad = self.pad
        self._init_machine_vars()
        self._rotary_kind_labels = {key: label for label, key
                                    in gui_buildplate._ROTARY_KINDS}
        # factory values, for "still the default?" -- taken now, before any
        # preset or session restore has written a single var
        self._setup_factory = {k: getattr(self, a).get()
                               for k, a in self._SETUP_VARS.items()
                               if hasattr(self, a)}
        self._machine_mode_head = ttk.Frame(mbody)
        self._machine_mode_head.grid(row=2, column=0, sticky="w",
                                     pady=(0, gui_fonts.GAP_HEADER))
        grid = gui_widgets.ModuleGrid(mbody, columns=2)
        grid.grid(row=3, column=0, sticky="nsew")
        mbody.rowconfigure(3, weight=1)
        self._machine_grid = grid
        # the toggle, not `_sync_rotary_enable`, grids the kind-dependent rows
        self._rotary_rows_by_mode = True
        reg = self._collector_register
        words = self._collector_text
        self._setup_items = {}       # item key -> its Instructions StatusChip
        self._setup_focus = {}       # item key -> widget that takes focus
        gap = gui_fonts.SP["s"]

        def first_input(w):
            # the first control a user types into or picks, depth first
            kinds = (ttk.Entry, ttk.Combobox, ttk.Radiobutton, ttk.Checkbutton,
                     ttk.Button)
            if isinstance(w, kinds):
                return w
            for c in w.winfo_children():
                hit = first_input(c)
                if hit is not None:
                    return hit
            return None

        def host(card, stretch=False):
            # An inner frame, NOT card.body: `ModuleGrid.add` weights body's
            # column 0, and a label gridded sticky="e" in a weighted column is
            # pushed to the far edge of the pane (the Physics page's Process
            # card). The
            # Forms gridded into it own its columns.
            f = ttk.Frame(card.body)
            f.grid(row=0, column=0, sticky=("ew" if stretch else "w"))
            return f

        def reg_row(row, mode):
            for w in row.widgets:
                reg(w, mode)

        # --- Instructions ------------------------------------------------- #
        setup = grid.add("setup", "Instructions", info=(
            "Five steps to set up your printer for the mode chosen above. Each "
            "step shows what is done and what is left. Go opens the card for "
            "that step, and each item opens it at that item's row.\n\n"
            "✓ Done   ✗ Missing   ! Auto-detected   ? Default values   "
            "– Waiting on an earlier step   ○ Optional"))
        self._setup_body = gui_form.exempt(
            ttk.Frame(setup.body),
            "the setup checklist: step statuses, not parameter rows")
        self._setup_body.grid(row=0, column=0, sticky="w")
        self._setup_sig = None

        # --- 1 Machine ----------------------------------------------------- #
        mach = grid.add("machine", "Printer model", info=(
            "Step 3: what your printer is made of. Load one STL per part and "
            "choose how it moves, so the 3D simulation moves like your "
            "printer. Optional on a plate: a first print does not need it.\n\n"
            "4-axis: the mandrel holder goes in the bed slot."))
        f = host(mach, stretch=True)
        # TRAVEL RANGES ARE NOT ON THIS PAGE. Their rows are built into a frame
        # that is never gridded: the limit vars keep their persist keys, a
        # machine preset still carries them to the simulation, and
        # `_refresh_motion_limits` still finds its frames -- there is just
        # nothing here to edit them with.
        travel = gui_form.exempt(
            ttk.Frame(mach.body),
            "never shown: travel-range rows kept only so their persist keys "
            "ride the printer preset")
        self._build_machine_stl_fields(f, pad, limits_into=travel)
        words(self._printhead_stl_label, "Printhead STL:", "Printhead STL:")
        self._setup_focus["printhead"] = self._machine_stl_entries["head"]
        # the holder is the bed slot, so one entry answers for both items
        self._setup_focus["bed"] = self._setup_focus["holder"] = \
            self._machine_stl_entries["bed"]
        self._setup_focus["kinematics"] = self._machine_kin_combo
        # No holder hint row: the bed slot's label and tooltip follow the mode
        # (`_sync_collector_words`), and say where the holder goes.
        reg(self._build_rotary_mount_fields(f, 17, pad), "mandrel")
        self._setup_focus["letter"] = self._rotary_letter_entry
        # the firmware dialect, row 19: a rig fact like the kinematics above,
        # so it rides the machine preset. The G-code page's documents start
        # in it.
        # the ACCELERATION LIMITS, row 21 on (the flavor takes 19-20): the
        # machine's own, so they sit with the rest of what it is made of and
        # ride the machine preset
        # (`gui_sim._machine_motion_keys`). Built before the Physics page, which
        # `gui_sim._lag_on` allows for. Rotary accel means a mandrel's A axis,
        # so its row shows on 4 axis only.
        self._build_machine_motion_limits(f, 21, pad)
        if getattr(self, "_rotary_accel_row", None) is not None:
            reg_row(self._rotary_accel_row, "mandrel")
        # the PROCESS LIMITS, row 30 on: the pressure, print speed and melt
        # temperature this rig can hold. Fiber target judges "out of reach"
        # against them (gui_fiber_target._ft_range_vars); they ride the
        # machine preset like the acceleration limits above.
        if hasattr(self, "_build_machine_process_limits"):
            self._build_machine_process_limits(f, 30)

        # --- 2 Calibrate --------------------------------------------------- #
        cal = grid.add("calibrate", "Calibrate", info=(
            "Step 4: where the key points are on your printer's STLs. Each one "
            "is picked in a 3D window on the part it belongs to.\n\n"
            "The nozzle and a flat bed's top face are found automatically; "
            "pick them only if the simulation looks wrong. A mandrel axis "
            "must be picked."))
        f = host(cal)
        # ONE FORM for every point, so the columns line up down the card and
        # nothing opens or closes to shift them: label | Pick in 3D… | its
        # coordinates in the tail column, each "X (mm):" + entry. The
        # coordinates are always shown; a pick writes the same vars, so the
        # entries fill in when the 3D window closes.
        C = gui_form.Form(f)

        def point_row(r, label, command, info, fields=(), mode=None):
            row = C.button(label, "Pick in 3D…", command, info, row=r)
            for text, var in fields:
                row.add_entry(text, var, info, unit="mm", width="coord")
            if mode is not None:
                reg_row(row, mode)
            return row

        tip = point_row(
            0, "Nozzle", self._calibrate_tip,
            "The point where the fiber leaves the nozzle, on the printhead "
            "STL. Found automatically from the lowest point of the printhead "
            "when X, Y and Z are blank. Pick it if a fan duct, shroud or probe "
            "hangs lower than the nozzle.",
            (("X", self.machine_tip_x), ("Y", self.machine_tip_y),
             ("Z", self.machine_tip_z)))
        self._setup_focus["tip"] = tip.field

        bed = point_row(
            1, "Bed top face", self._calibrate_bed, "",
            (("Top Z", self.bed_datum_z), ("Corner X", self.bed_corner_x),
             ("Corner Y", self.bed_corner_y)), mode=("plate", "mandrel"))
        # its tooltip text follows the mode; `_render_setup_statuses` writes it
        for w in [bed.label, bed.field] + list(bed.sublabels):
            bed._tip(w, " ")
        self._cal_bed_info = bed
        # the three datum labels are renamed with the mode (`_BED_DATUM_WORDS`)
        self._bed_datum_field_labels = list(bed.sublabels)
        words(bed.label, "Bed top face:", "Mandrel axis:")
        self._setup_focus["bed_face"] = self._setup_focus["mandrel_axis"] = \
            bed.field
        nudge = C.entry("Bed height fine-tune", self.machine_bed_nudge,
                        "Moves the bed up or down in the simulation only. It "
                        "does not change the print.", unit="mm",
                        width="coord", row=2)
        reg_row(nudge, ("plate", "mandrel"))

        # --- 3 Collector --------------------------------------------------- #
        coll = grid.add("collector", "Collector", info=(
            "Step 1: what is mounted for this print, where it sits, and where "
            "g-code zero is.\n\n"
            "The mandrel's diameter is part of the design, so it is on the "
            "Scaffold designer."))
        f = host(coll)
        # ONE SPACING RULE for the card: a section heading, then its rows, with
        # the same air above every heading but the first -- `Form.section`.
        # Every block is gridded flush (`gui_form.framed`), so its rows start
        # on the card's edges in every mode.
        pw, P = gui_form.framed(f, 0)
        reg(pw, "plate")
        # "Collector size", not "Plate": the collector is named for the
        # collector, never for one of its shapes (test_ui_shell's
        # one-vocabulary case, which reads every heading the finder indexes)
        P.section("Collector size")
        plate = ttk.Frame(pw)
        plate.grid(row=1, column=0, columnspan=4, sticky="w")
        self._build_plate_fields(plate, pad)
        self._setup_focus["plate"] = first_input(plate)

        rw, R = gui_form.framed(f, 0)
        reg(rw, "mandrel")
        R.section("Mandrel")
        self._build_rotary_rod_row(rw, 1, pad)
        self._setup_focus["rod"] = first_input(self._rotary_rod_row)

        ow = ttk.Frame(f)
        ow.grid(row=1, column=0, sticky="w")
        so = self._build_bed_offset_row(ow, 0, pad)
        self._setup_focus["position"] = first_input(so)

        _hw, H = gui_form.framed(f, 2)
        H.section("G-code zero", first=False)
        of = ttk.Frame(f)
        of.grid(row=3, column=0, sticky="w")
        self._build_origin_fields(of, pad, export_z=False, intro=False)
        reg(self._origin_face_row, "plate")
        reg(self._rotary_origin_row, "mandrel")
        self._setup_focus["zero_plate"] = first_input(self._origin_face_row)
        self._setup_focus["zero_rod"] = first_input(self._rotary_origin_row)

        # --- 4 G-code ------------------------------------------------------ #
        code = grid.add("gcode", "G-code", info=(
            "Step 2: which firmware reads the file (the g-code flavor), and "
            "the commands your printer runs at the start and end of each "
            "print."))
        self._setup_focus["gcode_flavor"] = self._build_machine_gcode_fields(
            host(code, stretch=True))
        self._setup_focus["gcode_blocks"] = self._machine_start_text
        self._setup_focus["gcode_rules"] = self._machine_rules_library

        # --- 5 Presets ----------------------------------------------------- #
        pre = grid.add("presets", "Presets", wide=True, info=(
            "Step 5: save this page as a named printer preset and reload it any "
            "time, independent of the scaffold and physics presets. Set it as "
            "the default for a plate, a mandrel or both: the plate default "
            "loads when the app starts, the mandrel default when you switch "
            "the Collector to Mandrel (4-axis)."))
        self._build_machine_presets_tab(pre.body)
        tree = self._pstores.get("machine", {}).get("table")
        if tree is not None:
            gui_widgets.scroll_guard(tree)

        # REQUIRED BEFORE OPTIONAL. Built in the order above (the collector
        # card reads widgets the model card made), drawn in the order a first
        # print needs them: the numbers are machine_setup.STEP_NO's.
        grid.reorder(("setup", "collector", "gcode", "machine", "calibrate",
                      "presets"))
        self._rotary_sections_built = True
        self._toggle_plate_shape()

    def _finish_machine_page(self):
        """The half of the Machine page that needs `s_collector`: the toggle,
        the mode -> rotary-stage sync and the statuses. Called once the
        Scaffold designer has created the variable."""
        if getattr(self, "_machine_grid", None) is None:
            return
        head = self._machine_mode_head
        self._machine_collector_bar = gui_widgets.SegmentedBar(
            head,
            options=(("plate", "Plate (3-axis)"), ("mandrel", "Mandrel (4-axis)")),
            variable=self.s_collector,
            command=self._collector_mode_chosen)
        # The bar reads "3 axis / 4 axis" with no caption in front of
        # it, so nothing in the widget tree says what it is called; the Ctrl+F
        # finder needs a name for it. A searchable name only -- no new text is
        # drawn (gui_finder._nearest_label).
        self._machine_collector_bar._finder_label = "Collector"
        self._machine_collector_bar.pack(side="left")
        dot = gui_tips.info_dot(
            head,
            "The same toggle as the Scaffold designer's, and the same setting: "
            "switching it on either page switches both, and loads that mode's "
            "default printer and scaffold presets.\n\n"
            "It also says whether a rotary stage is installed. 3-axis "
            "describes the printer without one, so a four-axis printer "
            "printing flat is described as a 3-axis printer. 4-axis declares "
            "one rotary joint.",
            title="Collector")
        gui_form.mark(dot, "intro").pack(side="left",
                                         padx=(gui_fonts.SP["s"], 0))
        # What the last click loaded, beside the switch that loaded it: the
        # status line alone was missed, so a switch read as a silent swap
        # of the printer settings (vireo, item 1)
        self._collector_loaded_note = tk.StringVar(value="")
        self._collector_loaded_label = ttk.Label(
            head, textvariable=self._collector_loaded_note, style="Hint.TLabel")
        gui_form.mark(self._collector_loaded_label, "live").pack(
            side="left", padx=(gui_fonts.SP["m"], 0))
        self.s_collector.trace_add("write", self._machine_mode_sync)
        self._machine_mode_sync()
        self._install_machine_summary_traces()
        self._refresh_machine_summaries()

    def _machine_mode_sync(self, *_a):
        """Set the rotary stage from the collector mode. Only writes a var that
        differs, so a sync that changes nothing fires no traces."""
        on, kind = self._MACHINE_MODE_ROTARY.get(self.s_collector.get(),
                                                 (False, None))
        if bool(self.rotary_on.get()) != on:
            self.rotary_on.set(on)
        label = self._rotary_kind_labels.get(kind) if kind else None
        if label and self.rotary_kind.get() != label:
            self.rotary_kind.set(label)
        self._sync_rotary_enable()

    def _apply_preset_for_kind(self, kind, data):
        """A machine preset carries its own `rotary.on` / `rotary.kind`, written
        by whichever mode it was saved in. The toggle is the newer intent, so it
        wins: re-derive the stage after the load."""
        super()._apply_preset_for_kind(kind, data)
        if kind == "machine" and getattr(self, "_machine_collector_bar",
                                         None) is not None:
            self._machine_mode_sync()
        if kind == "machine":
            # after the mode sync, so the snapshot is what the page now shows
            self._note_example_preset(data)
            self._refresh_machine_summaries()

    # -- the setup checklist (machine_setup) ------------------------------- #
    #: persist key -> the var attribute `machine_setup` reads it from
    _SETUP_VARS = {
        "machine.x_stl": "machine_x_stl", "machine.z_stl": "machine_z_stl",
        "machine.kinematics": "machine_kinematics",
        "rotary.word": "rotary_word",
        "rotary.j1_stl": "rotary1_stl", "rotary.j2_stl": "rotary2_stl",
        "rotary.j1_word": "rotary1_word", "rotary.j1_axis": "rotary1_axis",
        "rotary.j2_word": "rotary2_word", "rotary.j2_axis": "rotary2_axis",
        "machine.tip_x": "machine_tip_x", "machine.tip_y": "machine_tip_y",
        "machine.tip_z": "machine_tip_z",
        "bed.datum_z": "bed_datum_z", "bed.corner_x": "bed_corner_x",
        "bed.corner_y": "bed_corner_y",
        "rotary.j1_pivot": "rotary1_pivot", "rotary.j2_pivot": "rotary2_pivot",
        "rotary.j1_seat": "rotary_seat", "rotary.home": "rotary_home",
        "plate.shape": "plate_shape", "plate.x": "plate_x",
        "plate.y": "plate_y", "plate.diameter": "plate_diameter",
        "plate.thickness": "plate_thickness",
        "slide.offset_x": "slide_offset_x", "slide.offset_y": "slide_offset_y",
        "origin.face": "origin_face", "origin.anchor": "origin_anchor",
        "origin.custom": "origin_custom",
        "rotary.origin_custom": "rotary_origin_custom",
        "rotary.rod_length": "rotary_rod_length", "rotary.rod_x": "rotary_rod_x",
        "rotary.origin_axial": "rotary_origin_axial",
        "rotary.origin_radial": "rotary_origin_radial",
        "origin.gcode_flavor": "gcode_flavor",
        "origin.gcode_start": "machine_gcode_start",
        "origin.gcode_end": "machine_gcode_end",
        "origin.gcode_rules": "machine_gcode_rules",
    }

    def _setup_steps(self):
        import machine_setup
        values = {}
        for k, a in self._SETUP_VARS.items():
            var = getattr(self, a, None)
            if var is not None:
                try:
                    values[k] = var.get()
                except tk.TclError:
                    pass
        import machine_model
        return machine_setup.setup_steps(
            values, self._txt(self.s_collector, "plate"),
            getattr(self, "_setup_factory", {}),
            # found the way the player finds it: bundle, beside the app,
            # and a renamed part under its new name
            file_exists=machine_model.part_exists,
            example=getattr(self, "_setup_example", None))

    def _note_example_preset(self, data):
        """Remember what a SHIPPED example printer preset (its data carries
        `"_example": true`) put in the Setup fields, so the checklist reads
        those values as defaults to check rather than as the user's own. Any
        other printer preset clears it: a preset the user saved or updated is
        theirs, and `Update selected` writes no flag."""
        if not (isinstance(data, dict) and data.get("_example")):
            self._setup_example = None
            return
        snap = {}
        for k, a in self._SETUP_VARS.items():
            var = getattr(self, a, None)
            if var is not None:
                try:
                    snap[k] = var.get()
                except tk.TclError:
                    pass
        self._setup_example = snap

    def _setup_go(self, card, item_key=None):
        """Open `card` and put the keyboard on the item's row -- the Instructions
        tile's Go and item buttons. Focus is deferred one idle pass so it lands
        after the grid has swapped the detail pane in."""
        g = self._machine_grid
        g.open(card)
        target = self._setup_focus.get(item_key) if item_key else None
        if target is None:
            return

        def _focus():
            try:
                target.focus_set()
            except tk.TclError:
                pass
        try:
            self.root.after_idle(_focus)
        except (tk.TclError, AttributeError):
            _focus()

    def _render_setup_statuses(self, steps):
        """The card side of the checklist. Cards show no status, so all that
        follows the mode here is the Calibrate card's bed-face info text."""
        mode = self._txt(self.s_collector, "plate")
        info = getattr(self, "_cal_bed_info", None)
        if info is not None:
            if mode == "mandrel":
                info.set_tip(
                    "The mandrel's axis of rotation, picked on the holder STL. "
                    "It places the mandrel: its X and Y put the axis under the "
                    "nozzle and its Z sets the gap.\n\nIt must be picked. Until "
                    "it is, the mandrel is drawn on the holder's top face.")
            else:
                info.set_tip(
                    "The flat face the plate sits on, and its corner, on the "
                    "bed STL. Found automatically. Pick it if the bed STL is a "
                    "full assembly and the simulation puts the plate in the "
                    "wrong place.")

    def _render_setup_tile(self, steps):
        """Rebuild the Instructions body only when a status actually changed."""
        import machine_setup as ms
        mode = self._txt(self.s_collector, "plate")
        example = bool(getattr(self, "_setup_example", None))
        sig = (mode, example, tuple((st.key, st.state,
                            tuple((i.key, i.state) for i in st.items))
                           for st in steps))
        if sig == self._setup_sig:
            return
        self._setup_sig = sig
        body = self._setup_body
        for w in body.winfo_children():
            w.destroy()
        self._setup_items = {}
        name = {"plate": "3-axis", "mandrel": "4-axis"}.get(mode, "3-axis")
        s, gap = gui_fonts.SP["s"], gui_fonts.SP["l"]
        head = ttk.Frame(body)
        head.grid(row=0, column=0, sticky="w")
        ttk.Label(head, text="Set up your %s printer" % name,
                  style="Section.TLabel").grid(row=0, column=0, columnspan=3,
                                               sticky="w")
        if example:
            # the example's values are not the user's: say so above the list
            ttk.Label(head, style="Hint.TLabel", wraplength=480,
                      justify="left",
                      text="These values come from the example printer "
                           "preset that ships with the app. Each one shows ? "
                           "until you change it to match your printer.").grid(
                row=3, column=0, columnspan=3, sticky="w", pady=(s, 0))
        # the key: what each symbol means, three to a row so it fits a
        # single-width card
        for i, (state, word) in enumerate((
                (ms.DONE, "Done"), (ms.MISSING, "Missing"),
                (ms.AUTO, "Auto-detected"), (ms.DEFAULT, "Default values"),
                (ms.WAITING, "Waiting on an earlier step"),
                (ms.OPTIONAL, "Optional"))):
            c = gui_widgets.StatusChip(head)
            c.set(state, label=word)
            c.grid(row=1 + i // 3, column=i % 3, sticky="w", padx=(0, gap))
        # THE LIST is its own grid, so the key's width cannot widen the
        # number column:  0 step number | 1 title, what it needs, items | 2 Go
        # A step has no status of its own; its items carry it.
        lst = ttk.Frame(body)
        lst.grid(row=1, column=0, sticky="w", pady=(s, 0))
        rotary = mode != "plate"
        notes = {
            "machine": ("Required: the rotary axis letter. The STLs are "
                        "optional: only needed to see your printer in the "
                        "simulation." if rotary else "Optional: only needed "
                        "to see your printer in the simulation."),
            "calibrate": ("Optional: only needed to see your printer in the "
                          "simulation. Once a holder STL is loaded, pick its "
                          "mandrel axis." if rotary else "Optional: only "
                          "needed to see your printer in the simulation."),
            "collector": "Required: what is mounted, and where G-code zero is.",
            "gcode": "Required: the g-code flavor your printer's firmware "
                     "reads, and what runs at the start and end of a print.",
            # not "Optional": a setup that is not saved is gone at the next
            # start; saving never changes the default, Set as default does
            "save": "To keep it: save it under Presets, then Set as default "
                    "so it loads at every start.",
        }
        r = 0
        for st in steps:
            top = (gui_fonts.SP["m"], 0)
            ttk.Label(lst, text=str(st.number), style="Section.TLabel").grid(
                row=r, column=0, sticky="w", pady=top, padx=(0, s))
            ttk.Label(lst, text=st.title, style="Section.TLabel").grid(
                row=r, column=1, sticky="w", pady=top, padx=(0, gap))
            ttk.Button(lst, text="Go →",
                       command=lambda cd=st.card: self._setup_go(cd)).grid(
                row=r, column=2, sticky="w", pady=top)
            r += 1
            note = notes.get(st.key, "")
            if note:
                ttk.Label(lst, style="Hint.TLabel", text=note).grid(
                    row=r, column=1, columnspan=2, sticky="w", pady=(0, 2))
                r += 1
            for it in st.items:
                c = gui_widgets.StatusChip(
                    lst, title=it.label, font="MewBody",
                    command=lambda cd=it.card, k=it.key: self._setup_go(cd, k))
                c.set(it.state, it.tip, label=it.label)
                c.grid(row=r, column=1, sticky="w", padx=(gui_fonts.INDENT, gap),
                       pady=1)
                self._setup_items[it.key] = c
                r += 1

    def _summary_machine_presets(self):
        try:
            table = self._pstores.get("machine", {}).get("table")
            return "%d saved" % len(table.get_children()) if table else ""
        except Exception:  # noqa: BLE001
            return ""

    def _refresh_machine_summaries(self, *_a):
        """Coalesced like `_refresh_module_summaries`: a preset load writes
        dozens of these vars. One `setup_steps` answer feeds the tiles, the
        row chips and the Instructions checklist."""
        if getattr(self, "_msumm_pending", False):
            return
        if getattr(self, "_machine_grid", None) is None:
            return
        self._msumm_pending = True

        def _run():
            import machine_setup as ms
            self._msumm_pending = False
            g = self._machine_grid
            try:
                steps = self._setup_steps()
            except Exception:  # noqa: BLE001
                return
            # on a plate the printer model is only for the simulation, so
            # its two cards say "Optional" before a newcomer opens them
            flat = self._txt(self.s_collector, "plate") == "plate"
            summaries = {"setup": ms.next_action(steps),
                         "machine": ms.card_summary(steps, "machine", flat),
                         "calibrate": ms.card_summary(steps, "calibrate",
                                                      True),
                         "collector": ms.card_summary(steps, "collector"),
                         "gcode": ms.card_summary(steps, "gcode"),
                         "presets": self._summary_machine_presets()}
            for key, text in summaries.items():
                card = g.card(key)
                if card is not None:
                    card.set_summary(text)
            for fn in (self._render_setup_statuses, self._render_setup_tile):
                try:
                    fn(steps)
                except tk.TclError:
                    pass

        try:
            self.root.after_idle(_run)
        except (tk.TclError, AttributeError):
            _run()

    def _install_machine_summary_traces(self):
        for attr in ("s_collector",) + tuple(self._SETUP_VARS.values()):
            var = getattr(self, attr, None)
            if var is None:
                continue
            try:
                var.trace_add("write", self._refresh_machine_summaries)
            except (AttributeError, tk.TclError):
                pass

    def _apply_all_toggles(self):
        """super() re-syncs every grey/hide state over the same widgets; then
        settle the disclosure rows (open iff their feature came back on)."""
        super()._apply_all_toggles()
        self._sync_disclosures()

    def _sync_disclosures(self):
        for d in getattr(self, "_page_disclosures", []):
            try:
                d.sync()
            except tk.TclError:
                pass
