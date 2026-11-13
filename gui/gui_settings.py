#!/usr/bin/env python3
"""gui_settings.py -- the Settings window: a gear at the end of the top row that
opens one sheet under it, with its sections as a bar along the top
and a height that fits the section on show. Not a page:
nothing in it is a design parameter you tune while you work, so it has no
business among the row's tabs.

Two scopes, as Fusion keeps Preferences and Document Settings apart and Bambu
Studio keeps its Preferences apart from the printer / filament / process
presets:

APP -- about the person, kept in `gui_prefs.json`, never in a preset.

- General: dark mode, display units (gui_units; display only, every var, preset
  and g-code keeps its canonical unit), default presets (the same pointers each
  Presets card's "Set as default" writes, through the same `_commit_presets`),
  and the default Save-as folder (`gui_prefs.save_dir_kw`).

THIS DESIGN -- travels with the design: these are the Scaffold designer's own
Export and Quality cards, so they are saved in the design's presets. They
replaced the Quality and Export ribbon buttons.

- G-code output: the Export card (`_gce_panel_export`), whatever it holds in
  the mode the page is in (Calibrate swaps in its own box).
- Quality, in two groups that never share a knob. LIVE PREVIEW is the picture
  in the design window (`pv_*`: facets, curve quality, corner resolution, auto
  corner resolution, Disable preview). STL & SIMULATION is what Generate
  builds, Save exports and the player rides (`s_*`: the Quality card). Neither
  changes the other; "Preview at export quality" is the one button that runs
  the picture on the export's values.
- Each of the two keeps a "Make these my defaults" and "Reset to my defaults"
  strip. My defaults are `design_defaults` in `gui_prefs.json`: a map of
  persist key -> value. They are applied once at boot AFTER the boot default
  presets (`_apply_design_defaults`), so they are what a new design starts
  from; loading a preset afterwards brings that preset's own values.

Changes apply as they are made; there is no Save button. The window floats
over the page with place(), like the Ctrl+F finder, so opening it reflows
nothing. Esc, a click outside it or the gear again closes it. Esc is the
shell's Cancel key too (`_kb_escape`): with the window open it only closes the
window, so dismissing Settings can never cancel a running job.

The This-design sections are built by the SAME builders the ribbon panels used
(`_gce_panel_export` / `_gce_panel_quality`, over `gui_gcode_panels._View`),
so they bind the designer's own Tk vars: flipping a box here flips it on the
Scaffold designer page and the reverse. A section's widgets are destroyed when
you leave it, and the view's traces with them.
MODERN SHELL ONLY: `ModernApp` mixes this in; the classic UI keeps its
bottom-strip Dark-mode checkbox.
"""
import os
import tkinter as tk
from tkinter import filedialog, ttk

from gui_common import VScrollFrame

import gui_prefs
import gui_tips
import gui_units
import gui_widgets

# which preset kinds get a default-preset row, in reading order. The eight
# Calibration tabs keep theirs in their own Presets cards: eight more rows
# would bury the three that decide what the app opens with.
_DEFAULT_KINDS = (("machine", "Printer"), ("calibration", "Physics"),
                  ("scaffold", "Scaffold"))
_NONE = "None"


