#!/usr/bin/env python3
"""gui_sim.py -- sim mixin: the 'Printhead sim' tab, its four MEW process knobs,
and the hooks (sim tab / persist entries / player args / export prefix) that
override _CoreMixin's no-ops to wire the panel into the shared machinery.

Each physics law has its own generation/export switch on its Physics-page card:
Jet lag (`sim_physics_deposit` -- the touchdown centreline, with the corner
rounding, accel re-timing and coiling wave that ride it), Fiber sagging
(`sim_sag_law`), Fiber fusion (`sim_physical_fusion`) and Fiber diameter
(`sim_diam_law`, the inverse of "Override fiber diameter"). A law that is off is
absent from the generated files, the simulation and the embedded previews.

The diameter law predetermines each scaffold's fiber diameter from mass
conservation, d = sqrt(4Q/(pi v)) -- the same law that sizes the animated jet --
and writes it into that scaffold's existing `diameter` StringVar, so every
downstream consumer (g-code layer height, tube radius, .depo sidecar, player
groups) picks it up with no other change. Voltage and elevation do NOT enter the
diameter. Off (= "Override fiber diameter" on), the hand-typed field is used.
Part of the gcode_to_fibers_gui split; see gui_common.py."""
from gui_common import *

import math

import gui_form
import gui_tips
import gui_units

# The reduced-order jet model supplies the predetermined fiber diameter -- the same
# JetState.fiber_diameter the player draws the jet at, so the laid fiber matches the
# jet. Guarded so a missing physics module degrades to hand-editable diameter fields
# rather than breaking the GUI.
try:
    from mew_physics import MEWModel, Nozzle, ProcessParams
    _HAVE_PHYSICS = True
except Exception:  # noqa: BLE001
    _HAVE_PHYSICS = False

# the MEWron pressure regulator reads psi; the jet model wants Pa
PSI_TO_PA = 6894.76

# The export wrapper (installs the mew_deposition fiber transform,
# then drives the normal converter / scaffold main) is launched via
# worker_cmd("worker:export") -- frozen-safe; see _export_prefix.


