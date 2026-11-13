#!/usr/bin/env python3
"""gui_gcode_collector.py -- MODERN-UI ONLY: the collector on the CAD-style
G-code page, and the 4-axis (mandrel) design tools that follow it.

The collector (`s_collector`: plate / mandrel) is ONE app-wide
setting, shared with the Scaffold designer: changing it
on either page changes both. A change here does exactly what a click on the
designer's 3/4-axis bar does -- `_collector_mode_chosen`, which loads that
mode's default machine and design presets -- with no confirm.

  * Ribbon: a COLLECTOR group first. Its tool names the mode ("Collector /
    4 axis") and opens the Collector panel. The design groups after it are
    the collector's own: the 3-axis set (`gui_gcode_design`: Create, Pattern,
    Modify) or the 4-axis set (Create -- New scaffold; Modify -- Parameters,
    Fiber target, Advanced; the pattern and the custom cell are rows of
    Parameters, and of the tree under each scaffold). The other set is
    unpacked, not greyed. Fiber target is the plate's panel
    (`gui_fiber_target`); on a tube it solves the tube's surface speed.
  * Collector panel: the 3 / 4-axis choice and the collector's
    dimensions. The plate's are the MACHINE's (`plate_*`, machine preset;
    `_build_plate_fields`). The mandrel's diameter and place on the rod are
    the design's (`_build_mandrel_collector_fields`); the rod's length and
    near end are the machine's again (`_build_rotary_rod_row`).
  * 4 axis: every page scaffold is a TUBE of its own
    (`gui_gcode_twodesigns._gce_tube_scope`), as many as you like, each
    placed on the rod by its own Parameters rows. Everything a tube is --
    pattern, size, closure, the Resolved readout, both ladders, layers,
    placement -- is a row of the Parameters table (`gui_gcode_params`), one
    column per scaffold; there is no Tube panel. New scaffold adds a copy
    of the last, beside it on the rod. Pattern and Custom cell act on the
    selected scaffold; Custom cell is the designer's cell editor built over
    that scaffold's tube (`_TubeView`). Advanced is what is the RIG's, not a
    tube's (`_build_mandrel_rig_fields`): speed policy, feeds, calibrated
    placement.
  * Tree: a Collector row under Whole file. Double-click opens its panel; its
    eye is the picture's Collector toggle (pv_show_plate / pv_show_collector).
    In 4 axis a scaffold row opens the Parameters table on its column and a
    layer row the layer table on that layer. Under each scaffold sit its
    FEATURE rows, as the plate's scaffold carries Footprint / Array...:
    Pattern and Custom cell (a drawn family), ids
    ``d:m<scaffold>:<feature>``, each with an eye (display only) and a
    double-click to its form. A layer that differs gets one Layer parameters
    row inside its own branch, as on the plate -- never a row of its own
    under the scaffold.

The panels open through `gui_gcode_panels._gce_panel_open`, each built over a
`_View` so the second build never replaces a designer var or takes its
widgets. A mandrel builder also keeps a few single widget handles on the App
that the App's own refreshes draw into (`_m_resolved`, the cell canvas...).
While a page panel is open those handles are the panel's (`_MandrelView.
adopt`), so the resolved table and the canvas draw on the page; they go back
to the designer when it closes. The registration lists the builders append to
(`_mandrel_family_widgets`...) are the App's: a closed panel's dead widgets
are pruned from them.

Undo: a design step on the page carries the m_* values with the page's
design vars (`_gce_d_state` / `_gce_d_restore`), so Undo takes a tube change
back with its text."""
from gui_common import *

import types

import gui_tips
import gui_widgets
from gui_gcode_panels import _GcodePanelsMixin, _View

#: the collector modes, in the designer's bar's order and words
_MODES = (("plate", "Plate (3-axis)"), ("mandrel", "Mandrel (4-axis)"))
_SHORT = {"plate": "Plate (3-axis)", "mandrel": "Mandrel (4-axis)"}
#: the picture's collector toggle per mode (the Collector row's eye)
_EYE_VARS = {"plate": "pv_show_plate", "mandrel": "pv_show_collector"}

