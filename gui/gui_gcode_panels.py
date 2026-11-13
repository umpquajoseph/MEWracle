#!/usr/bin/env python3
"""gui_gcode_panels.py -- MODERN-UI ONLY: the Scaffold designer's Quality,
Export and Presets cards on the CAD-style G-code page.

  * Quality and Export are not ribbon tools: they are the This-design
    sections of the Settings window (gui_settings), which calls
    `_gce_panel_quality` / `_gce_panel_export` below to build them.
  * Quality -- the designer's Quality card (`_build_quality_tab`).
  * Export -- the designer's whole Export card (`_build_export_tab` plus the
    height rows of `_build_export_z_fields`), and one setting of this page's
    own: "Strip comments on save". It replaces the old Output options menu,
    whose four entries were rows of this same card.
  * Presets -- still a File-group ribbon tool, opening in the page's
    floating edit box: the designer's Presets card (`_build_one_presets_tab`), with
    a Design / Machine choice over it: kind "scaffold" or "machine", the
    same preset folders as the designer's and the Machine page's cards.

**The same Tk vars, not copies.** The designer's builders store their vars on
the App (``self.s_exit_move = tk.BooleanVar(...)``) and their widget handles
beside them (``self.s_exit_margin_entry``). Running one a second time on the
App would replace every var and steal every handle. So the builder runs over a
`_View` of the App instead: reads fall through to the App, so the second set
of widgets binds the App's own vars, while every assignment lands in the
view. An assignment over a var the App already has is dropped, so the var the
designer page shows stays the one this page shows. Flipping a setting here
flips it there, and the reverse.

A few methods must run with the view as `self` (a nested builder, a toggle
that greys the widget its own builder stored); `_View` is told which. Every
trace the second build adds to an App var is removed when the panel closes,
so opening a panel twenty times leaves nothing behind.

Presets is the one card whose state is not a var: each store keeps ONE grid
(``_pstores[kind]["table"]`` and its row maps), which every preset
button reads its selection from. The panel's grid is swapped in for the
length of each of its own button presses and swapped out after, so the
designer's grid never sees the page's.

`_gce_preset_scope()`, when the App has it, is a context manager the panel's
Design Load and Save run inside (Machine presets are shared, so they run
plain): the page's own design (the split of the page's
design from the designer's) answers the scaffold keys there. Without it they
act on the designer's vars."""
from gui_common import *
import contextlib
import types

import gui_form
import gui_tips
import gui_widgets

_TRACE_KEYS = ("table", "row_index", "parent_iid", "iid_name")
_TRACKED = {}


def _tracked(var, log):
    """`var` itself (same Tcl name, same value) whose trace_add also records
    the trace in `log`, so the panel can take it off again. Never unsets the
    Tcl var when collected: the App still owns it."""
    cls = type(var)
    sub = _TRACKED.get(cls)
    if sub is None:
        def trace_add(s, mode, callback):
            name = cls.trace_add(s, mode, callback)
            s._gcp_log.append((s, mode, name))
            return name
        sub = _TRACKED[cls] = type("Panel" + cls.__name__, (cls,),
                                   {"trace_add": trace_add,
                                    "__del__": lambda s: None})
    q = object.__new__(sub)
    q.__dict__.update(var.__dict__)
    q._gcp_log = log
    return q