class _SimMixin:
    def _build_sim_controls(self, parent, row, col):
        """Printhead-animator knobs forwarded to gcode_player on Play: the nozzle
        elevation (also the physics tip-to-collector gap) plus the three MEW process
        knobs that drive the jet model -- voltage, melt pressure, temperature. The
        jet's diameter and lag are computed from these; the fourth knob, speed, is
        read from the g-code feedrate. Shared across the whole print, so they live on
        their own tab. With "Physics-deposited scaffold" ticked they also predetermine
        the scaffold fiber diameter (see _apply_diam_mode).

        The classic (developer) UI wraps the controls in a titled box here; the modern
        shell mounts the SAME controls flat via _populate_sim_controls, so this wrapper
        keeps the classic boxed layout byte-identical."""
        frame = ttk.LabelFrame(parent, text="Printhead simulation", padding=8)
        frame.grid(row=row, column=col, sticky="ew", pady=(8, 0))
        self._populate_sim_controls(frame)

    def _populate_sim_controls(self, frame, hosts=None):
        """Create the printhead-sim vars + fields + traces -- the classic titled
        LabelFrame OR the modern Physics page's module-grid card bodies.

        ONE set of widget definitions, two homes -- the `_build_export_tab(...,
        quality_into=...)` idiom. `hosts` is None on the classic path, and every group
        then grids into `frame` at consecutive rows exactly as the single panel always
        did. The modern shell passes a dict of card bodies -- "process", "diameter",
        "fusion", "visual" -- and each group goes to its own card instead, so the
        Physics page can read as one module per physical law without a second copy of
        these fields drifting from this one.

        Split from _build_sim_controls so the modern shell can drop the box without
        duplicating this logic."""
        self.sim_standoff = tk.StringVar(value="4.5")
        self.sim_voltage = tk.StringVar(value="4.0")
        self.sim_pressure = tk.StringVar(value="3")
        self.sim_temperature = tk.StringVar(value="90" if hosts is None else "80")
        # made earlier by the Material card when that page is built first
        if getattr(self, "sim_polymer", None) is None:
            self.sim_polymer = tk.StringVar(value="")
        # The acceleration limits are machine properties, not jet knobs, so they
        # live on the Machine page, created by _build_machine_motion_limits.
        # The JET LAG law switch (the Jet lag card). On, the export runs the
        # mew_deposition transform, so the written STL and the .depo sidecar the
        # player reads carry the fiber as actually laid (the jet's trailing touchdown
        # path, corners rounded) rather than the commanded toolpath. Accel-limited
        # timing and the coiling wave are part of that transform and go with it.
        # The var keeps its historical name and persist key (sim.physics_deposit) so
        # saved presets still load. ON by default.
        self.sim_physics_deposit = tk.BooleanVar(value=True)
        # The diameter mode: on, the Fiber-diameter fields are editable and the typed
        # value is used. Always the inverse of sim_diam_law (the Fiber diameter
        # card's law switch); this one is the persisted half.
        self.sim_diam_override = tk.BooleanVar(value=False)
        # scaffold diameters we overwrote with the physics value, keyed by scaffold
        # index, so leaving physics-diameter mode restores what the user had typed.
        self._manual_diam = {}
        # Draw the molten Taylor cone at the nozzle in the player. Player-render only --
        # never touches the deposited scaffold or the export.
        self.sim_taylor_cone = tk.BooleanVar(value=True)
        # The reduced-order, rig-calibrated sag law instead of the geometric droop,
        # whenever the Fiber sagging switch (sim_sag_law) is on. It is a droop
        # RENDERER, not a validated mechanism (the PHYSICS_DEPENDENCY_GRAPH.md sag
        # dossier). Always on, so it is not persisted and has no checkbox; kept as a
        # var only so a headless test can force the geometric path. Rides
        # --physical-sag to mew_export AND the player.
        self.sim_physical_sag = tk.BooleanVar(value=True)
        # Below-CTS coiling-WAVE indicator (mew_coil_wave): below CTS the straight-fiber
        # model stops holding, so the deposition draws a tight lateral sine wave instead
        # of a straight fiber. Amplitude and wavelength are fixed USER knobs -- an
        # indicator, not the dormant physical --coil model. Rides --coil-wave-amp/-len to
        # the player, the physics export and the embedded preview, so all three agree;
        # unticked forwards amplitude 0, the straight below-CTS passthrough.
        self.sim_coil_wave = tk.BooleanVar(value=True)
        self.sim_coil_wave_amp = tk.StringVar(value="0.1")
        self.sim_coil_wave_len = tk.StringVar(value="0.3")
        # sim_physical_fusion and sim_mandrel_per_layer are created by the ensure-hook,
        # not here: on the modern page their widgets are built by a card on the
        # CALIBRATION side, which runs before this does.
        self._sim_ensure_switch_vars()

        h = hosts or {}
        # Classic: one frame, consecutive rows, and CTS rendered here beside the other
        # process knobs. Modern: four card bodies, each group starting at its own row 0,
        # and CTS is its own card (rendered from the registry by gui_pages -- the same
        # Tk var either way, so it is never two widgets on two variables).
        one = hosts is None
        row = self._sim_process_fields(h.get("process", frame), 0, cts=one)
        row = self._sim_lag_switch(h.get("lag", frame), row if one else 0)
        row = self._sim_sag_switch(h.get("sag", frame), row if one else 0)
        row = self._sim_diameter_fields(h.get("diameter", frame),
                                        row if one else 0)
        row = self._sim_fusion_switch(h.get("fusion", frame), row if one else 0)
        # The Taylor cone: in the classic panel, in sequence before the wave; on
        # the modern page, in the reserved row of the Material card's Jet
        # section (gui_pages sets `hosts["jet"]` and `_phys_jet_row`).
        if one:
            row = self._sim_cone_field(frame, row)
        elif "jet" in h:
            self._sim_cone_field(h["jet"], getattr(self, "_phys_jet_row", 0))
        self._sim_visual_fields(h.get("visual", frame), row if one else 0)

        # Either toggle (incl. a preset restoring it) re-applies the diameter mode. The
        # predicted diameter also tracks P, T and each scaffold's print speed, so
        # recompute live when any of those change.
        self.sim_physics_deposit.trace_add("write", self._apply_diam_mode)
        self.sim_diam_override.trace_add("write", self._apply_diam_mode)
        # The diameter law switch and the override are one setting shown twice:
        # ticking either un-ticks the other. Only the override is persisted.
        self.sim_diam_override.trace_add("write", self._sync_diam_law)
        self.sim_diam_law.trace_add("write", self._sync_diam_override)
        self._sync_diam_law()
        self.sim_pressure.trace_add("write", self._on_sim_diam_input_change)
        self.sim_temperature.trace_add("write", self._on_sim_diam_input_change)
        # ...and the Physics page's own constants: the bore, its length, the
        # viscosity anchor, Ea, a replaced flow or diameter law. Typing a new
        # bore used to leave every scaffold's diameter at the old bore's value,
        # and that var is what Generate sends. Coalesced, because a preset load
        # writes every one of these at once.
        for var in getattr(self, "cal_vars", {}).values():
            var.trace_add("write", self._on_cal_diam_input_change)
        for i, v in enumerate(self.scaf_vars):
            v["speed"].trace_add(
                "write", lambda *_a, idx=i: self._refresh_scaffold_phys_diam(idx))
        # set the initial (physics-off) state: override disabled, fields editable
        self._apply_diam_mode()

    # -- the field groups, one per Physics-page module card ----------------- #
    # Each takes (parent, row) and returns the NEXT free row, so the classic panel
    # can stack all four in one grid while the modern page hands each its own card
    # body at row 0. Nothing here knows which shell it is in.
    def _sim_process_fields(self, parent, row=0, *, cts=True):
        """The per-print process knobs: elevation, voltage, pressure, temperature --
        plus, on the classic path only, the measured CTS beside them. Laid out by
        `gui_form` from `row` of `parent`; returns the next free row.

        What these knobs drive rides every tooltip rather than a grey line under
        them: they drive the physics jet in the simulation, and through whichever
        law switches are on (Jet lag, Fiber sagging, Fiber fusion, Fiber
        diameter) they also shape the exported STL."""
        F = gui_form.Form(parent, row=row)
        drives = ("\n\nThis drives the physics jet in the simulation. Through "
                  "the laws switched on in their cards (Jet lag, Sagging, "
                  "Fusion, Fiber diameter), it also shapes the exported "
                  "STL.")
        gap_tip = gui_tips.GLOSSARY.get("standoff", "The nozzle–collector gap.")
        F.entry("Nozzle–collector gap", self.sim_standoff,
                gap_tip + drives, unit="mm")
        F.entry("Voltage", self.sim_voltage,
                "Nozzle-to-collector voltage. It sets the electric field that "
                "draws the jet to the collector." + drives, unit="kV")
        F.entry("Pressure", self.sim_pressure,
                "Air pressure pushing the melt out of the nozzle. More pressure "
                "= more flow = thicker fiber." + drives, unit="psi")
        F.entry("Temperature", self.sim_temperature,
                "Melt temperature at the nozzle. Hotter melt is thinner and more "
                "fluid, so the fiber comes out finer." + drives, unit="°C")
        if not cts:
            F.entry("Polymer", self.sim_polymer,
                    "Material name recorded in the print header. This label "
                    "does not change the physics model.", width="text")
        # CTS is a MEASURED process value, so on the classic panel it sits with the
        # other process knobs rather than in the calibration Core-parameters group. It
        # shares the calibration var cal.cts_ref (created by _init_calibration_vars,
        # which runs first), so it still rides the Physics preset and is not rendered a
        # second time. The modern page gives it its own card instead, same var.
        cts_var = getattr(self, "cal_vars", {}).get("cal.cts_ref")
        if cts and cts_var is not None:
            F.entry("CTS", cts_var, gui_tips.GLOSSARY.get("CTS", ""),
                    unit="mm/s")
        return F.row

    def _sim_lag_switch(self, parent, row=0):
        """The Jet lag law's generation/export ON/OFF, at the head of its card.
        Off, every fiber keeps its commanded g-code path in the STL, the
        simulation and the previews. Returns the next free row."""
        F = gui_form.Form(parent, row=row)
        F.check(
            "Jet lag (fiber lands behind the nozzle)", self.sim_physics_deposit,
            "Reshape each fiber into the jet's touchdown path: it trails the "
            "nozzle and rounds every corner. Applies to generation, export, the "
            "simulation and the previews.\n\n"
            "Accel-limited motion timing and the below-CTS coiling wave "
            "(Coiling wave card) are part of this laydown, so they "
            "switch off with it.\n\n"
            "Off = every fiber keeps its commanded g-code path.")
        return F.row

    def _sim_sag_switch(self, parent, row=0):
        """The Fiber sagging law's generation/export ON/OFF, at the head of its
        card. Off is the same as the Export panel's sag solver set to Off, for
        every worker and the player (`gui_args._sag_solver_args`)."""
        F = gui_form.Form(parent, row=row)
        F.check(
            "Sagging (fibers hang between supports)", self.sim_sag_law,
            "Let each fiber sag between the crossings that hold it up and "
            "rest on what is already laid, by this card's law. Applies to "
            "generation, export, the simulation and the previews.\n\n"
            "Off = fibers stay at their commanded height, with no sag.")
        return F.row

    def _sim_diameter_fields(self, parent, row=0):
        """The deposited-diameter mode: the law switch, the manual-override
        switch (its inverse) and the live per-scaffold readout of the
        predetermined value. Laid out by `gui_form` from `row` of `parent`;
        returns the next free row.

        The diameter LAW has no knobs of its own -- d = sqrt(4Q/(pi v)) over
        Hagen-Poiseuille Q -- so every input to it is a field on the Process or
        Material card. That is why this group is two linked controls and a
        readout rather than a parameter list."""
        F = gui_form.Form(parent, row=row)
        F.check(
            "Fiber diameter from physics", self.sim_diam_law,
            "Set each scaffold's fiber diameter from pressure, temperature and "
            "that scaffold's print speed. Voltage and the nozzle–collector gap don't change "
            "it. The fiber-diameter fields then lock and show the computed "
            "value. With Jet lag on, the exported fiber also thickens and thins "
            "along its length with the head's speed.\n\n"
            "Off = the diameter you type (the same as ticking 'Override fiber "
            "diameter' below).")
        self._diam_override_chk = F.check(
            "Override fiber diameter (enter manually)", self.sim_diam_override,
            "Type each fiber's diameter yourself instead of the computed "
            "value. The same setting as 'Fiber diameter from physics' above, "
            "inverted: ticking one un-ticks the other.").field
        # live readout of the predetermined fiber diameter per active scaffold
        # (see _update_phys_readout) -- a computed value, so it stays text
        # rather than a tooltip
        self.sim_diam_readout = F.live(None, text="").field
        self.sim_diam_readout.configure(foreground="#1f6b2e")
        return F.row

    def _sim_fusion_switch(self, parent, row=0):
        """The Fiber fusion law's generation/export ON/OFF; on the modern page it
        sits in the Fiber-fusion card, above the overlap magnitudes it switches
        on. Independent of Jet lag."""
        F = gui_form.Form(parent, row=row)
        self._physical_fusion_chk = F.check(
            "Fusion (lower stacked peaks and walls)",
            self.sim_physical_fusion,
            gui_tips.GLOSSARY.get("fusion", "") + "\n\nApplies to "
            "generation, export, the simulation and the previews. Needs "
            "Sagging on, because fusion lowers the height a fiber rests "
            "at.").field
        return F.row

    def _sim_cone_field(self, parent, row=0):
        """The molten Taylor cone at the nozzle (player jet render only); on by
        default, untick for the legacy constant-radius thin jet."""
        F = gui_form.Form(parent, row=row)
        F.check("Taylor cone at the nozzle (cone-jet profile)",
                self.sim_taylor_cone,
                gui_tips.GLOSSARY.get("Taylor cone", "") + "\n\nRender only.")
        return F.row

    def _sim_visual_fields(self, parent, row=0):
        """The below-CTS coiling wave with its amplitude/wavelength, which
        belong to the wave's toggle and so sit one indent under it. The wave is
        drawn by the jet-lag transform, so its toggle greys while Jet lag is off
        (`_apply_diam_mode`). The Taylor cone is `_sim_cone_field`."""
        F = gui_form.Form(parent, row=row)
        # Below-CTS coiling-wave indicator: toggle + amplitude/wavelength (mm). The
        # two fields grey out when the toggle is off (see _apply_coil_wave_mode).
        wave = ("Below the critical translation speed the jet over-supplies "
                "fiber and buckles. Rather than draw it straight, the simulation "
                "lays a tight sine wave so you can see at a glance where CTS was "
                "crossed. Amplitude and wavelength are visual settings, not a "
                "physical model.")
        self._coil_wave_chk = F.check(
            "Coiling wave below CTS (visual indicator)", self.sim_coil_wave,
            wave + "\n\nPart of the jet-lag laydown: needs Jet lag on.").field
        self._coil_wave_amp_entry = F.entry(
            "Wave amplitude", self.sim_coil_wave_amp,
            "How far the drawn wave swings to each side. " + wave, unit="mm",
            indent=1).field
        self._coil_wave_len_entry = F.entry(
            "Wave wavelength", self.sim_coil_wave_len,
            "The length of one drawn wave. " + wave, unit="mm",
            indent=1).field
        self.sim_coil_wave.trace_add("write", self._apply_coil_wave_mode)
        self._apply_coil_wave_mode()
        return F.row

    def _build_machine_motion_limits(self, parent, row, pad):
        """Override the shared no-op: the machine's ACCELERATION LIMITS, which the
        look-ahead planner (mew_kinematics) times the simulated head by. They are
        properties of the machine's controller, not jet knobs. The defaults are
        Voron 0.2 stock. Forwarded via _sim_play_args to BOTH the player (its clock,
        print-time total and animated head ramp) and the physics export (the
        deposited fiber's lag/diameter track the ramped speed), so the two re-time
        alike.

        The checkbox is the explicit ON/OFF. OFF (the default) bypasses the planner
        entirely -- naive length/feed timing, byte-identical to the pre-kinematics build
        in both the player and the deposited STL -- by forwarding max_accel = 0, which
        both consumers read as "disabled"; the fields are greyed. Default OFF because
        at MEW speeds (feed ~ CTS) the re-timing changes the deposited geometry only
        marginally: it is a completeness/timing feature, not a predictive law.

        ROTARY ACCEL is the mandrel's A-axis limit, deg/s^2. 0 (the default) is no
        separate limit: the linear value then caps the whole move along the surface,
        exactly the one-limit plan. Set, the linear value caps X/Z only and this caps
        A (`mew_kinematics.axis_seg_accels`). Its row is kept as
        `self._rotary_accel_row` so the Machine page can show it on 4 axis only.

        A headed block of `gui_form` rows from `row` of `parent`; the limits belong
        to the checkbox, so they sit one indent under it."""
        self.sim_kinematics = tk.BooleanVar(value=False)
        self.sim_max_accel = tk.StringVar(value="2000")
        self.sim_rotary_accel = tk.StringVar(value="0")
        self.sim_scv = tk.StringVar(value="6")
        F = gui_form.Form(parent, row=row)
        # air above the heading unless it opens its frame (the Machine card has
        # the STL and flavor rows over it)
        F.section("Acceleration limits", first=(row == 0))
        self._kinematics_chk = F.check(
            "Use acceleration limits",
            self.sim_kinematics,
            "Off = the simulated head changes speed instantly. On = it speeds up "
            "and slows down within your printer's acceleration limits, at fiber "
            "starts and ends, corners and turnarounds. That re-times the player "
            "clock, the print-time estimate and the physics-deposited fiber (its "
            "lag and diameter). The effect is small at MEW speeds.\n\n"
            "Part of the jet-lag laydown: needs Jet lag on.").field
        self._max_accel_entry = F.entry(
            "Linear accel", self.sim_max_accel,
            "The acceleration limit of the linear axes, from your printer's "
            "controller settings. On Klipper it is max_accel in printer.cfg.",
            unit="mm/s²", indent=1).field
        rot = F.entry(
            "Rotary accel", self.sim_rotary_accel,
            "The acceleration limit of the mandrel's rotary axis, from your "
            "printer's controller settings.\n\n"
            "0 = no separate limit: Linear accel then limits the whole move "
            "along the mandrel surface. Set, Linear accel limits only the "
            "linear axes and this limits the rotation.", unit="deg/s²",
            indent=1)
        self._rotary_accel_row = rot
        self._rotary_accel_entry = rot.field
        self._scv_entry = F.entry("Corner speed", self.sim_scv,
                                  gui_tips.GLOSSARY["SCV"], unit="mm/s",
                                  indent=1).field
        # enable/disable the fields with the checkbox (incl. a preset restoring it)
        self.sim_kinematics.trace_add("write", self._apply_kinematics_mode)
        self._apply_kinematics_mode()

    def _apply_kinematics_mode(self, *_):
        """Grey the acceleration-limit fields unless acceleration limits are on.
        Purely cosmetic (the OFF gating that actually bypasses the planner lives in
        _sim_play_args, which forwards max_accel = 0 when the box is unticked).
        The checkbox itself greys while Jet lag is off: the planner re-times the
        jet-lag laydown, so there is nothing for it to act on."""
        if not hasattr(self, "sim_kinematics"):   # its block is not built yet
            return
        lag = self._lag_on()
        self._set_entry_state(getattr(self, "_kinematics_chk", None),
                              "normal" if lag else "disabled")
        state = "normal" if lag and self.sim_kinematics.get() else "disabled"
        self._set_entry_state(getattr(self, "_max_accel_entry", None), state)
        self._set_entry_state(getattr(self, "_rotary_accel_entry", None), state)
        self._set_entry_state(getattr(self, "_scv_entry", None), state)

    def _apply_coil_wave_mode(self, *_):
        """Grey the coiling-wave amplitude/wavelength fields unless the indicator is on.
        Cosmetic only -- the OFF gating that forwards amplitude 0 lives in
        _sim_play_args, the same idiom as the accel-limited timing toggle. The
        toggle itself greys while Jet lag is off, the same as the planner's."""
        if not hasattr(self, "sim_coil_wave"):
            return
        lag = self._lag_on()
        self._set_entry_state(getattr(self, "_coil_wave_chk", None),
                              "normal" if lag else "disabled")
        state = "normal" if lag and self.sim_coil_wave.get() else "disabled"
        self._set_entry_state(getattr(self, "_coil_wave_amp_entry", None), state)
        self._set_entry_state(getattr(self, "_coil_wave_len_entry", None), state)

    def _lag_on(self):
        """The Jet lag law switch, True before its var exists (the planner block
        is built on the Physics page before the sim panel creates it)."""
        v = getattr(self, "sim_physics_deposit", None)
        return True if v is None else bool(v.get())

    def _machine_standoff_field(self, F):
        """Override the shared default (_BuildPlateMixin._machine_standoff_field): the
        physics build's printhead elevation lives on THIS tab as sim_standoff, so the
        Machine tab's Printer-model group shows no duplicate elevation field. Render
        nothing."""
        return None

    def _standoff_var(self):
        """Override the shared default (_BuildPlateMixin._standoff_var): route the
        printhead elevation -- both the optional g-code Z bump and the printer-model
        profile standoff -- to the Printhead-sim tab's sim_standoff, so one field feeds
        the whole physics build. (machine_standoff still exists as a shared var; in the
        physics build nothing reads it and it is no longer persisted.)"""
        return self.sim_standoff

    def _build_sim_tab(self, nb):
        """Insert the 'Printhead sim' tab into the global notebook, after Print
        slide and before Export (called from the shared _build_scaffold_tab)."""
        sim_tab = ttk.Frame(nb, padding=8)
        nb.add(sim_tab, text="  Printhead sim  ")
        self._build_sim_controls(sim_tab, row=0, col=0)

    def _build_sim_extra(self, parent):
        """Override the shared no-op (_CoreMixin._build_sim_extra): add the SECOND
        Simulation-tab launcher, 'real-time adjustment', which passes --live-adjust so
        the player enables live parameter editing (the v/f/h/[ ] keys edit the knobs AND
        re-lay the deposited fiber to match; 0 restores the original). A separate button
        rather than a checkbox on the first keeps the default launch unambiguously
        locked. Toggled with play_btn in the shared _sync_result_buttons, which finds
        play_live_btn via getattr."""
        self.play_live_btn = ttk.Button(
            parent, text="Open simulation window (real-time adjustment)",
            command=lambda: self.play_gcode(live=True), state="disabled")
        self.play_live_btn.pack(anchor="w", pady=(6, 0))
        ttk.Label(parent, justify="left", foreground="#777", wraplength=560,
                  text="Real-time adjustment lets you change voltage (v/V), "
                       "pressure (f/F), temperature (h/H) and print speed ([ ]) "
                       "live: the jet AND the deposited fiber respond — fiber laid "
                       "after a change takes the new diameter and whip, fiber "
                       "already laid keeps the look it was deposited with. Press 0 "
                       "(or the Reset params button) to return to the original "
                       "parameters. The plain button above locks the parameters."
                  ).pack(anchor="w", pady=(6, 0))

    def _sim_ensure_switch_vars(self):
        """Create the sim.* switch vars whose WIDGET is not on the sim panel.

        Idempotent, and called from two places on purpose -- the same shape as
        `gui_preview._pv_ensure_option_vars`, and for the same reason. Both shells build
        the Physics page's CALIBRATION section before `_build_sim_tab` runs, so a
        builder over there that waits for the var to appear draws nothing at all. The
        var must be creatable from whichever side gets there first.

        sim_mandrel_per_layer is 4-AXIS ONLY and OFF by default: the mandrel per-layer
        REGISTRATION hook (Calibration.mandrel_per_layer_registration; engine side in
        mew_deposition._reshape's placement branch). Off, one ContactPlacement is
        evaluated at the bare radius for every ply. On, it is re-evaluated at radius +
        wall height, so each ply lands slightly around the tube from the one below --
        the leaning wall, ~5.5 um/ply (MANDREL_LAG_DERIVATION.md 12.8). That composition
        is UNMEASURED on any mandrel, which is why the default is frozen. Its widget is
        on the Physics page under Advanced parameters, NOT in CALIBRATION_FIELDS: it is
        a modelling-composition switch, not a rig constant a user measures
        (mew_calibration pins that exclusion), so
        it rides the physics PRESET while staying out of the calibration PROFILE.

        sim_physical_fusion joined it when the modern Physics page became a module
        grid: its checkbox moved from the sim panel into the Fiber-fusion card, beside
        the overlap magnitudes it switches on, and that card is built on the
        CALIBRATION side -- before _build_sim_tab runs. The Fiber fusion law switch:
        sink fiber-on-fiber contacts by the fusion law. Off -> the geometric
        full-diameter stack, byte-identical. Routes --physical-fusion to mew_export AND
        the player, so the two agree. SUSPENDED spans are never touched -- there is
        nothing under them to fuse into. ON by default.

        sim_sag_law and sim_diam_law are the Fiber sagging and Fiber diameter law
        switches, made here for the same reason (their cards are built on the
        calibration side and their summaries watch them). sim_sag_law is
        persisted (sim.sag_law); sim_diam_law is not -- it is the inverse of the
        persisted sim.diam_override, kept in step by `_sync_diam_law`."""
        if not hasattr(self, "sim_mandrel_per_layer"):
            self.sim_mandrel_per_layer = tk.BooleanVar(value=False)
        if not hasattr(self, "sim_physical_fusion"):
            self.sim_physical_fusion = tk.BooleanVar(value=True)
        if not hasattr(self, "sim_sag_law"):
            self.sim_sag_law = tk.BooleanVar(value=True)
        if not hasattr(self, "sim_diam_law"):
            self.sim_diam_law = tk.BooleanVar(value=True)

    def _sync_diam_law(self, *_):
        """Override changed (a click or a preset load): the law switch shows its
        inverse. Writes only on a change, so the two traces cannot ping-pong."""
        want = not bool(self.sim_diam_override.get())
        if bool(self.sim_diam_law.get()) != want:
            self.sim_diam_law.set(want)

    def _sync_diam_override(self, *_):
        """The diameter law switch changed: the override takes its inverse."""
        want = not bool(self.sim_diam_law.get())
        if bool(self.sim_diam_override.get()) != want:
            self.sim_diam_override.set(want)

    def _sim_persist_entries(self):
        """The printhead-sim fields for the preset persist map."""
        return {
            "sim.standoff": self.sim_standoff, "sim.voltage": self.sim_voltage,
            "sim.pressure": self.sim_pressure,
            "sim.temperature": self.sim_temperature,
            "sim.polymer": self.sim_polymer,
            "sim.physics_deposit": self.sim_physics_deposit,
            "sim.diam_override": self.sim_diam_override,
            "sim.physical_fusion": self.sim_physical_fusion,
            "sim.sag_law": self.sim_sag_law,
            "sim.taylor_cone": self.sim_taylor_cone,
            "sim.coil_wave": self.sim_coil_wave,
            "sim.coil_wave_amp": self.sim_coil_wave_amp,
            "sim.coil_wave_len": self.sim_coil_wave_len,
            "sim.kinematics": self.sim_kinematics,
            "sim.max_accel": self.sim_max_accel, "sim.scv": self.sim_scv,
            "sim.rotary_accel": self.sim_rotary_accel,
            "sim.mandrel_per_layer": self.sim_mandrel_per_layer,
        }

    def _machine_motion_keys(self):
        """Override the shared no-op: the physics-build sim.* persist keys that belong
        to the MACHINE preset kind rather than the physics preset -- the acceleration
        limits (the toggle, linear and rotary accel, corner speed). Their widgets sit
        on the Machine page, so they ride a machine preset; folded in by
        gui_buildplate._machine_persist_keys.

        The printhead ELEVATION (sim.standoff) is NOT here: its field is on the Physics
        tab, so it rides the PHYSICS preset. Preset ownership follows the TAB the field
        lives on, not an abstract 'rig property' notion."""
        return ("sim.kinematics", "sim.max_accel", "sim.scv", "sim.rotary_accel")

    # The sim.* persist keys that ride the PHYSICS preset (the "calibration" kind) --
    # i.e. every persisted field on the Physics tab, which since the U1 tab merge rides
    # ONE preset, so a saved profile reproduces the DEPOSITED geometry (rounded corners,
    # jet lag, dynamic diameter) and the elevation it was printed at, not just the cal.*
    # jet-model anchors. This is every persisted sim.* key EXCEPT `_machine_motion_keys`
    # (the accel-planner knobs, whose fields are on the Machine tab). Kept as bare key
    # NAMES -- no Tk vars read -- so the calibration preset grid can claim its columns
    # before `_build_sim_tab` has created the vars. A new sim.* field must be added here
    # or to `_machine_motion_keys`, or the split silently drifts.
    _PHYSICS_PROCESS_KEYS = (
        "sim.standoff", "sim.voltage", "sim.pressure", "sim.temperature", "sim.polymer",
        "sim.physics_deposit", "sim.diam_override",
        "sim.physical_fusion", "sim.sag_law", "sim.taylor_cone",
        "sim.coil_wave", "sim.coil_wave_amp", "sim.coil_wave_len",
        "sim.mandrel_per_layer",
    )

    def _physics_process_persist_keys(self):
        """The sim.* process/deposition persist keys owned by the physics
        (calibration) preset kind (see `_PHYSICS_PROCESS_KEYS`)."""
        return self._PHYSICS_PROCESS_KEYS

    # -- predetermined fiber diameter (physics-deposited scaffolds) ---------- #
    def _diam_is_physics(self):
        """True when the deposited diameter should be predetermined from physics:
        the Fiber diameter law on (= the manual override off). Independent of
        Jet lag."""
        return not bool(self.sim_diam_override.get())

    @staticmethod
    def _solve_jet(pressure_pa, temperature_c, speed_mm_s, voltage_kv=4.0,
                   gap_mm=4.5):
        """Solve the reduced-order jet model for one parameter set -> JetState, or None
        on unusable input (or if the physics modules are absent). PURE: no Tk, so it is
        unit-testable headless. The diameter is
        JetState.fiber_diameter, the same value that sizes the animated jet. Voltage and
        the gap do not enter it (mass conservation); they are accepted only to build a
        complete ProcessParams / model."""
        if not _HAVE_PHYSICS or speed_mm_s <= 0.0 or gap_mm <= 0.0:
            return None
        try:
            model = MEWModel(nozzle=Nozzle(), working_distance=gap_mm)
            return model.solve(ProcessParams(
                pressure=pressure_pa, voltage=voltage_kv,
                speed=speed_mm_s, temperature=temperature_c))
        except Exception:  # noqa: BLE001
            return None

    def _jet_for_scaffold_speed(self, speed_mm_min):
        """JetState for the panel's pressure/temperature (and voltage/elevation)
        at the given scaffold print speed (mm/min -> mm/s), or None if any field is
        blank/garbage. The single place the sim-panel StringVars are read."""
        try:
            speed = float(speed_mm_min) / 60.0
            pressure_pa = float(self.sim_pressure.get()) * PSI_TO_PA
            temp = float(self.sim_temperature.get())
            volt = float(self.sim_voltage.get())
            gap = float(self.sim_standoff.get())
        except (ValueError, tk.TclError, AttributeError):
            return None
        return self._solve_jet_with_calibration(pressure_pa, temp, speed, volt, gap)

    def _solve_jet_with_calibration(self, pressure_pa, temperature_c, speed_mm_s,
                                    voltage_kv, gap_mm):
        """Like _solve_jet, but builds the model from the Calibration tab's rig profile
        (the _calibration_models hook) so the predetermined-diameter readout tracks the
        user's anchors. Falls back to the model defaults when no Calibration tab is
        present. Never raises: a bad field -> None -> the readout shows a dash."""
        if not _HAVE_PHYSICS or speed_mm_s <= 0.0 or gap_mm <= 0.0:
            return None
        try:
            models = self._calibration_models()
            if models is not None:
                material, nozzle, calibration = models
                model = MEWModel(material=material, nozzle=nozzle,
                                 working_distance=gap_mm, calibration=calibration)
            else:
                model = MEWModel(nozzle=Nozzle(), working_distance=gap_mm)
            return model.solve(ProcessParams(
                pressure=pressure_pa, voltage=voltage_kv,
                speed=speed_mm_s, temperature=temperature_c))
        except Exception:  # noqa: BLE001
            return None

    def _apply_diam_mode(self, *_):
        """Re-apply the diameter mode after either toggle flips (or a preset restores
        them), and grey what hangs off Jet lag. When
        the diameter is physics-driven, capture each scaffold's hand-typed value once,
        overwrite it with the predetermined physics value and lock the field; otherwise
        restore the captured values and unlock. Iterates every scaffold slot, hidden
        ones included, so revealing one later already shows the right state.

        Muted while a preset is loading: the preset sets `s.diameter` and
        `sim.physics_deposit` in separate steps, and running this mid-load would pop a
        STALE `_manual_diam` back over the just-loaded diameter. The preset engine
        re-settles once via `_after_scaffold_preset_applied` below."""
        if getattr(self, "_loading_preset", False):
            return
        # Corner resolution lives on the Export panel but refines the physics
        # transient windows, so it has nothing to act on with the transform off.
        for w in (getattr(self, "s_fill_per_tau_entry", None),
                  getattr(self, "s_auto_corner_grid_check", None)):
            self._set_entry_state(
                w, "normal" if self.sim_physics_deposit.get() else "disabled")
        # the planner and the coiling wave ride the same transform
        self._apply_kinematics_mode()
        self._apply_coil_wave_mode()
        physics = self._diam_is_physics()
        for i, v in enumerate(self.scaf_vars):
            # Scaffold 1's diameter has THREE entries: the planar geometry
            # grid's, the 4-axis grid's and the 5-axis grid's. Same variable, so
            # all must grey together or a hidden one comes back editable.
            entries = (v.get("diameter_entry"), v.get("diameter_entry_tube"),
                       v.get("diameter_entry_surface"))
            if physics:
                self._manual_diam.setdefault(i, v["diameter"].get())
                self._refresh_scaffold_phys_diam(i)
                for entry in entries:
                    self._set_entry_state(entry, "disabled")
            else:
                for entry in entries:
                    self._set_entry_state(entry, "normal")
                if i in self._manual_diam:
                    v["diameter"].set(self._manual_diam.pop(i))
        # The G-code window's own design (gui_gcode_twodesigns) is a second
        # set of scaffold vars. It has no diameter widgets of its own -- its
        # Parameters table greys off the designer's entry -- but its VALUE is
        # what that page generates from, so it must follow the physics too.
        # Without this a pressure edit moved the designer's diameter and left
        # the G-code window generating at the old one under a greyed field.
        manual = self.__dict__.setdefault("_manual_page_diam", {})
        for v in self._phys_page_scaffolds():
            if physics:
                manual.setdefault(id(v), v["diameter"].get())
                self._refresh_phys_diam_of(v)
            elif id(v) in manual:
                v["diameter"].set(manual.pop(id(v)))
        for entry, btn in getattr(self, "_diam_links", ()):
            self._place_diam_link(entry, btn, physics)
        self._update_phys_readout()

    # -- the "From Physics" jump beside a greyed diameter field ------------- #
    def _diam_link(self, entry):
        """Put a small link beside a scaffold diameter field. It jumps to the
        Physics page's Fiber diameter card and rings the override switch, the
        one control that makes the greyed field editable. Shown only while the
        field is greyed; `_apply_diam_mode` owns that.

        Modern shell only: the jump is the finder's `reveal_control`, and the
        classic UI has no card to open.

        PLACED against the entry, not gridded in the row's tail column. A
        form's column 1 is as wide as its widest control (a combobox, a radio
        row), so column 2 starts far to the right of a short entry and the
        link was clipped at the pane edge in all three collector modes. A
        placed widget is unmapped whenever the entry it is placed in is, so it
        still hides with its row."""
        if entry is None or not hasattr(self, "reveal_control"):
            return None
        btn = gui_form.mark(ttk.Button(
            entry.master, text="From Physics →", style="Link.TButton",
            command=self._goto_diam_override), "button")
        gui_tips.Tooltip(btn, (
            "The Physics page predicts this diameter from pressure, "
            "temperature and print speed. Click to open its Fiber diameter "
            "card, where ‘Override fiber diameter’ lets you type "
            "it here instead."))
        # Hidden until `_apply_diam_mode` runs: the scaffold grids are built
        # before the physics vars exist, the same reason the greying waits.
        self.__dict__.setdefault("_diam_links", []).append((entry, btn))
        return btn

    @staticmethod
    def _place_diam_link(entry, btn, show):
        try:
            if show:
                btn.place(in_=entry, relx=1.0, x=gui_form.PAD["padx"],
                          rely=0.5, anchor="w")
            else:
                btn.place_forget()
        except tk.TclError:
            pass

    def _goto_diam_override(self):
        """Open the Fiber diameter card and ring its override switch."""
        import gui_finder
        chk = getattr(self, "_diam_override_chk", None)
        for hit in gui_finder.index(self):
            if hit.widget is chk:
                self.reveal_control(hit)
                return
        page = getattr(self, "_physics_page", None)
        grid = getattr(self, "_physics_grid", None)
        if page is not None:
            self.goto_page(page)
        if grid is not None:
            grid.open("diameter")

    def _after_scaffold_preset_applied(self):
        """Re-settle the diameter mode once a scaffold preset has fully loaded (the
        `_apply_diam_mode` trace was muted during the bulk set). The preset carries BOTH
        the diameter and the physics-deposit state, so any diameter stashed before the
        load is stale: drop it, then apply the mode against the loaded values."""
        self._manual_diam.clear()
        self.__dict__.get("_manual_page_diam", {}).clear()
        self._apply_diam_mode()
        # The Curve-quality combobox shows a LABEL while the var holds the engine token,
        # so a bulk set leaves the two out of step until something re-syncs them.
        sync = getattr(self, "_sync_curve_quality_label", None)
        if sync is not None:
            sync()

    def _on_sim_diam_input_change(self, *_):
        """Pressure/temperature edited: refresh every scaffold's predetermined
        diameter (a no-op unless the diameter is physics-driven)."""
        if self._diam_is_physics():
            for i in range(len(self.scaf_vars)):
                self._refresh_scaffold_phys_diam(i)
            for v in self._phys_page_scaffolds():
                self._refresh_phys_diam_of(v)
        else:
            self._update_phys_readout()       # keep the reference readout current

    def _on_cal_diam_input_change(self, *_):
        """A Physics constant edited: the same refresh, once per idle. A preset
        load is skipped here because `_after_calibration_preset_applied`
        re-settles the diameters itself when the load is done."""
        if getattr(self, "_loading_preset", False) or getattr(
                self, "_cal_diam_pending", False):
            return
        self._cal_diam_pending = True

        def run():
            self._cal_diam_pending = False
            self._on_sim_diam_input_change()
        try:
            self.root.after_idle(run)
        except (tk.TclError, AttributeError):
            run()

    def _refresh_scaffold_phys_diam(self, i):
        """Recompute scaffold i's predetermined fiber diameter from the current knobs +
        its print speed and write it into the scaffold's `diameter` var, so the
        generator/converter/player all pick it up. A no-op unless the diameter is
        physics-driven, so the per-speed trace is free otherwise. Keeps the field locked."""
        # Muted mid-preset-load: this fires from the `s.diameter`/speed write-traces as
        # the preset sets each field, and would overwrite the preset's own saved diameter
        # with the predicted one. `_after_scaffold_preset_applied` re-bakes it after.
        if getattr(self, "_loading_preset", False):
            return
        if not self._diam_is_physics():
            return
        if not (0 <= i < len(self.scaf_vars)):
            return
        v = self.scaf_vars[i]
        st = self._jet_for_scaffold_speed(v["speed"].get())
        if st is not None:
            v["diameter"].set(self._fmt_diam(st.fiber_diameter))
        self._set_entry_state(v.get("diameter_entry"), "disabled")
        self._update_phys_readout()

    # -- the G-code window's own design follows the physics diameter too ----- #
    def _phys_page_scaffolds(self):
        """The G-code window's scaffold dicts once that page has made them
        (`gui_gcode_twodesigns.gce_scaf_vars`), else nothing. Read off
        `__dict__` so asking never builds them."""
        return list(self.__dict__.get("_gce_sv") or ())

    def _refresh_phys_diam_of(self, v):
        """`_refresh_scaffold_phys_diam` for a G-code-window scaffold dict:
        its own print speed, the same model, written into its own diameter.
        It has no entry to grey; its table reads the designer's lock."""
        if getattr(self, "_loading_preset", False) or not self._diam_is_physics():
            return
        try:
            st = self._jet_for_scaffold_speed(v["speed"].get())
        except (KeyError, tk.TclError):
            return
        if st is not None:
            text = self._fmt_diam(st.fiber_diameter)
            if v["diameter"].get() != text:
                v["diameter"].set(text)

    def _phys_page_attach(self, v):
        """A G-code-window scaffold dict was made: its speed drives its
        diameter while the diameter comes from physics, as the designer's
        speed drives the designer's. Called by gui_gcode_twodesigns."""
        try:
            v["speed"].trace_add("write",
                                 lambda *_a, d=v: self._refresh_phys_diam_of(d))
        except (KeyError, AttributeError, tk.TclError):
            return
        self._refresh_phys_diam_of(v)

    def _update_phys_readout(self):
        """Refresh the sim-panel readout listing each ACTIVE scaffold's predetermined
        diameter (and a below-CTS note). Under an override it is labelled a reference,
        because the hand-typed value is what is used."""
        lbl = getattr(self, "sim_diam_readout", None)
        if lbl is None:
            return
        overriding = bool(self.sim_diam_override.get())
        header = ("Override is on. Physics Ø, for reference:"
                  if overriding
                  else "Computed fiber Ø (mass conservation):")
        try:
            n = self._scaffold_count()
        except Exception:  # noqa: BLE001
            n = 1
        lines = [header]
        for i in range(min(n, len(self.scaf_vars))):
            v = self.scaf_vars[i]
            spd = v["speed"].get().strip()
            st = self._jet_for_scaffold_speed(v["speed"].get())
            if st is None:
                lines.append(f"  Scaffold {i + 1}: — (set the pressure, temperature and speed)")
            else:
                note = "" if st.straight_fibre else "  (below CTS)"
                d = gui_units.show("fiber_d", "%.4g" % st.fiber_diameter, "mm")
                v_txt = gui_units.show("speed", spd, "mm/min")
                lines.append(f"  Scaffold {i + 1}: {d} at {v_txt}{note}")
        lbl.configure(text="\n".join(lines))

    @staticmethod
    def _fmt_diam(d_mm):
        """A scaffold diameter (mm) formatted for the field: enough significant
        figures for sub-micron fibers without a wall of trailing digits."""
        return f"{d_mm:.4g}"

    @staticmethod
    def _set_entry_state(widget, state):
        """Set a widget's state, tolerating a missing handle / destroyed widget."""
        if widget is None:
            return
        try:
            widget.configure(state=state)
        except tk.TclError:
            pass

    def _export_prefix(self, worker, target, gcode=None):
        """Override the core no-op: with any of the Jet lag, Fiber sagging or Fiber
        fusion law switches on, run the export through mew_export.py, which installs
        the laws that are on and then drives the SAME downstream main() (`target` =
        'converter' for the G-code tab, 'scaffold' for the Parameters tab), so the
        written STL and the player's deposition sidecar are physics-shaped. Jet lag
        off passes --no-jet-lag, so the commanded centreline is kept while the sag
        and fusion laws still apply. All three off, it is the plain worker --
        byte-identical geometric output (the diameter law needs no wrapper: it
        writes the scaffold's own diameter field).

        With Jet lag on and the diameter physics-driven it also passes
        --dynamic-diameter, so the exported fiber thickens/thins along its length
        with the head's speed; the along-length variation is computed by the jet-lag
        transform, so without it the predetermined diameter is uniform."""
        lag = bool(self.sim_physics_deposit.get())
        if not (lag or self._sag_law_on() or self.sim_physical_fusion.get()):
            return worker_cmd(worker)
        cmd = (worker_cmd("worker:export", "--target", target)
               + self._sim_play_args(gcode))     # --coil rides here when toggled on
        if not lag:
            cmd.append("--no-jet-lag")
        elif self._diam_is_physics():
            cmd.append("--dynamic-diameter")
        return cmd

    def _sag_law_on(self):
        """The Fiber sagging law switch (True in a build that never made it)."""
        v = getattr(self, "sim_sag_law", None)
        return True if v is None else bool(v.get())

    def _file_temperatures(self, gcode):
        """The nozzle temperatures g-code file `gcode` sets, as
        `mew_gcode.nozzle_temperatures` lists them ([] for none, no path, or an
        unreadable file). Cached on (path, size, mtime): Play, the export and
        the status line all ask about the same file."""
        if not gcode:
            return []
        try:
            st = os.stat(gcode)
        except OSError:
            return []
        key = (os.path.abspath(gcode), st.st_size, st.st_mtime_ns)
        cache = getattr(self, "_file_temps_cache", None)
        if cache is not None and cache[0] == key:
            return cache[1]
        import mew_gcode
        try:
            with open(gcode, encoding="utf-8", errors="replace") as fh:
                temps = mew_gcode.nozzle_temperatures(fh)
        except OSError:
            temps = []
        self._file_temps_cache = (key, temps)
        return temps

    def _sim_temperature_arg(self, gcode=None):
        """The melt temperature (degC, as the --temperature string) the jet
        model runs at for g-code file `gcode`: the FIRST temperature the file
        sets, when it sets one, since that is the print the file describes;
        otherwise the Physics page's Temperature. The player and the physics
        export both take it from here, so the Process box, the simulated jet
        and the exported fibre agree."""
        temps = self._file_temperatures(gcode)
        if temps:
            return "%g" % temps[0][1]
        return self._sim_float(self.sim_temperature, 90.0)

    def _sim_temperature_note(self, gcode):
        """One plain sentence for the Simulation panel when the file's own
        temperature is the one simulated, or "" when the file sets none (the
        Physics page's value is then used, as the page says)."""
        temps = self._file_temperatures(gcode)
        if not temps:
            return ""
        line, t = temps[0]
        note = "Simulated at %g °C, set by this file (line %d)" % (t, line)
        try:
            page = float(self.sim_temperature.get())
        except (ValueError, tk.TclError, AttributeError):
            page = None
        if page is not None and abs(page - t) > 1e-9:
            note += ", not the Physics page's %g °C" % page
        later = next(((n, v) for n, v in temps[1:] if abs(v - t) > 1e-9), None)
        if later is not None:
            note += (". The file changes it to %g °C at line %d; the "
                     "simulation holds %g °C throughout" % (later[1], later[0], t))
        return note + "."

    def _sim_play_args(self, gcode=None):
        """Printhead-animator CLI args forwarded to gcode_player on Play AND to the
        physics export (both consume the same knobs; see _export_prefix). Validated,
        with the player's own defaults as the fallback so a blank field never breaks
        launch. Pressure is entered in psi (the MEWron regulator's unit) and converted
        to Pa (1 psi = 6894.76 Pa).

        `gcode` is the file being simulated or exported: a file that sets its
        own nozzle temperature (M104/M109) runs at that one, not the Physics
        page's (`_sim_temperature_arg`). None (a scaffold the app generates
        itself, from the Physics page's value) uses the page."""
        pa = str(float(self._sim_float(self.sim_pressure, 3.0)) * PSI_TO_PA)
        lag = bool(self.sim_physics_deposit.get())
        # accel-limited timing is gated by its checkbox (Machine and Collector tab) AND
        # by Jet lag, whose laydown it re-times: OFF -> forward max_accel = 0, which
        # BOTH the player (_retime_kinematic) and the export (mew_deposition's
        # max_accel <= 0 guard) read as "disabled".
        max_accel = (self._sim_float(self.sim_max_accel, 2000.0)
                     if lag and self.sim_kinematics.get() else "0")
        # the rotary limit rides with it, and only means anything on a mandrel;
        # 0 when the box is off so the worker's default can never re-enable it
        rot_accel = (self._sim_float(self.sim_rotary_accel, 0.0)
                     if lag and self.sim_kinematics.get() else "0")
        # below-CTS coiling wave: forward amplitude 0 when the box (or Jet lag, which
        # draws it) is off, so the worker's own default can never silently re-enable a
        # wave the user turned off, while the typed amplitude/wavelength stay in the
        # fields.
        cw_amp = (self._sim_float(self.sim_coil_wave_amp, 0.1)
                  if lag and self.sim_coil_wave.get() else "0")
        args = ["--standoff", self._sim_float(self.sim_standoff, 4.5),
                "--voltage", self._sim_float(self.sim_voltage, 4.0),
                "--pressure", pa,
                "--temperature", self._sim_temperature_arg(gcode),
                "--coil-wave-amp", cw_amp,
                "--coil-wave-len", self._sim_float(self.sim_coil_wave_len, 0.3),
                "--max-accel", max_accel,
                "--square-corner-velocity", self._sim_float(self.sim_scv, 6.0),
                "--rotary-accel", rot_accel]
        # physics-deposit MODE flag (not a knob): rides here so it reaches BOTH the
        # player -- which passes it to load_depo_cache to reject a stale .depo written in
        # the OTHER mode -- and mew_export, which absorbs it.
        # Corner resolution (the Export panel) rides the SHARED arg builder because the
        # export, the locked player and the live-adjust march must refine on the same
        # grid, which is what test_mew_incremental's bit-identity guard asserts.
        args += self._fill_per_tau_args()
        if lag:
            args.append("--physics-deposit")
            # the mandrel per-layer registration hook (mew_export's
            # --mandrel-per-layer): it re-registers the jet-lag placement, and is inert
            # on a flat collector, where nothing builds a ContactPlacement.
            if self.sim_mandrel_per_layer.get():
                args.append("--mandrel-per-layer")
        # The sag and fusion laws are their own switches, independent of Jet lag. They
        # ride the SAME args to the player and the export, so the two always agree on
        # the .depo cache key. The physical sag law is the only one the GUI offers
        # (sim_physical_sag is always on); sagging off sends --no-sag instead
        # (gui_args._sag_solver_args), so no sag law is installed at all.
        if self._sag_law_on() and self.sim_physical_sag.get():
            args.append("--physical-sag")
        if self.sim_physical_fusion.get():
            args.append("--physical-fusion")
        # the rig calibration profile (Material / Nozzle / Calibration anchors + hooks)
        # rides here too, so the player AND the physics export build the jet model from
        # the same per-rig profile. [] in a build without the Calibration tab.
        args += self._calibration_args()
        return args

    def _live_diam_args(self):
        """Override the core no-op: forward --dynamic-diameter to the live-adjust player
        exactly when the diameter is physics-driven -- the SAME condition that gives the
        export its --dynamic-diameter -- so the real-time fiber recomputes its diameter
        as you change pressure/temperature, and an override value shows in the live sim
        as it already does in the locked sim and the STL. Only meaningful with
        --live-adjust; harmless otherwise."""
        return (["--dynamic-diameter"]
                if self._diam_is_physics() and self.sim_physics_deposit.get()
                else [])

    def _sim_cone_args(self):
        """Override the core no-op: forward --no-cone to the player when the user unticks
        'Taylor cone'. Player-render only, so it rides the player launch
        (gui_viewers.play_gcode), NOT _sim_play_args, which also feeds the export where
        the flag would be unknown."""
        return [] if self.sim_taylor_cone.get() else ["--no-cone"]

    def _validate_process(self):
        """None, or the message Generate refuses with when one of the Physics
        page's four process settings is not a usable number.

        `_sim_float` falls back to a fixed default for a blank or unreadable
        field so a typo can never block the simulation's launch. At Generate that
        meant "3,5" in the gap ran at 4.5 mm and "1OO" degC at 90 degC, with the
        typo still showing on the card. So Generate names the field instead,
        in the unit the field is shown in."""
        press_unit = gui_units.shown("pressure", "psi")
        rows = (("Nozzle–collector gap (mm)", "sim_standoff", "gt0"),
                ("Voltage (kV)", "sim_voltage", "ge0"),
                ("Pressure (%s)" % press_unit, "sim_pressure", "ge0"),
                ("Temperature (°C)", "sim_temperature", "abs0"))
        for label, attr, rule in rows:
            var = getattr(self, attr, None)
            if var is None:
                continue
            raw = str(var.get()).strip()
            if attr == "sim_pressure" and press_unit != "psi":
                raw = gui_units.convert_text(raw, "pressure", "psi", press_unit)
            try:
                val = float(raw)
                ok = math.isfinite(val)
            except ValueError:
                ok = False
            if not ok:
                return ("Physics › Process parameters: “%s” must be a number "
                        "(it reads “%s”)." % (label, raw))
            if rule == "gt0" and val <= 0:
                return ("Physics › Process parameters: “%s” must be more "
                        "than 0." % label)
            if rule == "ge0" and val < 0:
                return ("Physics › Process parameters: “%s” cannot be "
                        "negative." % label)
            if rule == "abs0" and val <= -273.15:
                return ("Physics › Process parameters: “%s” must be above "
                        "absolute zero (−273.15 °C)." % label)
        return None

    @staticmethod
    def _sim_float(var, default):
        """A simulator field as a string float, falling back to `default` for a
        blank or non-numeric entry so a typo never blocks the player launch."""
        try:
            return str(float(var.get().strip()))
        except (ValueError, AttributeError):
            return str(default)
