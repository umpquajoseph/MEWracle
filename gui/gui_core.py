#!/usr/bin/env python3
"""gui_core.py -- Core mixin: window skeleton, the G-code input tab,
tab-change handling, file pickers -- plus the no-op hooks
that the feature mixins (_SimMixin, _CalibrationMixin, _SweepsMixin, ...) override. A no-op default here IS the mixin contract: it
names what the overriding mixin must return, so this file carries no sim,
calibration, analysis or sweep code inline."""
from gui_common import *

import gui_tips


class _CoreMixin:
    # Neutral default; each entry point (gcode_to_fibers_gui / mewracle_gui)
    # overrides this class attribute. Kept here so __init__ need not know which.
    WINDOW_TITLE = "MEWracle"

    # -- printhead-sim hooks: no-ops here, overridden by gui_sim._SimMixin. --
    def _build_sim_tab(self, nb):
        """No-op default; _SimMixin builds the printhead-sim tab."""

    def _build_sim_extra(self, parent):
        """Extra controls under the Simulation viewer tab. None by default;
        _SimMixin adds the 'real-time adjustment' launcher, which needs the jet."""

    def _build_machine_motion_limits(self, parent, row, pad):
        """The acceleration-limit fields (linear / rotary accel, corner speed)
        on the Machine page, which drive the accel-limited planner
        (mew_kinematics).
        A no-op default; left unused the row collapses to zero height."""

    def _sim_persist_entries(self):
        """No-op default; _SimMixin returns the sim.* fields."""
        return {}

    def _machine_motion_keys(self):
        """Persist keys for the Machine-tab motion-limit fields, folded into the
        machine-preset key set by gui_buildplate._machine_persist_keys. Empty
        here; _SimMixin returns the sim.kinematics / max_accel / scv /
        rotary_accel keys."""
        return ()

    def _physics_process_persist_keys(self):
        """Persist keys for the sim.* process/deposition fields that ride the
        physics (calibration) preset kind. Empty here; _SimMixin overrides this,
        and _CalibrationMixin's _calibration_persist_keys consumes it."""
        return ()

    def _sim_play_args(self, gcode=None):
        """No-op default; _SimMixin forwards the sim args to the player.
        `gcode` is the file being simulated, for what it sets itself."""
        return []

    def _live_diam_args(self):
        """Player args selecting the live-adjust diameter mode. Empty by default;
        _SimMixin adds --dynamic-diameter when the diameter is physics-driven."""
        return []

    def _sim_cone_args(self):
        """Player-only args for the molten Taylor-cone toggle. Empty by default;
        _SimMixin adds --no-cone when the user unticks the cone. Kept OUT of
        _sim_play_args, which also feeds the export worker, which has no such flag."""
        return []

    def _export_prefix(self, worker, target, gcode=None):
        """Program-prefix argv for an export run: just the plain worker here.
        `worker` is a "worker:xxx" key that worker_cmd resolves to a source or
        frozen invocation. _SimMixin overrides this to route through mew_export.py,
        installing the physics fiber transform, when its "Physics-deposited
        scaffold" toggle is on; `target` ('converter'|'scaffold') tells that
        wrapper which downstream main() to drive and is ignored here. `gcode`
        is the input file a converter export reads (None for a scaffold the
        worker writes), for the temperature it sets itself."""
        return worker_cmd(worker)

    # Cap on `export_index`. One entry is two paths and an int, so the bound is
    # not about memory: it keeps the Machine-link lookup's worst case a fixed
    # number of file comparisons rather than growing with a long session.
    EXPORT_INDEX_MAX = 32

    def register_export(self, gcode, depo):
        """Record that `gcode` was generated with `depo` beside it.

        Called at the one moment both halves are known -- the end of a successful
        run -- because the `.depo` is named off the STL stem and the g-code off its
        own, so nothing downstream can pair them from a path alone. Newest first.
        Re-registering the same g-code moves it to the front instead of
        duplicating: the newest `.depo` is the one that describes those bytes."""
        if not (gcode and depo):
            return
        if not (os.path.isfile(gcode) and os.path.isfile(depo)):
            return
        idx = [e for e in getattr(self, "export_index", [])
               if os.path.normcase(e[0]) != os.path.normcase(gcode)]
        idx.insert(0, (gcode, depo))
        self.export_index = idx[:self.EXPORT_INDEX_MAX]

    # -- calibration hooks: no-ops here, overridden by gui_calibration's
    #    _CalibrationMixin (the Calibration tab + rig profile). --
    def _build_calibration_tab(self, nb):
        """No-op default; _CalibrationMixin builds the Calibration tab."""

    def _calibration_persist_entries(self):
        """No-op default; _CalibrationMixin returns the cal.* fields."""
        return {}

    def _calibration_persist_keys(self):
        """Persist keys owned by the Calibration tab (a third preset kind). Empty
        here; _CalibrationMixin returns its cal.* keys, which is what makes the
        calibration preset kind exist -- gui_presets keys everything off this."""
        return ()

    def _sweep_persist_keys(self):
        """Persist keys owned by the Experiment-sweeps page (a fourth preset kind).
        Empty here; _SweepsMixin returns its sw.* keys, which is what makes the
        sweep preset kind exist."""
        return ()

    def _sweep_preset_tabs(self):
        """[(tab key, print protocol)] of the Calibration tabs that keep their
        own presets. Empty here; _SweepsMixin fills it."""
        return []

    def _sweep_tab_keys(self, tab):
        """The persist keys a Calibration tab's preset holds. Empty here;
        _SweepsMixin fills it."""
        return ()

    def _sweep_grid_labels(self):
        """key -> heading for the sweep preset comparison grid. Empty here;
        _SweepsMixin fills it."""
        return {}

    def _after_sweep_preset_applied(self):
        """Re-sync hook after a sweep preset loads. No-op default."""

    def _calibration_args(self):
        """Worker args carrying the rig calibration profile (--calibration <path>).
        Empty by default; _CalibrationMixin writes the profile JSON and returns the
        flag. Folded into _sim_play_args so it reaches the player AND the export."""
        return []

    def _calibration_grid_labels(self):
        """key -> heading for the calibration preset comparison grid. Empty here;
        _CalibrationMixin sources it from the mew_calibration registry."""
        return {}

    def _after_calibration_preset_applied(self):
        """Re-sync hook after a calibration preset loads. No-op default."""

    def _after_scaffold_preset_applied(self):
        """Re-sync hook after a SCAFFOLD preset loads. No-op default; _SimMixin
        re-settles the physics-deposit diameter mode, because the preset carries
        its own diameter and physics state."""

    def _calibration_models(self):
        """The (Material, Nozzle, Calibration) the GUI's in-process jet readout
        should use, from the current Calibration-tab fields. None here, so the sim
        readout falls back to the model defaults."""
        return None

    def _sweeps_persist_entries(self):
        """No sweep-designer fields outside the modern shell (the Experiment
        sweeps page is gui_sweeps._SweepsMixin, modern UI only)."""
        return {}

    def _preview_persist_entries(self):
        """No embedded-scaffold-preview display toggles outside the modern shell.
        The physics mixin returns pv.* keys, which ride the SCAFFOLD preset kind
        (they are the scaffold designer's own view options), so a saved scaffold
        preset restores the tube/sag preview state too."""
        return {}

    def __init__(self, root):
        self.root = root
        root.title(self.WINDOW_TITLE)
        root.minsize(960, 560)

        self.proc = None
        self.reader = None
        self.out_q = queue.Queue()
        self.cancelled = False          # set by Cancel so _on_finished cleans up
        self._run_mode = None           # the _active_mode() the run was started in
        # the last INPUT mode tab selected, so selecting a result-viewer tab
        # doesn't change what Generate targets
        self._last_input_mode = "gcode"
        # transient --scaffolds-json handoff for a multi-scaffold run; deleted
        # once the worker has read it (on finish / on close)
        self._scaffolds_json_tmp = None
        # paths the current run will produce, and the last STL finished OK
        self.expected_stl = None
        self.expected_gcode = None
        self._stl_preexisted = False    # did expected_stl exist before this run?
        self.last_stl = None            # exact STL FILE if one was built, else None
        self.last_depo = None           # .depo cache (sagged centrelines) if written
        self.last_plate = None          # <stem>_plate.stl build-plate sibling if drawn
        self.last_gcode = None
        # Every (g-code, .depo) this session has produced, newest first -- NOT just
        # the last one. The Machine-link page prints a file the PRINTER holds and
        # has to decide whether some local export describes the same bytes; keying
        # that off `last_gcode` alone made any later Generate drop the sagged
        # centrelines and re-solve the sag (6.0 s on a 10x10 mm 20-layer scaffold,
        # 67.1 s on 20x20x40, against 0.8 s from the cache). The `.depo` stem comes
        # from the STL name, not the g-code's, so it has to be recorded at the one
        # moment both halves are known. See `register_export`.
        self.export_index = []
        # was last_gcode produced by the scaffold generator (every G0 a travel,
        # every G2/G3 an arc, so the g-code emission rewrite may apply) vs consumed
        # from an imported file (never rewritten)? Set at run finish; see
        # gui_scaffold's _gcode_emit_opts.
        self._last_gcode_is_scaffold = False
        # ...or by the Calibration page, whose own Export card spells it
        # (gui_sweeps `_sw_emit_opts`). Set by `_sw_generate_done`.
        self._last_gcode_is_calibration = False
        # fiber params (CLI args) the last finished run used, forwarded to the
        # player so its deposition view matches the scaffold just generated
        self.last_fiber_params = []
        self.run_fiber_params = []
        # All generated files (g-code, STL, the plate/deposition/per-scaffold
        # siblings) land in a throwaway per-session scratch
        # folder. Nothing is "kept" in place: the user exports a copy with
        # "Save ... as", and the whole folder is removed on close (and swept on the
        # next launch if we crash). See gui_common.prepare_session_dir.
        self.session_dir = prepare_session_dir()

        self._build_widgets()
        # every persisted var now exists, so a saved default preset can be
        # applied across all tabs.
        self._apply_default_preset()
        # paint the whole window for the saved light/dark preference now that all
        # widgets (incl. the preset grid) exist; _ThemeMixin themes by class.
        self._apply_theme()
        self._poll_output()

    def _build_widgets(self):
        self.pad = dict(padx=6, pady=4)
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        # The notebook carries the log tab, so it (not a bottom log strip) takes
        # the stretch. Giving row 0 the weight also lets the notebook shrink when
        # the window is short, keeping the Generate bar + status pinned on-screen.
        outer.rowconfigure(0, weight=1)

        # --- notebook with the modes ---
        self.nb = ttk.Notebook(outer)
        self.nb.grid(row=0, column=0, sticky="nsew")
        # The input/form tabs sit in a vertical-scroll container each, so a short
        # window (or a high-DPI display) makes the tab scroll instead of clipping
        # its bottom. Content is built into `.body`; the notebook page is still the
        # VScrollFrame, so the identity checks in _active_mode / _viewer_frames
        # keep working. Viewer/Log tabs fill by design and stay plain frames.
        # Machine + build-plate setup FIRST: the bounds/collector apply to every
        # pipeline, so it leads the notebook and is a non-input tab. It is a plain
        # frame holding its OWN inner notebook (a "Setup" sub-tab and a "Presets"
        # sub-tab), so nb.select() still returns tab_machine and the identity
        # checks are unaffected.
        self.tab_machine = ttk.Frame(self.nb)
        self.nb.add(self.tab_machine, text="  Printer and Collector  ")
        self.machine_nb = ttk.Notebook(self.tab_machine)
        self.machine_nb.pack(fill="both", expand=True)
        machine_setup = VScrollFrame(self.machine_nb, padding=10)
        self.machine_nb.add(machine_setup, text="  Setup  ")
        self._build_buildplate_tab(machine_setup.body)
        machine_presets = ttk.Frame(self.machine_nb, padding=8)
        self.machine_nb.add(machine_presets, text="  Presets  ")
        self._build_machine_presets_tab(machine_presets)

        # Calibration: the rig's jet-model anchors + hooks, grouped right after the
        # global Machine tab because it is the same kind of global, per-rig setup
        # (not part of any one input mode).
        self._build_calibration_tab(self.nb)

        # Parameters -> G-code -> STL leads the input modes (just after the global
        # Machine tab) and is the tab selected on launch (the select() below).
        self.tab_scaffold = VScrollFrame(self.nb, padding=10)
        self.nb.add(self.tab_scaffold, text="  Parameters → G-code → STL  ")
        self._build_scaffold_tab(self.tab_scaffold.body)

        # (The Mandrel tab was merged INTO the Parameters tab above: it is the
        #  "mandrel" value of that tab's collector toggle. See
        #  gui_scaffold._build_collector_toggle.)

        # --- result viewer tabs (g-code text embedded; STL + sim windowed) ---
        # These live in the same notebook as the input modes; selecting one does
        # not change which mode Generate targets (see _active_mode).
        self._build_viewer_tabs()

        # --- secondary input mode, parked at the far right of the tab row ---
        # G-code -> STL is the less-used input mode, so it sits to the right of
        # the viewer/Log tabs, at the very end.
        self.tab_gcode = VScrollFrame(self.nb, padding=10)
        self.nb.add(self.tab_gcode, text="  G-code → STL  ")
        self._build_gcode_tab(self.tab_gcode.body)

        # Parameters -> G-code -> STL is the default active tab on launch.
        self.nb.select(self.tab_scaffold)

        # --- run / cancel (shared) ---
        btns = ttk.Frame(outer)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        self.run_btn = ttk.Button(btns, text="Generate", command=self.run)
        self.run_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        # Generation render quality. Wireframe / Tubes generate the sag + .depo
        # cache but SKIP the (minute-long) STL mesh build -- the viewers render the
        # cached centrelines, and the chosen level becomes their default. "Exact
        # mesh" builds the full STL. Either way Export STL produces the exact mesh,
        # built on demand from the cache when a preview was generated.
        #
        # Opens on FIBERS (SMOOTH). Full mesh was tried and reverted: `gen_render`
        # was also copied onto the viewers' quality, which silently put the unified
        # Viewer on `--render exact` and tubed every sag sample, so a 30x30x10 then
        # opened in 47-51 s at 10.4 GB instead of 3.4 s. The copy onto `sim_render`
        # is gone, and Generate has no reason to spend a minute on a mesh the
        # viewer does not read and Export rebuilds on demand.
        self.gen_render = tk.StringVar(value=RENDER_VAL2LBL["tube:12"])
        ttk.Label(btns, text="Build quality:").pack(side="left", padx=(16, 2))
        ttk.Combobox(btns, textvariable=self.gen_render, state="readonly", width=18,
                     values=[lbl for lbl, _ in RENDER_CHOICES]).pack(side="left")
        # The result-view controls live in the Simulation / STL viewer / G-code
        # text tabs; only Generate/Cancel stay in this shared strip. The progress
        # bar is determinate and empty when idle -- the workers report a real
        # fraction (gui_run._on_progress) -- and the runs that cannot, such as
        # the sweep builds, borrow it as a bouncer via _progress_bounce.
        self.progress = ttk.Progressbar(btns, mode="determinate", length=180,
                                        maximum=1.0, value=0)
        self.progress.pack(side="left", padx=12)

        # Dark-mode toggle: a global UI control, so it lives in the always-visible
        # bottom strip rather than any one tab. _ThemeMixin applies the theme by
        # widget class, so new tabs/fields inherit it with no extra wiring.
        self._build_theme_toggle(btns)

        # Generated files live in a throwaway scratch folder; to keep one, the user
        # exports a copy. Each button enables once a run has produced the matching
        # file and copies it out -- the scratch original is swept with the session
        # folder on close.
        savef = ttk.Frame(btns)
        savef.pack(side="right", padx=4)
        self.save_result_btn = ttk.Button(
            savef, text="Save STL as…", command=self.save_result_as,
            state="disabled")
        self.save_result_btn.pack(side="left", padx=2)
        self.save_gcode_btn = ttk.Button(
            savef, text="Save g-code as…", command=self.save_gcode_as,
            state="disabled")
        self.save_gcode_btn.pack(side="left", padx=2)

        # --- status (shared) ---
        # The log/output box is the last notebook tab ("Log"), not a fixed strip
        # here, so a short window cannot hide it. Only the status line is pinned.
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(outer, textvariable=self.status, anchor="w").grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(4, 2))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Re-label the context-aware controls (Generate / result view) as the
        # active tab changes.
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._on_tab_changed()

    def _build_gcode_tab(self, frm):
        """The G-code import form, laid out by `gui_form` (one row per
        parameter, unit in the label, help on hover) -- in both shells."""
        import gui_form
        F = gui_form.Form(frm, stretch=True)
        self.in_var = tk.StringVar()
        F.file("Input G-code", self.in_var,
               "The g-code file to convert, from any source. Each deposition "
               "move is swept into a round fiber and sagged — the same renderer "
               "the Scaffold designer uses.",
               command=self.browse_input)

        # The output STL is written automatically into the scratch session folder;
        # the user names a copy only via "Save STL as". Kept as a StringVar (set
        # per run in run()) so the arg-builder/tracking need no special-casing.
        self.out_var = tk.StringVar()

        self.diameter = tk.StringVar(value="0.02")
        self.faces = tk.StringVar(value="12")
        self.sag_mult = tk.StringVar(value="1.0")

        F.section("Conversion")
        F.entry("Fiber diameter", self.diameter,
                "The diameter every deposited fiber is swept at.", unit="mm")
        F.entry("Tube facets", self.faces,
                "How many flat sides each fiber tube has in the STL. More is "
                "rounder and a larger file.")
        # NO "Sag multiplier" box -- see the note in gui_scaffold's
        # `_build_quality_tab`. The var survives for the presets and for
        # `--sag-multiplier`; the droop is scaled by `c` on the Physics page.

        # Sag solver -- the same control (and vocabulary) as the scaffold Export
        # panel. An imported g-code file is exactly where the exact solve is
        # slowest, so "Approximate" belongs on this page as much as on that one.
        # Opens on APPROXIMATE: ~0.5 s against the exact contact solve's ~11 s on
        # the default scaffold, for 4-8 um RMS on a 20 um fiber. MEWracle Lite
        # opens on the same choice -- keep the two in step.
        self.sag_mode = tk.StringVar(value=SAG_SOLVER_VAL2LBL["approximate"])
        self.sag_mode_combo = F.combo(
            "Sag solver", self.sag_mode,
            [lbl for lbl, _ in SAG_SOLVER_CHOICES],
            "Which solver computes the deposited fibers' sag for this file's "
            "STL.\n\n"
            "Exact — the full contact solve; the reference the other two are "
            "measured against.\n\n"
            "Approximate (fast) — the same physics with contacts found "
            "analytically instead of by sampling. Roughly 5x faster, within a "
            "fraction of a fiber diameter of Exact.\n\n"
            "Off — lay every fiber straight, as commanded.").field
        self.sag_mode_combo.bind("<<ComboboxSelected>>",
                                 lambda _e: self._toggle_sag())

        # Mesh detail -- the same control as the scaffold Export panel: it changes
        # the STL's ring count and nothing else, and an imported file is exactly
        # where a multi-hundred-MB mesh shows up. Its own var (and persist key),
        # because the two panels export independently.
        self.mesh_tol = tk.StringVar(value=MESH_DETAIL_VAL2LBL["1.0"])
        self.mesh_tol_combo = F.combo(
            "Mesh detail", self.mesh_tol,
            [lbl for lbl, _ in MESH_DETAIL_CHOICES],
            "How finely the deposited centerline is sampled into tube rings for "
            "the STL. It changes the FILE, never the solve.\n\n"
            "Standard — the default: drop the samples a tube of this many facets "
            "cannot represent anyway. Roughly halves the file.\n\n"
            "Smaller file — about a third smaller again, measurably less "
            "faithful, indistinguishable on screen.\n\n"
            "Full — every sample gets a ring, exactly as before this setting "
            "existed.").field

    def _active_mode(self):
        """The active INPUT mode: 'gcode' | 'scaffold' | 'mandrel' | 'surface'
        (the modern shell adds 'calibration'). A result-VIEWER tab is not an
        input mode, so it returns the last input mode (what Generate should
        still target)."""
        try:
            frame = self.nb.nametowidget(self.nb.select())
        except Exception:  # noqa: BLE001
            return self._last_input_mode
        if frame in getattr(self, "_viewer_frames", set()):
            return self._last_input_mode
        # the global Machine/Collector setup tab is not an input mode either
        if frame is getattr(self, "tab_machine", None):
            return self._last_input_mode
        if frame is self.tab_scaffold:
            # One tab, two collectors. The mandrel is a real input mode (it
            # builds a different argv), so the toggle -- not the tab --
            # decides. The _mandrel_dispatch inversion is what lets that
            # mixin's run() hand the job to the SHARED scaffold run path once
            # it has validated.
            coll = getattr(self, "s_collector", None)
            coll = None if coll is None else coll.get()
            if coll == "mandrel" and not getattr(self, "_mandrel_dispatch",
                                                 False):
                return "mandrel"
            return "scaffold"
        return "gcode"

    #: the modes a tab switch REMEMBERS as the Generate target. A result-viewer
    #: tab is not an input mode, so `_active_mode` answers with the last one
    #: recorded here -- which means a real input mode missing from this tuple
    #: leaves every viewer answering with the mode before it. The modern shell
    #: extends it with its own "calibration" mode.
    _REMEMBERED_MODES = ("gcode", "scaffold", "mandrel")

    def _on_tab_changed(self, _event=None):
        """A viewer tab refreshes its own content and leaves the shared run
        controls (which target the last input mode) alone; an input tab updates
        the context-aware controls and is remembered as the run target."""
        try:
            frame = self.nb.nametowidget(self.nb.select())
        except Exception:  # noqa: BLE001
            frame = None
        if frame in getattr(self, "_viewer_frames", set()):
            self._refresh_viewer_tabs()
            return
        mode = self._active_mode()
        if mode in self._REMEMBERED_MODES:
            self._last_input_mode = mode
        self._refresh_result_controls()

    def _refresh_result_controls(self):
        """Point the shared result controls at what exists: View STL and Save
        STL enable once a run has left an STL or its .depo cache on disk."""
        self.run_btn.configure(text="Generate")
        have = bool((self.last_stl and os.path.isfile(self.last_stl))
                    or (self.last_depo and os.path.isfile(self.last_depo)))
        state = "normal" if have else "disabled"
        self.view_btn.configure(
            text="View STL", command=self.view_stl, state=state)
        self.save_result_btn.configure(text="Save STL as…", state=state)

    def browse_input(self):
        path = filedialog.askopenfilename(
            title="Select input G-code",
            filetypes=[("G-code", "*.gcode *.gco *.g *.nc *.txt"), ("All files", "*.*")])
        if path:
            self.in_var.set(path)