#: the 4-axis panels: key -> (builder, the App handles it takes over)
_MANDREL_PANELS = {
    "cell": ("_build_mandrel_cell_editor_fields",
             ("_m_cell_canvas", "_m_cell_status_label")),
    "advanced": ("_build_mandrel_rig_fields", ("_m_fixed_row",)),
}

#: lists on the App that a builder appends widgets to
_REG_LISTS = ("_mandrel_family_widgets", "_mandrel_mode_widgets",
              "_mandrel_solver_widgets", "_mandrel_auto_widgets",
              "_diam_links")

_MISSING = object()


def _alive(entry):
    w = entry[0] if isinstance(entry, tuple) else entry
    try:
        return bool(w.winfo_exists())
    except (tk.TclError, AttributeError):
        return False


class _MandrelView(_View):
    """A `_View` that, once built, lends the App the panel's copies of the
    handles in `adopt` (module doc) and takes them back on release."""

    def __init__(self, app, adopt=(), **kw):
        super().__init__(app, **kw)
        self.__dict__["_adopt"] = tuple(adopt)
        self.__dict__["_saved"] = {}

    def adopt(self):
        d = self.__dict__
        app = d["_app"]
        for name in d["_adopt"]:
            if name in d["_own"]:
                d["_saved"][name] = app.__dict__.get(name, _MISSING)
                app.__dict__[name] = d["_own"][name]

    def release(self, _e=None):
        super().release(_e)
        d = self.__dict__
        app = d["_app"]
        for name, old in d["_saved"].items():
            if app.__dict__.get(name) is d["_own"].get(name):
                if old is _MISSING:
                    app.__dict__.pop(name, None)
                else:
                    app.__dict__[name] = old
        d["_saved"] = {}
        if app.__dict__.get("_gce_m_view") is self:
            app.__dict__["_gce_m_view"] = None
        try:
            app.root.after_idle(app._gce_m_after_close)
        except (tk.TclError, AttributeError):
            pass


class _TubeView(_MandrelView):
    """A `_MandrelView` over ONE page scaffold's tube: everything the build
    reads, and every App method it calls -- then or later, from a click on
    its canvas -- runs inside `_gce_tube_scope(scaffold)`, so the designer's
    cell editor draws and edits that scaffold's cell, not the designer's."""

    def __init__(self, app, scaffold, **kw):
        super().__init__(app, **kw)
        self.__dict__["_tube"] = scaffold

    def __getattr__(self, name):
        d = self.__dict__
        app, i = d["_app"], d["_tube"]
        with app._gce_tube_scope(i):
            val = _MandrelView.__getattr__(self, name)
        if isinstance(val, (types.MethodType, types.FunctionType)):
            def scoped(*a, **k):
                with app._gce_tube_scope(i):
                    return val(*a, **k)
            return scoped
        return val


