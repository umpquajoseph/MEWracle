#!/usr/bin/env python3
"""gui_history.py -- the "Print history" page + capture hooks.

A lab-notebook layer over the twin: every Generate is auto-recorded (a full parameter
snapshot + timestamp + status + a fiber thumbnail), you promote the good runs into a
curated saved log with a note, and a run's DETAIL sheet shows its HEADLINE parameters
and notes, reproduces it, diffs two runs, edits a saved run, and exports it as a CSV
or a PDF run sheet.

Split of responsibility:
  * mew_history.py  -- the headless engine (store / diff / headline / CSV / optional
                       PDF). NO Tk/VTK.
  * this file       -- the Tk half: the capture hooks called from gui_run, and the
                       Results-section tab. Plain ttk so ONE builder serves the classic
                       dev UI and the modern shell.

Capture rides the single Generate choke point in gui_run.py, so it is UI-agnostic:
`_history_on_run_start` snapshots the fields when a run launches, and
`_history_on_run_finish` finalizes status/outputs and -- when the run produced an STL
-- fires an offscreen `stl_viewer --thumbnail` subprocess into the persistent history/
dir, because session outputs are otherwise swept on close. Both are called through a
`hasattr` guard in gui_run, so an App assembled without _HistoryMixin is a no-op.

Reproduce is the inverse of the snapshot: `_apply_preset_for_kind` per kind, which is
the preset-load path, so the trace-muting `_loading_preset` guard and the per-kind
re-sync come for free.

Part of the gcode_to_fibers_gui split; see gui_common.py.
"""
import datetime
import gui_prefs
import os
import secrets
import subprocess
import threading

from gui_common import *
import gui_fonts
import gui_form
import gui_tips
import gui_widgets

# The engine degrades gracefully: a missing engine drops the page (the
# Calibration posture). CSV always works; the PDF button is greyed when reportlab
# is absent (mew_history.HAS_REPORTLAB).
try:
    import mew_history
    _HAVE_HISTORY = True
except Exception:  # noqa: BLE001
    _HAVE_HISTORY = False

# The saved-log graph uses the optional-matplotlib plumbing:
# analysis_plots supplies the GUI-agnostic figure builder, the tkagg backend embeds
# it. Absent either, the graph panel shows a hint (the runs list still works).
try:
    import analysis_plots
    _HAVE_PLOTS = bool(analysis_plots.HAVE_MPL)
except Exception:  # noqa: BLE001
    analysis_plots = None
    _HAVE_PLOTS = False
try:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    _HAVE_MPL_TK = _HAVE_PLOTS
except Exception:  # noqa: BLE001
    _HAVE_MPL_TK = False


def _basename(p):
    return os.path.basename(p) if p else None


def _when(record):
    """A run's date and time as the list shows it, the name a person knows
    it by (the id's random suffix means nothing to them)."""
    return (record.get("ts", "") or record.get("id", "run")).replace("T", " ")


# The saved-log "Sort by" dropdown: (label, tree column, reverse?). The Date / time
# column holds zero-padded ISO-ish strings, so a lexical sort is chronological.
_HIST_SORT_MODES = (
    ("Most recent", "when", True),
    ("Oldest first", "when", False),
    ("Title (A-Z)", "title", False),
    ("Result", "result", False),
)