class _View:
    """The App as a second run of a designer builder sees it (module doc).

    `own`: method names run with the view as `self`. `both`: run on the view
    and then on the App, so the designer's copy of a greyed widget follows
    too. `wrap`: name -> a callable that replaces the App's method."""

    def __init__(self, app, own=(), both=(), wrap=None):
        d = self.__dict__
        d["_app"] = app
        d["_own"] = {}
        d["_own_names"] = frozenset(own)
        d["_both"] = frozenset(both)
        d["_wrap"] = dict(wrap or {})
        d["_traces"] = []
        d["_vars"] = {}
        d["_collector"] = []
        d["_texts"] = []

    def __getattr__(self, name):
        d = self.__dict__
        if name in d["_own"]:
            return d["_own"][name]
        if name in d["_wrap"]:
            return d["_wrap"][name]
        app = d["_app"]
        if name in d["_own_names"]:
            return types.MethodType(getattr(type(app), name), self)
        if name in d["_both"]:
            fn = getattr(type(app), name)

            def both(*a, **k):
                fn(self, *a, **k)
                return fn(app, *a, **k)
            return both
        if name == "_collector_register":
            return lambda w, mode: d["_collector"].append((w, mode))
        if name == "_collector_text":
            return lambda w, plate, mandrel: d["_texts"].append(
                (w, {"plate": plate, "mandrel": mandrel}))
        val = getattr(app, name)
        if isinstance(val, tk.Variable):
            q = d["_vars"].get(name)
            if q is None:
                q = d["_vars"][name] = _tracked(val, d["_traces"])
            return q
        return val

    def __setattr__(self, name, value):
        if isinstance(getattr(self.__dict__["_app"], name, None),
                      tk.Variable):
            return                      # keep the App's var: it is shared
        self.__dict__["_own"][name] = value

    def settle(self):
        """After the build: show only this collector's rows and words, as
        `_apply_collector_mode` does on the designer page."""
        mode = self._app.s_collector.get()
        for w, m in self.__dict__["_collector"]:
            if mode not in (m if isinstance(m, tuple) else (m,)):
                try:
                    w.grid_remove()
                except (tk.TclError, AttributeError):
                    pass
        for w, words in self.__dict__["_texts"]:
            try:
                w.configure(text=words.get(mode, words["plate"]))
            except (tk.TclError, AttributeError):
                pass

    def release(self, _e=None):
        """Take every trace this build added off the App's vars."""
        log = self.__dict__["_traces"]
        while log:
            var, mode, name = log.pop()
            try:
                tk.Variable.trace_remove(var, mode, name)
            except (tk.TclError, ValueError):
                pass


