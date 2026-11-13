#!/usr/bin/env python3
"""gui_calibration.py -- the CALIBRATION page (and the classic UI's Calibration tab):
one card per calibrated quantity, plus the hooks that wire a rig profile into the
workers.

The reduced-order jet model's tunables all live in ONE place -- mew_physics's
`Calibration` (+ `Material`, `Nozzle`) -- and their FIELDS are on the Physics page
(`_build_calibration_section`). What lives here is how to MEASURE each of them: one
card per quantity, each walking the operator PRINT -> MEASURE -> ENTER -> Compute ->
Apply.

  key                 writes                                    print   fit
  ------------------- ----------------------------------------- ------- --------------------
  cts                 cal.cts_ref                               cts     read off the print
  viscosity           cal.mat_viscosity_ref                     diam    fit_viscosity_ref
  activation_energy   cal.mat_activation_energy                 diam    fit_activation_energy
  lag3                cal.lag_amplitude + cal.lag_amplitude_gap circle  mew_circle_fit.fit_amplitude
  lag4                m_lag_axial/_circ/_speed (mandrel only)   --      mew_lag_fit.fit_radii
  sag                 cal.sag_coeff + cal.sag_span_exp          sag     fit_sag_ladder
  fusion              cal.fusion_overlap | fusion_wall_overlap  --      fit_fusion_rows

Three contracts:

  * The procedure prose is NOT here. Title, what the card calibrates, the numbered
    operator steps and what to measure all come from
    `calibration_procedures.PROCEDURES`, and that registry's insertion order IS the
    card order. A card the page cannot import a procedure for does not exist.
  * A card DESIGNS its print; it does not build it. The print knobs come from
    `gui_sweeps._SweepsMixin` and the card's radio only says which protocol the SHARED
    bottom bar targets.
  * ONE print serves TWO tabs. `viscosity` and `activation_energy` both carry the
    "diam" protocol -- the melt temperature ladder -- because the runbook's viscosity
    anchor is not a separate print but the hottest rung of the temperature sweep
    (the calibration runbook written with the accompanying paper). The two
    cards mount two widget sets over ONE set of `sw.diam_*` variables, so an edit on
    either is the same design, and `_cal_sync_diam_fit` keeps both fit forms pointed
    at the ladder that is actually designed.
  * `cal.lag_amplitude_gap` is written WITH the amplitude, never after it. A fitted A
    is conditional on the gap it was fitted at, so the profile records it; the law
    itself does not read it (mew_physics divides the gap out of A and uses the live
    gap).
  * `cal.lag_amplitude` IS Eq.4's A (2.8629 as shipped), not a multiplier on it. Both
    lag cards write that one field, so a plate fit and a mandrel fit are the same
    number on the same scale and the second silently replaces the first.

The edited values are written to a session calibration.json and forwarded to the
player and the physics export as `--calibration <path>` (folded into
`_sim_play_args`), and used in-process for the Printhead-sim diameter readout, so the
sim, the live re-lay, the exported STL and the readout all read the SAME rig profile.
The printhead elevation (gap) is NOT here -- it is the single-source `sim_standoff`.
Part of the gcode_to_fibers_gui split; see gui_common.py.
"""
import json
import math

from gui_common import *

import gui_fonts
import gui_form
import gui_units
import gui_tips
import gui_widgets
import mew_circle_fit

# The calibration registry + profile I/O + fit estimators. Guarded so a missing
# physics module degrades to "no Calibration tab" rather than breaking the GUI
# (same posture as gui_sim's physics import).
try:
    import mew_calibration
    from mew_physics import MEWModel
    _HAVE_CAL = True
except Exception:  # noqa: BLE001
    _HAVE_CAL = False

# the MEWron regulator reads psi; the flow law wants Pa (matches gui_sim)
PSI_TO_PA = 6894.76
# wrap width of the short description shown beside each parameter
_DESC_WRAP = 360

# Short headings for the sim.* process/deposition columns the merged Physics
# page's preset now also captures (alongside the cal.* fields, whose headings come
# from the mew_calibration registry). An unmapped key would fall back to its raw
# key, so this keeps the comparison grid readable.
_PROCESS_GRID_LABELS = {
    "sim.standoff": "Gap",
    "sim.voltage": "Voltage", "sim.pressure": "Pressure",
    "sim.polymer": "Polymer",
    "sim.temperature": "Temperature", "sim.physics_deposit": "Jet lag",
    "sim.diam_override": "Ø override",
    "sim.physical_fusion": "Fusion", "sim.sag_law": "Sagging",
    "sim.taylor_cone": "Cone",
}