class _HistoryMixin:
    # ------------------------------------------------------------------ #
    # capture (called from gui_run via hasattr guards)
    # ------------------------------------------------------------------ #
    def _hist_store(self):
        """The per-user HistoryStore, created (and cached) on first use so capture
        works in BOTH UIs without touching __init__. Tests may preset
        `self.history_store` to a tmp dir; this returns that when present."""
        store = getattr(self, "history_store", None)
        if store is None:
            store = mew_history.HistoryStore(os.path.join(USER_DATA_DIR, "history"))
            self.history_store = store
        return store

    def _history_app_version(self):
        """Short git hash of the running build, cached; "" when git is unavailable
        (e.g. a frozen exe with no repo). CREATE_NO_WINDOW so the git spawn never
        flashes a console window on Windows (same flag the worker/thumbnail use)."""
        v = getattr(self, "_hist_app_version", None)
        if v is None:
            try:
                with open(resource_path("mewracle_build_version.txt"),
                          encoding="utf-8") as fh:
                    v = fh.read().strip()
            except OSError:
                v = ""
            try:
                if not v:
                    flags = 0x08000000 if IS_WINDOWS else 0   # CREATE_NO_WINDOW
                    v = subprocess.check_output(
                        ["git", "rev-parse", "--short", "HEAD"], cwd=HERE,
                        stderr=subprocess.DEVNULL, text=True,
                        creationflags=flags).strip()
            except Exception:  # noqa: BLE001
                v = ""
            self._hist_app_version = v
        return v

    def _history_on_run_start(self, mode, args):
        """Snapshot the fields at Generate time into a pending record; finalized in
        _history_on_run_finish. Called from gui_run.run() AFTER the progress bar
        starts, so nothing here is on the user-visible Generate latency path. Just a
        widget-read snapshot (~1 ms); the git build hash is looked up ONCE in a
        background thread (a git spawn can stall seconds on Windows/Defender) and the
        finish hook reads whatever the cache holds -- no title left blank vs slow."""
        if not _HAVE_HISTORY:
            return
        if not self._hist_store().capture_enabled():
            self._pending_history = None      # so the finish hook is a clean no-op
            return
        if not getattr(self, "_hist_appver_started", False):
            self._hist_appver_started = True
            threading.Thread(target=self._history_app_version, daemon=True).start()
        params = {k: self._snapshot_for_kind(k) for k in mew_history.PARAM_GROUPS}
        now = datetime.datetime.now()
        self._pending_history = {
            "id": now.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2),
            "ts": now.isoformat(timespec="seconds"),
            "mode": mode,
            "source": self._history_source(mode),
            "collector": (self.s_collector.get()
                          if hasattr(self, "s_collector") else ""),
            "params": params,
            "argv": [str(a) for a in args],
            "title": "",              # named by the user when saved to the log
        }
        if mode == "calibration":
            # which Calibrate type built it: the form groups hold every
            # type's fields, so Reproduce needs this to open the right one
            cal_type = getattr(self, "_cal_type", lambda: None)()
            if cal_type:
                self._pending_history["cal_type"] = cal_type
            kind = getattr(self, "sw_vars", {}).get("kind")
            self._pending_history["collector"] = (
                "mandrel" if kind is not None and kind.get()
                in getattr(self, "_SW_ROTARY_KINDS", ()) else "plate")

    def _history_source(self, mode):
        """Which page started this run. The G-code window builds its design
        through the designer's own "scaffold" run (`_gce_dispatch`), so `mode`
        alone recorded a G-code window Generate as the Scaffold designer's."""
        if mode == "gcode" or getattr(self, "_gce_dispatch", None):
            return "gcode_window"
        return mode

    def _history_on_run_finish(self, status, exit_code):
        """Finalize + append the pending record; fire an offscreen thumbnail when
        the run produced an STL. Called from gui_run._on_finished (all branches)."""
        if not _HAVE_HISTORY:
            return
        pending = getattr(self, "_pending_history", None)
        if not pending:
            return
        self._pending_history = None
        store = self._hist_store()

        rec = dict(pending)
        rec["status"] = status
        rec["exit_code"] = exit_code
        rec["app_version"] = getattr(self, "_hist_app_version", "") or ""
        rec["outputs"] = {"gcode": None, "stl": None, "depo": None}
        rec["thumb"] = None
        rec["error"] = ""

        # Outputs / thumbnail / archivable g-code ONLY for a successful run -- on a
        # failure last_stl/last_gcode may still hold a PREVIOUS run's files, which
        # would attribute stale outputs (and a stale g-code copy on promote) to this
        # one. A cancel scrubs+reconciles them, but a plain failure does not.
        if status == "ok":
            gco = getattr(self, "last_gcode", None)
            stl = getattr(self, "last_stl", None)
            rec["outputs"] = {"gcode": _basename(gco), "stl": _basename(stl),
                              "depo": _basename(getattr(self, "last_depo", None))}
            # keep the current-session absolute g-code path in memory (not persisted
            # -- it lives in the session scratch and is swept on close) so "keep
            # g-code copy" on promote can archive it while it still exists
            if not hasattr(self, "_hist_abs_gcode"):
                self._hist_abs_gcode = {}
            files = (self._hist_calibration_files(rec["id"])
                     if rec.get("source") == "calibration" else None)
            if files:
                # one entry, every file the print leaves the app as
                self._hist_abs_gcode[rec["id"]] = files
                rec["outputs"]["gcode_files"] = [os.path.basename(f)
                                                 for f in files]
            elif gco and os.path.isfile(gco):
                self._hist_abs_gcode[rec["id"]] = gco
            # the scaffold preview thumbnail -- only when the run generated a
            # scaffold (an STL on disk). g-code-only / preview quality -> no image.
            if stl and os.path.isfile(stl):
                try:
                    os.makedirs(store.thumbs_dir, exist_ok=True)
                    thumb_abs = os.path.join(store.thumbs_dir, rec["id"] + ".png")
                    cmd = worker_cmd("worker:viewer", stl, "--thumbnail", thumb_abs,
                                     unbuffered=False)
                    flags = 0x08000000 if IS_WINDOWS else 0   # CREATE_NO_WINDOW
                    subprocess.Popen(cmd, cwd=HERE, creationflags=flags)
                    rec["thumb"] = os.path.join("thumbs", rec["id"] + ".png")
                except Exception:  # noqa: BLE001 -- a failed thumb must not lose the rec
                    rec["thumb"] = None

        store.append(rec)
        if hasattr(self, "_hist_tree"):        # tab is built -> refresh it live
            try:
                self._hist_refresh()
            except Exception:  # noqa: BLE001
                pass

    def _hist_calibration_files(self, run_id):
        """A calibration print leaves the app as one file per slide (one per
        rung of a temperature ladder), the bytes Save g-code as… writes.
        Write that same set into the session folder now, while the result
        is the one this run built, so Save run… can keep all of it."""
        exp = getattr(self, "_sw_export_gcode", None)
        if not callable(exp) or getattr(self, "_sw_result", None) is None:
            return None
        folder = os.path.join(self.session_dir, "history_files", run_id)
        try:
            os.makedirs(folder, exist_ok=True)
            names = exp(folder, self._sw_default_name(), False)
        except OSError:
            return None
        files = [os.path.join(folder, n) for n in names
                 if n.lower().endswith(".gcode")]
        return files or None

    def reproduce_run(self, record):
        """Re-arm every field from a run's grouped snapshot (the inverse of capture).
        Delegates to _apply_preset_for_kind per kind -- the preset-load path, so the
        `_loading_preset` trace-muting and each kind's re-sync happen for us."""
        params = record.get("params") or {}
        zero_was = self._hist_zero_now()
        for kind in mew_history.PARAM_GROUPS:
            data = params.get(kind)
            if data:
                self._apply_preset_for_kind(kind, data)
        # a run restores the zero it printed with together with its Start
        # X/Y (`_zero_design_loaded` is for saved designs, not runs): when
        # that is not where the printer's zero was, say so, since the zero
        # is the printer's setting and every later design is measured from it
        zero_is = self._hist_zero_now()
        zero = ""
        if zero_was and zero_is and (
                abs(zero_was[0][0] - zero_is[0][0]) > 1e-9
                or abs(zero_was[0][1] - zero_is[0][1]) > 1e-9):
            zero = ("The printer's zero is back where this run had it, at "
                    "%s (it was at %s). " % (zero_is[1], zero_was[1]))
        # a calibration print is generated on Calibrate, so go there; any
        # other run is generated from wherever Generate is pressed
        calib = record.get("source") == "calibration"
        if calib:
            try:
                self._nav_select("calibrate")
                var = getattr(self, "_cal_tab_var", None)
                if var is not None and record.get("cal_type"):
                    var.set(record["cal_type"])
            except Exception:  # noqa: BLE001 -- the settings are loaded anyway
                calib = False
        # which physics the reproduced print uses, as a design load says it
        cts = getattr(self, "_cal_cts_phrase", None)
        physics = ("Its physics are on the Physics page (%s). " % cts()
                   if params.get("calibration") and callable(cts) else "")
        self.status.set(
            "Loaded the settings of the run from " + _when(record) + ". "
            + physics + zero
            + ("This is its calibration print. " if calib else "")
            + "Review them, then Generate.")

    # ------------------------------------------------------------------ #
    # the tab -- a list (broad history <-> saved log) that opens a DETAIL sheet
    # ------------------------------------------------------------------ #
    def _build_history_tab(self, nb):
        """Append the Print-history page to `nb` (no-op if the engine is absent --
        the shell then registers no page). Mounted by gui_shell like Machine link.

        The broad history and the saved log switch via a segmented toggle over two
        STACKED tree frames -- NOT an inner ttk.Notebook (the modern shell forbids
        any notebook but its own; test_ui_shell.no_inner_notebooks). Double-click a
        run (or Preview) to open its detail sheet."""
        if not _HAVE_HISTORY:
            return
        # the page margin every other page uses, and the same head: a title
        # and the page's one grey intro line
        margin = gui_fonts.PAGE_MARGIN
        page = ttk.Frame(nb, padding=(margin, 8))
        page.columnconfigure(0, weight=1)
        page.rowconfigure(3, weight=1)

        ttk.Label(page, text="Print history", style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        gui_form.page_intro(
            page, "Every Generate is recorded here. Double-click a run for its "
                  "details and notes.", row=1, wrap=760)

        # top bar: the view toggle on the left -- the same SegmentedBar the
        # collector toggle is, not a pair of radios -- and the auto-capture
        # switch on the right. A list's toolbar, not a parameter form.
        self._hist_view = tk.StringVar(value="history")
        topbar = gui_form.exempt(ttk.Frame(page),
                                 "the runs list's toolbar, not a parameter row")
        topbar.grid(row=2, column=0, sticky="ew")
        topbar.columnconfigure(0, weight=1)
        seg = gui_widgets.SegmentedBar(
            topbar, options=(("history", "History"), ("saved", "Saved runs")),
            variable=self._hist_view,
            command=lambda _v=None: self._hist_switch_view())
        seg.grid(row=0, column=0, sticky="w")
        gui_tips.add_tip(seg, "History is every run Generate recorded. Saved "
                              "runs are the ones you kept with Save run…, "
                              "with a trend graph under them.")
        # auto-capture toggle -- persisted in the store's config.json, so it survives
        # restarts and gates _history_on_run_start without touching the preset map
        self._hist_capture_var = tk.BooleanVar(
            value=self._hist_store().capture_enabled())
        cap = ttk.Checkbutton(topbar, text="Record every Generate",
                              variable=self._hist_capture_var,
                              command=self._hist_toggle_capture)
        cap.grid(row=0, column=1, sticky="e")
        gui_tips.add_tip(cap, "Record every Generate here as it starts. Off, "
                              "a run is only recorded with Add a print….")

        # both tree frames occupy the SAME cell; one is grid_remove()d at a time
        container = ttk.Frame(page)
        container.grid(row=3, column=0, sticky="nsew", pady=4)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        self._hist_tree, self._hist_recmap, self._hist_frame = \
            self._hist_make_tree(container)
        # the SAVED view is split in half vertically: the runs list on top, a
        # time-series graph panel below (the History view stays a plain full list)
        self._hist_saved_tree, self._hist_saved_recmap, self._hist_saved_frame = \
            self._hist_make_saved_pane(container)
        self._hist_saved_frame.grid_remove()

        bar = ttk.Frame(page)
        bar.grid(row=4, column=0, sticky="ew", pady=(4, 8))
        add = ttk.Button(bar, text="Add a print…", command=self._hist_new_manual)
        add.pack(side="left")
        gui_tips.add_tip(add, "Record a print you ran outside the app, with "
                              "whatever settings you know.")
        prev = ttk.Button(bar, text="Preview",
                          command=self._hist_open_detail_selected)
        prev.pack(side="left", padx=4)
        gui_tips.add_tip(prev, "Open the selected run's details and notes. "
                               "Its run sheet exports from there.")
        repro = ttk.Button(bar, text="Reproduce", command=self._hist_reproduce)
        repro.pack(side="left", padx=4)
        gui_tips.add_tip(repro, "Load the selected run's settings back into "
                                "the app.")
        diff = ttk.Button(bar, text="Compare two", command=self._hist_diff)
        diff.pack(side="left", padx=4)
        gui_tips.add_tip(diff, "Compare the settings of two runs. Hold Ctrl "
                               "and click to select the second run.")
        promote = ttk.Button(bar, text="Save run…",
                             command=self._hist_promote)
        promote.pack(side="left", padx=4)
        gui_tips.add_tip(promote, "Keep the selected run under Saved runs, with "
                                  "a title and a note.")
        delete = ttk.Button(bar, text="Delete…", command=self._hist_delete)
        delete.pack(side="left", padx=4)
        ttk.Button(bar, text="Refresh", command=self._hist_refresh).pack(
            side="right")
        # each button waits for a selection it can act on, so a press never
        # ends in a "select a run" popup: one run, exactly two, or any
        self._hist_sel_btns = ((prev, "one"), (repro, "one"), (diff, "two"),
                               (promote, "one"), (delete, "any"))
        for tree in (self._hist_tree, self._hist_saved_tree):
            tree.bind("<<TreeviewSelect>>",
                      lambda _e: self._hist_sync_buttons(), add="+")

        self._hist_refresh()
        nb.add(page, text="Print history")

    def _hist_switch_view(self):
        """Raise the History or Saved-log tree per the segmented toggle."""
        if self._hist_view.get() == "saved":
            self._hist_frame.grid_remove()
            self._hist_saved_frame.grid()
        else:
            self._hist_saved_frame.grid_remove()
            self._hist_frame.grid()
        self._hist_sync_buttons()

    def _hist_sync_buttons(self):
        """Grey each toolbar button the visible list's selection cannot feed."""
        n = len(self._hist_selected_records())
        ok = {"one": n == 1, "two": n == 2, "any": n >= 1}
        for btn, need in getattr(self, "_hist_sel_btns", ()):
            try:
                btn.state(["!disabled"] if ok[need] else ["disabled"])
            except tk.TclError:
                pass

    def _hist_new_tree(self, parent):
        """Build a scrolled runs Treeview as a child of `parent`; return (tree,
        frame) with the frame UNGRIDDED (the caller places it). Shared by the
        History view and the Saved-log split pane. Double-click opens the run's
        detail sheet; the binding resolves the active list via _hist_active."""
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        cols = ("when", "mode", "result", "title")
        tree = ttk.Treeview(frame, columns=cols, show="headings",
                            selectmode="extended", height=8)
        for c, head, w in (("when", "Date / time", 160), ("mode", "Made in", 170),
                           ("result", "Result", 90), ("title", "Title", 260)):
            # click a header to sort by that column (Date / time gives most-recent
            # first — the ISO-ish timestamp string sorts chronologically)
            tree.heading(c, text=head, anchor="w",
                         command=lambda col=c, t=tree: self._hist_sort_column(t, col))
            tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        tree.bind("<Double-1>", lambda _e: self._hist_open_detail_selected())
        return tree, frame

    def _hist_make_tree(self, container):
        """A (Treeview, recmap, frame) triple gridded into `container` row 0 -- the
        History view's plain full-height list."""
        tree, frame = self._hist_new_tree(container)
        frame.grid(row=0, column=0, sticky="nsew")
        return tree, {}, frame

    def _hist_make_saved_pane(self, container):
        """The SAVED-log view: `container`'s cell split in half vertically -- the
        runs list on top, the time-series graph panel below. Returns
        (tree, recmap, frame) matching _hist_make_tree so the view toggle can
        grid_remove() it exactly like the History frame. The uniform rows force a
        true 50/50 split regardless of content."""
        frame = ttk.Frame(container)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1, uniform="hist_half")
        frame.rowconfigure(1, weight=1, uniform="hist_half")
        # top half: a visible "Sort by" control above the runs list (column headers
        # are also click-to-sort, but the dropdown makes the option discoverable)
        top = ttk.Frame(frame)
        top.grid(row=0, column=0, sticky="nsew")
        top.columnconfigure(0, weight=1)
        top.rowconfigure(1, weight=1)
        sortbar = gui_form.exempt(ttk.Frame(top),
                                  "the saved list's toolbar, not a parameter row")
        sortbar.grid(row=0, column=0, sticky="ew", pady=(0, 3))
        ttk.Label(sortbar, text="Sort by:").pack(side="left")
        self._hist_sort_choice = tk.StringVar(value=_HIST_SORT_MODES[0][0])
        cb = ttk.Combobox(sortbar, textvariable=self._hist_sort_choice,
                          values=[m[0] for m in _HIST_SORT_MODES],
                          state="readonly", width=16)
        cb.pack(side="left", padx=(4, 0))
        gui_tips.add_tip(cb, "The order of the saved runs. The column headers "
                             "also sort when clicked.")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._hist_apply_saved_sort())
        tree, treeframe = self._hist_new_tree(top)
        treeframe.grid(row=1, column=0, sticky="nsew")
        self._hist_build_graph_panel(frame).grid(row=1, column=0, sticky="nsew",
                                                  pady=(6, 0))
        return tree, {}, frame

    def _hist_apply_saved_sort(self):
        """Order the saved-log tree per the 'Sort by' dropdown (default most-recent).
        A no-op until the saved pane + its tree exist."""
        tree = getattr(self, "_hist_saved_tree", None)
        choice = getattr(self, "_hist_sort_choice", None)
        if tree is None or choice is None:
            return
        _label, col, reverse = next(
            (m for m in _HIST_SORT_MODES if m[0] == choice.get()),
            _HIST_SORT_MODES[0])
        rows = [(tree.set(iid, col), iid) for iid in tree.get_children("")]
        rows.sort(key=lambda r: r[0], reverse=reverse)
        for idx, (_v, iid) in enumerate(rows):
            tree.move(iid, "", idx)

    # ---- saved-log graph (bottom half of the saved view) ---- #
    def _hist_build_graph_panel(self, parent):
        """The graph panel: a Process-variable dropdown + Graph button over an
        embedded time-series figure. Select runs in the list above, choose a
        variable, press Graph to trend it across those runs by date/time. Degrades to
        a hint when matplotlib is absent (the runs list stays fully usable)."""
        outer = ttk.Frame(parent)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        ctl = gui_form.exempt(ttk.Frame(outer),
                              "the trend graph's toolbar, not a parameter row")
        ctl.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 4))
        ttk.Label(ctl, text="Trend variable:").pack(side="left")
        self._hist_graph_vars = mew_history.process_headline_keys()   # [(key,label)]
        labels = [lbl for _k, lbl in self._hist_graph_vars]
        self._hist_graph_var = tk.StringVar(value=labels[0] if labels else "")
        tv = ttk.Combobox(ctl, textvariable=self._hist_graph_var,
                          values=labels, state="readonly", width=30)
        tv.pack(side="left", padx=(4, 8))
        gui_tips.add_tip(tv, "The process value to trend across the runs "
                             "selected above, by date and time.")
        ttk.Button(ctl, text="Graph", command=self._hist_graph_selected).pack(
            side="left")

        self._hist_graph_body = ttk.Frame(outer)
        self._hist_graph_body.grid(row=1, column=0, sticky="nsew")
        self._hist_graph_body.columnconfigure(0, weight=1)
        self._hist_graph_body.rowconfigure(0, weight=1)
        self._hist_graph_canvas = None
        hint = ("Select runs above, pick a variable, then press Graph."
                if _HAVE_MPL_TK else
                "matplotlib is not installed, so graphs are off. Install it "
                "(pip install matplotlib). The runs list still works.")
        # the graph's empty state -- a status, so it stays text
        gui_form.mark(ttk.Label(self._hist_graph_body, text=hint,
                                style="Hint.TLabel", wraplength=520),
                      "live").grid(row=0, column=0)
        return outer

    def _hist_graph_selected(self):
        """Draw the chosen Process variable across the SELECTED saved runs, over
        time. Runs without a numeric value for that variable are skipped (reported in
        the status line)."""
        if not _HAVE_MPL_TK:
            self._refuse("matplotlib is not installed. Install it to "
                "draw graphs (pip install matplotlib).")
            return
        recs = self._hist_selected_records()
        if not recs:
            self._refuse("Select one or more saved runs to graph "
                                "(click, or Ctrl/Shift-click for several).")
            return
        label = self._hist_graph_var.get()
        key = next((k for k, lbl in self._hist_graph_vars if lbl == label), None)
        if key is None:
            return
        pts = mew_history.series_points(recs, key)      # run-identified points
        series = [(p["t"], p["v"]) for p in pts]
        try:
            fig = analysis_plots.history_series_figure(series, label=label)
        except Exception as exc:  # noqa: BLE001
            self._refuse('Could not draw the graph: %s.' % exc)
            return
        for w in self._hist_graph_body.winfo_children():
            w.destroy()
        self._hist_graph_canvas = FigureCanvasTkAgg(fig, master=self._hist_graph_body)
        self._hist_graph_canvas.draw()
        self._hist_graph_canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        # remember what's plotted so a click can name the run under the cursor
        self._hist_graph_points = pts
        self._hist_graph_label = label
        self._hist_graph_ax = fig.axes[0] if fig.axes else None
        self._hist_graph_annot = None
        self._hist_graph_canvas.mpl_connect("button_press_event",
                                            self._hist_graph_click)
        skipped = len(recs) - len(series)
        msg = f"Graphed {label} across {len(series)} run(s)."
        if skipped:
            msg += (f" {skipped} selected run(s) had no numeric value and were "
                    "skipped.")
        if len(series):
            msg += "  Click a point to identify its run."
        if hasattr(self, "status"):
            self.status.set(msg)

    def _hist_graph_click(self, event):
        """On a click in the graph, find the nearest plotted point (by pixel
        distance) and label it with its run — an on-plot callout plus the status
        line. Clicks far from any point are ignored."""
        ax = getattr(self, "_hist_graph_ax", None)
        pts = getattr(self, "_hist_graph_points", None)
        if ax is None or not pts or event.inaxes is not ax:
            return
        import matplotlib.dates as mdates
        best_i, best_d = None, None
        for i, p in enumerate(pts):
            px, py = ax.transData.transform((mdates.date2num(p["t"]), p["v"]))
            d = ((px - event.x) ** 2 + (py - event.y) ** 2) ** 0.5
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        if best_i is None or best_d > 40:       # not near enough to a point
            return
        p = pts[best_i]
        label = getattr(self, "_hist_graph_label", "value")
        when = p["t"].strftime("%Y-%m-%d %H:%M")
        title = p["title"] or p["id"]
        annot = getattr(self, "_hist_graph_annot", None)
        if annot is not None:
            try:
                annot.remove()
            except Exception:  # noqa: BLE001
                pass
        self._hist_graph_annot = ax.annotate(
            f"{title}\n{when}\n{label}: {p['v']:g}",
            xy=(mdates.date2num(p["t"]), p["v"]), xytext=(10, 12),
            textcoords="offset points", fontsize=8, ha="left", va="bottom",
            bbox=dict(boxstyle="round", fc="#ffffe0", ec="#999", alpha=0.95),
            arrowprops=dict(arrowstyle="->", color="#666"))
        self._hist_graph_canvas.draw_idle()
        if hasattr(self, "status"):
            self.status.set(f"{title}  ·  {when}  ·  {label} = {p['v']:g}")

    def _hist_refresh(self):
        """Reload both lists from the store (pruning expired unlogged runs first)."""
        if not hasattr(self, "_hist_tree"):
            return
        store = self._hist_store()
        store.prune()          # drop unlogged runs past the 30-day window on view
        self._hist_fill(self._hist_tree, self._hist_recmap, store.load_runs())
        self._hist_fill(self._hist_saved_tree, self._hist_saved_recmap,
                        store.load_saved())
        self._hist_apply_saved_sort()   # honor the Sort-by dropdown (default recent)
        self._hist_sync_buttons()       # a refill drops the selection

    def _hist_sort_column(self, tree, col):
        """Sort a runs tree by a clicked column header, toggling asc/desc per column.
        The 'Date / time' column holds zero-padded ISO-ish strings, so a lexical sort
        is chronological -- the first click gives most-recent-first."""
        if not hasattr(self, "_hist_sort_dir"):
            self._hist_sort_dir = {}
        key = (id(tree), col)
        reverse = not self._hist_sort_dir.get(key, False)   # 1st click on date → desc
        self._hist_sort_dir[key] = reverse
        rows = [(tree.set(iid, col), iid) for iid in tree.get_children("")]
        rows.sort(key=lambda r: r[0], reverse=reverse)
        for idx, (_v, iid) in enumerate(rows):
            tree.move(iid, "", idx)

    def _hist_fill(self, tree, recmap, records):
        tree.delete(*tree.get_children())
        recmap.clear()
        for r in records:
            rid = r.get("id", "")
            when = (r.get("ts", "") or "").replace("T", "  ")
            tree.insert("", "end", iid=rid,
                        values=(when, mew_history.mode_label(r), mew_history._result(r),
                                r.get("title", "")))
            recmap[rid] = r

    def _hist_active(self):
        """(tree, recmap, is_saved_view) for the visible sub-list."""
        if getattr(self, "_hist_view", None) is not None and \
                self._hist_view.get() == "saved":
            return self._hist_saved_tree, self._hist_saved_recmap, True
        return self._hist_tree, self._hist_recmap, False

    def _hist_selected_records(self):
        tree, recmap, _saved = self._hist_active()
        return [recmap[i] for i in tree.selection() if i in recmap]

    # ---- detail sheet (double-click / Preview) ---- #
    def _hist_open_detail_selected(self):
        recs = self._hist_selected_records()
        if len(recs) != 1:
            self._refuse("Select one run to preview.")
            return
        _tree, _rm, is_saved = self._hist_active()
        self._hist_open_detail(recs[0], is_saved)

    def _hist_open_detail(self, record, is_saved):
        top = tk.Toplevel(self.root)
        top.title(record.get("title") or "Run of " + _when(record))
        top.transient(self.root)
        # wide enough for the five action buttons, Close included
        gui_fonts.size_dialog(top, 680, 640)
        outer = ttk.Frame(top, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        scroll = VScrollFrame(outer)
        scroll.grid(row=0, column=0, sticky="nsew")
        host = scroll.body                     # VScrollFrame's inner content frame
        host.columnconfigure(0, weight=1)

        # preview at the TOP -- only when the run generated a scaffold thumbnail
        thumb = record.get("thumb")
        path = os.path.join(self._hist_store().base_dir, thumb) if thumb else None
        if path and os.path.isfile(path):
            try:
                img = tk.PhotoImage(file=path)
                f = max(1, img.width() // 240)
                im = img.subsample(f, f)
                lbl = ttk.Label(host, image=im)
                lbl.image = im         # ref on the widget -> multiple detail sheets
                lbl.grid(row=0, column=0, pady=(0, 8))   # don't blank each other
            except Exception:  # noqa: BLE001
                pass

        meta = "   ·   ".join(x for x in (
            (record.get("ts", "") or "").replace("T", "  "),
            mew_history.mode_label(record), mew_history._result(record),
            ("op: " + record["operator"]) if record.get("operator") else "",
            ("build " + record["app_version"]) if record.get("app_version") else "",
        ) if x)
        ttk.Label(host, text=meta, foreground="#777").grid(
            row=1, column=0, sticky="w")
        if record.get("tags"):
            ttk.Label(host, text="tags: " + ", ".join(record["tags"]),
                      foreground="#777").grid(row=2, column=0, sticky="w")

        # headline parameters
        r = 3
        for section, rows in mew_history.headline_rows(record):
            ttk.Label(host, text=section, font=("", 10, "bold")).grid(
                row=r, column=0, sticky="w", pady=(10, 2))
            r += 1
            grid = ttk.Frame(host)
            grid.grid(row=r, column=0, sticky="ew")
            grid.columnconfigure(1, weight=1)
            for i, (label, val) in enumerate(rows):
                ttk.Label(grid, text=label, foreground="#555").grid(
                    row=i, column=0, sticky="w", padx=(8, 16))
                ttk.Label(grid, text=val).grid(row=i, column=1, sticky="w")
            r += 1

        ttk.Label(host, text="Notes", font=("", 10, "bold")).grid(
            row=r, column=0, sticky="w", pady=(10, 2))
        r += 1
        ttk.Label(host, text=record.get("note") or "(no notes)",
                  foreground="#555" if record.get("note") else "#999",
                  wraplength=500, justify="left").grid(row=r, column=0, sticky="w",
                                                       padx=(8, 0))

        # action bar (bottom of the dialog)
        bar = ttk.Frame(outer)
        bar.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(bar, text="Edit",
                   command=lambda: (top.destroy(),
                                    self._hist_edit(record, is_saved))).pack(
            side="left")
        ttk.Button(bar, text="Reproduce",
                   command=lambda: (top.destroy(),
                                    self.reproduce_run(record))
                   ).pack(side="left", padx=4)
        kept = mew_history.kept_files(record) if is_saved else []
        if kept:
            ttk.Button(bar, text=("Save g-code…" if len(kept) == 1 else
                                  "Save its %d g-code files…" % len(kept)),
                       command=lambda: self._hist_save_kept(record)).pack(
                side="left", padx=4)
        ttk.Button(bar, text="Export CSV…",
                   command=lambda: self._hist_export_csv(record)).pack(
            side="left", padx=4)
        pdf_state = "normal" if mew_history.HAS_REPORTLAB else "disabled"
        pdf_btn = ttk.Button(bar, text="Export PDF…", state=pdf_state,
                             command=lambda: self._hist_export_pdf(record))
        pdf_btn.pack(side="left", padx=4)
        if not mew_history.HAS_REPORTLAB:
            self._hist_tip(pdf_btn, "PDF run sheets are not available in "
                                    "this build. Export CSV… writes the same "
                                    "values.")
        ttk.Button(bar, text="Close", command=top.destroy).pack(side="right")

    # ---- edit (notes + headline values) ---- #
    def _hist_edit(self, record, is_saved):
        if not is_saved:
            # a yes opens the Save-to-log dialog, which can still be
            # cancelled, so "Don't ask again" skips only the explanation
            text = ("Edits live on the SAVED log, so the auto-recorded "
                    "history stays a faithful record.\n\nSave this run to "
                    "the log now so you can edit its notes and headline "
                    "values?")
            ask = getattr(self, "_gw_dialog", None)     # the modern App's
            if gui_prefs.is_hidden("history-edit-save-first"):
                yes = True
            elif callable(ask):
                yes = ask("Edit run", text,
                          [("yes", "Save to the log…"), ("no", "Cancel")],
                          default=0, cancel="no",
                          dont_ask="history-edit-save-first",
                          remember=("yes",)) == "yes"
            else:
                yes = messagebox.askyesno("Edit run", text)
            if not yes:
                return
            self._hist_promote_dialog(record)     # promote, then user re-opens to edit
            return
        # `record` is the saved entry itself (from the saved-log tree)
        top = tk.Toplevel(self.root)
        top.title("Edit " + (record.get("title") or "the run of " + _when(record)))
        top.transient(self.root)
        frm = ttk.Frame(top, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Title").grid(row=0, column=0, sticky="w")
        title = ttk.Entry(frm)
        title.insert(0, record.get("title", ""))
        title.grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(frm, text="Note").grid(row=1, column=0, sticky="nw")
        note = tk.Text(frm, width=46, height=4)
        note.insert("1.0", record.get("note", ""))
        note.grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(frm, text="Tags (comma-separated)").grid(row=2, column=0, sticky="w")
        tags = ttk.Entry(frm)
        tags.insert(0, ", ".join(record.get("tags", [])))
        tags.grid(row=2, column=1, sticky="ew", pady=2)
        ttk.Label(frm, text="Operator").grid(row=3, column=0, sticky="w")
        oper = ttk.Entry(frm)
        oper.insert(0, record.get("operator", ""))
        oper.grid(row=3, column=1, sticky="ew", pady=2)
        ttk.Label(frm, text="Result").grid(row=4, column=0, sticky="w")
        result = ttk.Entry(frm)
        result.insert(0, mew_history._result(record))
        result.grid(row=4, column=1, sticky="ew", pady=2)

        ttk.Label(frm, text="Headline parameters", font=("", 10, "bold")).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(10, 2))
        flat = mew_history._flat_params(record)
        entries = {}    # key -> (Entry widget)
        row = 6
        for _section, keys in mew_history.HEADLINE_SECTIONS:
            for key, label in keys:
                # show a captured value, OR a manual field (env.humidity) even when
                # this run never captured one -- so humidity can be typed on any run
                if key not in flat and key not in mew_history.MANUAL_HEADLINE_KEYS:
                    continue
                ttk.Label(frm, text=label, foreground="#555").grid(
                    row=row, column=0, sticky="w", padx=(8, 0))
                e = ttk.Entry(frm)
                e.insert(0, str(flat.get(key, "")))
                e.grid(row=row, column=1, sticky="ew", pady=1)
                entries[key] = e
                row += 1

        def _save():
            record["title"] = title.get().strip()
            record["note"] = note.get("1.0", "end").strip()
            record["tags"] = [t.strip() for t in tags.get().split(",") if t.strip()]
            record["operator"] = oper.get().strip()
            record["result"] = result.get().strip()
            for key, e in entries.items():
                val = e.get().strip()
                # a manual key absent from the record is only added once it's given a
                # value -- an untouched blank humidity stays absent (default blank)
                mew_history.set_param(
                    record, key, val,
                    add_if_missing=(key in mew_history.MANUAL_HEADLINE_KEYS
                                    and val != ""))
            self._hist_store().update_saved(record)
            top.destroy()
            self._hist_refresh()
            self.status.set("Updated the saved run from " + _when(record) + ".")

        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Save", command=_save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=top.destroy).pack(
            side="right", padx=6)

    # ---- auto-capture toggle ---- #
    def _hist_toggle_capture(self):
        """Persist the auto-record switch (the gate _history_on_run_start reads)."""
        try:
            self._hist_store().set_capture_enabled(self._hist_capture_var.get())
        except Exception:  # noqa: BLE001 -- a config write failure must not crash
            pass

    # ---- New: log a print by hand ---- #
    def _hist_group_params(self, flat):
        """Distribute a flat {key: value} of headline params into the four
        PARAM_GROUPS using the app's OWN kind ownership, so a manual entry's params
        land in the same groups a captured run's would (reproduce/diff stay
        consistent). Degrades to the scaffold catch-all when kind ownership isn't
        available (e.g. a bare test stub) or a key is owned by no kind."""
        grouped = {g: {} for g in mew_history.PARAM_GROUPS}
        owned = {}
        if hasattr(self, "_keys_for_kind"):
            for g in mew_history.PARAM_GROUPS:
                try:
                    owned[g] = set(self._keys_for_kind(g))
                except Exception:  # noqa: BLE001
                    owned[g] = set()
        for key, val in flat.items():
            dest = "scaffold"
            for g in mew_history.PARAM_GROUPS:
                if key in owned.get(g, ()):
                    dest = g
                    break
            grouped[dest][key] = val
        return grouped

    def _hist_create_manual(self, flat, title="", mode="manual", result="", note=""):
        """Build + append a manual run record from a flat headline dict (blank values
        dropped), then refresh and select it. Factored out of the dialog so it can be
        tested without driving a Toplevel. Returns the record."""
        flat = {k: v for k, v in (flat or {}).items() if str(v).strip() != ""}
        rec = mew_history.new_manual_record(
            params=self._hist_group_params(flat), title=title, mode=mode,
            result=result, note=note)
        self._hist_store().append(rec)
        # manual rows are unlogged -> show them under the History view, then select
        if getattr(self, "_hist_view", None) is not None:
            self._hist_view.set("history")
            self._hist_switch_view()
        self._hist_refresh()
        if hasattr(self, "_hist_tree") and self._hist_tree.exists(rec["id"]):
            self._hist_tree.selection_set(rec["id"])
            self._hist_tree.see(rec["id"])
        return rec

    def _hist_new_manual(self):
        """A dialog to manually log a print done outside the app. EVERY field is
        optional -- leaving a parameter blank simply omits it from the record, so
        nothing breaks; an all-blank entry still records a timestamped run."""
        top = tk.Toplevel(self.root)
        top.title("Add a print")
        top.transient(self.root)
        frm = ttk.Frame(top, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Log a print you ran outside the app. Every field is "
                  "optional. Fill in only what you know.",
                  foreground="#666", wraplength=430).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(frm, text="Title").grid(row=1, column=0, sticky="w")
        title = ttk.Entry(frm, width=40)
        title.grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(frm, text="Mode").grid(row=2, column=0, sticky="w")
        mode = ttk.Entry(frm, width=40)
        mode.insert(0, "manual")
        mode.grid(row=2, column=1, sticky="ew", pady=2)
        ttk.Label(frm, text="Result").grid(row=3, column=0, sticky="w")
        result = ttk.Entry(frm, width=40)
        result.grid(row=3, column=1, sticky="ew", pady=2)
        ttk.Label(frm, text="Note").grid(row=4, column=0, sticky="nw")
        note = tk.Text(frm, width=40, height=3)
        note.grid(row=4, column=1, sticky="ew", pady=2)

        ttk.Label(frm, text="Headline parameters", font=("", 10, "bold")).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(10, 2))
        entries = {}
        row = 6
        for section, keys in mew_history.HEADLINE_SECTIONS:
            ttk.Label(frm, text=section, foreground="#777").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(6, 1))
            row += 1
            for key, label in keys:
                ttk.Label(frm, text=label, foreground="#555").grid(
                    row=row, column=0, sticky="w", padx=(8, 0))
                e = ttk.Entry(frm, width=40)
                e.grid(row=row, column=1, sticky="ew", pady=1)
                entries[key] = e
                row += 1

        def _ok():
            flat = {k: e.get().strip() for k, e in entries.items() if e.get().strip()}
            rec = self._hist_create_manual(
                flat, title=title.get(), mode=mode.get(),
                result=result.get(), note=note.get("1.0", "end"))
            top.destroy()
            self.status.set("Logged manual print " + rec["id"] + ".")

        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Add to log", command=_ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=top.destroy).pack(
            side="right", padx=6)

    # ---- list-bar actions ---- #
    def _hist_zero_now(self):
        """((x, y), words) for the printer's plate zero, or None (a tube, or
        a build without the zero)."""
        where = getattr(self, "_zero_on_plate", None)
        name = getattr(self, "_zero_name_now", None)
        if not (callable(where) and callable(name)):
            return None
        try:
            xy = where()
            return (xy, name()) if xy is not None else None
        except Exception:  # noqa: BLE001 -- a message must not raise
            return None

    def _hist_reproduce(self):
        recs = self._hist_selected_records()
        if len(recs) != 1:
            self._refuse("Select exactly one run to reproduce.")
            return
        self.reproduce_run(recs[0])

    def _hist_diff(self):
        recs = self._hist_selected_records()
        if len(recs) != 2:
            self._refuse("Select exactly two runs to compare (Ctrl-click the second).")
            return
        self._hist_show_diff(recs[0], recs[1])

    def _hist_show_diff(self, a, b):
        d = mew_history.diff_runs(a, b)
        top = tk.Toplevel(self.root)
        top.title("Compare runs: " + _when(a) + "  →  " + _when(b))
        cols = ("group", "param", "a", "b")
        tv = ttk.Treeview(top, columns=cols, show="headings", height=18)
        for c, head, w in (("group", "Group", 120), ("param", "Parameter", 200),
                           ("a", _when(a), 150), ("b", _when(b), 150)):
            tv.heading(c, text=head, anchor="w")
            tv.column(c, width=w, anchor="w")
        tv.pack(fill="both", expand=True, padx=8, pady=8)
        for g, k, va, vb in d["changed"]:
            tv.insert("", "end", values=(g, k, va, vb))
        for g, k, vb in d["added"]:
            tv.insert("", "end", values=(g, k, "—", vb))
        for g, k, va in d["removed"]:
            tv.insert("", "end", values=(g, k, va, "—"))
        if not (d["changed"] or d["added"] or d["removed"]):
            tv.insert("", "end", values=("", "(identical parameters)", "", ""))

    def _hist_promote(self):
        recs = self._hist_selected_records()
        if len(recs) != 1:
            self._refuse("Select exactly one run to save.")
            return
        self._hist_promote_dialog(recs[0])

    def _hist_promote_dialog(self, record):
        top = tk.Toplevel(self.root)
        top.title("Save run")
        top.transient(self.root)
        frm = ttk.Frame(top, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="Title").grid(row=0, column=0, sticky="w")
        title = ttk.Entry(frm, width=44)
        title.insert(0, record.get("title", ""))
        title.grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(frm, text="Note").grid(row=1, column=0, sticky="nw")
        note = tk.Text(frm, width=44, height=4)
        note.grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(frm, text="Tags (comma-separated)").grid(row=2, column=0, sticky="w")
        tags = ttk.Entry(frm, width=44)
        tags.grid(row=2, column=1, sticky="ew", pady=2)
        ttk.Label(frm, text="Operator").grid(row=3, column=0, sticky="w")
        oper = ttk.Entry(frm, width=44)
        oper.grid(row=3, column=1, sticky="ew", pady=2)
        ttk.Label(frm, text="Result").grid(row=4, column=0, sticky="w")
        result = ttk.Entry(frm, width=44)
        result.insert(0, mew_history._result(record))
        result.grid(row=4, column=1, sticky="ew", pady=2)
        keep = tk.BooleanVar(value=False)
        abs_gco = getattr(self, "_hist_abs_gcode", {}).get(record.get("id"))
        if isinstance(abs_gco, (list, tuple)):
            # a calibration print: every slide's file, kept together
            abs_gco = [p for p in abs_gco if os.path.isfile(p)] or None
            n = len(abs_gco or ())
            text = ("Keep a copy of its g-code" if n < 2 else
                    "Keep a copy of its %d g-code files, one per slide" % n)
        else:
            if not (abs_gco and os.path.isfile(abs_gco)):
                abs_gco = None
            text = "Keep a copy of the g-code"
        cb = ttk.Checkbutton(frm, variable=keep, text=text)
        cb.grid(row=5, column=1, sticky="w", pady=2)
        if not abs_gco:
            cb.configure(state="disabled")     # g-code no longer on disk

        def _ok():
            src = abs_gco if keep.get() else None
            if src and src == getattr(self, "last_gcode", None):
                # the copy kept is the file Save g-code writes, in the
                # Machine page's flavor with its start and end
                fn = getattr(self, "_gcode_save_text", None)
                text = fn(os.path.basename(src)) if fn is not None else None
                if text is not None:
                    src = os.path.join(os.path.dirname(src), os.path.basename(
                        os.path.splitext(src)[0]) + "_saved.gcode")
                    try:
                        with open(src, "w", encoding="utf-8",
                                  newline="\n") as fh:
                            fh.write(text)
                    except OSError:
                        src = abs_gco
            self._hist_store().promote(
                record["id"], note=note.get("1.0", "end").strip(),
                tags=[t.strip() for t in tags.get().split(",") if t.strip()],
                operator=oper.get().strip(), gcode_src=src,
                title=title.get().strip(), result=result.get().strip(),
                saved_ts=datetime.datetime.now().isoformat(timespec="seconds"))
            top.destroy()
            self._hist_refresh()
            self.status.set("Saved the run from " + _when(record)
                            + " to Saved runs.")

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(btns, text="Save", command=_ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=top.destroy).pack(
            side="right", padx=6)

    def _hist_save_kept(self, record):
        """Give back what a saved run kept: its one g-code file, or every
        slide's file of a calibration print, under their own names."""
        store = self._hist_store()
        kept = [os.path.join(store.base_dir, rel)
                for rel in mew_history.kept_files(record)]
        kept = [p for p in kept if os.path.isfile(p)]
        if not kept:
            self._refuse("Not saved: the kept g-code is no longer in the "
                         "history folder.")
            return
        if len(kept) == 1:
            dest = filedialog.asksaveasfilename(
                **gui_prefs.save_dir_kw(), parent=self.root,
                title="Save the kept g-code", defaultextension=".gcode",
                initialfile=os.path.basename(kept[0]),
                filetypes=[("G-code", "*.gcode"), ("All files", "*.*")])
            pairs = [(kept[0], dest)] if dest else []
        else:
            folder = filedialog.askdirectory(
                **gui_prefs.save_dir_kw(), parent=self.root,
                title="Save the %d kept g-code files into" % len(kept))
            pairs = [(p, os.path.join(folder, os.path.basename(p)))
                     for p in kept] if folder else []
        if not pairs:
            return
        try:
            for src, dest in pairs:
                shutil.copy2(src, dest)
        except OSError as exc:
            self._refuse("Not saved: %s. Pick another folder and save "
                         "again." % exc)
            return
        self.status.set("Saved " + ", ".join(os.path.basename(d)
                                             for _s, d in pairs) + ".")

    def _hist_export_csv(self, record):
        dest = filedialog.asksaveasfilename(
            **gui_prefs.save_dir_kw(),
            title="Export run CSV", defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
            initialfile="run_" + record.get("id", "") + ".csv")
        if not dest:
            return
        try:
            mew_history.export_run_csv(record, dest)
        except OSError as exc:  # noqa: BLE001
            self._refuse('Not exported: %s. Pick another folder and export again.' % exc)
            return
        self.status.set("Exported " + os.path.basename(dest) + ".")

    def _hist_export_pdf(self, record):
        dest = filedialog.asksaveasfilename(
            **gui_prefs.save_dir_kw(),
            title="Export run sheet (PDF)", defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf"), ("All files", "*.*")],
            initialfile="runsheet_" + record.get("id", "") + ".pdf")
        if not dest:
            return
        thumb = record.get("thumb")
        thumb_abs = (os.path.join(self._hist_store().base_dir, thumb)
                     if thumb else None)
        try:
            mew_history.build_run_sheet_pdf(record, dest, thumb_path=thumb_abs)
        except Exception as exc:  # noqa: BLE001
            self._refuse('Not exported: %s. Pick another folder and export again.' % exc)
            return
        self.status.set("Exported " + os.path.basename(dest) + ".")

    def _hist_delete(self):
        """Delete the selected runs from whichever list is showing: the saved log
        (delete_saved) or the broad/unlogged history (delete_run)."""
        _tree, _rm, is_saved = self._hist_active()
        recs = self._hist_selected_records()
        if not recs:
            self._refuse("Select one or more runs to delete.")
            return
        noun = ("saved entr" + ("y" if len(recs) == 1 else "ies")) if is_saved \
            else ("history run" + ("" if len(recs) == 1 else "s"))
        if not messagebox.askyesno("Delete", f"Remove {len(recs)} {noun}?"):
            return
        store = self._hist_store()
        for r in recs:
            if is_saved:
                store.delete_saved(r.get("id"))
            else:
                store.delete_run(r.get("id"))
        self._hist_refresh()

    def _hist_tip(self, widget, text):
        """A tiny hover tooltip (used for the greyed PDF button)."""
        def _enter(_e):
            self._hist_tip_win = tw = tk.Toplevel(widget)
            tw.wm_overrideredirect(True)
            x = widget.winfo_rootx() + 10
            y = widget.winfo_rooty() + widget.winfo_height() + 2
            tw.wm_geometry(f"+{x}+{y}")
            tk.Label(tw, text=text, background="#ffffe0", relief="solid",
                     borderwidth=1, padx=4, pady=2).pack()

        def _leave(_e):
            tw = getattr(self, "_hist_tip_win", None)
            if tw is not None:
                tw.destroy()
                self._hist_tip_win = None
        widget.bind("<Enter>", _enter)
        widget.bind("<Leave>", _leave)