class _GcodePanelsMixin:

    # ------------------------------------------------------------------ #
    # ribbon (the File group's three tools; gui_gcode_editor calls this)
    # ------------------------------------------------------------------ #
    def _build_gce_panel_tools(self, fil):
        tool = self._gce_tool
        self.gce_strip_comments = tk.BooleanVar(
            value=bool(self.machine_strip_comments.get()))
        def follow(*_a):
            # an opened file keeps its own setting (off unless ticked here)
            doc = getattr(self, "_gce_doc", None) or {}
            if doc.get("source") != getattr(self, "_GCE_FILE", object()):
                self.gce_strip_comments.set(
                    bool(self.machine_strip_comments.get()))
        self.machine_strip_comments.trace_add("write", follow)
        t = self._gce_ptools = {}
        t["presets"] = tool(
            fil, "file", "presets", "Presets",
            "Load or save a preset, by name: Design (every setting of the "
            "design) or Printer (the Printer "
            "page's presets, the collector's size among them).",
            command=lambda: self._gce_panel_open("presets"))

    # ------------------------------------------------------------------ #
    # the panels, in the page's floating edit box
    # ------------------------------------------------------------------ #
    _GCE_PANEL_TITLES = {"quality": "Quality", "export": "Export",
                         "presets": "Presets", "colour": "Color"}

    def _gce_panel_open(self, key):
        if getattr(self, "_gce_panel_key", None) == key and \
                getattr(self, "_gce_form", None) is not None:
            self._gce_host_close()          # the tool again closes it
            return
        host, body = self._gce_host_open(self._GCE_PANEL_TITLES[key])
        body.columnconfigure(0, weight=1)
        view = getattr(self, "_gce_panel_" + key)(body)
        if view is not None:
            view.settle()
            host.bind("<Destroy>", lambda e, v=view: v.release()
                      if e.widget is host else None, add="+")
        btns = ttk.Frame(host)
        btns.grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="Done", command=self._gce_host_close).pack(
            side="left")
        self._gce_panel_key = key
        host.bind("<Destroy>", lambda e: self._gce_panel_closed(e, host),
                  add="+")
        for btn_key, b in self._gce_ptools.items():
            b.set_on(btn_key == key)

        def _esc(_e=None):
            self._gce_host_close()
            return "break"
        for w in self._gce_walk(host):
            w.bind("<Escape>", _esc, add="+")

    def _gce_panel_closed(self, e, host):
        if e.widget is not host:
            return
        self._gce_panel_key = None
        for b in getattr(self, "_gce_ptools", {}).values():
            try:
                b.set_on(False)
            except tk.TclError:
                pass

    def _gce_build_quality_row(self, body):
        """Row 0 of a Quality panel, Design's or Calibrate's."""
        # one label column across both blocks
        body._form_card = True
        top = ttk.Frame(body)
        top.grid(row=0, column=0, sticky="ew")
        gui_form.Form(top).combo(
            "Build quality", self.gen_render,
            [lbl for lbl, _ in RENDER_CHOICES],
            "How much detail Generate builds now, on every page. Lines / "
            "Fibers draw the fiber paths from the fast cache and skip the "
            "slow full-mesh STL; Full mesh builds the exact STL. Saving or "
            "Export always produces the exact mesh.\n\n"
            + gui_tips.GLOSSARY.get("render quality", ""))

    def _gce_panel_quality(self, body):
        view = _View(self, own=("_build_quality_tab",),
                     both=("_toggle_scaffold_sag",))
        self._gce_build_quality_row(body)
        if not getattr(self, "_cal_active", lambda: False)():
            preview = ttk.Button(body, text="Preview at export quality",
                                 command=self._gce_export_preview_start)
            preview.grid(row=2, column=0, sticky="w", pady=(8, 0))
            gui_tips.add_tip(preview, "Run the selected export solver for this design. "
                                     "The live preview returns when you edit it.")
        rest = ttk.Frame(body)
        rest.grid(row=1, column=0, sticky="ew")
        view._build_quality_tab(rest)
        # what `_physics_deposit_changed` would grey (gui_sim)
        state = "normal" if self.sim_physics_deposit.get() else "disabled"
        for name in ("s_fill_per_tau_entry", "s_auto_corner_grid_check"):
            w = view.__dict__["_own"].get(name)
            if w is not None:
                self._set_entry_state(w, state)
        view._toggle_scaffold_sag()
        return view

    def _gce_panel_export(self, body):
        view = _View(self, own=("_build_export_tab", "_build_export_z_fields",
                                "_build_custom_gcode_fields",
                                "_sync_no_scaffold_row"),
                     both=("_toggle_exit_move",),
                     wrap={"_build_quality_tab": lambda parent: None})
        view._build_export_tab(body, quality_into=body)
        EF = view._export_form
        EF.section("Saving from this page")
        EF.check(
            "Strip comments on save", self.gce_strip_comments,
            "On: Save writes this text without its comments, "
            "so the saved g-code carries only commands. The text on this "
            "page keeps them.\n\n"
            "Off: Save writes every comment as typed. For the app's own "
            "design this starts from the Printer & collector page's Strip "
            "comments setting. A g-code file you opened starts Off, so its "
            "comments are kept unless you tick this.")
        return view

    #: the Presets panel's two kinds: (store kind, segment label, blurb)
    _GCE_PRESET_KINDS = (
        ("scaffold", "Design",
         "Load a preset into the design, or save the design as one."),
        ("machine", "Printer",
         "Load a printer preset, or save the printer as one: the collector's "
         "size, the axes, the flavor. These are the Printer & collector page's presets."),
    )

    def _gce_preset_kind(self):
        """Which kind the Presets panel shows: "scaffold" (the Design
        presets) or "machine". Remembered between sessions."""
        var = getattr(self, "gce_presets_kind", None)
        if var is None:
            kind = self._gce_prefs().get("presets_kind")
            if kind not in [k for k, _l, _b in self._GCE_PRESET_KINDS]:
                kind = "scaffold"
            var = self.gce_presets_kind = tk.StringVar(value=kind)
        return var

    def _gce_preset_kind_chosen(self):
        self._gce_prefs_set(presets_kind=self.gce_presets_kind.get())
        # the panel is rebuilt for the other kind's grid; after the click
        # that is still inside the panel's own widget
        self.root.after_idle(lambda: self._gce_panel_rebuild("presets"))

    def _gce_panel_rebuild(self, key):
        """Rebuild the open panel `key` in place (closed, opened again)."""
        if getattr(self, "_gce_panel_key", None) != key:
            return
        self._gce_host_close()
        self._gce_panel_open(key)

    def _gce_panel_presets(self, body):
        kvar = self._gce_preset_kind()
        kinds = [(k, lab, blurb) for k, lab, blurb in self._GCE_PRESET_KINDS
                 if k in self._pstores]
        if not kinds:
            ttk.Label(body, text="Presets are not available.").grid(
                row=0, column=0, sticky="w")
            return None
        if kvar.get() not in [k for k, _l, _b in kinds]:
            kvar.set(kinds[0][0])
        kind = kvar.get()
        blurb = dict((k, b) for k, _l, b in kinds)[kind]
        bar = gui_widgets.SegmentedBar(
            body, options=[(k, lab) for k, lab, _b in kinds], variable=kvar,
            command=self._gce_preset_kind_chosen)
        bar.grid(row=0, column=0, sticky="w", pady=(0, 6))
        # Design: the page's own design (`_gce_preset_scope`); Machine: the
        # machine, which the page and the designer share
        inner = ttk.Frame(body)
        inner.grid(row=1, column=0, sticky="nsew")
        inner.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)      # a taller box: a longer list
        st = self._pstores[kind]
        designer = {k: st.get(k) for k in _TRACE_KEYS}
        mine = {}
        scoped = ("_preset_load", "_preset_save_new", "_preset_update") \
            if kind == "scaffold" else ()
        app = self

        def call(name):
            def run(*a, **k):
                keep = {k2: st.get(k2) for k2 in _TRACE_KEYS}
                st.update(mine)
                try:
                    scope = getattr(app, "_gce_preset_scope", None)
                    ctx = scope() if (scope is not None and name in scoped) \
                        else contextlib.nullcontext()
                    with ctx:
                        return getattr(app, name)(*a, **k)
                finally:
                    mine.update({k2: st.get(k2) for k2 in _TRACE_KEYS})
                    st.update(keep)
                    app._refresh_preset_table(kind)
            return run
        names = ("_preset_load", "_preset_save_new", "_preset_update",
                 "_preset_rename", "_preset_delete", "_preset_import",
                 "_preset_export", "_preset_set_default", "_preset_reset")
        view = _View(self, own=("_build_one_presets_tab",),
                     wrap={n: call(n) for n in names})
        try:
            view._build_one_presets_tab(inner, kind, blurb)
        finally:
            mine.update({k: st.get(k) for k in _TRACE_KEYS})
            st.update(designer)
        tree = mine.get("table")
        if tree is not None:
            # the panel floats over the picture: the first columns only (the
            # designer's card scrolls through them all)
            tree.configure(height=8,
                           displaycolumns=list(tree["columns"])[:4])
            # a wider box gives its width to the names
            tree.column("#0", stretch=True)
            load = call("_preset_load")

            def dbl(_e):
                sel = tree.selection()
                if sel and mine["row_index"].get(sel[0]) is not None:
                    load()
                return "break"
            tree.unbind("<Double-Button-1>")
            tree.bind("<Double-Button-1>", dbl)
        return view

    # ------------------------------------------------------------------ #
    # Save: this file's comment-stripping override
    # ------------------------------------------------------------------ #
    def _gce_save_ready(self, text):
        """`text` as Save writes it: without its comments while "Strip
        comments on save" is on. The engine's own Strip-comments rule
        (`mew_gcode.strip_comment`, in the document's flavor): a line that
        held only a comment goes, an empty line stays."""
        var = getattr(self, "gce_strip_comments", None)
        eng = self._gce_engine()
        if var is None or not var.get() or eng is None:
            out = text
        else:
            out = eng.strip_export_comments(text, self.gce_flavor_name())
        self._gce_remember_structure(text, out)
        return out

    def _gce_remember_structure(self, text, saved):
        """Every route that writes the page's text to a file comes through
        `_gce_save_ready`: keep the file's structure for when it is opened
        again (`mew_gcode.Document.remember_structure`). `text` must be the
        document's export as it stands; any other text is left alone."""
        m = self._gce_model_now() if hasattr(self, "_gce_model_now") \
            else None
        if m is None or self._gce_engine() is None:
            return
        try:
            # the model as the caller left it: `_gce_render` would re-sync
            # its options and lose the file name the header carries
            r = m.render()
            if r.text != text:
                return
            m.remember_structure(saved, r)
        except Exception:  # noqa: BLE001 -- a file opened later guesses
            pass