class _CalibrationMixin:
    # -- tab construction --------------------------------------------------- #
    def _init_calibration_vars(self):
        """Create the calibration Tk vars ONCE (no widgets); False when the physics
        modules are missing. Split from the builder so the modern shell can re-container
        the same fields.

        IDEMPOTENT, and that is the whole point. Two builders call it -- the Physics
        page's Core/Advanced section and this page -- and it used to rebuild `cal_vars`
        from scratch each time. A Tk `StringVar` unsets its Tcl variable in `__del__`
        and an `Entry` holds only the variable's NAME, so dropping the first set left
        every entry the Physics page had already built bound to a variable that no
        longer existed: they rendered EMPTY, and a card's Apply wrote the second set,
        which nothing visible was bound to. So the FIRST caller creates the vars and
        every later caller re-uses them;
        `test_gui_calibration.apply_reaches_the_physics_page` pins it."""
        if getattr(self, "cal_vars", None):
            return True
        self.cal_vars = {}
        if not _HAVE_CAL:
            return False
        # one Tk var per registry field, seeded with the live dataclass default
        for fs in mew_calibration.CALIBRATION_FIELDS:
            self.cal_vars[fs.key] = tk.StringVar(
                value=mew_calibration.fmt_value(fs.default))
            # a gated field also gets its tickbox var, off by default (the field's
            # dataclass default IS the "inherit" behaviour). It rides cal_vars so it
            # persists, kinds, and reaches both shells exactly like a real field --
            # models_from_profile ignores it as an unknown key.
            if fs.gate_label:
                self.cal_vars[fs.gate_key] = tk.BooleanVar(value=False)
        # The two USER-REPLACED-LAW keys (mew_lawlang), each one JSON-encoded in a
        # single StringVar. They ride `cal_vars` for the same reason a gate
        # tickbox does, and it buys four things at once with no further plumbing:
        # `_calibration_persist_keys` is `list(cal_vars)`, so they enter the
        # persist map; that map claims `cal.*` for the "calibration" preset kind,
        # so they ride a Physics preset; `_calibration_profile_snapshot` walks
        # `cal_vars`, so they reach the workers' profile file; and
        # `models_from_profile` decodes them explicitly.
        #
        # One var per KEY rather than one per law on purpose: the number of
        # editable laws is `mew_lawlang.LAWS`' business, and a var per law would
        # put that list into the persist map, where adding a law later would
        # then read as a dropped control.
        self.cal_vars[mew_calibration.LAW_SOURCE_KEY] = tk.StringVar(value="")
        self.cal_vars[mew_calibration.LAW_CONSTS_KEY] = tk.StringVar(value="")
        # which values came from Calibrate (`mew_calibration.MEASURED_KEY`):
        # rides cal_vars for the same four reasons the law keys do
        self.cal_vars[mew_calibration.MEASURED_KEY] = tk.StringVar(value="")
        self.cal_show_advanced = tk.BooleanVar(value=False)
        self._fit_results = {}            # section name -> last computed value
        return True

    def _build_calibration_tab(self, nb):
        """Override the shared no-op: insert the global "Calibration" tab.

        The classic developer UI hosts the SAME card stack the modern page does
        (`_cal_build_cards`), so the two shells cannot word a procedure differently. The
        classic App carries no `_SweepsMixin`, so the two cards with a print protocol
        say where the print designer is instead of drawing its knobs."""
        if not self._init_calibration_vars():
            return

        tab = ttk.Frame(nb)
        nb.add(tab, text="  Calibration  ")
        cal_nb = ttk.Notebook(tab)
        cal_nb.pack(fill="both", expand=True)

        cards = VScrollFrame(cal_nb, padding=10)
        cal_nb.add(cards, text="  Calibrate  ")
        self._cal_build_cards(cards.body)

        params = VScrollFrame(cal_nb, padding=10)
        cal_nb.add(params, text="  Printer & material  ")
        self._build_calibration_params(params.body)

        # The rig + material profile's ONE box in this shell, beside the fields
        # it saves -- the classic analogue of the modern Physics page's
        # "Physics presets" (gui_pages._build_calibration_section), and named
        # the same so the two shells read alike. The card stack above does NOT
        # build one: two boxes on one preset kind orphan the first,
        # and the box the Calibration PAGE owns in the modern shell is a
        # different kind -- the print design.
        presets = ttk.Frame(cal_nb, padding=8)
        cal_nb.add(presets, text="  Physics presets  ")
        self._build_calibration_presets_tab(presets)

    # -- Rig & material parameters ------------------------------------------ #
    def _build_calibration_params(self, parent):
        pad = self.pad
        parent.columnconfigure(0, weight=1)
        ttk.Label(parent, wraplength=660, justify="left", text=(
            "Calibrate the reduced-order jet model to YOUR printer and material. Only "
            "the Required fields must be measured for a new printer — everything else "
            "has a defensible default (most hooks are off / zero) you can refine "
            "later with a sweep. Use the Guided fit tab to turn a measured print "
            "into a value, and Presets to save a printer/material as a named preset. "
            "These feed the Printhead-sim jet, the real-time re-lay, and the "
            "physics-deposited export. (The nozzle–collector gap stays on the "
            "Printhead-sim tab.)")).grid(row=0, column=0, sticky="w", **pad)

        ctrls = ttk.Frame(parent)
        ctrls.grid(row=1, column=0, sticky="w", **pad)
        ttk.Checkbutton(ctrls, text="Show advanced parameters",
                        variable=self.cal_show_advanced,
                        command=self._toggle_calibration_advanced).pack(side="left")
        ttk.Button(ctrls, text="Reset all to defaults",
                   command=self._reset_calibration_defaults).pack(
            side="left", padx=(12, 0))

        req = ttk.LabelFrame(parent, padding=8,
                             text="Core parameters — measure these for your printer")
        req.grid(row=2, column=0, sticky="ew", **pad)
        # CTS is rendered up in the Printhead-sim "Process" box (a measured process
        # value), so it is excluded here to avoid showing it twice.
        self._render_cal_fields(
            req, [fs for fs in mew_calibration.CALIBRATION_FIELDS
                  if fs.group == "required" and fs.attr != "cts_ref"])

        self._cal_adv_frame = ttk.LabelFrame(
            parent, padding=8,
            text="Advanced — off-by-default hooks & fitted constants")
        self._cal_adv_frame.grid(row=3, column=0, sticky="ew", **pad)
        self._render_cal_fields(
            self._cal_adv_frame, [fs for fs in mew_calibration.CALIBRATION_FIELDS
                                  if fs.group == "advanced"])
        self._build_advanced_switches(self._cal_adv_frame)
        self._toggle_calibration_advanced()       # apply the initial hidden state

    def _render_cal_fields(self, parent, fields, headers=True):
        """Lay out `fields` (already in registry order) into `parent` through
        `gui_form`: label with its unit, entry, and the field's description as the
        hover tooltip on both, grouped by section under a heading.

        `headers=False` drops the section headings, for a caller whose CONTAINER
        already names the section -- the modern Physics page's module cards, one
        per law, where the heading would repeat the card title immediately under
        it. A caller passing fields from more than one section still wants them.

        A field carrying a `gate_label` gets its own tickbox on the row above and
        is indented under it -- the one thing an indent means; unticked, the
        entry is greyed and the field reads as unset (see
        `_calibration_profile_snapshot`). Entry handles are stashed in
        `_cal_entries` so `_sync_cal_gates` can grey them after a preset load."""
        if not hasattr(self, "_cal_entries"):
            self._cal_entries = {}
        F = gui_form.Form(parent)
        section = None
        for fs in fields:
            if fs.section != section:
                section = fs.section
                if headers:
                    F.section(section)
            if fs.gate_label:
                F.check(fs.gate_label, self.cal_vars[fs.gate_key],
                        "Tick to use the value below; unticked, the field "
                        "reads as unset.\n\n" + fs.desc,
                        command=self._sync_cal_gates)
            r = F.entry(fs.label, self.cal_vars[fs.key], fs.desc, unit=fs.unit,
                        indent=(1 if fs.gate_label else 0))
            # a fitted value keeps every digit in its var; the field shows five
            gui_units.bind_rounded(r.field, self.cal_vars[fs.key])
            if fs.gate_label:
                self._cal_entries[fs.key] = r.field
        self._sync_cal_gates()

    def _sync_cal_gates(self):
        """Grey every gated calibration entry whose tickbox is off. Safe to call
        before the widgets exist (a preset load can land first)."""
        for fs in mew_calibration.CALIBRATION_FIELDS:
            if not fs.gate_label:
                continue
            ent = getattr(self, "_cal_entries", {}).get(fs.key)
            var = self.cal_vars.get(fs.gate_key)
            if ent is None or var is None:
                continue
            try:
                ent.configure(state="normal" if var.get() else "disabled")
            except tk.TclError:                     # widget already destroyed
                pass

    def _build_advanced_switches(self, parent, row=None):
        """The Advanced-section toggles that are NOT calibration fields.

        `_render_cal_fields` can only draw what is in `CALIBRATION_FIELDS`, and the
        per-layer registration hook is deliberately excluded from that registry
        (mew_calibration.py:339) -- it is a modelling-composition switch, not a rig
        constant a user measures. So it is drawn here by ONE builder both shells call,
        rather than by two copies that could drift.

        `row` is the grid row for a plain container; None appends below whatever
        `parent` already holds."""
        # Both shells build THIS section before `_build_sim_tab`, so the var does
        # not exist yet at this point -- ask the sim mixin to make it rather than
        # waiting for it. Reading it with a plain getattr and returning on None
        # is what made the checkbox silently absent from the page.
        ensure = getattr(self, "_sim_ensure_switch_vars", None)
        if ensure is not None:
            ensure()
        var = getattr(self, "sim_mandrel_per_layer", None)
        if var is None:                  # a build with no printhead-sim mixin
            return
        F = gui_form.Form(parent, row=(parent.grid_size()[1] if row is None
                                       else row))
        F.check(
            "Mandrel: re-register the lag on every layer", var,
            "4-axis only, and off on purpose.\n\n"
            "Off (the default), the jet-lag placement is solved once at the "
            "bare mandrel radius and every layer is laid on that same solution. "
            "On, it is re-solved at radius + wall height for each layer, so the "
            "lag grows with the wall and each layer lands slightly around the "
            "tube from the one below. The walls lean about 5.5 µm per layer.\n\n"
            "The lean is what the model predicts. Nobody has measured whether "
            "a real mandrel does it, which is why this is frozen by default. "
            "Tick it to experiment, not to print from.\n\n"
            "Needs Jet lag on (its card's switch), and does nothing on a flat "
            "collector.")

    def _toggle_calibration_advanced(self, *_):
        frame = getattr(self, "_cal_adv_frame", None)
        if frame is None:
            return
        if self.cal_show_advanced.get():
            frame.grid()
        else:
            frame.grid_remove()

    # -- what "default" means on the Physics page --------------------------- #
    _shipped_cache = None

    #: the physics preset that ships with the app and that a fresh install
    #: boots into. A release fact, named here rather than read off
    #: `presets/defaults.json`: run from source, that pointer IS the user's own
    #: (app_paths puts user data in the repo), so Set as default, or an
    #: Apply keeping "My calibration", moved "default" with it and the page
    #: called the user's own preset "the lab's".
    SHIPPED_PHYSICS_PRESET = "MEWron 0.2 22G"

    @classmethod
    def _shipped_physics_values(cls):
        """The `cal.*` values of the physics preset that ships with the app
        (`SHIPPED_PHYSICS_PRESET`), read from the bundled, read-only
        `presets/physics/` (never the user's copy, which they may have edited,
        renamed or deleted). {} when it cannot be read.

        This is the ONE meaning of "default" on the Physics page. Reset used the
        registry's dataclass defaults, a different printer (21 G bore, 25.4 mm,
        η 1041), so the app booted at CTS 480 mm/min and η 228.2 and Reset gave
        450 and 1041, both labelled "default"."""
        if cls._shipped_cache is not None:
            return cls._shipped_cache
        vals, found = {}, ""
        name = cls.SHIPPED_PHYSICS_PRESET
        try:
            folder = resource_path("presets", "physics")
            for fn in sorted(os.listdir(folder)) if name else ():
                if not fn.lower().endswith(".json"):
                    continue
                with open(os.path.join(folder, fn), encoding="utf-8") as fh:
                    pre = json.load(fh)
                if isinstance(pre, dict) and pre.get("name") == name:
                    vals = {k: str(v) for k, v in (pre.get("data") or {}).items()
                            if k.startswith(mew_calibration.KEY_PREFIX)}
                    found = name
                    break
        except (OSError, ValueError, TypeError, AttributeError):
            vals, found = {}, ""
        cls._shipped_cache = vals
        cls._shipped_name = found
        return vals

    @classmethod
    def _shipped_physics_name(cls):
        """The shipped physics preset's name ("" when it cannot be read)."""
        cls._shipped_physics_values()
        return getattr(cls, "_shipped_name", "")

    def _physics_is_shipped(self):
        """True while every Physics value is still the shipped preset's: the
        predictions are the lab's printer, not the user's. False when it
        cannot be told (no shipped preset readable, no Physics page)."""
        shipped = self._shipped_physics_values()
        cal = getattr(self, "cal_vars", None)
        if not shipped or not cal:
            return False
        for fs in mew_calibration.CALIBRATION_FIELDS:
            var = cal.get(fs.key)
            if var is None:
                continue
            if str(var.get()).strip() != self._cal_default_text(fs).strip():
                return False
        for law_key in (mew_calibration.LAW_SOURCE_KEY,
                        mew_calibration.LAW_CONSTS_KEY):
            var = cal.get(law_key)
            if var is not None and str(var.get()).strip():
                return False
        # The mandrel-only values (mandrel CTS, the lag pair and its speed,
        # support S) are not registry fields, and the shipped preset leaves
        # them blank: one that is set was measured or typed. Missing them,
        # a mandrel picture drawn with a measured mandrel CTS read "lab
        # physics (not calibrated)".
        for _attr, name in getattr(self, "_MANDREL_CAL_VARS", ()):
            var = getattr(self, name, None)
            if var is not None and str(var.get()).strip():
                return False
        return True

    def _cal_default_text(self, fs):
        """The default text for registry field `fs`: the shipped preset's value,
        else the registry's own default for a key that preset does not carry."""
        shipped = self._shipped_physics_values()
        if fs.key in shipped:
            return shipped[fs.key]
        return mew_calibration.fmt_value(fs.default)

    def _cal_gate_default(self, fs):
        """The shipped tickbox state of a gated field (off when not shipped)."""
        raw = self._shipped_physics_values().get(fs.gate_key, "")
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def _reset_calibration_defaults(self):
        for fs in mew_calibration.CALIBRATION_FIELDS:
            self.cal_vars[fs.key].set(self._cal_default_text(fs))
            if fs.gate_label:
                self.cal_vars[fs.gate_key].set(self._cal_gate_default(fs))
        # a user-replaced law is a calibrated value like any other, so "reset to
        # defaults" has to put the SHIPPED laws back too -- leaving one applied
        # would reset every number it multiplies and keep the law itself
        for law_key in (mew_calibration.LAW_SOURCE_KEY,
                        mew_calibration.LAW_CONSTS_KEY):
            var = self.cal_vars.get(law_key)
            if var is not None:
                var.set("")
        self._cal_unmark()
        self._after_calibration_preset_applied()
        self.status.set("Every Physics card is back to the defaults that came "
                        "with the app.")

    # -- preset / worker hooks (override the _CoreMixin no-ops) ------------- #
    def _calibration_persist_entries(self):
        """The cal.* fields for the preset persist map."""
        return dict(getattr(self, "cal_vars", {}))

    def _calibration_persist_keys(self):
        """The persist keys owned by the merged Physics page's preset kind: the cal.*
        rig-calibration fields PLUS the sim.* process/deposition fields (see
        `_physics_process_persist_keys`). The whole Physics page rides ONE preset, so a
        saved profile reproduces the deposited geometry, not just the jet-model anchors.
        The printhead elevation and accel-planner knobs stay on the MACHINE preset. This
        is also what makes "calibration" a real preset kind in gui_presets and supplies
        the flat comparison grid's columns."""
        return (list(getattr(self, "cal_vars", {}))
                + list(self._physics_process_persist_keys()))

    def _calibration_grid_labels(self):
        """Headings for the Physics preset comparison grid (kind "calibration"):
        the cal.* headings from the registry, plus the process/deposition sim.*
        columns the Physics preset also captures."""
        if not _HAVE_CAL:
            return {}
        labels = dict(mew_calibration.grid_labels())
        labels.update(_PROCESS_GRID_LABELS)
        return labels

    def _calibration_profile_snapshot(self):
        """The current fields as a flat profile dict (string-valued, the shape the
        workers + mew_calibration.models_from_profile read).

        A GATED field whose tickbox is off reads as BLANK whatever its entry remembers,
        so the model takes the dataclass default. Folding that sentinel in HERE rather
        than in the worker is the `_sag_reset_arg` idiom: the worker's own default can
        never silently re-enable a split the user unticked, and unticking cannot lose
        the typed value."""
        snap = {k: v.get() for k, v in getattr(self, "cal_vars", {}).items()}
        for fs in (mew_calibration.CALIBRATION_FIELDS if _HAVE_CAL else ()):
            if fs.gate_label and not snap.get(fs.gate_key):
                snap[fs.key] = ""
        return snap

    def _calibration_models(self):
        """(Material, Nozzle, Calibration) from the current fields, for the
        in-process Printhead-sim diameter readout. None when calibration is
        unavailable -> the readout keeps its model-default behaviour."""
        if not _HAVE_CAL or not getattr(self, "cal_vars", None):
            return None
        return mew_calibration.models_from_profile(
            self._calibration_profile_snapshot())

    def _validate_calibration(self):
        """None, or the message Generate should refuse with, naming the Physics
        field. `models_from_profile` deliberately skips a value it cannot read
        so a stale PROFILE still loads -- but at Generate that same rule meant
        "abc" typed into the sag coefficient printed SILENTLY at the factory
        default, and the operator believed it had used their number. Blank is
        still the default and is not refused. A value the model itself cannot
        run on (a zero viscosity anchor, a zero nozzle bore) is named by
        `MEWModel`'s own check, so this and the workers cannot disagree.
        The four process settings ride every run as well, and are checked first
        (`_SimMixin._validate_process`)."""
        proc = getattr(self, "_validate_process", None)
        err = proc() if callable(proc) else None
        if err:
            return err
        if not _HAVE_CAL or not getattr(self, "cal_vars", None):
            return None
        snap = self._calibration_profile_snapshot()
        for fs in mew_calibration.CALIBRATION_FIELDS:
            raw = str(snap.get(fs.key, "")).strip()
            if not raw:
                continue
            try:
                ok = math.isfinite(float(raw))
            except ValueError:
                ok = False
            if not ok:
                return ("Physics page: “%s” must be a number (it reads “%s”)."
                        % (fs.label, raw))
        try:
            mat, noz, cal = mew_calibration.models_from_profile(snap)
            MEWModel(material=mat, nozzle=noz, calibration=cal)
        except ValueError as exc:
            return "Physics page: %s." % exc
        return None

    def _calibration_args(self):
        """Write the current rig profile to the session scratch folder and return
        `--calibration <path>` for the player + the physics export (folded into
        _sim_play_args). The defaults reproduce the factory model byte-for-byte,
        so this is safe to pass on every run. [] on a write error -> the workers
        fall back to the defaults."""
        if not _HAVE_CAL or not getattr(self, "cal_vars", None):
            return []
        path = os.path.join(SESSION_DIR, "calibration.json")
        try:
            with open(path, "w") as fh:
                json.dump(self._calibration_profile_snapshot(), fh, indent=2)
        except OSError:
            return []
        return ["--calibration", path]

    def _after_calibration_preset_applied(self):
        """Re-sync after the physics/calibration preset changes (a load, a reset, or a
        guided-fit apply): re-settle the physics-deposit diameter mode AND refresh the
        predicted-diameter readout.

        The Physics preset carries the physics-deposit toggle and its sub-options, so a
        load can flip physics-deposit on or off, and `_apply_diam_mode` then locks and
        bakes the physics diameter into each scaffold or restores the hand-typed value.
        Unlike the SCAFFOLD re-settle it must NOT clear `_manual_diam`: a calibration
        preset carries no `s.diameter`, so the stashed manual value is still the one to
        restore when physics goes back off. Gated fields re-grey here too, because a
        preset carries its tickbox and the tickbox's own `command` never fires on a bulk
        set."""
        self._sync_cal_gates()
        fn = getattr(self, "_apply_diam_mode", None)
        if fn is None:
            fn = getattr(self, "_update_phys_readout", None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001 -- a re-settle must never raise
                pass

    def _build_calibration_presets_tab(self, parent):
        """The "Physics presets" box -- an independent preset kind that captures ONLY the
        cal.* rig + material fields, so a calibrated rig can be saved and shared apart
        from the machine geometry and the scaffold recipe.

        ONE caller per shell, and that is a rule: the modern shell's is the Physics page
        (gui_pages), the classic UI's is the Calibration notebook's sub-tab. A second
        call re-creates `self._pstores["calibration"]` and orphans the first box's
        table.
        The Calibration PAGE's box is a
        different kind entirely -- the calibration print's design."""
        self._init_pstore("calibration", CALIBRATION_PRESETS_DIR,
                           "calibration_default", "physics preset")
        self._build_one_presets_tab(
            parent, "calibration",
            ("Save the whole printer and material calibration as a named preset, "
             "then load it back any time. It is independent of the scaffold, "
             "printer and calibration presets."))

    # -- a model from the current fields (for the guided fits) -------------- #
    def _calibration_model(self):
        """A MEWModel built from the current fields, for the guided-fit estimators
        (so a fit uses the rig's own nozzle/material/calibration). The gap is read
        from the Printhead-sim standoff; it does not affect any of the fits."""
        mat, noz, cal = mew_calibration.models_from_profile(
            self._calibration_profile_snapshot())
        try:
            gap = float(self.sim_standoff.get())
        except (ValueError, AttributeError, tk.TclError):
            gap = 4.0
        if gap <= 0.0:
            gap = 4.0
        return MEWModel(material=mat, nozzle=noz, working_distance=gap,
                        calibration=cal)

    # -- small shared widgets ----------------------------------------------- #
    def _fit_result_row(self, F, compute_cmd, apply_cmd, compute_text="Compute",
                        applies=None):
        """The shared Compute / Apply strip at the bottom of a card, and the
        result line under it. Returns the result label (a live readout: its text
        and colour are the fit's answer, so it is not help text).

        `compute_text` exists for the ONE card that has no fit. On the CTS card
        the operator reads the number off the print, so the left button checks
        and echoes it rather than computing anything, and it says "Check" --
        "Compute" there would name a fit that does not exist.

        `applies`, a list of (text, command, tip), replaces the one Apply button
        for a card that writes two values the operator applies separately --
        the Fusion card's junction and wall overlaps. `apply_cmd` is then
        unused."""
        bar = gui_form.mark(ttk.Frame(F.frame), "group")
        for i, (text, cmd, tip) in enumerate((
                (compute_text, compute_cmd,
                 "Check the entries and show the result below, without "
                 "writing anything." if compute_text == "Check" else
                 "Fit the entries above and show the result below, without "
                 "writing anything."),) + tuple(applies or (
                ("Apply to calibration", apply_cmd,
                 "Write the result into the Physics page's calibration "
                 "fields."),))):
            b = gui_form.mark(ttk.Button(bar, text=text, command=cmd), "button")
            if i and self._cal_apply_on_ribbon:     # built, unpacked: the
                self._cal_apply_btns.setdefault(    # ribbon's Apply presses it
                    self._cal_building, []).append(b)
                continue
            b.pack(side="left", padx=((gui_fonts.SP["s"] if i else 0), 0))
            gui_tips.add_tip(b, tip)
        F.slot(None, bar)
        out = F.live(None, wrap=600).field
        out.configure(foreground="#1f6b2e")

        # wrap to the width the card is given, not a fixed 600 px: in the
        # G-code window's box at 1280x720 the card is ~400 px wide, and a
        # result line ran off its right edge, cut mid-word
        def _rewrap(e, out=out):
            try:
                out.configure(wraplength=max(240, e.width - 24))
            except tk.TclError:
                pass
        F.frame.bind("<Configure>", _rewrap, add="+")
        return out

    @staticmethod
    def _fit_table(F, tip):
        """A measurement table (rows of cells under column heads) as one slot of
        the card's Form. Returns the frame; the caller grids heads and cells in
        it and tags each with `_fit_cell`."""
        t = gui_form.mark(ttk.Frame(F.frame), "table")
        F.slot(None, t, tip)
        # A table wider than the label and field columns would otherwise have
        # its extra width spread over them, pushing this card's fields off the
        # field edge. The empty fourth column takes it instead.
        F.frame.columnconfigure(3, weight=1)
        return t

    @staticmethod
    def _fit_cell(widget, tip, kind="cell"):
        """Tag one head or cell of a measurement table and give it the table's
        hover help."""
        gui_form.mark(widget, kind)
        if tip:
            gui_tips.add_tip(widget, tip)
        return widget

    @staticmethod
    def _fit_num(text):
        """A typed measurement as a float -- the ONE parse every fit card uses.

        Python's `float` accepts "nan", "inf" and "1e400", and each of those
        passes a `<= 0` guard (every comparison with NaN is False), so before
        this rule a card handed them to its fit and Apply wrote "nan" into the
        Physics page. A non-finite entry is refused here with the text that was
        typed, the same way an unparseable one already was."""
        text = str(text).strip()
        try:
            val = float(text)
        except ValueError:
            hint = (" Use a point, not a comma, for decimals."
                    if "," in text else "")
            raise ValueError("‘%s’ is not a number.%s Fix or clear it, then "
                             "press Compute again" % (text, hint)) from None
        if not math.isfinite(val):
            raise ValueError("‘%s’ is not a usable number. Fix or clear it, "
                             "then press Compute again" % text)
        return val

    def _fit_say(self, exc, fallback="Check the entries above."):
        """A refusal as a sentence: capitalised and ending in a full stop.
        The engines raise lowercase clauses ("enter at least three rows").
        Kept as the last refusal, so a ribbon Apply that wrote nothing can
        say why in the status line (`_cal_apply_pressed`)."""
        text = str(exc).strip() or fallback
        text = text[:1].upper() + text[1:]
        text = text if text.endswith((".", "?", "!")) else text + "."
        self._fit_last_refusal = text
        return text

    #: a fiber diameter outside this, in mm, is a typed unit, not a fiber:
    #: 0.5 um to 1 mm brackets every melt-electrowritten fiber
    _FIBER_D_MM = (0.0005, 1.0)

    @staticmethod
    def _cell_shown(var):
        """A diameter cell's number as the user sees it (display unit)."""
        return gui_units.num("fiber_d", var.get().strip(), "mm")

    def _fit_check_diam(self, d_mm, shown):
        """Refuse a diameter no MEW fiber has, naming the unit the cell is in.
        `shown` is the number as typed, in the display unit."""
        lo, hi = self._FIBER_D_MM
        if lo <= d_mm <= hi:
            return
        u = gui_units.shown("fiber_d", "mm")
        raise ValueError(
            "‘%s’ %s is too %s for a melt-electrowritten fiber, which is %s "
            "to %s. These cells are in %s: check the unit you measured in"
            % (shown, u, "thin" if d_mm < lo else "thick",
               gui_units.show("fiber_d", "%g" % lo, "mm"),
               gui_units.show("fiber_d", "%g" % hi, "mm"), u))

    @classmethod
    def _fit_float(cls, var):
        return cls._fit_num(var.get())

    #: the most diameter cells one specimen can show. A card shows ONE CELL PER
    #: LINE the plate actually lays (`visc_reps` / `diam_reps`) and no more: a cell
    #: with no fiber behind it is a cell the operator cannot fill, and a cell that
    #: is filled but hidden is a number the mean uses and nobody can see -- the
    #: same overflow that had to be taken off the lag3 card.
    _FIT_REP_COLS_MAX = 12

    def _fit_rep_cells(self, F, label, tip, unit=None, width=7):
        """A labelled row of `_FIT_REP_COLS_MAX` diameter Entries.

        `_cal_sync_diam_fit` grids only as many as the plate lays and BLANKS the
        rest, so lowering the line count after typing cannot leave a measurement
        feeding the mean from off-screen. The cells are GRIDDED in the row's
        group frame (that is what lets the sync hide them), so they are made
        here rather than with `row.add_entry`, which packs."""
        row = F.group(label, tip, unit=unit)
        holder = row.host()
        variables, widgets = [], []
        for i in range(self._FIT_REP_COLS_MAX):
            var = tk.StringVar()
            ent = self._fit_cell(ttk.Entry(holder, textvariable=var, width=width),
                                 tip, kind="field")
            ent.grid(row=0, column=i, padx=(0, 3))
            # display units: the row's label is relabelled once, by the first
            gui_units.bind(ent, var, unit, row.label if i == 0 else None,
                           label, relabel=gui_form.label_text)
            variables.append(var)
            widgets.append(ent)
        return variables, widgets

    @staticmethod
    def _fit_mean_diam(variables, log=False):
        """Collapse the filled diameter cells to one number; None if none are.

        `log` picks the GEOMETRIC mean, which is what a fit linear in ln d wants:
        the trial's own script collapses each plate by the mean of ln d, NOT by
        ln of the mean d, because the model is in ln d (the analysis script of
        the paper's first fibre-diameter trial, `plate_stats`). The
        viscosity anchor is not a log fit -- it inverts one diameter directly --
        so it takes the plain arithmetic mean, which is that same script's
        `d_mean` (`k_estimates`). Blank, unparseable and non-positive cells are
        skipped, so measuring three of five lines is a three-line mean rather
        than an error.

        A cell that is FILLED but unusable -- a typo, a comma, zero, a negative
        -- is refused, not skipped: skipping it quietly fitted the other cells,
        and the operator never learnt the number they meant was left out."""
        vals = []
        for var in variables:
            text = var.get().strip()
            if not text:
                continue
            d = _CalibrationMixin._fit_num(text)
            if not d > 0.0:
                raise ValueError("‘%s’ is not a length. A measurement must be "
                                 "above zero; clear the cell if you did not "
                                 "take it" % text)
            vals.append(d)
        if not vals:
            return None
        if log:
            return math.exp(sum(math.log(d) for d in vals) / len(vals))
        return sum(vals) / len(vals)

    @staticmethod
    def _fit_rep_filled(variables):
        return sum(1 for v in variables if v.get().strip())

    def _cal_rep_count(self, sw, key):
        """How many diameter cells the plate designed under `key` earns."""
        try:
            n = int(float(sw[key].get()))
        except (KeyError, tk.TclError, ValueError):
            n = 1
        return max(1, min(n, self._FIT_REP_COLS_MAX))

    def _cal_sync_rep_cells(self, n, widgets, variables):
        """Show exactly `n` cells and BLANK every hidden one."""
        for j, (ent, var) in enumerate(zip(widgets, variables)):
            if j < n:
                ent.grid()
            else:
                ent.grid_remove()
                if var is not None:
                    var.set("")

    def _cal_apply(self, key, value, fmt="{:.6g}", name=None):
        """Write a fitted value into a calibration field, mark it measured and
        refresh the readouts. `name` is what the Physics page calls the field,
        when the registry's label is not it."""
        self.cal_vars[key].set(fmt.format(value))
        self._cal_mark_measured(key)
        self._after_calibration_preset_applied()
        label = name or next((fs.label for fs in mew_calibration.CALIBRATION_FIELDS
                              if fs.key == key), key)
        self._cal_applied_status("‘%s’" % label)

    # -- default / measured ------------------------------------------------- #
    def _cal_applied_status(self, what):
        """The one sentence every Apply ends on: what changed, that it now reads
        "measured", and the next step (Design, then Generate)."""
        self._cal_applied_count = getattr(self, "_cal_applied_count", 0) + 1
        kept = self._cal_keep_applied()
        self.status.set(
            "Applied %s. The Physics page now marks it measured, and the next "
            "Generate uses it. %s"
            % (what, ("It is kept in the physics preset “%s”, which now "
                      "loads at startup." % kept) if kept else
               "It could not be kept for the next start: save it under "
               "“Physics presets”."))

    #: the physics preset every Apply keeps itself in
    KEPT_PHYSICS_PRESET = "My calibration"

    def _cal_keep_applied(self):
        """Keep what Apply just wrote past a restart. Reported: a calibration
        applied and not saved by hand was gone at the next start, because
        startup loads the default physics preset and nothing else is
        remembered. The whole Physics page, its measured record with it, is
        written to the physics preset “My calibration”, which becomes the
        startup default. Returns that name, or None when it could not be
        written."""
        st = getattr(self, "_pstores", {}).get("calibration")
        if st is None or not hasattr(self, "_write_preset_file"):
            return None
        name = self.KEPT_PHYSICS_PRESET
        data = self._snapshot_for_kind("calibration")
        idx = self._preset_name_index("calibration", name)
        if idx is None:
            st["presets"].append({"name": name, "data": data})
            idx = len(st["presets"]) - 1
        else:
            st["presets"][idx]["data"] = data
        preset = st["presets"][idx]
        if not self._write_preset_file("calibration", preset):
            return None
        mode = None if None in st["ptr_keys"] else next(iter(st["ptr_keys"]))
        st["defaults"][mode] = preset["name"]
        if not self._write_pointer({key: st["defaults"][m]
                                    for m, key in st["ptr_keys"].items()}):
            return None
        self._refresh_preset_list("calibration", select=idx)
        return preset["name"]

    def _cal_restore_measured(self):
        """Put back the mandrel values the measured record holds. They live
        in the scaffold preset's keys (`m.cts`...), so the scaffold default
        loaded at startup, or on a switch to the mandrel, reset a measured
        mandrel CTS to the design's."""
        rec = self._cal_measured_record()
        for key in self._CAL_MANDREL_KEYS:
            var = getattr(self, key, None)
            if key in rec and var is not None \
                    and str(var.get()).strip() != rec[key]:
                var.set(rec[key])

    # -- a design keeps the physics it was made with ------------------------ #
    #: a process value's unit, for the sentence that names a difference
    _PROCESS_UNITS = {"sim.standoff": "mm", "sim.voltage": "kV",
                      "sim.pressure": "psi", "sim.temperature": "°C",
                      "sim.coil_wave_amp": "mm", "sim.coil_wave_len": "mm"}

    def _cal_design_physics(self):
        """The Physics page as a design preset keeps it: every value of the
        physics preset kind, its measured record with it. {} in a build
        without the Physics page."""
        if not getattr(self, "cal_vars", None):
            return {}
        return self._snapshot_for_kind("calibration")

    def _cal_physics_diffs(self, kept):
        """[(key, design text, page text)] for each Physics value that loading
        `kept` would change. A key `kept` lacks (a field newer than the save)
        would load at its factory default, so that is what it is compared as.
        The measured record is not a value the print uses and is not listed.
        `kept` is read as loading it would read it: through the preset
        migrations, so a design saved before a rename (the needle's bore
        radius, now its inner diameter) is not reported as different."""
        mig = getattr(self, "_migrate_preset_data", None)
        if callable(mig):
            kept = mig(dict(kept))
        now = self._cal_design_physics()
        defaults = getattr(self, "_field_defaults", {})
        out = []
        for key, have in now.items():
            if _HAVE_CAL and key == mew_calibration.MEASURED_KEY:
                continue
            want = kept.get(key, defaults.get(key))
            if want is None:
                continue
            a, b = str(want).strip(), str(have).strip()
            if not self._cal_same(a, b):
                out.append((key, a, b))
        return out

    def _cal_value_text(self, key, text):
        """One Physics value as the page shows it: number and unit, on/off."""
        if text in ("True", "False") or (
                key.startswith("sim.") and key not in self._PROCESS_UNITS
                and text in ("1", "0")):
            return "on" if text in ("True", "1") else "off"
        if text == "":
            return "blank"
        fs = mew_calibration._BY_KEY.get(key) if _HAVE_CAL else None
        unit = fs.unit if fs is not None else self._PROCESS_UNITS.get(key, "")
        q = gui_units.quantity_of(unit) if unit else None
        if q is not None:
            return gui_units.show(q, text, unit)
        return ("%s %s" % (text, unit)) if unit else text

    def _cal_diff_line(self, key, first, second, first_is="in the design",
                       second_is="now"):
        fs = mew_calibration._BY_KEY.get(key) if _HAVE_CAL else None
        label = fs.label if fs is not None else _PROCESS_GRID_LABELS.get(
            key, key)
        return "%s: %s %s, %s %s" % (
            label, self._cal_value_text(key, first), first_is,
            self._cal_value_text(key, second), second_is)

    def _cal_unkept(self):
        """[(key, value the next start loads, value on the page)] for each
        Physics value a restart would lose. Reported: a CTS typed on the
        Physics page was back at the default after a restart, and closing
        had asked nothing, because the next start loads the default physics
        preset and remembers nothing else. Compared the way that load sets
        the page: the preset's value, or the factory default it omits."""
        st = getattr(self, "_pstores", {}).get("calibration")
        if st is None or not getattr(self, "cal_vars", None):
            return []
        mode = None if None in st["ptr_keys"] else next(iter(st["ptr_keys"]))
        name = st["defaults"].get(mode)
        idx = self._preset_name_index("calibration", name) if name else None
        kept = (self._migrate_preset_data(st["presets"][idx]["data"])
                if idx is not None else {})
        return self._cal_physics_diffs(kept)

    def _cal_cts_phrase(self, kept=None):
        """"CTS 480 mm/min", the CTS a print uses: the mandrel's own when the
        collector is the mandrel and one is set, else the flat one. With
        `kept`, the flat CTS that physics copy holds."""
        if kept is not None:
            raw = str(kept.get("cal.cts_ref", "")).strip()
            return ("CTS %s" % self._cal_value_text("cal.cts_ref", raw)
                    if raw else "")
        coll = getattr(self, "s_collector", None)
        m = getattr(self, "m_cts", None)
        try:
            if coll is not None and coll.get() == "mandrel" and m is not None \
                    and str(m.get()).strip():
                return "mandrel CTS %s" % gui_units.show(
                    "speed", str(m.get()).strip(), "mm/s")
        except tk.TclError:
            pass
        var = getattr(self, "cal_vars", {}).get("cal.cts_ref")
        raw = str(var.get()).strip() if var is not None else ""
        return ("CTS %s" % self._cal_value_text("cal.cts_ref", raw)
                if raw else "CTS blank")

    def _cal_design_choice(self, preset, startup=False):
        """Before a design preset loads: "design" to print it with the physics
        it was saved with, "current" to keep the Physics page, "cancel" to load
        nothing. Asked only when the two differ; "current" when the design
        keeps no physics (a shipped design, or one saved before designs kept
        them) or when they agree. `startup`: the design the app opened with,
        already on the page, so there is nothing to cancel."""
        kept = (preset.get("data") or {}).get(self.DESIGN_PHYSICS_KEY)
        if not isinstance(kept, dict) or not getattr(self, "cal_vars", None):
            return "current"
        diffs = self._cal_physics_diffs(kept)
        if not diffs:
            return "current"
        return self._cal_ask_design_physics(preset["name"], diffs, startup)

    def _cal_bullets(self, rows):
        """Up to four "•  line" rows, then "and N more"."""
        lines = list(rows[:4])
        if len(rows) > 4:
            lines.append("and %d more" % (len(rows) - 4))
        return "\n".join("•  " + ln for ln in lines)

    def _cal_ask_design_physics(self, name, diffs, startup=False):
        """The question, in the page's own dialog when there is one. A rig that
        is not interactive gets "current", which is what loading did before
        designs kept their physics."""
        listed = self._cal_bullets([self._cal_diff_line(*d) for d in diffs])
        if startup:
            text = ("The app opened with the design “%s”, which was saved "
                    "with different physics from the ones the Physics page "
                    "opened with:\n\n%s\n\nPrint it with the physics it was "
                    "saved with, or keep current physics?"
                    % (name, listed))
        else:
            text = ("“%s” was saved with different physics from the ones on "
                    "the Physics page now:\n\n%s\n\nPrint it with the physics "
                    "it was saved with, or keep current physics?"
                    % (name, listed))
        buttons = [("design", "Use the design's physics"),
                   ("current", "Keep current physics")]
        if not startup:
            buttons.append(("cancel", "Cancel"))
        ask = getattr(self, "_gw_dialog", None)
        live = getattr(self, "_gw_interactive", None)
        if callable(ask) and callable(live):
            if not live():
                return "current"
            return ask("Load design", text, buttons, default=0,
                       cancel="current" if startup else "cancel")
        ans = messagebox.askyesnocancel(
            "Load design", text + "\n\nYes uses the design's physics, No "
            "keeps current physics.", parent=self.root)
        return "cancel" if ans is None else ("design" if ans else "current")

    def _cal_design_note(self, data, choice):
        """The sentence after "Loaded design X.": which physics it prints with."""
        kept = (data or {}).get(self.DESIGN_PHYSICS_KEY)
        now = self._cal_cts_phrase()
        if not isinstance(kept, dict) or not getattr(self, "cal_vars", None):
            return ("It keeps no physics of its own, so it prints with the "
                    "Physics page as it is (%s)." % now)
        if choice == "design":
            return ("The physics it was saved with are back on the Physics "
                    "page, and it prints with them (%s)." % now)
        diffs = self._cal_physics_diffs(kept)
        if not diffs:
            return "It prints with the physics it was saved with (%s)." % now
        then = self._cal_cts_phrase(kept)
        if then and then != now:
            was = " (%s)" % then
        else:
            was = " (%d value%s differ)" % (len(diffs),
                                            "" if len(diffs) == 1 else "s")
        return ("It prints with the Physics page as it is (%s), not the "
                "physics it was saved with%s." % (now, was))

    def _cal_measured_record(self):
        """The `MEASURED_KEY` record as a dict; {} when blank or unreadable."""
        var = getattr(self, "cal_vars", {}).get(
            mew_calibration.MEASURED_KEY) if _HAVE_CAL else None
        try:
            text = var.get().strip() if var is not None else ""
            rec = json.loads(text) if text else {}
        except (ValueError, tk.TclError):
            rec = {}
        return rec if isinstance(rec, dict) else {}

    def _cal_value_var(self, key):
        """The var behind a measured key: a `cal.*` field, or one of the
        mandrel's own (`m_cts`, `m_lag_axial`...), which are App attributes."""
        var = getattr(self, "cal_vars", {}).get(key)
        return var if var is not None else getattr(self, key, None)

    def _cal_mark_measured(self, *keys):
        """Record that `keys` now hold a value Calibrate applied."""
        if not _HAVE_CAL or mew_calibration.MEASURED_KEY not in getattr(
                self, "cal_vars", {}):
            return
        rec = self._cal_measured_record()
        for key in keys:
            var = self._cal_value_var(key)
            if var is not None:
                rec[key] = str(var.get()).strip()
        self.cal_vars[mew_calibration.MEASURED_KEY].set(
            json.dumps(rec, sort_keys=True))

    def _cal_unmark(self, keys=None):
        """Forget that `keys` (every key when None) came from Calibrate: a
        Reset puts the default back, so the card must say "default" even when
        the measured value happened to equal it."""
        var = getattr(self, "cal_vars", {}).get(
            mew_calibration.MEASURED_KEY) if _HAVE_CAL else None
        if var is None:
            return
        rec = {} if keys is None else {
            k: v for k, v in self._cal_measured_record().items()
            if k not in set(keys)}
        var.set(json.dumps(rec, sort_keys=True) if rec else "")

    def _cal_is_measured(self, key):
        """True while `key` still holds the value Calibrate applied to it.

        Compared as numbers, so a display-unit round trip that respells the
        same value does not turn it back into a default; anything else that
        changed it (typing, a preset, Reset to defaults) does."""
        want = self._cal_measured_record().get(key)
        var = self._cal_value_var(key)
        if want is None or var is None:
            return False
        try:
            have = str(var.get()).strip()
        except tk.TclError:
            return False
        if have == want:
            return True
        try:
            a, b = float(have), float(want)
        except ValueError:
            return False
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-12)

    #: the mandrel's own measured values: App vars, not `cal.*` keys, blank
    #: by default (blank means the flat value is used)
    _CAL_MANDREL_KEYS = ("m_cts", "m_lag_axial", "m_lag_circ", "m_lag_speed")

    def _cal_note_preset_loaded(self, kind):
        """Remember what a preset load just put in the provenance-tracked vars,
        so a value can read "from preset" while it still holds it. A physics
        preset sets the `cal.*` values; any load can set the mandrel's."""
        snap = getattr(self, "_cal_loaded_snap", None)
        if snap is None:
            snap = self._cal_loaded_snap = {}
        keys = list(self._CAL_MANDREL_KEYS)
        if kind == "calibration":
            keys += list(getattr(self, "cal_vars", {}))
        for key in keys:
            var = self._cal_value_var(key)
            try:
                if var is not None:
                    snap[key] = str(var.get()).strip()
            except tk.TclError:
                pass

    @staticmethod
    def _cal_same(a, b):
        """Two field texts hold the same value (as numbers when both are)."""
        if a == b:
            return True
        try:
            return math.isclose(float(a), float(b), rel_tol=1e-6,
                                abs_tol=1e-12)
        except (TypeError, ValueError):
            return False

    def _cal_origin(self, key):
        """Where the value in `key` came from, in the Physics page's words:
        "measured" (Calibrate applied it), "default" (the value the app ships
        with), "from preset" (the last physics preset loaded it) or "edited"."""
        if self._cal_is_measured(key):
            return "measured"
        var = self._cal_value_var(key)
        try:
            have = str(var.get()).strip() if var is not None else ""
        except tk.TclError:
            have = ""
        fs = mew_calibration._BY_KEY.get(key) if _HAVE_CAL else None
        default = self._cal_default_text(fs) if fs is not None else ""
        if self._cal_same(have, str(default).strip()):
            return "default"
        loaded = getattr(self, "_cal_loaded_snap", {}).get(key)
        if loaded is not None and self._cal_same(have, loaded):
            return "from preset"
        return "edited"

    def _cal_provenance(self, *keys):
        """One word for `keys` together: their shared `_cal_origin`, "partly
        measured" when only some came from Calibrate, else the least settled
        of the rest (edited, then from preset, then default)."""
        got = [self._cal_origin(k) for k in keys]
        if not got:
            return "default"
        if all(g == got[0] for g in got):
            return got[0]
        if "measured" in got:
            return "partly measured"
        for word in ("edited", "from preset"):
            if word in got:
                return word
        return "default"

    # -- mandrel lag radii (one calibration tube, micrographs only) --------- #
    #: how many (angle, wide, narrow) rows the form offers. Three fitted angles
    #: is the procedure's minimum (two parameters plus one); the tube this tab
    #: designs lays five bands by default -- 20/45/70 plus the 0 and 90 deg
    #: CHECKS -- and the sixth row takes one more angle without displacing any.
    _MANDREL_FIT_ROWS = 6

    def _build_fit_mandrel_lag(self, parent):
        """The gap table, and nothing else to type: the fit reads what the tube
        was printed on and at from where each value lives -- the mandrel
        diameter from the Scaffold designer, the gap from the Physics page, the
        print speed and each band's spacing from this tab's design, the CTS
        from the mandrel's own (the CTS (4-axis) tab) or else the flat one.
        A second set of boxes for the same numbers was what made the tab
        unreadable; if you printed at something else, change the design."""
        F = gui_form.Form(parent)
        tip = ("One row per band: its winding angle, then one wide and one "
               "narrow gap between neighbouring lines, measured across the "
               "lines in the middle of the band. The 0° and 90° rows are "
               "checks: fill them in if you like, and expect the two gaps to "
               "match. A blank row is skipped.")
        t = self._fit_table(F, tip)
        for c, text in enumerate(("Winding angle (°)", "Wide gap (mm)",
                                  "Narrow gap (mm)", "")):
            self._fit_cell(ttk.Label(t, text=text), tip, "colhead").grid(
                row=0, column=c, padx=(0, gui_fonts.SP["xs"]), sticky="w")
        self._fit_lag_rows = []
        self._fit_lag_checks = []
        for i in range(self._MANDREL_FIT_ROWS):
            trio = (tk.StringVar(), tk.StringVar(), tk.StringVar())
            for c, var in enumerate(trio):
                self._fit_cell(ttk.Entry(t, textvariable=var, width=12),
                               tip).grid(row=1 + i, column=c,
                                         padx=(0, gui_fonts.SP["xs"]), pady=1,
                                         sticky="w")
            mark = tk.StringVar(value="")
            # a live per-row note (the check rows), not a parameter's help
            gui_form.mark(ttk.Label(t, textvariable=mark, style="Hint.TLabel"),
                          "live").grid(row=1 + i, column=3, sticky="w")
            trio[0].trace_add("write", lambda *_a, a=trio[0], m=mark:
                              m.set(self._fit_lag_check_text(a.get())))
            self._fit_lag_rows.append(trio)
            self._fit_lag_checks.append(mark)

        self._fit_lag_out = self._fit_result_row(
            F, self._compute_fit_mandrel_lag, self._apply_fit_mandrel_lag)
        self._fit_lag_result = None
        # the angle column follows the tube this tab designs
        make = getattr(self, "_sw_vars_make", None)
        if callable(make):
            make()
        sw = getattr(self, "sw_vars", None)
        if sw is not None and "tube_angles" in sw:
            sw["tube_angles"].trace_add(
                "write", lambda *_a: self._fit_lag_seed_angles())
            self._fit_lag_seed_angles()

    @staticmethod
    def _fit_lag_check_text(angle_text):
        """The note beside a row: a 0 or 90 deg row is a check, not a fit
        point (the same split `mew_lag_fit` makes)."""
        try:
            a = float(angle_text)
        except ValueError:
            return ""
        import mew_sweeps
        return ("check: the two gaps should match"
                if mew_sweeps._is_null_angle(a) else "")

    def _fit_lag_seed_angles(self):
        """Put the tube's winding angles in the angle column, one per row.

        Only rows with NO gap typed are touched, so a design edit never moves an
        angle away from the gaps measured at it; a row the design no longer has
        is cleared the same way. The angles are the tube's bands, in print
        order, so row n is the n-th band along the mandrel."""
        rows = getattr(self, "_fit_lag_rows", None)
        sw = getattr(self, "sw_vars", None)
        if not rows or sw is None or "tube_angles" not in sw:
            return
        import mew_sweeps
        angles = mew_sweeps.parse_float_list(sw["tube_angles"].get())
        for i, (a_var, w_var, n_var) in enumerate(rows):
            if w_var.get().strip() or n_var.get().strip():
                continue
            a_var.set("%g" % angles[i] if i < len(angles) else "")

    def _fit_lag_programmed(self, angle, dia):
        """The spacing this tab's tube lays at `angle` on a `dia` mm mandrel,
        or None when the design does not read -- the engine's own layout
        (`mew_sweeps.tube_band_layout`), lines per band included."""
        import mew_sweeps
        try:
            s = float(self.sw_vars["tube_spacing"].get())
            text = self.sw_vars["tube_lines"].get().strip()
            lines = int(float(text)) if text else None
            band = float(self.sw_vars["tube_band_len"].get())
        except (AttributeError, KeyError, ValueError, tk.TclError):
            return None
        if not s > 0.0:
            return None
        return mew_sweeps.tube_band_layout(0.5 * dia, angle, s, lines,
                                           band)[1]

    def _fit_lag_fallback(self, var, live, scale=1.0, name=""):
        """A typed value, or the page that already owns it -- never a copy.
        (The lag-circle card's gap box reads through this.)

        The gap, the mandrel diameter and CTS each have ONE home elsewhere in the app.
        Duplicating them into this form would let the two drift apart silently, and
        pre-filling would go stale the moment the other page changed. So a blank field
        reads the live one at Compute time, and the result strip says which it used.
        """
        text = var.get().strip()
        if text:
            return self._fit_num(text), ""
        return self._fit_num(live()) * scale, name

    def _fit_lag_design(self):
        """What the tube was printed on and at, each from its one home:
        `(dia_mm, gap_mm, speed_mm_s, cts_mm_s, cts_is_mandrel)`."""
        dia = (self._sw_mandrel_diameter()
               if hasattr(self, "_sw_mandrel_diameter")
               else self._fit_num(self.m_diameter.get()))
        gap = self._fit_num(self.sim_standoff.get())
        try:
            speed = self._fit_num(self.sw_vars["tube_speed"].get()) / 60.0
        except (AttributeError, KeyError):
            speed = self._fit_num(self.scaf_vars[0]["speed"].get()) / 60.0
        text = getattr(self, "m_cts", None)
        text = text.get().strip() if text is not None else ""
        cts = self._fit_num(text or self.cal_vars["cal.cts_ref"].get())
        return dia, gap, speed, cts, bool(text)

    def _compute_fit_mandrel_lag(self):
        import mew_lag_fit

        try:
            triples = []
            for a_var, w_var, n_var in self._fit_lag_rows:
                if not (a_var.get().strip() and w_var.get().strip()
                        and n_var.get().strip()):
                    continue
                triple = (self._fit_num(a_var.get()),
                          self._fit_num(w_var.get()),
                          self._fit_num(n_var.get()))
                if not (triple[1] > 0.0 and triple[2] > 0.0):
                    raise ValueError(
                        "the %g° row has a gap of %g mm. A gap between two "
                        "lines is more than 0: type the two neighbouring gaps "
                        "you measured, in mm" % (triple[0], min(triple[1:])))
                triples.append(triple)
            if not triples:
                raise ValueError(
                    "enter at least three (angle, wide, narrow) rows")
            dia, gap, speed, cts, cts_mandrel = self._fit_lag_design()
            rows = [mew_lag_fit.GapRow(
                        a, w, n, programmed_mm=self._fit_lag_programmed(a, dia))
                    for a, w, n in triples]
            # A gap typed in um, or a column of the wrong band, is far off
            # the spacing the tube printed. The fit took it anyway and
            # returned R_ax = 60 mm, which Apply would have written; the
            # result's own 2 % scale note is for a slightly wrong scale.
            for r in rows:
                if r.programmed_mm and not (
                        0.7 <= r.spacing_mm / r.programmed_mm <= 1.3):
                    raise ValueError(
                        "the %g° band's gaps average %.4g mm, but the tube "
                        "printed its lines %.3g mm apart. Gaps are in mm: "
                        "check the unit, and that the row is that band's"
                        % (r.angle_deg, r.spacing_mm, r.programmed_mm))
            _mat, _noz, cal = mew_calibration.models_from_profile(
                self._calibration_profile_snapshot())
            fit = mew_lag_fit.fit_radii(rows, radius=0.5 * dia, gap=gap,
                                        speed=speed, cts=cts,
                                        calibration=cal)
            # A radius pinned at the fit's bracket is not a measurement: the
            # gaps asked for a lean no jet has, and the fit stopped at the
            # wall. It returned R_ax = 60.0000 mm, which Apply wrote.
            lo, hi = mew_lag_fit.R_MIN, mew_lag_fit.R_MAX
            for label, value in (("R_ax", fit.R_ax), ("R_cir", fit.R_cir)):
                if not lo * 1.01 < value < hi * 0.99:
                    raise ValueError(
                        "these gaps ask for a lag beyond what the fit allows "
                        "(%s stopped at its %s mm limit), so they do not "
                        "describe a jet's lean. Check that each row's wide and "
                        "narrow gaps are two neighbouring gaps in that angle's "
                        "band, in mm" % (label, "%g" % (hi if value >= hi * 0.99
                                                        else lo)))
        except Exception as exc:                     # noqa: BLE001 -- to the UI
            self._fit_lag_result = None
            self._fit_lag_out.configure(text=self._fit_say(exc),
                                        foreground="#a33")
            return
        self._fit_lag_result = fit
        self._fit_lag_speed_used = speed
        text = mew_lag_fit.summarise(fit)
        text += ("\n - printed on a %.3f mm mandrel (Scaffold designer) at a "
                 "%.3f mm gap (Physics page) and %.0f mm/min (this type)"
                 % (dia, gap, speed * 60.0))
        if cts_mandrel:
            text += ", mandrel CTS %.0f mm/min" % (cts * 60.0)
        else:
            text += ("\n - no mandrel CTS measured, so the flat CTS (%.0f "
                     "mm/min) was used: both radii absorb any difference "
                     "between it and the mandrel's" % (cts * 60.0))
        text += ("\n - Apply writes R_ax, R_cir and the speed they were "
                 "measured at to the mandrel only; the flat lag amplitude A "
                 "is not changed.")
        # its speeds in the display unit (gui_units)
        self._fit_lag_out.configure(text=gui_units.localize(text),
                                    foreground="#1f6b2e")

    def _apply_fit_mandrel_lag(self):
        """The two radii AND the speed they were measured at -- mandrel only.

        NEVER the flat lag amplitude A: flat prints read that same constant, so
        writing it made a mandrel calibration silently move every flat
        prediction. The pair plus its speed is enough for a rotary lay to carry
        R_ax to any speed through the lag law, R_ax(eps) = R_ax,cal * L(eps) /
        L(eps_cal), with R_cir held at its fraction of R_ax
        (`mew_lag_fit.mandrel_calibration`); a flat lay never reads them.
        """
        self._compute_fit_mandrel_lag()       # what is on screen NOW
        fit = self._fit_lag_result
        if fit is None:
            return
        if not all(hasattr(self, n) for n in ("m_lag_axial", "m_lag_circ",
                                               "m_lag_speed")):
            self.status.set("This build has no mandrel page to apply to.")
            return
        self.m_lag_axial.set("%.6g" % fit.R_ax)
        self.m_lag_circ.set("%.6g" % fit.R_cir)
        self.m_lag_speed.set("%.6g" % self._fit_lag_speed_used)
        self._cal_mark_measured("m_lag_axial", "m_lag_circ", "m_lag_speed")
        self._cal_applied_status(
            "R_ax, R_cir and their speed to the mandrel (Jet lag card). The "
            "flat lag amplitude is unchanged")

    # -- viscosity magnitude anchor ----------------------------------------- #
    def _build_fit_viscosity(self, parent):
        F = gui_form.Form(parent)
        self._fit_visc_press = tk.StringVar()
        self._fit_visc_temp = tk.StringVar()
        self._fit_visc_speed = tk.StringVar()
        F.entry("Pressure", self._fit_visc_press,
                "The feed pressure the plate was printed at. Seeded from the "
                "Physics page.", unit="psi")
        F.entry("Temperature", self._fit_visc_temp,
                "The nozzle temperature the plate was printed at. Seeded from "
                "this type's print design.", unit="°C")
        F.entry("Print speed", self._fit_visc_speed,
                "The speed the lines were printed at. Seeded from this "
                "type's print speed.", unit="mm/s")
        self._fit_visc_diams, self._fit_visc_diam_widgets = self._fit_rep_cells(
            F, "Measured fiber Ø",
            "One cell per line on the slide. Blank cells are skipped, so "
            "measuring three of five lines gives a three-line mean.",
            unit="mm")
        #: the FIRST cell under its own name. A one-line slide is still the common
        #: case, and the seeding contract ("this one must stay blank") is stated
        #: about it.
        self._fit_visc_diam = self._fit_visc_diams[0]
        self._fit_visc_out = self._fit_result_row(
            F, self._compute_fit_visc, self._apply_fit_visc)
        self._cal_sync_diam_fit()

    def _compute_fit_visc(self):
        try:
            p_pa = self._fit_float(self._fit_visc_press) * PSI_TO_PA
            t = self._fit_float(self._fit_visc_temp)
            v = self._fit_float(self._fit_visc_speed)
            d = self._fit_mean_diam(self._fit_visc_diams)
            if d is None:
                raise ValueError("type at least one measured fiber diameter")
            for var in self._fit_visc_diams:
                if var.get().strip():
                    self._fit_check_diam(self._fit_num(var.get()),
                                         self._cell_shown(var))
            val = mew_calibration.fit_viscosity_ref(
                self._calibration_model(), d, p_pa, t, v)
        except (ValueError, ZeroDivisionError) as exc:
            self._fit_results.pop("visc", None)
            self._fit_visc_out.configure(
                text=self._fit_say(exc, "Check the pressure, temperature, "
                                        "speed and diameter."),
                foreground="#a33")
            return
        self._fit_results["visc"] = val
        n = self._fit_rep_filled(self._fit_visc_diams)
        mean = "" if n < 2 else "  (mean of %d lines, Ø %s)" % (
            n, gui_units.show("fiber_d", "%.4g" % d, "mm"))
        self._fit_visc_out.configure(
            text=f"Melt viscosity anchor = {val:.5g} Pa·s{mean}",
            foreground="#1f6b2e")

    def _apply_fit_visc(self):
        # Always from what is on screen NOW: a result kept from an earlier
        # Compute is a fit of numbers the operator may since have changed.
        self._compute_fit_visc()
        if "visc" in self._fit_results:
            self._cal_apply("cal.mat_viscosity_ref", self._fit_results["visc"])

    # -- activation energy -------------------------------------------------- #
    #: the most measurement rows the Ea card can show. The card shows ONE ROW PER
    #: RUNG of the designed ladder and no more -- the print lays one measurable
    #: slide per rung, so a sixth empty row would be a row with no specimen behind
    #: it. Two is the floor because the fit needs two distinct temperatures
    #: (mew_calibration.fit_activation_energy raises below that).
    _EA_FIT_ROWS_MAX = 8

    def _build_fit_ea(self, parent):
        F = gui_form.Form(parent)
        tip = ("One row per temperature rung: its temperature, then one fiber "
               "Ø cell per line on that temperature's slide, in {fiber_d}. Blank cells "
               "are skipped; each rung is averaged by the mean of ln d.")
        # the caption sits above the table, out of its grid, for the reason
        # the sag card gives: spanning the cell columns would stretch them
        F.slot(None, self._fit_cell(
            gui_units.track(ttk.Label(F.frame),
                            "Fiber Ø ({fiber_d}): one cell per line on "
                            "that temperature’s slide"), tip, "colhead"))
        f = self._fit_table(F, tip)
        self._fit_cell(ttk.Label(f, text="Temperature (°C)"), tip,
                       "colhead").grid(row=1, column=0, padx=(0, 4), sticky="w")
        self._fit_ea_heads = []
        for j in range(self._FIT_REP_COLS_MAX):
            lab = self._fit_cell(ttk.Label(f, text=str(j + 1)), tip, "colhead")
            lab.grid(row=1, column=1 + j, padx=2, sticky="w")
            self._fit_ea_heads.append(lab)
        self._fit_ea_rows = []
        self._fit_ea_widgets = []
        self._fit_ea_diams = []
        self._fit_ea_diam_widgets = []
        for i in range(self._EA_FIT_ROWS_MAX):
            tvar = tk.StringVar()
            te = self._fit_cell(ttk.Entry(f, textvariable=tvar, width=10), tip)
            te.grid(row=2 + i, column=0, padx=(0, 4), pady=1, sticky="w")
            dvars, dents = [], []
            for j in range(self._FIT_REP_COLS_MAX):
                dvar = tk.StringVar()
                de = self._fit_cell(ttk.Entry(f, textvariable=dvar, width=7), tip)
                de.grid(row=2 + i, column=1 + j, padx=2, pady=1, sticky="w")
                # display units: the header above names the unit (tracked)
                gui_units.bind(de, dvar, "mm", None, "Fiber Ø")
                dvars.append(dvar)
                dents.append(de)
            self._fit_ea_diams.append(dvars)
            self._fit_ea_diam_widgets.append(dents)
            #: `_fit_ea_rows` stays (temperature, FIRST diameter) so the row-level
            #: seeding and visibility contract keeps one name to reach for.
            self._fit_ea_rows.append((tvar, dvars[0]))
            self._fit_ea_widgets.append((te, dents[0]))
        self._fit_ea_out = self._fit_result_row(
            F, self._compute_fit_ea, self._apply_fit_ea)
        self._cal_sync_diam_fit()

    # -- the ladder design -> the two fit forms it feeds -------------------- #
    def _cal_sync_diam_fit(self):
        """Point both ladder fits at the ladder that is actually designed.

        The Ea card shows exactly one measurement row per RUNG, because the print
        lays exactly one measurable slide per rung; a row with no slide behind it is
        a row the operator has no way to fill. The same rule runs the other way for
        the diameter CELLS: one per line the slide lays (`diam_reps`, `visc_reps`),
        because the plate lays several and a fit that reads one of them throws the
        rest away. Hidden cells are BLANKED, not just ungridded -- a number left
        behind a lowered line count would go on feeding the mean unseen. The
        temperature column is seeded from
        the ladder, and the viscosity card's three condition fields are seeded from
        the anchor rung, the ladder feed and the Physics page's pressure -- the
        anchor IS the hottest rung (the calibration runbook), so retyping it
        is a chance to disagree with the print for no gain.

        SEEDS, never overwrites. A cell is filled only while it is blank or still
        holds the value this method last wrote into it, so a number the operator
        typed survives every later edit of the design."""
        sw = getattr(self, "sw_vars", None)
        if sw is None or "diam_temps" not in sw:
            return
        try:
            import mew_sweeps
            temps = mew_sweeps.temperature_ladder(
                mew_sweeps.parse_float_list(sw["diam_temps"].get()))
        except Exception:  # noqa: BLE001 -- mid-typing / module missing
            return
        seeded = getattr(self, "_cal_diam_seeded", None)
        if seeded is None:
            seeded = self._cal_diam_seeded = {}

        def seed(name, var, value):
            if var is None:
                return
            cur = var.get().strip()
            if cur and cur != seeded.get(name):
                return                       # the operator's own number: leave it
            var.set(value)
            seeded[name] = value

        def unseed(name, var):
            if var is not None and var.get().strip() == seeded.get(name):
                var.set("")
                seeded.pop(name, None)

        rows = getattr(self, "_fit_ea_rows", None)
        widgets = getattr(self, "_fit_ea_widgets", None)
        diams = getattr(self, "_fit_ea_diams", None)
        dwidgets = getattr(self, "_fit_ea_diam_widgets", None)
        if rows and widgets and diams and dwidgets:
            n = max(2, min(len(temps), len(rows)))
            # one COLUMN per line the slide lays, exactly as one ROW per rung
            n_rep = self._cal_rep_count(sw, "diam_reps")
            for j, lab in enumerate(getattr(self, "_fit_ea_heads", ())):
                if j < n_rep:
                    lab.grid()
                else:
                    lab.grid_remove()
            for i, ((tvar, _first), (te, _de)) in enumerate(zip(rows, widgets)):
                dvars, dents = diams[i], dwidgets[i]
                if i < n:
                    te.grid()
                    self._cal_sync_rep_cells(n_rep, dents, dvars)
                    if i < len(temps):
                        seed("ea_t%d" % i, tvar, "%g" % temps[i])
                    else:
                        # a visible row the ladder has no rung for -- the two-row
                        # floor over a one-rung ladder. It must show NO temperature
                        # rather than the one it was seeded with last time, which
                        # would name a slide this print does not lay.
                        unseed("ea_t%d" % i, tvar)
                else:
                    te.grid_remove()
                    self._cal_sync_rep_cells(0, dents, dvars)
                    tvar.set("")
                    seeded.pop("ea_t%d" % i, None)

        # the viscosity slide's own line count drives its row of cells
        if getattr(self, "_fit_visc_diams", None):
            self._cal_sync_rep_cells(
                self._cal_rep_count(sw, "visc_reps"),
                self._fit_visc_diam_widgets, self._fit_visc_diams)

        # The viscosity card is seeded from the VISCOSITY plate's own design, not
        # from the Ea ladder: the two tabs do not share one print,
        # so seeding the anchor rung here would name a temperature
        # this tab's plate is not laid at.
        vtemps = []
        if "visc_temp" in sw:
            try:
                import mew_sweeps as _ms
                vtemps = _ms.temperature_ladder(
                    _ms.parse_float_list(sw["visc_temp"].get()))[:1]
            except Exception:  # noqa: BLE001 -- mid-typing / module missing
                vtemps = []
        if vtemps and getattr(self, "_fit_visc_temp", None) is not None:
            seed("visc_t", self._fit_visc_temp, "%g" % vtemps[0])
        try:
            feed = float(sw["visc_speed"].get())
        except (tk.TclError, ValueError, KeyError):
            feed = 0.0
        if feed > 0 and getattr(self, "_fit_visc_speed", None) is not None:
            seed("visc_v", self._fit_visc_speed, "%g" % round(feed / 60.0, 4))
        press = getattr(self, "sim_pressure", None)
        if press is not None and getattr(self, "_fit_visc_press", None) is not None:
            try:
                seed("visc_p", self._fit_visc_press, press.get().strip())
            except tk.TclError:
                pass

    #: a nozzle temperature outside this, in degC, is a typo or kelvin
    _EA_T_C = (20.0, 400.0)

    def _compute_fit_ea(self):
        pts, counts = [], []
        try:
            for (tvar, _first), dvars in zip(self._fit_ea_rows,
                                             self._fit_ea_diams):
                ts = tvar.get().strip()
                filled = self._fit_rep_filled(dvars)
                if not ts:
                    if filled:
                        raise ValueError(
                            "a row has fiber diameters but no temperature. "
                            "Type the temperature that slide was printed at")
                    continue
                t = self._fit_num(ts)
                lo, hi = self._EA_T_C
                if not lo <= t <= hi:
                    raise ValueError(
                        "%s °C is not a nozzle temperature (%g to %g °C). "
                        "Temperatures are in °C, not kelvin" % (ts, lo, hi))
                d = self._fit_mean_diam(dvars, log=True)
                if d is None:
                    continue
                for var in dvars:
                    if var.get().strip():
                        self._fit_check_diam(self._fit_num(var.get()),
                                             self._cell_shown(var))
                pts.append((t, d))
                counts.append(filled)
            val = mew_calibration.fit_activation_energy(pts)
        except (ValueError, ZeroDivisionError) as exc:
            self._fit_results.pop("ea", None)
            self._fit_ea_out.configure(text=self._fit_say(exc),
                                       foreground="#a33")
            return
        self._fit_results["ea"] = val
        lines = ""
        if counts and max(counts) > 1:
            lo, hi = min(counts), max(counts)
            span = str(hi) if lo == hi else f"{lo}–{hi}"
            lines = f"  ({span} lines per temperature, averaged)"
        self._fit_ea_out.configure(
            text=f"Ea = {val:.5g} J/mol  ({val / 1000:.3g} kJ/mol){lines}",
            foreground="#1f6b2e")

    def _apply_fit_ea(self):
        self._compute_fit_ea()                # what is on screen NOW
        if "ea" in self._fit_results:
            self._cal_apply("cal.mat_activation_energy", self._fit_results["ea"])

    # -- sag magnitude + span exponent -------------------------------------- #
    #: the most measurement rows the sag card can show. One row per DISTINCT span
    #: the ladder lays and no more -- a row with no gap behind it is a row the
    #: operator cannot fill. Twelve because the default ladder is seven spans and
    #: the list is free text; a longer ladder shows its first twelve.
    _SAG_FIT_ROWS_MAX = 12

    def _build_fit_sag(self, parent):
        """The sag LADDER fit: one row per span, one cell per reading of it.

        Fitting the span exponent takes a ROW of spans -- one pore gives one point
        and can only invert the magnitude -- so the card is a grid, and the print
        that fills it is this tab's own ladder. A span is read once per bridging
        fiber per pass, so the cells on a row are the readings the DESIGN actually
        offers (`_cal_sync_sag_fit`); measuring the second fiber is optional and a
        blank cell is simply skipped, as is a span whose fiber bottomed out.

        NO fiber diameter and NO temperature field, unlike the card this replaces.
        One ladder is one fiber at one temperature, so both are constant across
        every point in the regression and fold into the magnitude -- and nothing in
        the procedure MEASURES either of them, so a field for them would be a
        number the operator has to invent (`mew_calibration.fit_sag_ladder`)."""
        F = gui_form.Form(parent)
        tip = ("One row per span the ladder lays: the span, then one cell per "
               "reading of it, in mm of midspan sag. Leave a cell blank for "
               "a reading you did not take and for any fiber that bottomed out "
               "on the collector; measuring the second bridging fiber is "
               "optional. The exponent needs at least %d spans covering at "
               "least %g× in span; below that the fit keeps the Physics "
               "page's exponent and fits the magnitude alone."
               % (mew_calibration.SAG_MIN_SPANS,
                  mew_calibration.SAG_MIN_SPAN_RATIO))
        # The wide caption is its own row ABOVE the table, not a grid cell of
        # it: a label spanning the cell columns stretches every column it
        # covers once the hidden ones collapse, which pushed the fourth reading
        # of a mirrored two-fibre ladder off the form pane. Out of the grid,
        # the heads and the cells share columns, so every head sits on its
        # cells' left edge.
        F.slot(None, self._fit_cell(
            ttk.Label(F.frame, text="Midspan sag (mm): one cell per "
                                    "reading of that span"), tip, "colhead"))
        f = self._fit_table(F, tip)
        self._fit_cell(ttk.Label(f, text="Span (mm)"), tip, "colhead").grid(
            row=0, column=0, padx=(0, 4), sticky="w")
        self._fit_sag_heads = []
        for j in range(self._FIT_REP_COLS_MAX):
            lab = self._fit_cell(ttk.Label(f, text=str(j + 1)), tip, "colhead")
            lab.grid(row=0, column=1 + j, padx=(0, 3), sticky="w")
            self._fit_sag_heads.append(lab)
        self._fit_sag_rows = []
        self._fit_sag_widgets = []
        self._fit_sag_depths = []
        self._fit_sag_depth_widgets = []
        for i in range(self._SAG_FIT_ROWS_MAX):
            svar = tk.StringVar()
            se = self._fit_cell(ttk.Entry(f, textvariable=svar, width=10), tip)
            se.grid(row=1 + i, column=0, padx=(0, 4), pady=1, sticky="w")
            dvars, dents = [], []
            for j in range(self._FIT_REP_COLS_MAX):
                dvar = tk.StringVar()
                de = self._fit_cell(ttk.Entry(f, textvariable=dvar, width=7),
                                    tip)
                de.grid(row=1 + i, column=1 + j, padx=(0, 3), pady=1,
                        sticky="w")
                dvars.append(dvar)
                dents.append(de)
            self._fit_sag_depths.append(dvars)
            self._fit_sag_depth_widgets.append(dents)
            #: `_fit_sag_rows` stays (span, FIRST droop) so the row-level seeding
            #: and visibility contract keeps one name to reach for, exactly as on
            #: the Ea card.
            self._fit_sag_rows.append((svar, dvars[0]))
            self._fit_sag_widgets.append((se, dents[0]))
        self._fit_sag_out = self._fit_result_row(
            F, self._compute_fit_sag, self._apply_fit_sag)
        self._fit_sag_result = None
        self._cal_sync_sag_fit()

    def _cal_sag_readings(self):
        """(spans, readings per span) the DESIGNED ladder offers the operator.

        The spans are the typed list in the order it was typed, deduplicated: the
        form is one measurement row per distinct span. The readings are how many
        times that span is laid -- each occurrence in the list, doubled by "Print
        each span twice", times the bridging fibers the travel lane leaves
        measurable (`mew_sweeps.SagParams`: the middle lane clears both fibers, an
        edge lane only one). `mew_sweeps.sag_gaps` is not reused here because it
        FLATTENS the mirror into a gap sequence, and what a row needs is the
        count."""
        sw = getattr(self, "sw_vars", None)
        if sw is None or "sag_spans" not in sw:
            return [], []
        try:
            import mew_sweeps
            vals = [round(float(x), 4)
                    for x in mew_sweeps.parse_float_list(sw["sag_spans"].get())
                    if float(x) > 0]
        except Exception:  # noqa: BLE001 -- mid-typing / module missing
            return [], []
        spans, seen = [], {}
        for v in vals:
            seen[v] = seen.get(v, 0) + 1
            if seen[v] == 1:
                spans.append(v)
        per = 1
        try:
            if bool(sw["sag_mirror"].get()):
                per *= 2
        except (KeyError, tk.TclError):
            pass
        try:
            if str(sw["sag_lane"].get()).strip() != "edge":
                per *= 2
        except (KeyError, tk.TclError):
            pass
        return spans, [seen[v] * per for v in spans]

    def _cal_sync_sag_fit(self):
        """Point the sag fit form at the ladder that is actually designed.

        The same two rules the Ea card follows: one ROW per span the print lays,
        seeded with that span, and one CELL per reading of it. Hidden cells are
        BLANKED rather than merely ungridded -- a droop left behind a shortened
        ladder would go on feeding the fit unseen. SEEDS, never overwrites: a
        number the operator typed survives every later edit of the design."""
        rows = getattr(self, "_fit_sag_rows", None)
        if not rows:
            return
        spans, per = self._cal_sag_readings()
        seeded = getattr(self, "_cal_sag_seeded", None)
        if seeded is None:
            seeded = self._cal_sag_seeded = {}
        n = min(len(spans), self._SAG_FIT_ROWS_MAX)
        shown = [min(p, self._FIT_REP_COLS_MAX) for p in per[:n]]
        for j, lab in enumerate(self._fit_sag_heads):
            if j < max(shown, default=0):
                lab.grid()
            else:
                lab.grid_remove()
        for i, (svar, _first) in enumerate(rows):
            se, _de = self._fit_sag_widgets[i]
            dvars = self._fit_sag_depths[i]
            dents = self._fit_sag_depth_widgets[i]
            name = "sag_s%d" % i
            if i < n:
                se.grid()
                self._cal_sync_rep_cells(shown[i], dents, dvars)
                cur = svar.get().strip()
                if not cur or cur == seeded.get(name):
                    svar.set("%g" % spans[i])
                    seeded[name] = svar.get()
            else:
                se.grid_remove()
                self._cal_sync_rep_cells(0, dents, dvars)
                svar.set("")
                seeded.pop(name, None)

    def _cal_sag_span_exp(self):
        """The rig's own span exponent p -- the Physics page's field, or the
        shipped default when it is unreadable. Read ONLY as what the refusal rule
        falls back to; a ladder that earns the exponent overwrites it."""
        try:
            return float(self.cal_vars["cal.sag_span_exp"].get())
        except (KeyError, ValueError, AttributeError, tk.TclError):
            return float(mew_calibration.models_from_profile({})[2].sag_span_exp)

    def _compute_fit_sag(self):
        try:
            pts, counts = self._fit_sag_points()
            fit = mew_calibration.fit_sag_ladder(
                pts, fallback_span_exp=self._cal_sag_span_exp())
        except (ValueError, ZeroDivisionError) as exc:
            self._fit_sag_result = None
            self._fit_results.pop("sag", None)
            self._fit_sag_out.configure(text=self._fit_say(exc),
                                        foreground="#a33")
            return
        self._fit_sag_result = fit
        self._fit_results["sag"] = fit.coeff
        self._fit_sag_out.configure(text=self._fit_sag_text(fit, counts),
                                    foreground="#1f6b2e")

    def _fit_sag_points(self):
        """(span, depth) per filled row, and the readings behind each."""
        pts, counts = [], []
        for i, (svar, _first) in enumerate(self._fit_sag_rows):
            text = svar.get().strip()
            if not text:
                if self._fit_rep_filled(self._fit_sag_depths[i]):
                    raise ValueError("a row has sag readings but no span. "
                                     "Type the span it was measured over")
                continue
            span = self._fit_num(text)
            if not span > 0.0:
                raise ValueError("‘%s’ is not a span. Spans are above "
                                 "zero, in mm" % text)
            # the GEOMETRIC mean of that span's readings, because the regression
            # is in log space and that is the collapse belonging to it -- the same
            # argument `_fit_mean_diam(log=True)` already carries for the Ea
            # ladder. Blank and bottomed-out cells are simply not there.
            depth = self._fit_mean_diam(self._fit_sag_depths[i], log=True)
            if depth is None:
                continue
            if depth >= span:
                raise ValueError(
                    "the %s mm span reads a sag of %.4g mm, as deep as the "
                    "span is wide. Sag is in mm: check the unit you measured in"
                    % (text, depth))
            pts.append((span, depth))
            counts.append(self._fit_rep_filled(self._fit_sag_depths[i]))
        return pts, counts

    @staticmethod
    def _fit_sag_text(fit, counts):
        """The result strip's words: what was fitted, out of how much, how well.

        `c` is stated as mm^(1-p) whenever p was fitted, because that IS its unit
        once p moves off 2 -- the Physics page's field says 1/mm, which is only
        true at the shipped exponent."""
        if fit.exp_fitted:
            head = ("Span exponent p = %.4g \u00b7 magnitude c = %.5g mm^(1-p)"
                    % (fit.span_exp, fit.coeff))
        else:
            # the engine's reason is a lowercase clause with a " -- " join;
            # here it starts its own sentence
            why = (fit.reason or "").replace(" -- ", ", and ")
            head = ("Magnitude c = %.5g mm^(1-p) at the Physics page's "
                    "exponent p = %.4g. %s."
                    % (fit.coeff, fit.span_exp, why[:1].upper() + why[1:]))
        bits = ["%d span%s over %.3g\u00d7 of span"
                % (fit.n_spans, "" if fit.n_spans == 1 else "s", fit.span_ratio)]
        if fit.r2 is not None:
            bits.append("R\u00b2 = %.4g in log-log" % fit.r2)
        if counts and max(counts) > 1:
            lo, hi = min(counts), max(counts)
            bits.append("%s reading%s per span, averaged"
                        % (str(hi) if lo == hi else "%d\u2013%d" % (lo, hi),
                           "" if hi == 1 else "s"))
        return head + "\n" + " \u00b7 ".join(bits)

    def _apply_fit_sag(self):
        """The magnitude always; the exponent only when the row earned it.

        Writing a refused p back would overwrite the rig's own value with itself
        in the good case and with a two-point guess in the bad one, which is the
        very thing the estimator refuses -- so a refused exponent is left alone
        and the status line names only what moved."""
        self._compute_fit_sag()               # what is on screen NOW
        fit = getattr(self, "_fit_sag_result", None)
        if fit is None:
            return
        self._cal_apply("cal.sag_coeff", fit.coeff)
        if fit.exp_fitted:
            self._cal_apply("cal.sag_span_exp", fit.span_exp)
            self._cal_applied_status("the sag magnitude c and the span "
                                     "exponent p (Sagging card)")
        else:
            self._cal_applied_status("the sag magnitude c (Sagging card). The "
                                     "span exponent p was kept")

    # -- fusion overlap: junction and wall phi off the operator's own print -- #
    #: how many rows each of the two tables opens with. A starting count only;
    #: the operator adds and removes rows like the lag-circle card.
    _FUSION_FIT_ROWS = 2

    def _build_fit_fusion(self, parent):
        """Junction and wall heights measured on the scaffold the operator printed.

        No calibration print and no g-code. Fusion depends on layer time, which the
        law does not carry, so a phi only holds for repeats of the scaffold it was
        measured on -- and the operator fits it on that scaffold directly. Each
        reading is (layers stacked, measured height); the unfused reference is N*d
        with the fiber diameter measured on the same print, and each of the two
        tables fits its own phi (`mew_calibration.fit_fusion_rows`), and each has
        its own Apply button, so either value can be written without the other.
        The wall table is optional."""
        F = gui_form.Form(parent)
        self._fit_fus_diam = tk.StringVar()
        F.entry("Measured fiber Ø", self._fit_fus_diam,
                "The fiber diameter you measured on this same print.", unit="mm")
        self._fit_fus = {}
        for which, caption, add, tip in (
                ("junction", "Junctions: one row per crossover you measured",
                 "Add junction row",
                 "One row per crossover: how many layers are stacked there and "
                 "the height you measured, in mm. Rows with a blank cell are "
                 "skipped."),
                ("wall", "Walls, optional: one row per straight span you "
                         "measured",
                 "Add wall row",
                 "One row per wall, measured midway between two crossovers: how "
                 "many fibers are stacked in it and the height you measured, in "
                 "mm. On a 0/90 scaffold only every other layer runs along a "
                 "wall. Leave the table empty to keep the current straight-span "
                 "sink.")):
            F.slot(None, self._fit_cell(ttk.Label(F.frame, text=caption), tip,
                                        "colhead"))
            tbl = self._fit_table(F, tip)
            for c, text in enumerate(("Layers N", "Height (mm)")):
                self._fit_cell(ttk.Label(tbl, text=text), tip, "colhead").grid(
                    row=0, column=c, padx=(0, gui_fonts.SP["xs"]), sticky="w")
            self._fit_fus[which] = dict(tbl=tbl, tip=tip, rows=[], widgets=[])
            for _ in range(self._FUSION_FIT_ROWS):
                self._fit_fus_add_row(which)
            bar = gui_form.mark(ttk.Frame(F.frame), "group")
            b = gui_form.mark(ttk.Button(
                bar, text=add,
                command=lambda w=which: self._fit_fus_add_row(w)), "button")
            b.pack(side="left")
            gui_tips.add_tip(b, "Add one %s measurement row." % which)
            F.slot(None, bar)
        self._fit_fus_out = self._fit_result_row(
            F, self._compute_fit_fusion, None, applies=(
                ("Apply junction sink", self._apply_fit_fusion_junction,
                 "Write the junction sink φ into the Physics page's "
                 "“Junction sink”."),
                ("Apply straight-span sink", self._apply_fit_fusion_wall,
                 "Write the straight-span sink φ into the Physics page's "
                 "“Straight-span "
                 "sink” and tick its switch.")))
        self._fit_fus_result = None

    def _fit_fus_add_row(self, which, values=("", "")):
        """Append one (layers, height) row to a table and return its vars."""
        g = self._fit_fus[which]
        pair = tuple(tk.StringVar(value=v) for v in values)
        ws = [self._fit_cell(ttk.Entry(g["tbl"], textvariable=var, width=10),
                             g["tip"]) for var in pair]
        ws.append(self._fit_cell(
            ttk.Button(g["tbl"], text="−", width=2,
                       command=lambda: self._fit_fus_remove_row(which, pair)),
            "Remove this row.", "button"))
        g["rows"].append(pair)
        g["widgets"].append(ws)
        self._fit_fus_regrid(which)
        return pair

    def _fit_fus_remove_row(self, which, pair):
        """Drop one row; the last one is cleared instead, so the table always
        has a row to type into."""
        g = self._fit_fus[which]
        if pair not in g["rows"]:
            return
        if len(g["rows"]) == 1:
            for var in pair:
                var.set("")
            return
        i = g["rows"].index(pair)
        for w in g["widgets"].pop(i):
            w.destroy()
        g["rows"].pop(i)
        self._fit_fus_regrid(which)

    def _fit_fus_regrid(self, which):
        for i, row in enumerate(self._fit_fus[which]["widgets"]):
            for c, w in enumerate(row):
                w.grid(row=1 + i, column=c, padx=(0, gui_fonts.SP["xs"]),
                       pady=1, sticky="w")

    def _fit_fus_readings(self, which):
        """Every filled row of one table as (layers, height). Blank rows skip."""
        out = []
        for n_var, h_var in self._fit_fus[which]["rows"]:
            n, h = n_var.get().strip(), h_var.get().strip()
            if bool(n) != bool(h):
                raise ValueError(
                    "a %s row has only its %s. Fill in both, or clear the row"
                    % (which, "layer count" if n else "height"))
            if n and h:
                layers = self._fit_num(n)
                if not layers.is_integer():      # int() would quietly floor 2.7
                    raise ValueError("layers must be a whole number, got %s" % n)
                out.append((int(layers), self._fit_num(h)))
        return out

    def _fit_fus_check(self, which, rows, d):
        """A stack can be no taller than N fibers and no shorter than one.

        The fit clamps phi to [0, 1), so a height typed in um instead of mm
        came out as phi = 0, read "Junction phi = 0", and Apply wrote it."""
        dia = gui_units.show("fiber_d", "%g" % d, "mm")
        for n, h in rows:
            if h > 1.05 * n * d:
                raise ValueError(
                    "a %s of %d layers reads %.4g mm, taller than %d unfused "
                    "fibers of %s (%.4g mm). Heights are in mm and the fiber "
                    "Ø is the one you measured: check both units"
                    % (which, n, h, n, dia, n * d))
            if h < 0.9 * d:
                raise ValueError(
                    "a %s of %d layers reads %.4g mm, less than one fiber of "
                    "%s. Heights are in mm: check the unit you measured in"
                    % (which, n, h, dia))

    def _compute_fit_fusion(self):
        self._fit_fus_result = None
        try:
            if not self._fit_fus_diam.get().strip():
                raise ValueError("type the fiber diameter you measured on "
                                 "this print into “Measured fiber Ø”")
            d = self._fit_float(self._fit_fus_diam)
            self._fit_check_diam(d, self._cell_shown(self._fit_fus_diam))
            got = {}
            for which in ("junction", "wall"):
                rows = self._fit_fus_readings(which)
                if rows:
                    self._fit_fus_check(which, rows, d)
                    got[which] = mew_calibration.fit_fusion_rows(rows, d)
            if not got:
                raise ValueError("enter at least one junction or wall reading")
        except (ValueError, ZeroDivisionError) as exc:
            self._fit_fus_out.configure(
                text=self._fit_say(exc, "Check the fiber Ø and the readings."),
                foreground="#a33")
            return
        self._fit_fus_result = got
        lines = []
        for which, name in (("junction", "Junction sink"),
                            ("wall", "Straight-span sink")):
            fit = got.get(which)
            if fit is None:
                lines.append("%s φ: no readings, left as it is" % name)
                continue
            text = "%s φ = %.4g" % (name, fit.phi)
            if fit.n_rows > 1:
                text += "  (%d readings, each alone %.3g–%.3g)" % (
                    fit.n_rows, min(fit.per_row), max(fit.per_row))
            lines.append(text)
        self._fit_fus_out.configure(text="\n".join(lines),
                                    foreground="#1f6b2e")

    def _apply_fit_fusion_junction(self):
        self._apply_fit_fusion("junction")

    def _apply_fit_fusion_wall(self):
        self._apply_fit_fusion("wall")

    def _apply_fit_fusion(self, which):
        """Write ONE of the two fitted phis. The wall phi goes to the
        straight-span field WITH its tickbox, since an unticked field reads as
        blank and the value would never reach the model."""
        self._compute_fit_fusion()            # what is on screen NOW
        got = self._fit_fus_result
        if got is None:
            return
        fit = got.get(which)
        if fit is None:
            self._fit_fus_out.configure(
                text=self._fit_say("no %s readings to apply. Type them in "
                                   "the %s table and press Compute"
                                   % (which, which)), foreground="#a33")
            return
        if which == "junction":
            self._cal_apply("cal.fusion_overlap", fit.phi,
                            name="Junction sink")
        else:
            fs = next(f for f in mew_calibration.CALIBRATION_FIELDS
                      if f.attr == "fusion_wall_overlap")
            self.cal_vars[fs.gate_key].set(True)
            self._cal_apply(fs.key, fit.phi, name="Straight-span sink")
            self._sync_cal_gates()

    # ===================================================================== #
    # The Calibration PAGE: one card per calibrated quantity
    # ===================================================================== #
    #: card key -> the method that builds that card's fit inputs + Compute /
    #: result / Apply strip. The ORDER of the cards is the registry's
    #: (calibration_procedures.PROCEDURES is insertion-ordered), never this
    #: dict's -- so adding a procedure adds a card without touching the page.
    _CARD_FITS = {
        "cts": "_build_fit_cts",
        "cts4": "_build_fit_cts4",
        "viscosity": "_build_fit_viscosity",
        "activation_energy": "_build_fit_ea",
        "lag3": "_build_fit_lag_circles",
        "lag4": "_build_fit_mandrel_lag",
        "sag": "_build_fit_sag",
        "fusion": "_build_fit_fusion",
    }

    #: True where the ribbon carries Apply (`gui_gcode_calibrate`): the fit
    #: strips then keep only Compute on screen
    _cal_apply_on_ribbon = False

    def _cal_procedures(self):
        """B's procedure registry, or {} when it is not importable.

        The page NEVER hardcodes procedure prose -- title, what the card
        calibrates, the operator steps and what to measure all come from
        `calibration_procedures`. An import failure degrades to a page with no
        cards and a message, not to a second copy of the text living here."""
        try:
            import calibration_procedures
        except Exception:  # noqa: BLE001
            return {}
        return dict(getattr(calibration_procedures, "PROCEDURES", {}) or {})

    def _cal_build_cards(self, body, boxes=None):
        """The per-quantity forms, built once. `boxes` is the CAD window's
        `gui_gcode_calibrate.CardBox`, which takes over the module grid and
        whose ribbon takes over the tab strip; the classic developer UI passes
        none and hosts them in a grid under a rail in `body`."""
        # `_page_disclosures` is the MODERN shell's re-sync list (gui_pages
        # `_sync_disclosures`). The classic developer UI hosts the same cards and
        # has no such list, so seed it here rather than assuming a shell built it
        # -- the classic App otherwise dies on the first card's Procedure row.
        self._page_disclosures = getattr(self, "_page_disclosures", [])
        body.columnconfigure(0, weight=1)
        GAP = gui_fonts.GAP_SECTION
        r = 0
        if boxes is None:
            ttk.Label(body, text="Calibration", style="Title.TLabel").grid(
                row=r, column=0, sticky="w")
            r += 1
            # No ⓘ: what it said is on the page already. The line under the
            # tabs names what Generate builds, and the Fit results card says
            # where Apply writes.
            gui_form.page_intro(
                body, "Each type measures one number the physics model needs.",
                row=r, wrap=self._INSTR_WRAP)
            r += 1

        procs = self._cal_procedures()
        if not procs:
            gui_form.mark(ttk.Label(
                body, style="Hint.TLabel", justify="left", wraplength=430,
                foreground="#c0392b",
                text="The calibration procedure registry "
                     "(calibration_procedures.py) could not be imported, "
                     "so there is nothing to walk you through. The "
                     "calibration fields themselves are on the Physics "
                     "page."), "live").grid(row=r, column=0, sticky="w")
            return

        # -- the tab strip ---------------------------------------------------- #
        # Seven quantities, one visible at a time; seven stacked cards were one
        # unreadable page. `PillRail` is the shell's existing segmented-selector family
        # -- the scaffold rail is the same widget -- taught to carry words, because a
        # ttk.Notebook clips its tab strip rather than wrapping it and seven labelled
        # tabs do not fit across this form pane.
        #
        # `sticky="ew"`, not "w", and that is load-bearing: the rail sizes its own rows
        # from the width it is GIVEN, so it has to be given the content column rather
        # than shrink-wrap its pills. Stretching the frame does not stretch the pills.
        self._cal_proc_order = list(procs)
        self._cal_tab_var = self._cal_tab_store()
        if boxes is None:
            self._cal_rail = gui_widgets.PillRail(
                body, command=self._cal_rail_picked,
                labels=[procs[k].tab for k in self._cal_proc_order])
            self._cal_rail.grid(row=r, column=0, sticky="ew",
                                pady=(0, gui_fonts.SP["s"]))
            r += 1
        # What Generate will build, in words, on every tab. A tab with no print
        # protocol does NOT retarget the bar -- it leaves it where the last
        # protocol tab put it -- so it has to SAY so, or Generate silently
        # builds a print the operator is not looking at.
        # A live line (it follows the tab and the bar's target), not help text.
        if boxes is None:
            self._cal_target_lbl = gui_form.mark(
                ttk.Label(body, style="Hint.TLabel", justify="left",
                          wraplength=self._INSTR_WRAP), "live")
            self._cal_target_lbl.grid(row=r, column=0, sticky="w",
                                      pady=(0, gui_fonts.SP["s"]))
            r += 1

        # -- the module grid ------------------------------------------------- #
        # The Scaffold designer's layout primitive: tiles, and one module's fields
        # over the whole form pane when a tile is clicked. Registration order is
        # reading order. Three cards are PER TAB -- Calibration settings,
        # Instructions and Fit results -- and hold one frame per tab, only the
        # selected one gridded, so switching tabs is a show/hide and never a
        # rebuild (the fit inputs an operator has typed survive a tab switch). The
        # other three are the shared print design, one widget set on every tab.
        # The classic developer UI carries no print engine and gets the three
        # per-tab cards alone.
        grid = boxes
        if grid is None:
            grid = gui_widgets.ModuleGrid(
                body, columns=2, on_open=self._cal_on_card_open)
            grid.grid(row=r, column=0, sticky="nsew",
                      pady=(0, gui_fonts.SP["s"]))
            body.rowconfigure(r, weight=1)
            r += 1
        self._cal_grid = grid
        self._page_disclosures.append(grid)     # sync() re-flows after a load
        self._cal_summ = {k: tk.StringVar(value="")
                          for k in ("settings", "instructions", "fit")}
        self._cal_cards = {}              # tab key -> {"settings", "instructions",
        #                                   "fit"}: that tab's frame in each card
        self._cal_section_dots = {}       # tab key -> {section id: the ⓘ dot}
        self._cal_page_dots = {}          # page-level section id -> (dot, ...)
        has_print = hasattr(self, "_sw_add_print_cards")

        # Instructions FIRST, top left: the procedure is what an operator
        # reads before touching anything else on the page.
        instr = grid.add("instructions", "Instructions",
                         summary=self._cal_summ["instructions"], info=(
            "The procedure for this quantity, step by step. The controls it "
            "names are on the other cards."))
        settings = grid.add("settings", "Parameters",
                            summary=self._cal_summ["settings"], info=(
            "What this calibration's print is, or what the fit needs to know "
            "about a print you already made. Each section's dot names its "
            "Instructions step. Readings go in Measurements."))
        settings.body.columnconfigure(0, weight=1)
        per_tab = {"settings": ttk.Frame(settings.body)}
        per_tab["settings"].grid(row=0, column=0, sticky="ew")
        per_tab["settings"].columnconfigure(0, weight=1)
        if has_print:
            plate_host = ttk.Frame(settings.body)
            plate_host.grid(row=1, column=0, sticky="ew")
            self._cal_plate_host = plate_host   # Plate & feed limit, hideable
            self._sw_add_print_cards(grid, plate_host)
        fit = grid.add("fit", "Measurements", summary=self._cal_summ["fit"],
                       info=(
            "What you carry back from the bench: type your measurements, press "
            "Compute, then Apply to write the value to the Physics page."))
        per_tab["instructions"] = ttk.Frame(instr.body)
        per_tab["fit"] = ttk.Frame(fit.body)
        for part, card in (("instructions", instr), ("fit", fit)):
            card.body.columnconfigure(0, weight=1)
            per_tab[part].grid(row=0, column=0, sticky="ew")
            per_tab[part].columnconfigure(0, weight=1)
        if has_print:
            self._sw_add_presets_card(grid)

        for key, proc in procs.items():
            parts = self._cal_build_card(per_tab, key, proc)
            if parts is None:
                continue
            for frame in parts.values():
                frame.grid(row=0, column=0, sticky="new")
                frame.grid_remove()
            self._cal_cards[key] = parts

        if hasattr(self, "_sw_build_warnings"):
            r = self._sw_build_warnings(body, r)
        if hasattr(self, "_sw_build_preview_options") and \
                getattr(self, "_sw_panel", None) is not None:
            self._sw_build_preview_options(self._sw_panel)
        # every shared ⓘ, registered under every tab that shows it, so the
        # settings_notes <-> ⓘ audit sees it from both sides. A section the tab
        # does not show (`Procedure.shared_cards`) is not registered under it.
        for pkey in self._cal_cards:
            if pkey in self._CAL_FIT_ONLY:          # its print tiles are hidden
                continue
            shows = self._cal_shared_cards(pkey)
            for src in (getattr(self, "_sw_shared_dots", {}),
                        self._cal_page_dots):
                for sid, (dot, _t, _i) in src.items():
                    if shows is None or sid in shows:
                        self._cal_register_dot(pkey, sid, dot)
        self._cal_show_tab(self._cal_tab_var.get(), from_var=True)
        self._cal_tab_var.trace_add(
            "write", lambda *_: self._cal_show_tab(self._cal_tab_var.get(),
                                                   from_var=True))
        summ = getattr(self, "_sw_summary_var", None)
        if summ is not None:
            summ.trace_add("write", lambda *_: self._cal_refresh_summaries())
        # NO rig-profile preset box here. This page has exactly one preset box, the
        # print-design one `_sw_add_presets_card` mounts, labelled "Calibration
        # presets" -- the calibration PRINT is what this page designs. The rig +
        # material profile is saved where its fields live, on the Physics page. A second
        # box on the same kind orphans the first box's table, because `_init_pstore`
        # overwrites `self._pstores[kind]`.

    # -- the tab strip ------------------------------------------------------ #
    def _cal_tab_store(self):
        """The Tk var holding the selected tab.

        In the modern shell it is `sw_vars["cal_tab"]`, in the sweep key group, so a
        Print History reproduce reopens the page on the tab that print came from.
        No PRESET carries it: presets are per tab, so the tab is where a preset
        lives, not something it sets (`gui_sweeps._sweep_tab_keys`). The
        classic developer UI carries no `_SweepsMixin` and no sw.* keys at all, so it
        gets a plain local var and the same behaviour within a session."""
        vars_ = getattr(self, "sw_vars", None)
        if vars_ is not None and "cal_tab" in vars_:
            var = vars_["cal_tab"]
            if var.get() not in self._cal_proc_order:
                var.set(self._cal_proc_order[0])
            return var
        return tk.StringVar(value=self._cal_proc_order[0])

    def _cal_rail_picked(self, i):
        """The rail clicked: write the var, which drives everything else."""
        keys = getattr(self, "_cal_proc_order", ())
        if 0 <= i < len(keys) and self._cal_tab_var.get() != keys[i]:
            self._cal_tab_var.set(keys[i])

    def _cal_show_tab(self, key, from_var=False):
        """Show ONE card, sync the rail, and re-target the bottom bar.

        Selecting a tab that owns a print protocol points `sw_vars["kind"]`, and
        therefore the shared bottom bar, at that print. Selecting one that does NOT own
        a print leaves the bar where it was, and `_cal_target_text` then says on the
        card which print Generate would still build -- the alternative to silently
        building the wrong protocol.

        **NO paint fence**, for the same reason `gui_pages._show_scaffold_frame`
        has none: a fence on a container this big presents its redirection
        surface before the background is drawn into it, and the swap flashes
        black. It was measured doing exactly that on the scaffold module grid
        (53% darker for a frame, 5 reps of 5). The same flash was
        reported on this strip; the probe did NOT reproduce it here (a 3% dip on
        the page average, where the card is a small share of the area), so the
        fence came off this site on the mechanism rather than on its own
        measurement.

        Left as a show/hide rather than the stack `gui_pagestack` uses for
        pages: stacking the seven tabs' frames would make each card permanently
        as tall as the tallest of them."""
        keys = getattr(self, "_cal_proc_order", ())
        if key not in self._cal_cards:
            key = keys[0] if keys else None
        if key is None:
            return
        for k, parts in self._cal_cards.items():
            for frame in parts.values():
                if k == key:
                    frame.grid()
                else:
                    frame.grid_remove()
        self._cal_place_shared(key)
        rail = getattr(self, "_cal_rail", None)
        if rail is not None and keys and rail.index() != keys.index(key):
            rail.select(keys.index(key))          # a preset load moved the var
        if not from_var and self._cal_tab_var.get() != key:
            self._cal_tab_var.set(key)
        proc = self._cal_procedures().get(key)
        sw = getattr(self, "sw_vars", None)
        if proc is not None and proc.print_protocol and sw is not None:
            sw["kind"].set(proc.print_protocol)
        lbl = getattr(self, "_cal_target_lbl", None)
        if lbl is not None:
            lbl.configure(text=self._cal_target_text(key))
            if key in self._CAL_FIT_ONLY:
                lbl.grid_remove()
            else:
                lbl.grid()
        grid = getattr(self, "_cal_grid", None)
        for tile in self._CAL_PRINT_TILES:
            card = grid.card(tile) if grid is not None else None
            if card is not None:
                if key in self._CAL_FIT_ONLY:
                    card.grid_remove()
                else:
                    card.grid()
        self._cal_show_shared(key)
        show_presets = getattr(self, "_sw_show_presets_tab", None)
        if callable(show_presets):
            show_presets(key)
        self._cal_refresh_summaries()
        shown = getattr(self, "_cal_type_shown", None)
        if callable(shown):
            shown(key)
        sync = getattr(self, "_sync_disclosures", None)
        if callable(sync):
            try:
                sync()
            except Exception:  # noqa: BLE001 -- a re-sync must never raise
                pass

    #: tabs that are a fit and nothing else: they show only Instructions and Fit
    #: results, with no print preview and no "Generate still builds ..." line.
    #: Fusion is fitted on the operator's own scaffold, so no calibration print
    #: belongs beside it; another tab's print there only reads as this tab's.
    _CAL_FIT_ONLY = frozenset({"fusion"})
    #: the tiles a fit-only tab hides: its own (empty) settings and the shared
    #: print design -- stabilization, export with the target slide, presets.
    _CAL_PRINT_TILES = ("settings", "stabilization", "export", "quality",
                        "presets")

    def _cal_on_card_open(self, _key):
        """A card opened or closed (`_key` None): re-read the tile summaries,
        and show the preview's "Where to measure" strip only while the
        Instructions card is the open one."""
        self._cal_refresh_summaries()
        sync = getattr(self, "_sw_sync_tube_map", None)
        if callable(sync):
            sync()

    def _cal_shared_cards(self, key):
        """The shared print sections tab `key` shows (`Procedure.shared_cards`),
        or None for all of them when the registry does not say."""
        proc = self._cal_procedures().get(key)
        return getattr(proc, "shared_cards", None)

    def _cal_show_shared(self, key):
        """Hide the shared print sections tab `key` does not show -- a mandrel
        tab has no plate, no base pad and one print -- and restore them on a tab
        that does. The page owns this so no procedure step has to say "ignore
        this card". A fit-only tab has already hidden its print tiles whole."""
        shows = self._cal_shared_cards(key)
        if key in self._CAL_FIT_ONLY:
            return

        def on(sid):
            return shows is None or sid in shows

        host = getattr(self, "_cal_plate_host", None)
        if host is not None:
            (host.grid if on("plate_feed_limit") else host.grid_remove)()
        grid = getattr(self, "_cal_grid", None)
        card = grid.card("stabilization") if grid is not None else None
        if card is not None:
            (card.grid if on("stabilization_base") else card.grid_remove)()
        kind = getattr(self, "_sw_show_print_kind", None)
        if callable(kind):
            proto = getattr(self._cal_procedures().get(key), "print_protocol",
                            None)
            kind(proto in getattr(self, "_SW_ROTARY_KINDS", ()))

    def _cal_refresh_summaries(self, *_a):
        """The one-line tile readouts, for the tab showing now.

        Calibration settings names the print design in one line on a print tab,
        and otherwise says what the card holds. Refreshed on a tab switch, a card
        close, and every finished preview build (the `_sw_summary_var` trace),
        which follows every edit of the design."""
        summ = getattr(self, "_cal_summ", None)
        var = getattr(self, "_cal_tab_var", None)
        if summ is None or var is None:
            return
        key = var.get()
        proc = self._cal_procedures().get(key)
        if proc is None:
            return
        try:
            sw = getattr(self, "sw_vars", None)
            if proc.print_protocol and sw is not None:
                text = self._cal_print_summary(proc.print_protocol)
            elif key in self._CARD_CONDITIONS:
                text = "The conditions your print was made at"
            else:
                text = "No print of its own. Make yours as the " \
                       "Instructions describe"
            summ["settings"].set(text)
            summ["instructions"].set("%d steps" % len(proc.steps))
            # not "Compute": the CTS types' button says Check (reported: the
            # subtitle named a button the panel did not have)
            summ["fit"].set("Type what you measured, then Apply")
        except (tk.TclError, KeyError):
            pass
        refresh = getattr(self, "_sw_refresh_card_summaries", None)
        if refresh is not None:
            refresh()

    def _cal_print_summary(self, protocol):
        """One short line for a print design, read off the sw.* fields.

        Not the engine's `SweepResult.summary`: that is a paragraph -- "CTS sweep
        600 -> 240 mm/min, step 60 (1.000 mm/s) x 3 reps: 7 speeds, 21 lines @
        43.3 mm, ..." -- which ran to three lines on a tile and made the grid's
        rows uneven."""
        v = self.sw_vars

        def g(k):
            return str(v[k].get()).strip()

        if protocol in ("cts", "cts4"):
            p = protocol + "_"
            sp = lambda k: gui_units.num("speed", g(k), "mm/min")  # noqa: E731
            text = "%s → %s %s, step %s · %s lines per speed" % (
                sp(p + "start"), sp(p + "stop"),
                gui_units.shown("speed", "mm/min"), sp(p + "step"),
                g(p + "reps"))
            if protocol == "cts4":
                text += " · on the mandrel"
            elif v["cts_loops_on"].get():
                text += " · linking loops"
        elif protocol == "diam":
            temps = g("diam_temps").split()
            span = ("%s → %s °C" % (temps[0], temps[-1]) if len(temps) > 1
                    else "%s °C" % (temps[0] if temps else "?"))
            text = "%s at %s · %s lines each" % (
                span, gui_units.show("speed", g("diam_speed"), "mm/min"),
                g("diam_reps"))
        elif protocol == "visc":
            text = "%s °C at %s · %s lines" % (
                g("visc_temp"),
                gui_units.show("speed", g("visc_speed"), "mm/min"),
                g("visc_reps"))
        elif protocol == "sag":
            spans = g("sag_spans").split()
            span_txt = ("%s → %s mm" % (spans[0], spans[-1]) if len(spans) > 1
                        else "%s mm" % (spans[0] if spans else "?"))
            text = "%s · %s layer walls · %s%s" % (
                span_txt, g("sag_layers"),
                "2 fibers" if g("sag_lane") != "edge" else "1 fiber",
                " · each span twice" if v["sag_mirror"].get() else "")
        elif protocol == "tube":
            text = "%s° at %s" % (
                " / ".join(g("tube_angles").split()) or "?",
                gui_units.show("speed", g("tube_speed"), "mm/min"))
        elif protocol == "circle":
            if g("circ_specimens"):
                text = "Specimen table · %s layout" % g("circ_layout")
            else:
                text = "ε %s · %s reps · %s layout" % (
                    g("circ_eps"), g("circ_reps"), g("circ_layout"))
        else:
            text = self._PROTOCOL_NAMES.get(protocol, protocol)
        res = getattr(self, "_sw_result", None)
        # the result must be THIS design's, not the last tab's still standing
        # while the rebuild is debounced (the viscosity plate runs "diam")
        engine_kind = {"visc": "diam"}.get(protocol, protocol)
        # a mandrel print is one print, never a slide
        if res is not None and v["kind"].get() == protocol \
                and res.kind == engine_kind:
            if protocol == "tube":
                # a tube is one print on a rod: its length is the news
                bb = res.prints[0].bbox
                text += " · %.0f mm long" % (bb[2] - bb[0])
            elif engine_kind not in ("tube", "cts4"):
                text += " · %d slide%s" % (res.n_slides,
                                           "" if res.n_slides == 1 else "s")
        return text

    #: protocol token -> what to call that print in a sentence.
    _PROTOCOL_NAMES = {"diam": "the melt temperature ladder",
                       "visc": "the viscosity plate",
                       "cts": "the CTS speed ladder",
                       "cts4": "the CTS speed ladder on the mandrel",
                       "circle": "the lag circles",
                       "sag": "the sag ladder",
                       "tube": "the mandrel lag tube"}

    def _cal_target_text(self, key):
        """One line saying exactly what the bottom bar's Generate will build."""
        procs = self._cal_procedures()
        proc = procs.get(key)
        sw = getattr(self, "sw_vars", None)
        if proc is None or sw is None:
            return ""
        if proc.print_protocol:
            return ("Generate and Save g-code as… build this type's print: %s."
                    % self._PROTOCOL_NAMES.get(proc.print_protocol,
                                               proc.print_protocol))
        kind = sw["kind"].get()
        owner = next((p.tab for p in procs.values()
                      if p.print_protocol == kind), None)
        name = self._PROTOCOL_NAMES.get(kind, kind)
        return ("This type has no print of its own. Make yours as the "
                "instructions describe. Generate still builds %s%s."
                % (name, (" from the ‘%s’ type" % owner) if owner else ""))

    #: card key -> the method that builds that card's PROCESS SETTINGS when the quantity
    #: owns no print protocol: the conditions the fit has to be told about the print you
    #: already made. The numbers you MEASURED off that print are deliberately not here --
    #: they belong under "Enter your measurements", because that is the step the operator
    #: is on when they type them.
    #: Only the mandrel tab has one. Activation energy had one until the temperature
    #: ladder landed -- it said "nothing to set", which stopped being true the moment
    #: the tab owned a print. The registry gives the other three fit-only tabs a
    #: single section id for their whole fit form, so splitting those forms would need an
    #: id nobody wrote -- and an id invented here is exactly the hand-typed prose this
    #: page does not carry. The mandrel tube's geometry and speeds are their own registry
    #: section and are genuinely settings, so they move.
    _CARD_CONDITIONS = {}

    #: card key -> the `calibration_procedures` section id whose ⓘ note the
    #: "Print conditions" sub-section carries. None where the registry names no
    #: settings section for that tab.
    _CARD_CONDITION_SIDS = {}

    #: card key -> the registry section id the "Enter your measurements" HEADER carries.
    #: The ids are the registry's, never invented here, and
    #: `test_gui_calibration.settings_notes_match_the_info_dots` checks that every id in
    #: a tab's `settings_notes` is rendered by exactly one dot. lag3's other measurement
    #: id rides the gap field's own dot; the three tabs missing here have their whole
    #: fit form named by `fit_inputs` up in Process settings.
    _CARD_MEASURE_SID = {
        "cts": "measured_cts",
        "cts4": "measured_cts",
        "viscosity": "fit_inputs",
        "activation_energy": "temperature_rows",
        "lag3": "measured_rows",
        "lag4": "gap_rows",
        "sag": "fit_inputs",
        "fusion": "fit_inputs",
    }

    #: the sections that live BELOW the card, on the page, and are therefore on screen
    #: on every tab: id -> (title, the text under the step line). One widget each, with
    #: the step line retargeted per tab like the shared print sections.
    #: The id stays `experiment_presets` while the LABEL is "Calibration presets": the
    #: id names the preset KIND (the print design, engine kind "sweep") and the label
    #: is what the operator reads. The rig profile's box is the Physics page's.
    _CAL_PAGE_SECTIONS = (
        ("experiment_presets", "Calibration presets",
         "Save this type's calibration print as a named preset and reload it "
         "any time. Each type keeps its own presets."),
        ("target_slide", "Slides",
         "Every slide is on screen at once, each its own row in the tree. "
         "Save g-code as… writes one file per slide."),
    )

    #: the readable line length the Instructions prose is wrapped to. ~90
    #: characters at the modern shell's hint font; the form pane is ~735 px, so
    #: this leaves room for the disclosure indent and never clips.
    _INSTR_WRAP = 600

    # -- Process-settings sub-sections and their ⓘ dots ---------------------- #
    def _cal_settings_notes(self, key):
        """`calibration_procedures`' section_id -> (step number, one-line
        summary) map for one tab, or {} when the registry has none yet."""
        proc = self._cal_procedures().get(key)
        return dict(getattr(proc, "settings_notes", None) or {})

    def _cal_note_text(self, key, sid, info=""):
        """What one sub-section's ⓘ says.

        The FIRST line is always "Step N of the Instructions: <summary>", and
        it is never typed here -- it comes from the registry, so the number an
        info dot quotes and the number the Instructions render are the same
        number by construction. Whatever the section already explained about
        itself follows underneath."""
        note = self._cal_settings_notes(key).get(sid)
        head = ""
        if note:
            try:
                n, summary = note
                head = "Step %d of the Instructions: %s" % (int(n), summary)
            except (TypeError, ValueError):     # a malformed entry, shown raw
                head = str(note)
        if head and info:
            return head + "\n\n" + info
        return head or info or (
            "Open the Instructions below for the step this setting is part of.")

    def _cal_register_dot(self, key, sid, dot):
        self._cal_section_dots.setdefault(key, {})[sid] = dot
        return dot

    def _cal_form_section(self, F, key, sid, title, info="", first=None):
        """A sub-section heading on the Form `F`, with the ⓘ that carries its
        Instructions step line. The dot is the section's note, not a parameter's
        help, which is why it stays a dot. Returns the Form row."""
        row = F.section(title, first=first,
                        info=self._cal_note_text(key, sid, info), title=title)
        self._cal_register_dot(key, sid, row.dot)
        return row

    class _TipNote:
        """A ROW's hover tooltip standing in for an ⓘ dot.

        Help on one parameter is a tooltip, never a dot -- but a few parameters'
        help opens with an Instructions step line, which the registry audit
        (`test_gui_calibration.settings_notes_match_the_info_dots`) reads through
        `info_text()` and the page retargets per tab through `set_info_text()`.
        This carries that same two-method contract onto the row's tooltips."""

        def __init__(self, row, text):
            self._row = row
            self._text = ""
            if not row.tips:
                for w in (row.label, row.field):
                    if w is not None:
                        row._tip(w, " ")
            self.set_info_text(text)

        def set_info_text(self, text, title=None):
            self._text = text
            self._row.set_tip(text)

        def info_text(self):
            return self._text

    def _cal_tip_row(self, row, text):
        """The `_TipNote` for `row`, with `text` as its hover help."""
        return self._TipNote(row, text)

    def _cal_tip_note(self, key, sid, row, info=""):
        """Put one registry section's step line on a ROW's tooltip, and register
        it where the audit and the per-tab retarget look for the section's dot."""
        note = self._cal_tip_row(row, self._cal_note_text(key, sid, info))
        self._cal_register_dot(key, sid, note)
        return note

    def _cal_section_disc(self, parent, key, sid, title, info="", open=False):
        """A Process-settings sub-section that is long enough to collapse. Same
        header contract as `_cal_form_section` -- a title and one ⓘ carrying the
        step line -- drawn as a Disclosure so an open Process settings is a page
        and not a scroll."""
        d = gui_widgets.Disclosure(parent, title, open=open,
                                   info=self._cal_note_text(key, sid, info))
        self._page_disclosures = getattr(self, "_page_disclosures", [])
        self._page_disclosures.append(d)
        if d.info_dot is not None:
            self._cal_register_dot(key, sid, d.info_dot)
        return d

    def _cal_page_dot(self, sid, dot, title, info):
        """Record a ⓘ that belongs to a section BELOW the card, on the page,
        and is therefore on screen on every tab -- the two preset stores and the
        slide pager. One widget, its step line retargeted per tab exactly like
        the shared print sections."""
        self._cal_page_dots = getattr(self, "_cal_page_dots", {})
        if dot is not None:
            self._cal_page_dots[sid] = (dot, title, info)
        return dot

    def _cal_place_shared(self, key):
        """Retarget the shared sections' step lines to the tab showing now.

        One set of widgets serves every tab -- plate & feed limit, the
        stabilization scaffold, the preview options, export, the target slide and
        the presets -- so the step number their ⓘ quotes cannot be baked in at
        build time: "Step 3" on the CTS tab is a different step on the lag tab.
        `set_info_text` is what makes that a text update instead of a second copy
        of the widgets."""
        for src in (getattr(self, "_sw_shared_dots", {}),
                    getattr(self, "_cal_page_dots", {})):
            for sid, (dot, title, info) in src.items():
                dot.set_info_text(self._cal_note_text(key, sid, info), title)

    def _cal_build_card(self, hosts, key, proc):
        """ONE calibrated quantity, as three frames -- one per per-tab card:

          * "Calibration settings" is every software parameter for this quantity --
            the print knobs for a tab that owns a print, the fit's print conditions
            for one that does not. Nothing is measured here.
          * "Instructions" is words only: what the tab writes, then the numbered
            steps. No entry, no button: the moment a control lives in it the
            operator has to read and act in the same place.
          * "Fit results" is what you carry back from the bench -- "Enter your
            measurements", plus Compute and Apply.

        `hosts` maps each of those part names to the frame the tab's frame is
        built into. Returns {part: frame}, or None for a procedure with no fit
        wired yet. Each settings sub-section's dot names the Instructions step it
        belongs to, so the cards point at each other."""
        builder = getattr(self, self._CARD_FITS.get(key, ""), None)
        if builder is None:
            return None
        parts = {part: ttk.Frame(host) for part, host in hosts.items()}
        for f in parts.values():
            f.columnconfigure(0, weight=1)

        # -- 1. the software parameters -------------------------------------- #
        self._cal_fill_process(parts["settings"], key, proc)

        # -- 2. what it writes, and the operator instructions ----------------- #
        # WORDS ONLY. The numbering runs 1..N across the whole tab, not per
        # group: it is one procedure told in parts, and an operator citing
        # "step 11" means the eleventh.
        _fr, IF = gui_form.framed(parts["instructions"], 0)
        IF.section(proc.title)
        IF.intro(proc.calibrates, wrap=self._INSTR_WRAP)
        steps = ttk.Frame(IF.frame)
        IF.slot(None, steps, sticky="ew")
        gui_form.exempt(steps, "the operator procedure: numbered prose the "
                               "registry writes, no parameter rows")
        self._cal_fill_instructions(steps, proc)

        # -- 3. what you carried back from the bench ------------------------- #
        # What to measure is the card's one grey intro; the rows below it are
        # the builder's own Form, flush on the same edges.
        fb, FF = gui_form.framed(parts["fit"], 0)
        msid = self._CARD_MEASURE_SID.get(key)
        head = FF.section(
            "Enter your measurements",
            info=(self._cal_note_text(key, msid, proc.measure) if msid
                  else proc.measure), title="Enter your measurements")
        if msid:
            self._cal_register_dot(key, msid, head.dot)
        FF.intro(proc.measure, wrap=self._INSTR_WRAP)
        fitf = ttk.Frame(fb)
        fitf.grid(row=FF.row, column=0, columnspan=4, sticky="w")
        self._cal_building = key
        builder(fitf)
        return parts

    def _cal_fill_process(self, body, key, proc):
        """The "Process settings" body for one tab: this tab's own settings
        first, then the four print-design sections every tab carries.

        The four shared ones are on every tab, not only the two that own a
        print, and that is deliberate: they are on screen either way, so the
        tab that does NOT use them has to say so rather than leave the operator
        guessing. Their ⓘ says exactly that, in the registry's words."""
        body.columnconfigure(0, weight=1)
        r = 0
        if proc.print_protocol and hasattr(self, "_sw_build_protocol"):
            # No radio here. Until the tab strip landed each card carried one,
            # because seven cards were on screen at once and something had to
            # say which print the shared bottom bar built. SELECTING THE TAB is
            # now that statement (`_cal_show_tab` writes sw_vars["kind"]).
            knobs = ttk.Frame(body)
            knobs.grid(row=r, column=0, sticky="ew")
            r += 1
            self._sw_build_protocol(knobs, proc.print_protocol, key)
        elif proc.print_protocol:
            _fr, PF = gui_form.framed(body, r)
            PF.intro("This card's print is designed in the standard MEWracle "
                     "window; this developer build has no print designer.",
                     wrap=self._INSTR_WRAP)
            r += 1
        # The fit's print CONDITIONS, below the print design where a tab has
        # both. The mandrel tab is the one that does: it designs the tube, and
        # its fit still has to be told what the tube was printed on and at.
        cond = getattr(self, self._CARD_CONDITIONS.get(key, ""), None)
        sid = self._CARD_CONDITION_SIDS.get(key)
        if cond is not None or not proc.print_protocol:
            if cond is not None and sid:
                _fr, PF = gui_form.framed(body, r)
                r += 1
                self._cal_form_section(
                    PF, key, sid, "Print conditions",
                    info="The conditions the print you are measuring was made "
                         "at. The fit reads them; it cannot infer them from "
                         "the measurement.")
                holder = ttk.Frame(body)
                holder.grid(row=r, column=0, sticky="w")
                r += 1
                cond(holder)
            elif cond is not None:
                holder = ttk.Frame(body)
                holder.grid(row=r, column=0, sticky="ew")
                r += 1
                cond(holder)
            else:
                # nothing of this tab's own to set; say so rather than leave a
                # blank space over the shared Plate & feed limit
                _fr, PF = gui_form.framed(body, r)
                PF.intro("This type designs no print, and its fit needs no "
                         "print conditions.", wrap=self._INSTR_WRAP)

    def _cal_fill_instructions(self, sb, proc):
        """The "Instructions" body: the registry's groups, as sub-headers over
        steps numbered 1..N across the whole card. Labels only -- a control
        here would put reading and doing in the same place, which is the shape
        this block exists to undo."""
        sb.columnconfigure(0, weight=1)
        sr = 0
        n = 0
        for heading, lines in proc.groups:
            ttk.Label(sb, text=heading, font=("TkDefaultFont", 9, "bold"),
                      justify="left", wraplength=self._INSTR_WRAP).grid(
                row=sr, column=0, sticky="w",
                pady=(gui_fonts.SP["m"] if sr else 0, gui_fonts.SP["xs"]))
            sr += 1
            for line in lines:
                n += 1
                lbl = ttk.Label(sb, style="Hint.TLabel", justify="left",
                                wraplength=self._INSTR_WRAP - gui_fonts.SP["m"])
                # display units (gui_units): a step that names a speed,
                # pressure or fiber-diameter unit reads in the fields' unit
                gui_units.track(lbl, line, fmt=lambda t, n=n: f"{n}.  {t}")
                lbl.grid(row=sr, column=0, sticky="w",
                         padx=(gui_fonts.SP["m"], 0),
                         pady=(0, gui_fonts.SP["xs"]))
                sr += 1

    # -- lag circles: the flat lag amplitude k (mew_circle_fit) -------------- #
    #: how many measurement rows the card OPENS with. The shipped ladder is four
    #: speed ratios x two reps, so eight rows is a whole default plate. It is a
    #: starting count and nothing else -- the list grows and shrinks (see
    #: `_fit_circ_add_row`), so a bigger plate does not have to come in
    #: through "Load CSV...".
    _CIRCLE_FIT_ROWS = 8

    def _build_fit_lag_circles(self, parent):
        """Typed rows OR the page's own measurement CSV, into mew_circle_fit.

        This fit existed only as a command line before, printing two numbers for the
        operator to retype. Both halves are wired here: the CSV the Export options wrote
        comes back through `rows_from_csv`, which carries the printed GAP so A cannot be
        attributed to the wrong one, and Apply writes A AND that gap --
        `cal.lag_amplitude_gap` was write-only in the app before."""
        F = gui_form.Form(parent)
        self._fit_circ_gap = tk.StringVar()
        gap_row = F.entry("Printed gap", self._fit_circ_gap, "", unit="mm")
        self._cal_tip_note("lag3", "printed_gap", gap_row, (
            "The gap the plate was PRINTED at. k is conditional on it, because the "
            "law carries the gap as H^1, so it is stored beside k. Blank reads "
            "the Physics page's " + ("nozzle–collector gap" if hasattr(
                self, "_gce_page_scope") else "nozzle–collector gap") + ". A loaded CSV overrides "
            "both, because the generator wrote the gap into every line."))

        # -- the measurement rows: a real editable list --------------------- #
        # One row per measured circle, with an add button and a per-row remove.
        # A FIXED set of rows with a hidden overflow list a longer CSV spilled
        # into would fit rows that could not be seen or corrected, which is the one
        # thing a measurement form must not do. There is no overflow now --
        # every row the fit reads is a row on the screen.
        self._fit_circ_tip = (
            "One row per measured circle: its specimen label, speed ratio ε, "
            "the commanded radius and the deposited radius you measured, in "
            "mm. Rows with a blank ε or radius are skipped.")
        self._fit_circ_tbl = self._fit_table(F, self._fit_circ_tip)
        for c, text in enumerate(("Specimen", "ε", "Commanded R (mm)",
                                  "Deposited R (mm)")):
            self._fit_cell(ttk.Label(self._fit_circ_tbl, text=text),
                           self._fit_circ_tip, "colhead").grid(
                row=0, column=c, padx=(0, gui_fonts.SP["xs"]), sticky="w")
        self._fit_circ_rows = []
        self._fit_circ_widgets = []
        for _ in range(self._CIRCLE_FIT_ROWS):
            self._fit_circ_add_row()

        load = gui_form.mark(ttk.Frame(F.frame), "group")
        for i, (text, cmd, tip) in enumerate((
                ("Add row", self._fit_circ_add_row,
                 "Add one measurement row."),
                ("Load CSV…", self._load_fit_circle_csv,
                 "Load the *_measure.csv this page exported, with measured_r_mm "
                 "filled in. It fills one row per measured circle, and the gap "
                 "it was printed at."))):
            b = gui_form.mark(ttk.Button(load, text=text, command=cmd), "button")
            b.pack(side="left", padx=((gui_fonts.SP["s"] if i else 0), 0))
            gui_tips.add_tip(b, tip)
        F.slot(None, load)

        self._fit_circ_out = self._fit_result_row(
            F, self._compute_fit_circles, self._apply_fit_circles)
        self._fit_circ_result = None

    def _fit_circ_add_row(self, values=("", "", "", "")):
        """Append one measurement row and return its (label, ε, R, r) vars."""
        quad = tuple(tk.StringVar(value=v) for v in values)
        tip = getattr(self, "_fit_circ_tip", "")
        ws = [self._fit_cell(ttk.Entry(self._fit_circ_tbl, textvariable=var,
                                       width=10 if c else 12), tip)
              for c, var in enumerate(quad)]
        rm = self._fit_cell(
            ttk.Button(self._fit_circ_tbl, text="−", width=2,
                       command=lambda q=quad: self._fit_circ_remove_row(q)),
            "Remove this row.", "button")
        ws.append(rm)
        self._fit_circ_rows.append(quad)
        self._fit_circ_widgets.append(ws)
        self._fit_circ_regrid()
        return quad

    def _fit_circ_remove_row(self, quad):
        """Drop one row. The last row is kept: an empty list would leave the
        card with nothing to type into and no way back."""
        if len(self._fit_circ_rows) <= 1 or quad not in self._fit_circ_rows:
            if len(self._fit_circ_rows) == 1:
                for var in self._fit_circ_rows[0]:
                    var.set("")               # clear it instead of removing it
            return
        i = self._fit_circ_rows.index(quad)
        for w in self._fit_circ_widgets.pop(i):
            w.destroy()
        self._fit_circ_rows.pop(i)
        self._fit_circ_regrid()

    def _fit_circ_regrid(self):
        for i, ws in enumerate(self._fit_circ_widgets):
            for c, w in enumerate(ws):
                w.grid(row=1 + i, column=c, padx=(0, gui_fonts.SP["xs"]),
                       pady=1, sticky="w")

    def _fit_circle_rows(self):
        """Every row ON SCREEN that is filled in. There is no second source."""
        rows = []
        for l_var, e_var, c_var, d_var in self._fit_circ_rows:
            if not (e_var.get().strip() and c_var.get().strip()
                    and d_var.get().strip()):
                continue
            rows.append(mew_circle_fit.CircleRow(
                eps=self._fit_num(e_var.get()),
                commanded_r_mm=self._fit_num(c_var.get()),
                deposited_r_mm=self._fit_num(d_var.get()),
                label=l_var.get().strip()))
        return rows

    def _load_fit_circle_csv(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            parent=self.root, title="Open the lag-circle measurement CSV",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        refusal = self._fit_circle_csv_refusal(path)
        if refusal is None:
            try:
                rows, gap = mew_circle_fit.rows_from_csv(path)
            except (ValueError, KeyError, OSError) as exc:
                refusal = self._fit_circle_csv_refusal(path, exc)
        if refusal is not None:
            self._fit_circ_out.configure(text=self._fit_say(refusal),
                                         foreground="#a33")
            return
        self._fill_fit_circle_rows(rows, gap)
        self._fit_circ_out.configure(
            text=f"Loaded {len(rows)} measured circle(s) at a {gap:g} mm gap. "
                 f"Press Compute.", foreground="#1f6b2e")

    #: the columns `mew_circle_fit.rows_from_csv` reads
    _CIRCLE_CSV_COLUMNS = ("eps", "commanded_r_mm", "measured_r_mm", "gap_mm")

    def _fit_circle_csv_refusal(self, path, exc=None):
        """Why `path` cannot load, in words, or None. Without `exc` it checks
        the header only; with it, it words the reader's own refusal.

        The reader's messages were written for its command line: every bad
        file, empty or not a CSV at all, read "<full path>: no row has a
        measured_r_mm value yet", which names a column and not the fix."""
        name = "“%s”" % os.path.basename(path)
        if exc is None:
            import csv
            try:
                with open(path, newline="", encoding="utf-8-sig") as fh:
                    head = {h.strip() for h in next(csv.reader(fh), [])}
            except (OSError, UnicodeDecodeError, csv.Error):
                head = set()
            if set(self._CIRCLE_CSV_COLUMNS) <= head:
                return None
            return ("%s is not the measurement CSV of a Jet lag (plate) "
                    "print. Tick “Also write the measurement-CSV template”, "
                    "press “Save g-code as…”, fill in the *_measure.csv it "
                    "writes and load that, or type the readings in the "
                    "table" % name)
        text = str(exc)
        if "no row has" in text:
            return ("%s has no measured radius yet. Type each circle's "
                    "radius in mm into its measured_r_mm column, save it, "
                    "and load it again" % name)
        if "different gaps" in text:
            return ("%s holds circles printed at different gaps. Each gap "
                    "needs its own fit, so load one gap's rows at a time"
                    % name)
        if isinstance(exc, OSError):
            return "%s could not be read: %s" % (name, exc.strerror or exc)
        bad = re.search(r"float: '(.*)'$", text)
        cell = " (‘%s’)" % bad.group(1) if bad else ""
        return ("%s has a cell that is not a number%s. Fix it, save it, "
                "and load it again" % (name, cell))

    def _fill_fit_circle_rows(self, rows, gap):
        """Put loaded rows into the typed form (headless-testable): the form
        stays the single input the fit reads, so a loaded CSV is visible and
        editable rather than a hidden second source."""
        self._fit_circ_gap.set("%g" % gap)
        # EXACTLY as many rows as the file has. A fixed count would spill a longer CSV
        # into rows that are fitted but never shown, and leave stale blanks for a
        # shorter one.
        # Counted, not `while len(...) != n`: either loop spins if the row it drives
        # ever stops changing the count, and proving this file's guards RED with
        # `_fit_circ_remove_row` stubbed hung the run until the timeout killed it.
        for _ in range(len(self._fit_circ_rows) - max(1, len(rows))):
            self._fit_circ_remove_row(self._fit_circ_rows[-1])
        for _ in range(len(rows) - len(self._fit_circ_rows)):
            self._fit_circ_add_row()
        for i, quad in enumerate(self._fit_circ_rows):
            if i < len(rows):
                r = rows[i]
                quad[0].set(r.label or "")
                quad[1].set("%g" % r.eps)
                quad[2].set("%g" % r.commanded_r_mm)
                quad[3].set("%g" % r.deposited_r_mm)
            else:
                for var in quad:
                    var.set("")

    def _compute_fit_circles(self):
        try:
            rows = self._fit_circle_rows()
            if not rows:
                raise ValueError(
                    "enter at least one (ε, commanded R, deposited R) row, "
                    "or load the measurement CSV")
            gap, src = self._fit_lag_fallback(
                self._fit_circ_gap, lambda: self.sim_standoff.get(),
                name=("Nozzle–collector gap" if hasattr(self, "_gce_page_scope")
                      else "Nozzle–collector gap"))
            _mat, _noz, cal = mew_calibration.models_from_profile(
                self._calibration_profile_snapshot())
            fit = mew_circle_fit.fit_amplitude(rows, gap=gap, calibration=cal)
        except Exception as exc:  # noqa: BLE001 -- to the UI
            self._fit_circ_result = None
            self._fit_circ_out.configure(text=self._fit_say(exc), foreground="#a33")
            return
        self._fit_circ_result = fit
        text = mew_circle_fit.summarise(fit)
        if src:
            text += f"\n - gap {gap:g} mm taken from elsewhere in the app ({src})"
        self._fit_circ_out.configure(text=text, foreground="#1f6b2e")

    def _apply_fit_circles(self):
        """A AND the gap it was fitted at, together.

        `cal.lag_amplitude_gap` is read by mew_physics (:548) but nothing in the
        app ever wrote it, so a fitted amplitude landed with its condition lost.
        They are one measurement and are applied as one."""
        self._compute_fit_circles()           # what is on screen NOW
        fit = self._fit_circ_result
        if fit is None:
            return
        self.cal_vars["cal.lag_amplitude"].set("%.6g" % fit.A)
        self.cal_vars["cal.lag_amplitude_gap"].set("%.6g" % fit.A_gap_mm)
        self._cal_mark_measured("cal.lag_amplitude", "cal.lag_amplitude_gap")
        self._after_calibration_preset_applied()
        self._cal_applied_status("jet lag amplitude A = %.4f, fitted at a %g mm "
                                 "gap (Jet lag card)" % (fit.A, fit.A_gap_mm))

    # -- CTS: no fit, the operator reads it off the print ------------------- #
    # Two tabs, one reading: the slowest speed whose lines are all straight
    # (paper section 2.4.2). The box is mm/MIN, the unit the ladder above it is
    # printed in, so the operator types the number they read off the print. The
    # Physics page holds CTS in mm/S (`cal.cts_ref`, `Calibration.mandrel_cts`),
    # and Apply converts -- the one unit crossing, named on both sides.
    def _build_fit_cts(self, parent):
        self._fit_cts_val, self._fit_cts_out = self._build_fit_cts_card(
            parent, "cts", "the slide")

    def _build_fit_cts4(self, parent):
        self._fit_cts4_val, self._fit_cts4_out = self._build_fit_cts_card(
            parent, "cts4", "the mandrel")

    def _build_fit_cts_card(self, parent, key, where):
        F = gui_form.Form(parent)
        val = tk.StringVar()
        # NOT the bare "CTS (mm/s):" -- that label belongs to the Physics
        # page's box, which is CTS's one home and which test_ui_shell guards as
        # rendering exactly once.
        F.entry("Measured CTS", val,
                "The slowest speed on %s whose lines are all straight, in "
                "mm/min, as printed. Check echoes it in mm/s, the unit the "
                "Physics page shows." % where, unit="mm/min")
        out = self._fit_result_row(
            F, lambda k=key: self._compute_fit_cts(k),
            lambda k=key: self._apply_fit_cts(k), compute_text="Check")
        return val, out

    def _compute_fit_cts(self, key="cts"):
        """There is no fit here -- CTS is read straight off the printed ladder.

        What this DOES do is the units check, which is why the button says "Check"
        rather than "Compute": it echoes the mm/min reading in the mm/s the
        Physics page will show ("450 mm/min = 7.5 mm/s"). Apply calls this itself
        when it has not run, so the button is a preview, never a required step.
        `_fit_results[key]` holds mm/S, the unit Apply writes."""
        box, out = ((self._fit_cts4_val, self._fit_cts4_out) if key == "cts4"
                    else (self._fit_cts_val, self._fit_cts_out))
        try:
            if not box.get().strip():
                raise ValueError("type the slowest speed whose lines are all "
                                 "straight, in mm/min")
            val = self._fit_float(box)
            if val <= 0:
                raise ValueError("a speed is above zero. Type the slowest "
                                 "speed whose lines are all straight")
            lo, hi = self._cts_ladder_range(key)
            if lo is not None and not lo * 0.999 <= val <= hi * 1.001:
                raise ValueError(
                    "%s is not a speed this ladder printed (%s to %s). "
                    "Type the speed written beside the lines, in mm/min. "
                    "If you printed a different ladder, set its speeds "
                    "under Parameters first"
                    % (gui_units.show("speed", "%g" % val, "mm/min"),
                       gui_units.show("speed", "%g" % lo, "mm/min"),
                       gui_units.show("speed", "%g" % hi, "mm/min")))
        except (ValueError, ZeroDivisionError) as exc:
            self._fit_results.pop(key, None)
            out.configure(text=self._fit_say(exc), foreground="#a33")
            return
        self._fit_results[key] = val / 60.0
        out.configure(text=f"CTS = {val:.4g} mm/min = {val / 60.0:.4g} mm/s",
                      foreground="#1f6b2e")

    def _cts_ladder_range(self, key):
        """(slowest, fastest) mm/min of the ladder this CTS type designs, or
        (None, None) when it does not read (mid-typing, classic UI)."""
        p = "cts4_" if key == "cts4" else "cts_"
        try:
            a = float(self.sw_vars[p + "start"].get())
            b = float(self.sw_vars[p + "stop"].get())
        except (AttributeError, KeyError, ValueError, tk.TclError):
            return None, None
        if not (a > 0 and b > 0):
            return None, None
        return min(a, b), max(a, b)

    def _apply_fit_cts(self, key="cts"):
        """CTS (3-axis) writes the flat CTS. CTS (4-axis) writes the MANDREL's
        (`m_cts`) and never the flat one: flat prints read `cal.cts_ref`, and a
        mandrel ladder must not move them."""
        self._compute_fit_cts(key)            # what is on screen NOW
        if key not in self._fit_results:
            return
        if key == "cts4":
            if not hasattr(self, "m_cts"):
                self.status.set("This build has no mandrel page to apply to.")
                return
            self.m_cts.set("%.6g" % self._fit_results[key])
            self._cal_mark_measured("m_cts")
            self._cal_applied_status("the mandrel CTS (Jet lag card). The flat "
                                     "CTS is unchanged")
            return
        self._cal_apply("cal.cts_ref", self._fit_results[key])