class _SettingsMixin:

    # -- the gear ------------------------------------------------------------ #
    def _build_settings_button(self, parent):
        """The top row's gear. Returns the button."""
        from gui_gcode_cad import GlyphButton
        btn = GlyphButton(parent, "⚙", self.toggle_settings, size=11)
        gui_tips.add_tip(btn, "Settings (Ctrl+,).")
        self._settings_btn = btn
        self._settings_frame = None
        self.root.bind("<Control-comma>", self.toggle_settings, add="+")
        self.root.bind("<Button-1>", self._settings_click_away, add="+")
        return btn

    def toggle_settings(self, _event=None):
        if self._settings_open():
            self.close_settings()
        else:
            self.open_settings()
        return "break"

    def _settings_open(self):
        f = getattr(self, "_settings_frame", None)
        return f is not None and f.winfo_exists()

    def _settings_click_away(self, event):
        if not self._settings_open():
            return
        w = str(getattr(event, "widget", ""))
        keeps = [self._settings_frame, self._settings_btn]
        # Calibrate's cards are children of the root, gridded into the window
        boxes = getattr(self, "_cal_boxes", None)
        if boxes is not None and boxes.shown:
            keeps.append(boxes.card(boxes.shown))
        for keep in keeps:
            k = str(keep)
            if w == k or w.startswith(k + "."):
                return
        self.close_settings()

    def close_settings(self, _event=None):
        f = getattr(self, "_settings_frame", None)
        if f is not None:
            try:
                f.destroy()
            except tk.TclError:
                pass
        self._settings_frame = None

    # -- the window ---------------------------------------------------------- #
    #: (heading, ((section id, its name), ...)), in the order the list shows
    SETTINGS_SECTIONS = (("App", (("general", "General"),)),
                         ("This design", (("output", "G-code output"),
                                          ("quality", "Quality"))))
    SETTINGS_W = 680
    SETTINGS_H = 940
    _SETTINGS_WRAP = 620

    def open_settings(self, _event=None, section=None):
        """Open the window on `section` (an id from SETTINGS_SECTIONS), or on
        the one it was last left on."""
        self.close_settings()
        have = self._settings_have_design()
        ids = [i for _h, rows in self.SETTINGS_SECTIONS for i, _n in rows
               if have or i == "general"]
        sec = section or getattr(self, "_settings_last", "general")
        self._settings_last = sec if sec in ids else "general"
        pal = gui_widgets.palette()
        line = pal.get("hairline", "#d9dce1")
        # a tk.Frame only for its one-pixel border; everything inside is ttk
        # on the theme's styles, so the theme engine recolours it
        outer = tk.Frame(self.root, highlightthickness=1,
                         highlightbackground=line, highlightcolor=line,
                         bg=pal.get("card", "#ffffff"))
        self._settings_frame = outer
        outer.columnconfigure(0, weight=1)

        head = ttk.Frame(outer, padding=(14, 10, 14, 8))
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="Settings", font="MewSection").pack(side="left")
        ttk.Label(head, text="Esc closes", style="Hint.TLabel").pack(
            side="right")

        # the sections as one joined bar along the top, as the Scaffold
        # designer's axis-mode toggle is; the scope each belongs to is named
        # beside it
        bar_row = ttk.Frame(outer, padding=(14, 0, 14, 8))
        bar_row.grid(row=1, column=0, sticky="ew")
        self._settings_bar_row = bar_row
        names = {i: n for _h, rows in self.SETTINGS_SECTIONS for i, n in rows}
        self._settings_var = tk.StringVar(value=self._settings_last)
        bar = gui_widgets.SegmentedBar(
            bar_row, [(i, names[i]) for i in ids], self._settings_var,
            command=lambda: self._settings_show(self._settings_var.get()))
        bar.pack(side="left")
        self._settings_scope = tk.StringVar(value="")
        ttk.Label(bar_row, textvariable=self._settings_scope,
                  style="Hint.TLabel").pack(side="left", padx=(12, 0))

        self._settings_scroll = VScrollFrame(outer, padding=(18, 4, 18, 8))
        self._settings_scroll.grid(row=2, column=0, sticky="nsew")
        outer.rowconfigure(2, weight=1)
        self._settings_scroll.body.columnconfigure(0, weight=1)
        # pinned under the scroll: the defaults strip of a This-design
        # section, always on screen however far its page is scrolled
        self._settings_footer = ttk.Frame(outer, padding=(18, 0, 18, 10))
        self._settings_footer.grid(row=3, column=0, sticky="ew")
        self._settings_view = None
        self._settings_host = None
        self._settings_traces = []
        self._settings_show(self._settings_last)
        self._settings_place()
        outer.lift()
        # a Calibrate card lent to the window must stay above it
        boxes = getattr(self, "_cal_boxes", None)
        if boxes is not None and boxes.shown:
            boxes.card(boxes.shown).lift()

    def _settings_place(self):
        """Put the window under the gear, as tall as its section needs (up to
        what the main window has room for), right edges together."""
        outer = self._settings_frame
        self.root.update_idletasks()
        gear = self._settings_btn
        x = gear.winfo_rootx() - self.root.winfo_rootx() + gear.winfo_width()
        y = gear.winfo_rooty() - self.root.winfo_rooty() + gear.winfo_height()
        top = self.root.winfo_height()
        legacy = getattr(self, "_legacy_on", False)
        # down to the bottom row's edge, not over it: the panel ran on top of
        # Generate's row and the status line, which kept showing through
        floor = top
        bar = getattr(self, "_shell_bar", None)
        try:
            if bar is not None and bar.winfo_ismapped():
                floor = bar.master.winfo_rooty() - self.root.winfo_rooty()
        except tk.TclError:
            pass
        room = max(300, top - 24 if legacy else floor - y - 16)
        # the rows above and below the scrolled body, plus its own padding
        fixed = sum(w.winfo_reqheight() for w in outer.grid_slaves()
                    if w is not self._settings_scroll)
        want = fixed + self._settings_scroll.body.winfo_reqheight() + 16
        h = int(min(room, self.SETTINGS_H, max(220, want)))
        if legacy:
            # the sidebar's foot: beside it, bottom-aligned
            outer.place(x=x + 8, rely=1.0, y=-8, anchor="sw",
                        width=self.SETTINGS_W, height=h)
        else:
            outer.place(x=x, y=y + 8, anchor="ne", width=self.SETTINGS_W,
                        height=h)

    def _settings_have_design(self):
        """The This-design sections need the CAD page's panel builders."""
        return (hasattr(self, "_gce_panel_export")
                and hasattr(self, "_gce_panel_quality"))

    def _settings_show(self, sid):
        """Show section `sid` in the content area, taking the last one down
        first (its view's traces go with it)."""
        self._settings_leave()
        self._settings_last = sid
        self._settings_var.set(sid)
        self._settings_scope.set("Saved with the design" if sid in
                                 self._DESIGN_KEYS else "Applies to the app")
        body = self._settings_scroll.body
        for w in body.winfo_children() + self._settings_footer.winfo_children():
            w.destroy()
        self._settings_scroll._canvas.yview_moveto(0)
        host = ttk.Frame(body)
        host.grid(row=0, column=0, sticky="nsew")
        host.columnconfigure(0, weight=1)
        self._settings_host = host
        getattr(self, "_settings_build_" + sid)(host)
        # not in a Calibrate print: its Quality and Export cards are the
        # print's own, and the strip would save the DESIGN's values under them
        cal = getattr(self, "_cal_print_on", None)
        if sid in self._DESIGN_KEYS and not (cal is not None and cal()):
            self._settings_defaults_strip(self._settings_footer, sid)
            self._settings_footer.grid()
        else:
            self._settings_footer.grid_remove()
        if self._settings_frame.place_info():
            self.root.after_idle(self._settings_refit)

    def _settings_refit(self):
        if self._settings_open():
            self._settings_place()
            boxes = getattr(self, "_cal_boxes", None)
            if boxes is not None and boxes.shown:
                boxes.card(boxes.shown).lift()

    def _settings_leave(self):
        """Release whatever the section on screen holds: the view's traces
        and ours. (Calibrate's boxes go back when their frame is destroyed:
        `_settings_lend`.)"""
        view, self._settings_view = getattr(self, "_settings_view", None), None
        if view is not None:
            view.release()
        for var, name in getattr(self, "_settings_traces", []):
            try:
                var.trace_remove("write", name)
            except (tk.TclError, ValueError):
                pass
        self._settings_traces = []

    def _settings_heading(self, body, r, title):
        ttk.Label(body, text=title.upper(), style="Hint.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 2))
        return r + 1

    def _settings_hint(self, parent, text, **grid):
        lab = ttk.Label(parent, text=text, style="Hint.TLabel",
                        wraplength=self._SETTINGS_WRAP, justify="left")
        lab.grid(sticky="w", **grid)
        return lab

    # -- General (App) ------------------------------------------------------- #
    def _settings_build_general(self, host):
        body = host
        body.columnconfigure(1, weight=1)
        r = 0

        # -- appearance --
        r = self._settings_heading(body, r, "Appearance")
        ttk.Checkbutton(body, text="Dark mode", variable=self.dark_mode,
                        command=self._settings_toggle_dark).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2); r += 1
        import vtk_helpers
        self._settings_persp = tk.BooleanVar(
            value=vtk_helpers.prefer_perspective())
        ttk.Checkbutton(body, text="Perspective view",
                        variable=self._settings_persp,
                        command=self._settings_toggle_perspective).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2); r += 1
        self._settings_hint(
            body, "The 3-D views (the design preview, the simulation and the "
                  "viewer) show depth. Off, they are orthographic: parallel "
                  "fibers stay parallel.", row=r, column=0, columnspan=3,
            pady=(0, 2)); r += 1
        self._settings_origin = tk.BooleanVar(
            value=vtk_helpers.prefer_origin())
        ttk.Checkbutton(body, text="Show G-code zero",
                        variable=self._settings_origin,
                        command=self._settings_toggle_origin).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2); r += 1
        self._settings_hint(
            body, "Draws g-code zero as X, Y and Z lines (red, green, blue) "
                  "in the design preview, the simulation and the viewer.",
            row=r, column=0, columnspan=3, pady=(0, 2)); r += 1

        # -- display units --
        r = self._settings_heading(body, r, "Display units")
        for q in gui_units.QUANTITIES:
            ttk.Label(body, text=gui_units.LABELS[q]).grid(
                row=r, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=gui_units.unit(q))
            cb = ttk.Combobox(body, textvariable=var, state="readonly",
                              values=list(gui_units.QUANTITIES[q]), width=10)
            cb.grid(row=r, column=1, sticky="w", padx=(8, 0))
            cb.bind("<<ComboboxSelected>>",
                    lambda _e, q=q, v=var: self._settings_set_unit(q, v.get()))
            r += 1
        self._settings_hint(
            body, "What the fields and readouts show. Presets and g-code "
                  "keep their own units.", row=r, column=0, columnspan=3,
            pady=(2, 0)); r += 1

        # -- default presets --
        r = self._settings_heading(body, r, "Default presets")
        r = self._settings_defaults(body, r)

        # -- files --
        r = self._settings_heading(body, r, "Files")
        ttk.Label(body, text="Save-as folder").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2); r += 1
        # its own full-width row: sharing the preset rows' columns squeezed
        # the path box to a stub beside two wide buttons
        self._settings_dir = tk.StringVar(value=gui_prefs.get("save_dir", ""))
        row = ttk.Frame(body)
        row.grid(row=r, column=0, columnspan=3, sticky="ew"); r += 1
        ttk.Button(row, text="Clear",
                   command=self._settings_clear_dir).pack(side="right")
        ttk.Button(row, text="Browse…",
                   command=self._settings_pick_dir).pack(side="right",
                                                        padx=(4, 4))
        ttk.Entry(row, textvariable=self._settings_dir,
                  state="readonly").pack(side="left", fill="x", expand=True)
        self._settings_hint(
            body, "Where Save STL as…, Save g-code as… and the other exports "
                  "open. Blank opens the last folder used.", row=r, column=0,
            columnspan=3, pady=(2, 0)); r += 1

        # -- messages: every "Don't show again" comes back from here --
        r = self._settings_heading(body, r, "Messages")
        row = ttk.Frame(body)
        row.grid(row=r, column=0, columnspan=3, sticky="ew"); r += 1
        self._settings_hidden_btn = ttk.Button(
            row, text="Show hidden messages again",
            command=self._settings_show_hidden)
        self._settings_hidden_btn.pack(side="left")
        self._settings_hidden_var = tk.StringVar()
        ttk.Label(row, textvariable=self._settings_hidden_var,
                  style="Hint.TLabel").pack(side="left", padx=(8, 0))
        self._settings_hidden_sync()
        self._settings_hint(
            body, "Brings back every message you chose “Don't show "
                  "again” or “Don't ask again” on, and the "
                  "note after your first Generate.", row=r, column=0,
            columnspan=3, pady=(2, 0)); r += 1

    # -- This design: the shared frame --------------------------------------- #
    def _settings_lend(self, box):
        """The builders borrow Calibrate's boxes in that mode; hand them back
        when `box` is destroyed (`_gce_panel_closed`, as the ribbon panel's
        close did)."""
        box.bind("<Destroy>", lambda e, b=box: self._settings_box_gone(e, b),
                 add="+")

    def _settings_box_gone(self, e, box):
        if e.widget is not box:
            return
        try:
            self._gce_panel_closed(e, box)
        except tk.TclError:
            pass

    # -- This design: G-code output ------------------------------------------ #
    def _settings_build_output(self, host):
        self._settings_hint(
            host, "How the g-code and the STL are written. These belong to the "
                  "design and are saved with its presets.", row=0, column=0,
            pady=(0, 6))
        box = ttk.Frame(host)
        box.grid(row=1, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)
        self._settings_lend(box)
        view = self._gce_panel_export(box)
        if view is not None:
            view.settle()
        self._settings_view = view

    # -- This design: Quality ------------------------------------------------ #
    def _settings_build_quality(self, host):
        self._settings_hint(
            host, "Two separate sets of knobs. The live preview is the picture "
                  "on the Design pages; the STL and simulation are what "
                  "Generate builds. Neither changes the other.", row=0,
            column=0, pady=(0, 6))
        # LIVE PREVIEW: the picture's own knobs (gui_preview `pv_*`)
        ttk.Label(host, text="Live preview", font="MewSection").grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        self._settings_hint(
            host, "How the picture on the Design pages is built. Never "
                  "changes the STL, the g-code or the simulation.", row=2,
            column=0, pady=(0, 4))
        pv = ttk.Frame(host)
        pv.grid(row=3, column=0, sticky="ew")
        self._settings_preview_rows(pv)
        gui_widgets.hairline(host, row=4, column=0, sticky="ew", pady=10)

        # STL & SIMULATION: the designer's Quality card
        ttk.Label(host, text="STL & simulation", font="MewSection").grid(
            row=5, column=0, sticky="w")
        self._settings_hint(
            host, "What Generate builds, what Save exports and what the "
                  "simulation plays. Never changes the preview, except "
                  "through “Preview at export quality”.", row=6, column=0,
            pady=(0, 4))
        box = ttk.Frame(host)
        box.grid(row=7, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)
        self._settings_lend(box)
        view = self._gce_panel_quality(box)
        if view is not None:
            view.settle()
        self._settings_view = view

    def _settings_preview_rows(self, parent):
        """The Live-preview group, on the designer's own form builder."""
        import gui_form
        from gui_scaffold import CURVE_QUALITY_ORDER, CURVE_QUALITY_VAL2LBL
        parent.columnconfigure(0, weight=0)
        F = gui_form.Form(parent)
        F.check("Disable preview", self.pv_disabled,
                "Stops the live preview from rebuilding, for every collector "
                "and every scaffold. A large design can take minutes to "
                "preview; with this ticked the app runs at the speed of "
                "Generate alone. The same switch as the designer's.")
        F.entry("Tube facets", self.pv_tube_sides,
                "How many flat sides the picture's tubes get. Fewer is "
                "quicker to draw. The STL has its own (below).")
        cq = F.combo(
            "Curve quality", self._pv_curve_quality_label,
            [CURVE_QUALITY_VAL2LBL[v] for v in CURVE_QUALITY_ORDER],
            "How closely the picture follows a curve laid by the jet at a "
            "turnaround, in fiber diameters of allowed error. Draft is the "
            "quick one the picture is meant for. The STL and the simulation "
            "have their own (below).").field
        cq.bind("<<ComboboxSelected>>", self._pv_curve_quality_from_label)
        fill = F.entry(
            "Corner resolution", self.pv_fill_per_tau,
            "How finely the picture samples the jet's catch-up through a "
            "turnaround or a fiber start, in samples per lag time. Only "
            "applies with Jet lag on (the Physics page). The STL and the "
            "simulation have their own (below).",
            unit="per lag time", width="small").field
        auto = F.check(
            "Auto corner resolution", self.pv_auto_corner_grid,
            "Adds samples to the picture wherever the drawn fiber would cut a "
            "corner, and nowhere else, at the Curve quality above. The STL's "
            "own box is below.").field
        # greyed with the physics transform, as the export's pair is
        self._settings_grey_with_physics((fill, auto))

    def _settings_grey_with_physics(self, widgets):
        var = getattr(self, "sim_physics_deposit", None)
        if var is None:
            return

        def sync(*_a):
            state = "normal" if var.get() else "disabled"
            for w in widgets:
                try:
                    self._set_entry_state(w, state)
                except tk.TclError:
                    pass
        sync()
        self._settings_traces.append((var, var.trace_add("write", sync)))

    # -- This design: my defaults -------------------------------------------- #
    #: persist keys (gui_presets._persist_map) each This-design section owns,
    #: plus "app.gen_render", Build quality, which no preset carries
    _DESIGN_KEYS = {
        # not the printer's output settings (spelling, heat wait, arcs, the
        # height rows, the custom line): Setup holds them and the Machine
        # preset saves them, and a design default applied at boot would
        # override the printer's preset
        "output": ("s.exit_move", "s.exit_margin", "s.make_stl",
                   "s.travel_in_stl", "s.travel_gcode_line",
                   "s.deposition_order"),
        "quality": ("s.faces", "s.sag_mode", "s.sag_reset",
                    "s.sag_reset_layers", "s.sag_resolution", "s.mesh_tol",
                    "s.fill_per_tau", "s.curve_quality", "s.auto_corner_grid",
                    "pv.disabled", "pv.tube_sides", "pv.curve_quality",
                    "pv.fill_per_tau", "pv.auto_corner_grid",
                    "app.gen_render"),
    }

    def _design_default_vars(self, section):
        """{key: Tk var} for `section`'s settings, skipping any this build
        does not have."""
        pm = self._persist_map()
        out = {}
        for k in self._DESIGN_KEYS[section]:
            var = (getattr(self, "gen_render", None) if k == "app.gen_render"
                   else pm.get(k))
            if var is not None:
                out[k] = var
        return out

    @staticmethod
    def _design_default_read(key, var):
        """The value to store: what the var holds, except Build quality,
        which holds a label and is kept as its stable engine value."""
        v = var.get()
        if key == "app.gen_render":
            from gui_common import RENDER_LBL2VAL
            return RENDER_LBL2VAL.get(v, v)
        return v

    @staticmethod
    def _design_default_write(key, var, value):
        if key == "app.gen_render":
            from gui_common import RENDER_VAL2LBL
            value = RENDER_VAL2LBL.get(value, value)
        var.set(value)

    def _settings_defaults_strip(self, footer, section):
        gui_widgets.hairline(footer).pack(fill="x", pady=(0, 8),
                                          padx=(0, 0))
        strip = ttk.Frame(footer)
        strip.pack(fill="x")
        strip.columnconfigure(2, weight=1)
        mk = ttk.Button(strip, text="Make these my defaults",
                        command=lambda: self._settings_make_default(section))
        mk.grid(row=0, column=0, sticky="w")
        gui_tips.add_tip(mk, "Remember every setting on this page as what a "
                             "new design starts from. A preset you load "
                             "later brings its own values.")
        rs = ttk.Button(strip, text="Reset to my defaults",
                        command=lambda: self._settings_reset_default(section))
        rs.grid(row=0, column=1, sticky="w", padx=(6, 0))
        gui_tips.add_tip(rs, "Put every setting on this page back to your "
                             "defaults, or to the app's defaults if you have "
                             "set none.")
        self._settings_default_note = tk.StringVar(
            value=self._settings_default_text(section))
        ttk.Label(strip, textvariable=self._settings_default_note,
                  style="Hint.TLabel", wraplength=220,
                  justify="left").grid(row=0, column=2, sticky="w",
                                       padx=(12, 0))

    def _settings_default_text(self, section):
        have = gui_prefs.get("design_defaults", {}) or {}
        mine = [k for k in self._DESIGN_KEYS[section] if k in have]
        return ("Using your defaults." if mine
                else "Using the defaults.")

    def _settings_make_default(self, section):
        saved = dict(gui_prefs.get("design_defaults", {}) or {})
        for k, var in self._design_default_vars(section).items():
            saved[k] = self._design_default_read(k, var)
            if k in getattr(self, "_field_defaults", {}):
                # a preset that omits this key resets to it from now on
                self._field_defaults[k] = var.get()
        gui_prefs.update(design_defaults=saved)
        self._settings_default_note.set("Saved. New designs start from these.")
        self.status.set("These settings are now what a new design starts from.")

    def _settings_reset_default(self, section):
        saved = gui_prefs.get("design_defaults", {}) or {}
        shipped = getattr(self, "_shipped_defaults", {}) or {}
        for k, var in self._design_default_vars(section).items():
            if k in saved:
                self._design_default_write(k, var, saved[k])
            elif k in shipped:
                self._design_default_write(k, var, shipped[k])
        self._settings_sync_labels()
        self._settings_show(section)
        self.status.set("Settings put back to your defaults.")

    def _settings_sync_labels(self):
        """Comboboxes show a label while the var holds the engine token."""
        for fn in ("_sync_curve_quality_label", "_sync_pv_curve_quality_label"):
            getattr(self, fn, lambda: None)()

    def _apply_design_defaults(self):
        """Boot: put the saved defaults on the design, after the boot default
        presets, so they are what a new design starts from. Remembers the
        shipped value of every key first (`_shipped_defaults`) for "Reset to
        my defaults" when none were saved."""
        saved = gui_prefs.get("design_defaults", {}) or {}
        base = dict(getattr(self, "_field_defaults", {}))
        self._shipped_defaults = {}
        for section in self._DESIGN_KEYS:
            for k, var in self._design_default_vars(section).items():
                self._shipped_defaults[k] = (
                    base[k] if k in base else self._design_default_read(k, var))
        if not isinstance(saved, dict):
            return
        for section in self._DESIGN_KEYS:
            for k, var in self._design_default_vars(section).items():
                if k not in saved:
                    continue
                try:
                    self._design_default_write(k, var, saved[k])
                except (tk.TclError, ValueError):
                    continue
                if k in getattr(self, "_field_defaults", {}):
                    self._field_defaults[k] = var.get()
        self._settings_sync_labels()

    # -- appearance --------------------------------------------------------- #
    def _settings_toggle_dark(self):
        self._toggle_theme()
        # the border frame is plain tk, so re-open to repaint it
        self.root.after_idle(self.open_settings)

    def _settings_toggle_perspective(self):
        import gui_preview
        import gui_widgets as gw
        import vtk_helpers
        on = bool(self._settings_persp.get())
        gui_prefs.update(perspective=on)
        vtk_helpers.set_prefer_perspective(on)
        # every view on show follows now; one opened later reads the pref
        for panel in list(gui_preview._PANELS) + list(gw._REGISTRY):
            fn = getattr(panel, "set_perspective", None)
            if fn is not None:
                try:
                    fn(on)
                except tk.TclError:
                    pass
        self.status.set("The 3-D views are now in %s." % (
            "perspective" if on else "orthographic"))

    def _settings_toggle_origin(self):
        import gui_preview
        import gui_widgets as gw
        import vtk_helpers
        on = bool(self._settings_origin.get())
        gui_prefs.update(show_origin=on)
        vtk_helpers.set_prefer_origin(on)
        for panel in list(gui_preview._PANELS) + list(gw._REGISTRY):
            fn = getattr(panel, "set_origin_visible", None)
            if fn is not None:
                try:
                    fn(on)
                except tk.TclError:
                    pass
        self.status.set("G-code zero is %s in the 3-D views." % (
            "shown" if on else "hidden"))

    # -- display units ------------------------------------------------------ #
    def _settings_set_unit(self, quantity, u):
        gui_units.set_unit(quantity, u)
        # the read-only readouts that print a converted unit
        for name in ("_refresh_phys_summaries", "_update_phys_readout",
                     "_cal_refresh_summaries", "_ds_refresh"):
            fn = getattr(self, name, None)
            if fn is not None:
                fn()
        self.status.set(f"{gui_units.LABELS[quantity]} now shows in {u}.")

    # -- default presets ---------------------------------------------------- #
    def _settings_default_rows(self):
        """[(kind, mode, label)] for every default this panel offers. A
        per-axis kind gets one row per collector mode it keeps a pointer
        for; the others one row with mode None."""
        rows = []
        stores = getattr(self, "_pstores", {})
        for kind, name in _DEFAULT_KINDS:
            st = stores.get(kind)
            if not st:
                continue
            modes = list(st["ptr_keys"])
            if modes == [None]:
                rows.append((kind, None, name))
                continue
            import gui_presets
            for mode in modes:
                rows.append((kind, mode, "%s, %s" % (
                    name, gui_presets.AXIS_SHORT.get(mode, mode))))
        return rows

    def _settings_defaults(self, body, r):
        self._settings_default_vars = {}
        for kind, mode, label in self._settings_default_rows():
            st = self._pstores[kind]
            names = [p["name"] for p in st["presets"]]
            var = tk.StringVar(value=st["defaults"].get(mode) or _NONE)
            ttk.Label(body, text=label).grid(
                row=r, column=0, sticky="w", pady=2)
            cb = ttk.Combobox(body, textvariable=var, state="readonly",
                              values=[_NONE] + names, width=24)
            cb.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(8, 0))
            cb.bind("<<ComboboxSelected>>",
                    lambda _e, k=kind, m=mode, v=var:
                    self._settings_set_default(k, m, v.get()))
            self._settings_default_vars[(kind, mode)] = var
            r += 1
        self._settings_hint(
            body, "The Plate defaults and the Physics default load at "
                  "startup. The Mandrel ones load when you switch the "
                  "collector to Mandrel.", row=r, column=0,
            columnspan=3, pady=(2, 0))
        return r + 1

    def _settings_set_default(self, kind, mode, name):
        """Point `kind`'s `mode` default at `name` (or clear it), through the
        same `_commit_presets` the Presets card's button uses."""
        st = self._pstores[kind]
        name = None if name == _NONE else name
        st["defaults"][mode] = name
        idx = self._preset_name_index(kind, name) if name else None
        noun = st["noun"]
        if name:
            status = f"“{name}” is now the default {noun}."
        else:
            status = f"No default {noun}."
        self._commit_presets(kind, idx, status)

    # -- files --------------------------------------------------------------- #
    def _settings_pick_dir(self):
        start = self._settings_dir.get() or None
        d = filedialog.askdirectory(parent=self.root, initialdir=start,
                                    title="Default Save-as folder")
        if d:
            self._settings_dir.set(d)
            gui_prefs.update(save_dir=d)
            self.status.set(f"Save-as dialogs now open in {d}.")

    def _settings_clear_dir(self):
        self._settings_dir.set("")
        gui_prefs.update(save_dir="")
        self.status.set("Save-as dialogs open in the last folder used.")

    # -- messages ------------------------------------------------------------ #
    def _settings_hidden_sync(self):
        n = gui_prefs.hidden_count()
        self._settings_hidden_var.set(
            "%d hidden" % n if n else "None hidden")
        self._settings_hidden_btn.state(["!disabled"] if n else ["disabled"])

    def _settings_show_hidden(self):
        gui_prefs.reset_hidden()
        self._settings_hidden_sync()
        self.status.set("Every hidden message shows again.")