class _GcodeCollectorMixin:

    _GCE_PANEL_TITLES = dict(_GcodePanelsMixin._GCE_PANEL_TITLES,
                             collector="Collector", cell="Custom cell",
                             advanced="Advanced")

    # ------------------------------------------------------------------ #
    # ribbon
    # ------------------------------------------------------------------ #
    def _build_gce_design_groups(self, bar, ribbon_group):
        """Collector first, then the 3-axis groups and the 4-axis groups,
        each set in a frame of its own so the collector swaps them whole."""
        self._mandrel_vars_make()
        tool = self._gce_tool
        lead = ttk.Frame(bar, style="Bar.TFrame")
        lead.pack(side="left", fill="y")
        self._gce_c_lead = lead
        col = ribbon_group(lead, "Collector")
        self._gce_ctools = {"collector": tool(
            col, "add", "collector", "Collector\n" + self._gce_c_short(),
            "What the print lands on: the plate (3-axis) or a rotating "
            "mandrel (4-axis), and its size. Changing it loads that "
            "collector's default printer and design.",
            command=lambda: self._gce_panel_open("collector"))}
        plate = ttk.Frame(bar, style="Bar.TFrame")
        plate.pack(side="left", fill="y")
        super()._build_gce_design_groups(plate, ribbon_group)
        tube = ttk.Frame(bar, style="Bar.TFrame")
        tube.pack(side="left", fill="y")
        mt = self._gce_mtools = {}
        cre = ribbon_group(tube, "Create")
        mt["scaffold"] = tool(
            cre, "add", "scaffold", "New\nscaffold",
            "Add a tube to the mandrel, beside the last one with a 5 mm gap. Its "
            "settings start as a copy of the one before it.",
            command=self._gce_m_new_scaffold)
        mod = ribbon_group(tube, "Modify")
        mt["params"] = tool(
            mod, "change", "params", "Parameters",
            "Every scaffold's tube in one table: pattern, size, pore, "
            "closure, what the software resolved, layers and where it sits "
            "on the mandrel. Double-click a scaffold in the tree to open it on "
            "that scaffold.",
            command=self._gce_d_params)
        if hasattr(self, "_ft_open_cad"):
            # the same panel as the plate's (gui_fiber_target): on a tube it
            # solves and applies the tube's surface speed
            mt["target"] = tool(
                mod, "change", "target", "Fiber\ntarget",
                "Type the fiber diameter you want, and a jet lag or a "
                "pressure, temperature or speed to hold if you like: get the "
                "settings that make it on your printer, with a map of every "
                "setting that does. Apply sets them for the selected tube "
                "(its surface speed).",
                command=self._ft_open_cad)
        mt["advanced"] = tool(
            mod, "change", "advanced", "Advanced",
            "The printer's settings, shared by every tube: the speed policy, "
            "the feeds, and the calibrated placement radii.",
            command=lambda: self._gce_panel_open("advanced"))
        self._gce_c_sets = {"plate": plate, "mandrel": tube}
        self.s_collector.trace_add("write", self._gce_c_mode_changed)
        self._gce_c_mode_sync()

    def _build_gce_panel_tools(self, fil):
        super()._build_gce_panel_tools(fil)
        # the panel tools draw pressed while their panel is open
        self._gce_ptools.update(getattr(self, "_gce_ctools", {}))
        self._gce_ptools.update(getattr(self, "_gce_mtools", {}))

    def _gce_c_short(self):
        return _SHORT.get(self.s_collector.get(), "Plate (3-axis)")

    def _gce_m_family_menu(self):
        """Pattern's menu: every family, the selected scaffold's ticked.
        Picking one opens what shapes it: the cell editor for a drawn cell,
        the Parameters table on that scaffold for the rest."""
        import gui_mandrel
        m = getattr(self, "_gce_m_fmenu", None)
        if m is None:
            m = self._gce_m_fmenu = tk.Menu(self.root, tearoff=False)
            # hovering a pattern shows its unit cell (gui_cell_hover): one
            # cell solid, a few neighbours in grey
            import gui_cell_hover
            gui_cell_hover.attach_menu(
                m, lambda: self._gce_m_fhover,
                graph_of=lambda: self._gce_m_fgraph())
        m.delete(0, "end")
        self._gce_m_fhover = {}
        i = self._gce_d_target()
        if i is None:
            return m
        var = self.gce_scaf_vars[i]["m_family"]
        for k, (tok, lab) in enumerate(gui_mandrel.MANDREL_FAMILY_CHOICES):
            m.add_radiobutton(label=lab, value=tok, variable=var,
                              command=lambda: self._gce_m_family_picked(i))
            self._gce_m_fhover[k] = tok
        return m

    def _gce_m_fgraph_of(self, i):
        """Scaffold `i`'s drawn cell text, for the Pattern hover."""
        try:
            return self.gce_scaf_vars[i]["m_cell_graph"].get()
        except (TypeError, KeyError, IndexError, tk.TclError):
            return None

    def _gce_m_fgraph(self):
        """The selected scaffold's drawn cell, for the Pattern hover."""
        i = self._gce_d_target()
        try:
            return self.gce_scaf_vars[i]["m_cell_graph"].get()
        except (TypeError, KeyError, IndexError, tk.TclError):
            return None

    def _gce_m_family_picked(self, i):
        import gui_mandrel
        with self._gce_tube_scope(i):
            self._mandrel_family_backfill()
            drawn = (self._mandrel_family_value()
                     == gui_mandrel.MANDREL_DRAWN_FAMILY)
        self._gce_c_soon("refresh")
        if drawn:
            self._gce_m_cell_open(i)
        else:
            self._gce_params_open(col=i)

    def _gce_m_new_scaffold(self):
        """Another tube: a copy of the last, beside it on the rod with a
        5 mm gap (`_gce_tube_place_beside`), and the table open on it."""
        if self._gce_m_why():
            return
        n = self._gce_scaffold_count()
        self._gce_ensure_scaffolds(n + 1)
        self._gce_copy_vars([self.gce_scaf_vars[n - 1]],
                            [self.gce_scaf_vars[n]], keep=("color",))
        self._gce_tube_place_beside(n)
        self.gce_s_count.set(str(n + 1))
        self._gce_params_open(col=n)

    def _gce_m_cell_open(self, i):
        """The cell editor over scaffold `i`'s tube (0-based)."""
        if i is None or self._gce_m_why():
            return
        self._gce_m_cell_for = int(i)
        if getattr(self, "_gce_panel_key", None) == "cell" and \
                getattr(self, "_gce_form", None) is not None:
            self._gce_host_close()
        self._gce_panel_open("cell")

    def _gce_c_open(self, key):
        """Open panel `key`, or leave it open (the tool's second press
        closes a panel; a row or a menu never does)."""
        if getattr(self, "_gce_panel_key", None) == key and \
                getattr(self, "_gce_form", None) is not None:
            return
        self._gce_panel_open(key)

    # ------------------------------------------------------------------ #
    # the collector changed (here, on the designer, or by a preset)
    # ------------------------------------------------------------------ #
    def _gce_c_mode_changed(self, *_):
        # A different collector is a different design: blank the Design
        # preview so the old collector's scaffold does not sit there for the
        # whole of the new build (`PreviewPanel.clear`). Only on a real
        # change -- a preset re-asserting the same collector rebuilds nothing,
        # so a blank would stay blank.
        mode = self.s_collector.get()
        if mode != getattr(self, "_gce_c_last_mode", None):
            panel = getattr(self, "_gce_pv_panel", None)
            if panel is not None:
                # a fresh state, never None: `_gce_pv_apply` fills it in place
                self._gce_pv_state = self._pv_new_state()
                # ...and no old picture to re-show: `_gce_pv_show` (the tree's
                # refresh calls it) re-puts `_gce_pv_full` into the state, and
                # with no colour in the fresh state the other collector's
                # fibres came back in the panel's red default
                self._gce_pv_full = None
                try:
                    panel.clear()
                    panel.set_status("updating…")
                except tk.TclError:
                    pass
        self._gce_c_last_mode = mode
        self._gce_c_mode_sync()
        self._gce_c_soon("tree")
        # the Collector panel shows the new collector's dimensions; a 4-axis
        # panel has nothing to show on another collector
        key = getattr(self, "_gce_panel_key", None)
        if key == "collector":
            self._gce_c_soon("collector")
        elif key in _MANDREL_PANELS and self.s_collector.get() != "mandrel":
            self._gce_host_close()
        # the Parameters tables are the collector's (scaffolds or the tube)
        shown = getattr(self, "_gce_params_shown", None)
        if shown is not None and shown():
            self._gce_params_close()

    def _gce_c_mode_sync(self):
        sets = getattr(self, "_gce_c_sets", None)
        if not sets:
            return
        show = "mandrel" if self.s_collector.get() == "mandrel" else "plate"
        for k, fr in sets.items():
            try:
                if k == show:
                    if not fr.winfo_manager():
                        fr.pack(side="left", fill="y", after=self._gce_c_lead)
                else:
                    fr.pack_forget()
            except tk.TclError:
                pass
        # the other set is a different width: fit the ribbon to it now
        fit = getattr(self, "_gce_ribfit", None)
        if fit is not None:
            fit.fit()
        self._gce_design_refresh()

    def _gce_c_soon(self, what):
        """Coalesce the follow-ups of a change into one idle pass (a preset
        load sets the collector more than once)."""
        pend = getattr(self, "_gce_c_pending", None)
        if pend is None:
            pend = self._gce_c_pending = set()
        first = not pend
        pend.add(what)
        if first:
            self.root.after_idle(self._gce_c_flush)

    def _gce_c_flush(self):
        pend, self._gce_c_pending = self._gce_c_pending or set(), set()
        if "collector" in pend:
            self._gce_panel_rebuild("collector")
        if "tree" in pend and hasattr(self, "_gce_refresh_anatomy"):
            self._gce_refresh_anatomy()
        if "refresh" in pend:
            self._gce_design_refresh()
        if "eye" in pend and getattr(self, "_gce_tree", None) is not None:
            self._gce_vis_apply()

    def _gce_c_chosen(self):
        """A click on the Collector panel's bar: what a click on the
        designer's bar does."""
        self._collector_mode_chosen()

    # ------------------------------------------------------------------ #
    # which tools can act
    # ------------------------------------------------------------------ #
    def _gce_m_why(self):
        """Why the 4-axis tools cannot act now, or None."""
        if self.s_collector.get() != "mandrel":
            return "These are the 4-axis tools: pick Mandrel (4-axis) under Collector."
        return self._gce_d_doc_why()

    def _gce_design_refresh(self):
        super()._gce_design_refresh()
        import gui_mandrel
        why = self._gce_m_why()
        i = self._gce_d_target() if why is None else None
        for k, b in (getattr(self, "_gce_mtools", None) or {}).items():
            w = why
            if w is None and k in ("family", "cell") and i is None:
                w = "Select a scaffold in the tree (or one of its layers) first."
            if w is None and k == "cell":
                with self._gce_tube_scope(i):
                    fam = self._mandrel_family_value()
                if fam != gui_mandrel.MANDREL_DRAWN_FAMILY:
                    w = ("Pick “Custom cell (drawn)” under Pattern first: this "
                         "draws that pattern's cell.")
            try:
                b.set_why(w)
            except tk.TclError:
                pass
        b = (getattr(self, "_gce_ctools", None) or {}).get("collector")
        if b is not None:
            try:
                b.configure(text="Collector\n" + self._gce_c_short())
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ #
    # regenerate on a 4-axis or collector edit
    # ------------------------------------------------------------------ #
    def _gce_d_trace_vars(self):
        first = not getattr(self, "_gce_d_traced", False)
        super()._gce_d_trace_vars()
        if not first:
            return
        cb = lambda *_: self._gce_design_regen()   # noqa: E731
        vs = list(self._gce_m_vars().values())
        try:
            self._rotary_vars_make()
        except AttributeError:
            pass
        for name in ("s_collector", "rotary_on", "rotary_rod_length",
                     "rotary_rod_x",
                     "plate_shape", "plate_x", "plate_y", "plate_diameter",
                     "plate_thickness"):
            x = getattr(self, name, None)
            if isinstance(x, tk.Variable):
                vs.append(x)
        for x in vs:
            x.trace_add("write", cb)
        # the Collector row's words and eye
        for name in ("plate_shape", "plate_x", "plate_y", "plate_diameter",
                     "m_diameter"):
            x = getattr(self, name, None)
            if isinstance(x, tk.Variable):
                x.trace_add("write", lambda *_a: self._gce_c_soon("tree"))
        for name in _EYE_VARS.values():
            x = getattr(self, name, None)
            if isinstance(x, tk.Variable):
                x.trace_add("write", lambda *_a: self._gce_c_soon("eye"))

    def _gce_m_vars(self):
        """The 4-axis design's vars (the m.* preset keys)."""
        try:
            return {k: v for k, v in self._mandrel_persist_entries().items()
                    if isinstance(v, tk.Variable)}
        except AttributeError:
            return {}

    # undo: a tube change comes back with its text
    def _gce_d_state(self):
        st = super()._gce_d_state()
        st["mandrel"] = {k: v.get() for k, v in self._gce_m_vars().items()}
        return st

    def _gce_d_restore(self, st):
        vals = st.get("mandrel") or {}
        if vals:
            self._gce_d_restoring = True
            try:
                for k, v in self._gce_m_vars().items():
                    if k in vals and v.get() != vals[k]:
                        v.set(vals[k])
            finally:
                self._gce_d_restoring = False
        super()._gce_d_restore(st)

    # ------------------------------------------------------------------ #
    # the panels
    # ------------------------------------------------------------------ #
    def _gce_panel_collector(self, body):
        mode = self.s_collector.get()
        bar = gui_widgets.SegmentedBar(body, options=_MODES,
                                       variable=self.s_collector,
                                       command=self._gce_c_chosen)
        bar.grid(row=0, column=0, sticky="w", pady=(0, 8))
        box = ttk.Frame(body)
        box.grid(row=1, column=0, sticky="ew")
        if mode == "plate":
            view = _MandrelView(self, own=("_build_plate_fields",))
            view.__dict__["_wrap"]["_toggle_plate_shape"] = \
                lambda: self._gce_c_plate_shape(view)
            view._build_plate_fields(box, self.pad)
            self._gce_c_plate_shape(view)
            return view
        if mode == "mandrel":
            self._rotary_vars_make()
            view = _MandrelView(
                self, own=("_build_mandrel_collector_fields",
                           "_build_rotary_rod_row"),
                adopt=("_m_place_offset_entry",))
            view._build_mandrel_collector_fields(box, tips=gui_tips)
            rodbox = ttk.Frame(body)
            rodbox.grid(row=2, column=0, sticky="ew", pady=(6, 0))
            view._build_rotary_rod_row(rodbox, 0, self.pad)
            note = ttk.Label(body, style="Hint.TLabel", wraplength=360,
                             justify="left",
                             text="The mandrel's length and near end are the "
                                  "Printer & collector page's, used once it declares a "
                                  "rotary stage.")
            note.grid(row=3, column=0, sticky="w", pady=(2, 0))

            def sync_rod(*_a):
                on = bool(self.rotary_on.get())
                try:
                    self._set_widgets_state(rodbox,
                                            "normal" if on else "disabled")
                    note.grid() if not on else note.grid_remove()
                except tk.TclError:
                    pass
            view.rotary_on.trace_add("write", sync_rod)
            sync_rod()
            view.adopt()
            self._sync_place_offset()
            return view
        return None

    def _gce_c_plate_shape(self, view):
        """`_toggle_plate_shape` for the panel's rows, then the Machine
        page's own copy, which a click here changed too."""
        circ = self.plate_shape.get() == "circular"
        own = view.__dict__["_own"]
        for name, on in (("plate_rect_frame", not circ),
                         ("plate_circ_frame", circ)):
            fr = own.get(name)
            if fr is not None:
                self._set_widgets_state(fr, "normal" if on else "disabled")
        try:
            self._toggle_plate_shape()
        except (AttributeError, tk.TclError):
            pass

    def _gce_m_panel(self, key, body):
        builder, adopt = _MANDREL_PANELS[key]
        if key == "cell":
            # one scaffold's cell: the view keeps every call in its scope
            i = getattr(self, "_gce_m_cell_for", None) or 0
            i = min(i, self._gce_scaffold_count() - 1)
            view = _TubeView(self, i, own=(builder,), adopt=adopt)
            ttk.Label(body, text="Scaffold %d" % (i + 1),
                      style="Hint.TLabel").grid(row=99, column=0, sticky="w")
        else:
            view = _MandrelView(self, own=(builder,), adopt=adopt)
        with self._gce_page_scope():
            getattr(view, builder)(body, tips=gui_tips)
        view.adopt()
        self._gce_m_view = view
        if key == "cell":
            view._cell_redraw()
        return view

    def _gce_panel_cell(self, body):
        return self._gce_m_panel("cell", body)

    def _gce_panel_advanced(self, body):
        return self._gce_m_panel("advanced", body)

    def _gce_m_after_close(self):
        """A panel closed: the builders' lists drop its widgets, and the
        designer's own copies of what it showed are drawn again."""
        for name in _REG_LISTS:
            lst = self.__dict__.get(name)
            if isinstance(lst, list):
                lst[:] = [e for e in lst if _alive(e)]
        for fn in ("_sync_place_offset", "_cell_redraw",
                   "_schedule_mandrel_preview"):
            try:
                getattr(self, fn)()
            except Exception:  # noqa: BLE001 -- a designer form not built
                pass

    # ------------------------------------------------------------------ #
    # the tree
    # ------------------------------------------------------------------ #
    def _gce_top_rows(self):
        """Rows right under Whole file: the Collector, its size where a
        part's line range goes."""
        return [("d:collector",) + self._gce_c_label()]

    def _gce_c_label(self):
        """(label, size) of the Collector row, size in mm as the panel's."""
        mode = self.s_collector.get()
        try:
            if mode == "plate":
                if self.plate_shape.get() == "circular":
                    return ("Collector · wafer",
                            "Ø %s" % self.plate_diameter.get().strip())
                return ("Collector · plate", "%s×%s" % (
                    self.plate_x.get().strip(), self.plate_y.get().strip()))
            if mode == "mandrel":
                return ("Collector · mandrel",
                        "Ø %s" % self.m_diameter.get().strip())
        except (AttributeError, tk.TclError):
            pass
        return ("Collector · %s" % _SHORT.get(mode, mode), "")

    def _gce_design_open(self, iid):
        if iid == "d:collector":
            self._gce_c_open("collector")
            return True
        if self.s_collector.get() == "mandrel" and self._gce_m_why() is None:
            i = self._gce_d_index_of(iid)
            if iid.startswith("n:g:") and isinstance(i, int):
                self._gce_params_open(col=i)
                return True
            if iid.startswith("n:l:") and isinstance(i, int):
                # a layer: the layer table on it (4 axis; a plate's layer
                # row keeps its own double-click)
                self._gce_layer_params_open(i, int(iid.split(":")[4]))
                return True
            p = self._gce_m_parse(iid)
            if p is not None:
                self._gce_m_row_open(*p)
                return True
        return super()._gce_design_open(iid)

    def _gce_c_eye_var(self):
        return getattr(self, _EYE_VARS.get(self.s_collector.get(), ""), None)

    def _gce_design_eye(self, iid):
        if iid == "d:collector":
            var = self._gce_c_eye_var()
            if isinstance(var, tk.Variable):
                var.set(not bool(var.get()))
            self._gce_vis_apply()
            return
        p = self._gce_m_parse(iid)
        if p is not None:
            self._gce_m_eye(iid, *p)
            return
        return super()._gce_design_eye(iid)

    def _gce_row_hidden(self, iid):
        if iid == "d:collector":
            # the collector's own toggle, never a hidden or isolated part
            var = self._gce_c_eye_var()
            return isinstance(var, tk.Variable) and not bool(var.get())
        return super()._gce_row_hidden(iid)

    # ------------------------------------------------------------------ #
    # each tube's feature rows (4 axis): what `gui_gcode_design` puts under a
    # scaffold row, for a tube -- its pattern and its custom cell. A layer
    # that differs gets one Layer parameters row inside its own branch, as a
    # plate's does, never a row of its own under the scaffold (that
    # would list every layer twice). Row ids are
    # ``d:m<i>:<feature>``, i the page scaffold (0-based), and a layer's
    # Layer parameters row ``d:m<i>:L<k>`` (k 0-based).
    # ------------------------------------------------------------------ #
    def _gce_d_sources(self):
        # each page scaffold is a tube and a group of its own
        # (`scaffold_gcode` names them scaffold1..)
        if self.s_collector.get() == "mandrel":
            return list(range(self._gce_scaffold_count()))
        return super()._gce_d_sources()

    def _gce_design_rows(self, iid, node):
        if self.s_collector.get() != "mandrel":
            return super()._gce_design_rows(iid, node)
        if iid is None or node is None or self._gce_m_why() is not None:
            return []
        try:
            i = self._gce_d_index_of(iid)
            if not isinstance(i, int):
                return []
            if iid.startswith("n:l:"):
                k = int(iid.split(":")[4])
                if self._gce_layer_overrides(i).get(k):
                    return [("d:m%d:L%d" % (i, k), "Layer parameters")]
                return []
            if not iid.startswith("n:g:"):
                return []
            return self._gce_m_rows(i)
        except Exception:  # noqa: BLE001 -- a half-typed field: no rows
            return []

    def _gce_m_rows(self, i):
        import gui_mandrel
        supp = self._gce_d_suppressed()
        with self._gce_tube_scope(i):
            fam = self._mandrel_family_value()
        pre = "d:m%d:" % i
        rows = [(pre + "pattern", "Pattern · %s" % _family_short(fam))]
        if fam == gui_mandrel.MANDREL_DRAWN_FAMILY:
            rows.append((pre + "cell", "Custom cell"))
        return rows

    @staticmethod
    def _gce_m_parse(iid):
        """A tube row as (scaffold, feature): feature pattern / cell, or a
        layer's (layer,); None for any other row."""
        m = re.fullmatch(r"d:m(\d+):(pattern|cell|L(\d+))", iid or "")
        if not m:
            return None
        i = int(m.group(1))
        if m.group(3) is not None:
            return (i, (int(m.group(3)),))
        return (i, m.group(2))

    def _gce_m_row_open(self, i, feat):
        if isinstance(feat, tuple):
            self._gce_layer_params_open(i, feat[0])
        elif feat == "cell":
            self._gce_m_cell_open(i)
        else:
            self._gce_params_open(col=i)

    def _gce_m_groups(self, i):
        tree = self._gce_tree
        return [g for g in tree.get_children("") if g.startswith("n:g:")
                and self._gce_d_index_of(g) == i]

    def _gce_m_eye(self, iid, i, feat):
        """A tube row's eye HIDES what it makes, display only: a Layer
        parameters row its layer, the rest the whole tube."""
        tree = self._gce_tree
        groups = self._gce_m_groups(i)
        if isinstance(feat, tuple):
            a, b = feat[0], feat[-1]
            targets = [x for g in groups for x in tree.get_children(g)
                       if x.startswith("n:l:")
                       and a <= int(x.split(":")[4]) <= b]
        else:
            targets = groups
        hid = self._gce_hidden
        if self._gce_isolated:
            self._gce_isolated = None
        if iid in hid or any(g in hid for g in targets):
            hid.discard(iid)
            hid.difference_update(targets)
        else:
            hid.add(iid)
            hid.update(targets)
        self._gce_vis_apply()

    def _gce_design_menu(self, menu, iid):
        if self.s_collector.get() != "mandrel":
            return super()._gce_design_menu(menu, iid)
        if self._gce_m_why() is not None:
            return
        p = self._gce_m_parse(iid)
        if (iid or "").startswith("n:l:"):
            i = self._gce_d_index_of(iid)
            if not isinstance(i, int):
                return
            p = (i, (int(iid.split(":")[4]),))
        if p is None:
            return
        i, feat = p
        if isinstance(feat, tuple):
            menu.add_separator()
            menu.add_command(label="Layer parameters…",
                             command=lambda: self._gce_layer_params_open(
                                 i, feat[0]))


def _family_short(tok):
    """A family's label without its parenthesis: "Diamond helix"."""
    import gui_mandrel
    return gui_mandrel.MANDREL_FAMILY_VAL2LBL.get(tok, tok).split(" (")[0]


