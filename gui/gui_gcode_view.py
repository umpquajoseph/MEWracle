#!/usr/bin/env python3
"""gui_gcode_view.py -- MODERN-UI ONLY: how the G-code page's picture is
inspected. Four tools, one mixin (`_GcodeViewMixin`, before `_GcodeJetMixin`
in the MRO), on `view_tools.ViewTools` (the VTK half, its docstring is the
contract for how the picture is cut and coloured without a rebuild).

  * THE BAR (Left / Right / Up / Down, Home, End) -- ONE bar along the
    picture's foot, built by `gui_gcode_jet` and placed and dressed here:
    Play, a slider that steps the print a layer at a time, 'Layer 7 / 20'
    with the print time, and the playback speed. The slider's position is the
    number of layers laid: its left end is layer 0, the empty collector before
    anything is laid, position k shows layers 1 to k in full, and its right
    end is the whole print. The picture is NOT
    rebuilt: the layers become a print-time window the fiber's shader cuts at
    (`ViewTools.set_window`). Scrubbing never shows the jet.
  * COLOUR (key C steps, key K or the ribbon's Color tool opens) -- Scaffold
    (the scaffolds' own colours: the picture as it always was), Layer or
    Speed, chosen and tuned in the page's floating box (`_gce_panel_colour`),
    which also sets the collector's colour and transparency. All of it is
    remembered in the page's prefs (`color_by`, `collector_color`, ...).
  * MEASURE (key I, the ribbon's Measure tool) and FIT (key Z, the display
    row's button) -- see `_view_measure_*` and `_view_fit`.

THE BAR floats over the picture, under the page's own boxes, so the g-code
text or an edit box is never covered. It is dragged by its grip (where it was
dragged is kept the way the g-code box's is, `_gce_dragged`), and has a toggle
in the display row (`show_bar` in the prefs). A bar that is off holds nothing
back: hiding it shows the whole print.

PLAY. Play carries on from the slider: it lays the layer after the ones shown
(from the empty collector at layer 0, and from the start again when the slider
is at its right end, where the whole print shows).
The slider follows the layer being laid. Dragging the slider, or an arrow
key, while it runs pauses it, drops the jet and shows the picture through that
layer. Esc (or the end of the print) brings the picture back through the layer
the jet had reached (layer 0 when it had laid nothing yet). The window and
the colours are kept across it.

EYES. A tree row hidden with its eye is cut from the picture's geometry as
before (`_gce_pv_show`); the window then works on what is left.

KEYS follow `_gce_key_m`: the letters fire anywhere on the page except while
typing; the arrow keys and Home only when the picture (or the slider) has the
focus, since the tree and the text use them. Clicking the picture gives it the
focus."""

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

import numpy as np

import gui_form
import gui_widgets
import preview_pick
import view_tools
from gui_gcode_jet import _fmt as _fmt_time
from gui_tips import Tooltip, add_tip

_NOT_TYPING_MASK = 0x0004 | 0x20000      # Control, Alt (Windows)

#: the colour choices, with the sentence that says what each one shows
_COLOR_TIP = {
    "scaffold": "Each scaffold in its own color (set on its card).",
    "layer": "Every layer its own shade, first to last.",
    "speed": "The feed each move is written with, in mm/s.",
}


def _fmt_int(n):
    return f"{int(n):,}"


def _fmt_mm(v):
    return f"{v:.3f}"


class Grip(tk.Canvas):
    """A floating bar's drag handle: a small block of dots, centred along the
    strip. `_gce_drag_bind` gives it the press, the drag and the cursor."""

    def __init__(self, parent, vertical=False):
        self.vertical = vertical
        super().__init__(parent, width=12 if vertical else 30,
                         height=20 if vertical else 10,
                         highlightthickness=0, takefocus=0)
        self.bind("<Configure>", lambda _e: self.redraw(), add="+")
        gui_widgets.register(self)
        self.retheme()

    def retheme(self):
        self.configure(background=gui_widgets.palette()["card"])
        self.redraw()

    def redraw(self):
        self.delete("all")
        ink = gui_widgets.palette()["hint"]
        cx, cy = self.winfo_width() / 2.0, self.winfo_height() / 2.0
        cols, rows = (2, 3) if self.vertical else (3, 2)
        for i in range(cols):
            for j in range(rows):
                x = cx + (i - (cols - 1) / 2.0) * 4
                y = cy + (j - (rows - 1) / 2.0) * 4
                self.create_rectangle(x - 1, y - 1, x + 1, y + 1, fill=ink,
                                      outline=ink)


class Chip(tk.Label):
    """A one-word toggle, washed with the accent while it is on."""

    def __init__(self, parent, text, command):
        super().__init__(parent, text=text, font="MewHint", padx=5, pady=1,
                         cursor="hand2")
        self.command = command
        self.on = False
        self.enabled = True
        self.bind("<ButtonRelease-1>", self._click, add="+")
        gui_widgets.register(self)
        self.retheme()

    def retheme(self):
        pal = gui_widgets.palette()
        ink = pal["fg"] if self.enabled else pal["disabled"]
        self.configure(background=pal["select_bg"] if self.on else pal["card"],
                       foreground=pal["select_fg"] if self.on else ink)

    def set_on(self, on):
        self.on = bool(on)
        self.retheme()

    def set_enabled(self, on):
        self.enabled = bool(on)
        self.configure(cursor="hand2" if on else "")
        self.retheme()

    def _click(self, _e):
        if self.enabled:
            self.command()


class MoveScrub(ttk.Scale):
    """The layer scrubber: the Play bar's `ttk.Scale`, in whole layers. Its
    position is the number of layers laid, 0 (the empty collector) to n (the
    whole print), so `n` layers make n + 1 stops. A click on the trough jumps
    there (a scale's own click only nudges), and the trough's outline takes
    the accent while it has the keyboard focus. `on_move(k)` runs on every
    change the user makes."""

    STYLE = "View.Horizontal.TScale"

    def __init__(self, parent, command):
        super().__init__(parent, from_=0, to=1, orient="horizontal",
                         style=self.STYLE, command=self._moved)
        self.on_move = command
        self.n, self.k = 0, 0
        self.enabled = False
        self._quiet = False
        self.bind("<ButtonPress-1>", self._press, add="+")
        gui_widgets.register(self)
        self.retheme()

    _styled_for = None

    def retheme(self):
        # the accent is the style's `focus` state, set once per palette: a
        # ttk style configure on every FocusIn / FocusOut restyled every ttk
        # widget in the app each time the slider took or lost the keyboard
        pal = gui_widgets.palette()
        style = ttk.Style(self)
        key = (style.theme_use(), pal["hairline"], pal["select_bg"])
        if MoveScrub._styled_for == key:
            return
        MoveScrub._styled_for = key
        style.configure(self.STYLE, bordercolor=pal["hairline"])
        style.map(self.STYLE, bordercolor=[("focus", pal["select_bg"])])

    def _clamp(self, k):
        return int(min(max(k, 0), self.n))

    def _moved(self, value):
        if self._quiet:
            return
        k = self._clamp(round(float(value)))
        if k != self.k:
            self.k = k
            self.on_move(k)

    def _show(self, k):
        # a disabled ttk scale ignores `set`: a new picture configured while
        # the bar was greyed (catching up) kept the old handle, past the end
        # of a shorter print, until the user moved it
        self._quiet = True
        off = self.instate(["disabled"])
        try:
            if off:
                self.state(["!disabled"])
            self.set(k)
        finally:
            if off:
                self.state(["disabled"])
            self._quiet = False

    def configure_moves(self, n, k):
        self.n = max(int(n), 0)
        self.k = self._clamp(k)
        self._quiet = True
        try:
            # k layers laid sits at k: 0 is the empty collector, n the
            # whole print, and the playhead crosses layer k + 1 between them
            self.configure(from_=0, to=max(self.n, 1))
        finally:
            self._quiet = False
        self._show(self.k)

    def set_enabled(self, on):
        self.enabled = bool(on) and self.n >= 1
        self.state(["!disabled"] if self.enabled else ["disabled"])

    def set_pos(self, k, notify=True):
        k = self._clamp(k)
        if k == self.k:
            return
        self.k = k
        self._show(k)
        if notify:
            self.on_move(k)

    def set_progress(self, x):
        """The handle at the fractional position `x` (layers laid, so 2.5 is
        half way through the third), without a change event. This is how Play
        moves it, smoothly; a drag still lands on whole layers."""
        self.k = self._clamp(int(x))
        self._show(min(max(x, 0.0), float(self.n)))

    def step(self, d):
        if self.enabled:
            self.set_pos(self.k + d)

    def _press(self, e):
        if not self.enabled:
            return
        self.focus_set()
        if "slider" not in str(self.identify(e.x, e.y)):
            self.set(self.get(e.x, e.y))
            self.update_idletasks()


class _GcodeViewMixin:
    """The page half of the inspection tools; see the module docstring."""

    # ------------------------------------------------------------------ #
    # building
    # ------------------------------------------------------------------ #
    def _build_gcode_editor_page(self, page):
        super()._build_gcode_editor_page(page)
        self._view_layers = []
        # lo stays 0: layers 1 to hi + 1 show (0-based hi); hi = -1 is
        # layer 0, the empty collector
        self._view_lo = self._view_hi = 0
        self._view_clock = None
        self._view_feed = None
        self._view_why = "Nothing drawn yet."
        self._view_hl_after = None
        self._view_info = {}
        panel = getattr(self, "_gce_pv_panel", None)
        ins = getattr(self, "_gce_inspect", None)
        self._view_panel_ok = panel is not None and ins is not None
        if not self._view_panel_ok:
            return
        panel.want_view_tools = True
        mode = self._gce_prefs().get("color_by")
        self._view_mode = mode if mode in view_tools.MODES else "scaffold"
        parent = self._gce_browser.master
        self._view_build_bar(parent)
        self._view_build_measure(parent)
        self._view_build_tools(ins)
        self._view_build_display()
        self._view_load_collector()
        host = panel._host
        host.bind("<Button-1>", lambda _e: host.focus_set(), add="+")
        for seq in ("i", "z", "c", "k"):
            self.root.bind_all("<KeyPress-%s>" % seq, self._view_key_letter,
                               add="+")
        for seq in ("Up", "Down", "Left", "Right", "Home", "End",
                    "Shift-Up", "Shift-Down", "Shift-Left", "Shift-Right"):
            self.root.bind_all("<%s>" % seq, self._view_key_arrow, add="+")
        self.root.bind_all("<KeyPress-Escape>", self._view_key_esc, add="+")
        self._view_refresh_ui()

    def _view_build_bar(self, parent):
        """The one bar along the foot is built by the jet mixin (Play, the
        layer slider, the speed); this gives it its grip and its place."""
        bar = self._jet_bar
        grip = gui_form.exempt(Grip(bar, vertical=True), "drag handle")
        grip.pack(side="left", fill="y", padx=(0, 6), before=bar.winfo_children()[0])
        add_tip(grip, "Drag to move the bar.")
        self._view_scale = self._jet_scale
        self._view_bar = bar
        self._gce_drag_bind("moves", bar, grip)

    def _view_build_measure(self, parent):
        from gui_gcode_cad import GlyphButton
        box = ttk.Frame(parent, style="Bar.TFrame", padding=(10, 6, 6, 8),
                        borderwidth=1, relief="solid")
        box.columnconfigure(1, weight=1)
        head = ttk.Label(box, text="Measure", style="Bar.TLabel",
                         font="MewSection")
        gui_form.exempt(head, "measure heading")
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        add_tip(head, "Millimeters, between the fibers' centerlines in the "
                      "approximate preview, not the solver's geometry. X, Y "
                      "and Z are the g-code's axes; on a mandrel, X runs along it. "
                      "Drag this title to move the box.")
        close = GlyphButton(box, "✕", lambda: self._view_measure_set(False),
                            size=10)
        gui_form.exempt(close, "measure: leave")
        close.grid(row=0, column=2, sticky="e")
        add_tip(close, "Leave Measure (Esc). The picture goes back to "
                       "selecting.")
        self._view_m_dist = tk.StringVar(value="–")
        dist = ttk.Label(box, textvariable=self._view_m_dist,
                         style="Bar.TLabel", font="MewTitle")
        gui_form.mark(dist, "live")
        dist.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 4))
        self._view_m_d = {}
        for row, ax in enumerate("xyz", start=2):
            ttk.Label(box, text="Δ" + ax.upper(), style="Bar.TLabel",
                      foreground=self._view_hex(view_tools.AXIS_RGB[ax])
                      ).grid(row=row, column=0, sticky="w")
            var = self._view_m_d[ax] = tk.StringVar(value="–")
            lab = ttk.Label(box, textvariable=var, style="Bar.TLabel",
                            anchor="e")
            gui_form.mark(lab, "live")
            lab.grid(row=row, column=1, sticky="ew", padx=(12, 4))
            ttk.Label(box, text="mm", style="BarHint.TLabel").grid(
                row=row, column=2, sticky="w", padx=(0, 4))
        gui_widgets.hairline(box).grid(row=5, column=0, columnspan=3,
                                       sticky="ew", pady=(6, 4), padx=(0, 4))
        self._view_m_pts = []
        for k in range(2):
            var = tk.StringVar(value="")
            self._view_m_pts.append(var)
            lab = ttk.Label(box, textvariable=var, style="BarHint.TLabel",
                            justify="left", wraplength=216)
            gui_form.mark(lab, "live")
            lab.grid(row=6 + k, column=0, columnspan=3, sticky="w",
                     pady=(0, 3))
        self._view_m_note = tk.StringVar(value="")
        lab = ttk.Label(box, textvariable=self._view_m_note,
                        style="BarHint.TLabel", justify="left",
                        wraplength=216)
        gui_form.mark(lab, "live")
        lab.grid(row=8, column=0, columnspan=3, sticky="w", pady=(1, 3))
        row = ttk.Frame(box, style="Bar.TFrame")
        row.grid(row=9, column=0, columnspan=3, sticky="w")
        cp = ttk.Button(row, text="Copy", style="BarLink.TButton",
                        command=self._view_copy)
        gui_form.exempt(cp, "measure: copy")
        cp.pack(side="left")
        add_tip(cp, "Copy both points, the distance and its X, Y and Z parts "
                    "as plain text.")
        again = ttk.Button(row, text="Clear", style="BarLink.TButton",
                           command=lambda: self._view_measure_clear())
        gui_form.exempt(again, "measure: clear")
        again.pack(side="left", padx=(12, 0))
        add_tip(again, "Forget the points and measure again. A third click "
                       "on the picture does the same.")
        self._view_mbox = box
        self._view_mtext_last = ""
        self._gce_drag_bind("measure", box, head)

    def _view_build_tools(self, ins):
        from gui_gcode_cad import GROUP_TINT, ToolButton
        b = ToolButton(
            ins, "measure", "Measure\n(I)",
            command=self._view_measure_toggle, tint=GROUP_TINT["inspect"],
            tip="Click two points on the picture, on a fiber or on the "
                "collector, to read the distance and its X, Y and Z parts "
                "in mm. A white dot shows where a click will land. Orbit, "
                "pan and zoom still work; a third click starts again, Esc "
                "leaves. It measures the preview's fiber centerlines, which "
                "are approximate, not the solver's geometry.")
        b.pack(side="left", padx=1, pady=(3, 0))
        self._view_measure_btn = b
        c = ToolButton(
            ins, "colour", "Color",
            command=lambda: self._gce_panel_open("colour"),
            tint=GROUP_TINT["inspect"],
            tip="What the fibers are colored by, and in which colors: each "
                "scaffold's own, by layer, or by the speed the g-code writes "
                "each move with. Press C to step through them.")
        c.pack(side="left", padx=1, pady=(3, 0))
        self._view_color_btn = c
        tools = getattr(self, "_gce_ptools", None)
        if tools is not None:
            tools["colour"] = c
        fit = getattr(self, "_gce_ribfit", None)
        if fit is not None:
            fit.fit()

    def _view_build_display(self):
        prefs = self._gce_prefs()
        self.gce_view_bar = tk.BooleanVar(
            value=bool(prefs.get("show_bar", prefs.get("show_moves", True))))
        flow = getattr(self, "_gce_flow", None)
        if flow is not None:
            reset = getattr(self, "_gce_reset_item", None)
            if reset is not None:
                flow._items.remove(reset)
            c = ttk.Checkbutton(flow, text="Play bar", variable=self.gce_view_bar,
                                style="Bar.TCheckbutton")
            gui_form.exempt(c, "display toggle")
            add_tip(c, "The bar along the foot of the picture: Play, and the "
                       "slider that steps the print a layer at a time. Off "
                       "shows the whole print.")
            flow.add(c)
            fb = ttk.Button(flow, text="Fit (Z)", style="BarLink.TButton",
                            command=self._view_fit)
            add_tip(fb, "Frame the selected fibers, layer or scaffold; with "
                        "nothing selected, the whole print as it is shown "
                        "(Z).")
            flow.add(fb)
            self._view_fit_btn = fb
            if reset is not None:
                flow._items.append(reset)
                flow.reflow()
            self.root.after_idle(self._gce_disp_fit)
        self.gce_view_bar.trace_add("write",
                                    lambda *_a: self._view_bar_changed())

    def _view_bar_changed(self):
        on = bool(self.gce_view_bar.get())
        self._gce_prefs_set(show_bar=on)
        if not on:
            # a bar that is off holds nothing back
            if getattr(self, "_jet", None) is not None:
                self._jet_stop()
            self._view_show_all()
        self._view_refresh_ui()

    # ------------------------------------------------------------------ #
    # the picture hands over its data: layers, clock, colours
    # ------------------------------------------------------------------ #
    def _gce_pv_aux(self, state, shown, meta):
        """Called by `_gce_pv_show` with the picture about to go up (after the
        eyes' cut): put the print clock and the colour arrays on `state`."""
        state["aux"] = None
        state["gcode_t"] = None
        self._view_why = None
        pt = meta.get("ptimes")
        clock = meta.get("line_clock")
        if pt is None or len(pt) != len(shown):
            self._view_why = ("This picture was thinned, so it has no print "
                              "clock: layers, moves and colors need one.")
            self._view_layers = []
            return
        if clock is None:
            self._view_why = ("The picture is still catching up with the "
                              "text: no print clock yet.")
            self._view_layers = []
            return
        if clock is not self._view_clock:
            self._view_clock = clock
            self._view_feed = view_tools.modal_feed(clock["text"])
            try:
                nodes, text = self._gce_pick_outline()
            except Exception:  # noqa: BLE001 -- no outline: one range
                nodes, text = [], None
            layers = []
            if text is None or text == clock["text"]:
                layers = self._view_layer_table(nodes, clock)
            if not layers:
                times = np.asarray(clock["times"], np.float64)
                n = clock["text"].count("\n") + 1
                layers = [dict(scaffold=1, name="the print", layer=1,
                               first=1, last=n, t0=float(times[0]),
                               t1=float(times[-1]))]
            self._view_layers = layers
            self._view_new_clock = True
        T = np.concatenate([np.asarray(t, np.float64) for t in pt])
        if not len(T):
            return
        base = float(T.min())
        aux = {"mew_view_t": (T - base)}
        aux.update(view_tools.point_arrays(T, self._view_layers, clock,
                                           self._view_feed))
        state["aux"] = aux
        state["aux_base"] = base
        gp, gt = meta.get("gcode_paths"), meta.get("gcode_ptimes")
        if gp and gt is not None and len(gp) == len(gt):
            state["gcode_t"] = np.concatenate(
                [np.asarray(t, np.float64) for t in gt]) - base
        nscaf = max((r["scaffold"] for r in self._view_layers), default=1)
        state["aux_meta"] = dict(
            layers=len(self._view_layers), nscaf=nscaf,
            scaffold_rgb=self._view_scaffold_colors(nscaf,
                                                    state.get("color")))
        if "mew_speed" not in aux:
            self._view_why_speed = ("No feed (F) words in this text: there "
                                    "is no speed to color by.")
        else:
            self._view_why_speed = None

    def _view_scaffold_colors(self, n, fallback):
        out = []
        sv = getattr(self, "gce_scaf_vars", None) or []
        extra = self._gce_prefs().get("color_scaf_extra") or {}
        for i in range(n):
            hx = extra.get(str(i)) if sv and i >= len(sv) else None
            rgb = self._view_rgb(hx) if hx else None
            if rgb is not None:
                out.append(rgb)
                continue
            try:
                out.append(tuple(self._pv_parse_color(sv[min(i, len(sv) - 1)]
                                                      ["color"])))
            except Exception:  # noqa: BLE001
                out.append(tuple(fallback or (0.8, 0.25, 0.2)))
        return out

    def _gce_pv_show(self):
        super()._gce_pv_show()
        if not getattr(self, "_view_panel_ok", False):
            return
        n = len(self._view_layers)
        if getattr(self, "_view_new_clock", False):
            self._view_new_clock = False
            keep_all = (self._view_lo == 0 and
                        self._view_hi >= getattr(self, "_view_n", 0) - 1)
            if keep_all or self._view_hi > n - 1:
                self._view_lo, self._view_hi = 0, n - 1
            self._view_lo = min(self._view_lo, max(n - 1, 0))
            self._view_hi = min(max(self._view_hi, -1), n - 1)
            self._view_n = n
        self._view_refresh_ui()
        self._view_apply()

    def _gce_hl_take(self, paths, meta):
        super()._gce_hl_take(paths, meta)
        if getattr(self, "_view_panel_ok", False):
            self._view_hl_window()

    # ------------------------------------------------------------------ #
    # the window
    # ------------------------------------------------------------------ #
    def _view_state(self):
        """(ViewTools or None, enabled?) for the picture now."""
        panel = getattr(self, "_gce_pv_panel", None)
        view = panel._view() if panel is not None else None
        self._view_load_custom(view)
        ok = (view is not None and bool(self._view_layers)
              and (panel._state or {}).get("aux") is not None)
        return view, ok

    def _view_range(self):
        n = len(self._view_layers)
        return (0, n - 1) if not n else (self._view_lo, self._view_hi)

    def _view_full(self):
        return self._view_lo == 0 and self._view_hi >=             len(self._view_layers) - 1

    def _view_apply(self):
        """Set the picture's window from the slider's layer: two uniforms and
        a draw (never a rebuild)."""
        view, ok = self._view_state()
        if view is None:
            return
        if not ok or self._view_full():
            view.set_window()
        elif self._view_hi < 0:
            # layer 0: the empty collector, before anything is laid
            t = self._view_layers[0]["t0"] - 1.0
            view.set_window(t, t)
        else:
            view.set_window(*view_tools.window_for(
                self._view_layers, self._view_lo, self._view_hi))
        self._view_hl_window()
        self._view_readout()

    def _view_hl_window(self):
        """Tell the selection's lit fiber the window (so a selection never
        floats outside it), and redraw it once the dragging pauses."""
        hl = getattr(self, "_gce_hl", None)
        view, _ok = self._view_state()
        if hl is None or view is None:
            return
        hl["window"] = view.window
        if self._view_hl_after is not None:
            try:
                self.root.after_cancel(self._view_hl_after)
            except tk.TclError:
                pass
        self._view_hl_after = self.root.after(120, self._view_hl_now)

    def _view_hl_now(self):
        self._view_hl_after = None
        self._gce_hl_refresh()

    # -- the slider's layer ------------------------------------------------------ #
    def _view_layer_at(self, t):
        """The 0-based layer the print time `t` (seconds from the print's
        start) falls in."""
        L = self._view_layers
        if not L:
            return 0
        t0 = np.fromiter((r["t0"] for r in L), np.float64, len(L))
        i = int(np.searchsorted(t0, t + view_tools.EPS_T, side="right")) - 1
        return min(max(i, 0), len(L) - 1)

    def _view_laid_at(self, t):
        """How many layers are laid in full at print time `t` (seconds from
        the print's start): 0 before the first is done, n at the end."""
        L = self._view_layers
        if not L:
            return 0
        t1 = np.fromiter((r["t1"] for r in L), np.float64, len(L))
        return int(np.searchsorted(t1, t + view_tools.EPS_T, side="right"))

    def _view_cur_layer(self):
        """The slider's position, the layers laid (0 is the empty
        collector): the jet's while it runs, else the slider's."""
        scene = getattr(self, "_jet", None)
        if scene is not None and self._view_layers:
            return self._view_laid_at(self._jet_t - scene.t_start)
        return self._view_hi + 1

    def _view_set_layer(self, k):
        """The picture through layer `k` (1-based, the slider's position):
        layers 1 to k in full, and 0 the empty collector."""
        n = len(self._view_layers)
        if not n:
            return
        self._view_lo, self._view_hi = 0, min(max(int(k), 0), n) - 1
        self._view_apply()
        self._view_scale.set_pos(self._view_hi + 1, notify=False)

    def _view_show_all(self):
        self._view_set_layer(len(self._view_layers))

    def _jet_start_time(self, scene):
        """Play carries on from the slider: it lays the layer after the ones
        shown, from the print's start at layer 0. At the right end, where the
        whole print shows, it starts from the beginning."""
        L = self._view_layers
        k = self._view_hi + 1
        if not L or k <= 0 or k >= len(L):
            return scene.t_start
        return scene._t_base + L[k]["t0"]

    # ------------------------------------------------------------------ #
    # readouts and layout
    # ------------------------------------------------------------------ #
    def _view_layer_text(self, i):
        L = self._view_layers
        r = L[i]
        if max(x["scaffold"] for x in L) <= 1:
            return f"layer {r['layer']} of {len(L)}"
        return (f"layer {i + 1} of {len(L)} ({r['name'].lower()}, "
                f"layer {r['layer']})")

    def _view_layer_of_line(self, line):
        L = self._view_layers
        firsts = [r["first"] for r in L]
        i = int(np.searchsorted(firsts, line, side="right")) - 1
        return min(max(i, 0), len(L) - 1)

    def _view_readout(self):
        """The bar's label while no jet runs ('Layer 7 / 20' and the print
        time at the end of that layer). While the jet runs its frames write
        it."""
        if getattr(self, "_jet", None) is not None:
            return
        L = self._view_layers
        if not L or self._view_clock is None:
            self._jet_time_var.set(self._view_why or "")
            return
        k = min(self._view_hi + 1, len(L))
        t = L[k - 1]["t1"] if k > 0 else 0.0
        self._jet_time_var.set(f"Layer {k} / {len(L)}  ·  {_fmt_time(t)}")

    def _view_refresh_ui(self):
        """Enable, grey (with the reason) and fill every control from the
        picture's state, and place them."""
        if not getattr(self, "_view_panel_ok", False):
            return
        view, ok = self._view_state()
        n = len(self._view_layers)
        scale = self._view_scale
        scale.configure_moves(n, self._view_cur_layer())
        scale.set_enabled(ok and n >= 1 and bool(self.gce_view_bar.get()))
        self._view_readout()
        self._view_colour_update()
        self._view_measure_why()
        self._view_place()

    def _gce_layout(self):
        super()._gce_layout()
        if getattr(self, "_view_panel_ok", False):
            self._view_place()

    def _view_stack(self, w, key):
        """Over the picture, under the page's own boxes, so the bar never
        covers the g-code text or an edit: unless it is the box dragged last,
        which stays where it was put on top, as the g-code box does."""
        if getattr(self, "_gce_drag_top", None) == key:
            w.lift()
        else:
            w.lower(self._gce_browser)

    def _view_place(self):
        """Lay the overlays over the picture: the bar (Play, the layer
        slider, the speed) along the foot and the measure box beside the tree
        (or the open edit). Either that was dragged stays where it was put."""
        if not getattr(self, "_view_panel_ok", False):
            return
        vp = self._gce_viewport
        try:
            W, H = vp.winfo_width(), vp.winfo_height()
        except tk.TclError:
            return
        if W < 200 or H < 200:
            return
        m = getattr(self, "_GCE_OVERLAY_GAP", 10)
        ok = self._view_state()[1]
        n = len(self._view_layers)
        bar = self._view_bar
        if n and ok and self.gce_view_bar.get():
            bw = max(520, min(720, W - 2 * 360))
            bh = max(bar.winfo_reqheight(), 28)
            # centred, but clear of the status box in the corner
            left = m
            if self.gce_corner_var.get():
                left += self._gce_corner.winfo_reqwidth() + m
            x = max((W - bw) // 2, left)
            bw = max(240, min(bw, W - m - x))
            x, y = self._gce_drag_spot("moves", x, H - m - bh, bw, bh, W, H)
            bar.place(in_=vp, x=x, y=y, anchor="nw", width=bw)
            self._view_stack(bar, "moves")
        else:
            bar.place_forget()
        mb = self._view_mbox
        if getattr(self, "_view_measuring", False):
            left = m
            for w in (getattr(self, "_gce_browser", None),
                      getattr(self, "_gce_formbox", None)):
                if w is not None and w.winfo_ismapped():
                    left = max(left, w.winfo_x() + w.winfo_width() + m)
            mw = 240
            x, y = self._gce_drag_spot("measure", min(left, W - m - mw), m, mw,
                                       mb.winfo_reqheight(), W, H)
            mb.place(in_=vp, x=x, y=y, anchor="nw", width=mw)
            mb.lift()
        else:
            mb.place_forget()

    # ------------------------------------------------------------------ #
    # Play and the layer window share the bar
    # ------------------------------------------------------------------ #
    def _jet_start(self):
        super()._jet_start()
        self._view_refresh_ui()

    def _jet_stop(self):
        """Esc, or the end of the playback: the picture comes back through
        the layer the jet had reached (layer 0 when it had laid nothing of
        the first). A stop the slider asked for goes to the slider's layer
        instead (`_view_set_layer`)."""
        scene = getattr(self, "_jet", None)
        layer = None
        if scene is not None and not self._jet_by_scrub and self._view_layers:
            t = self._jet_t - scene.t_start
            layer = self._view_layer_at(t)
            if t <= self._view_layers[layer]["t0"] + view_tools.EPS_T:
                layer -= 1          # nothing of it laid yet
        super()._jet_stop()
        if layer is not None:
            self._view_lo, self._view_hi = 0, layer
            self._view_apply()
        self._view_refresh_ui()

    # ------------------------------------------------------------------ #
    # colour: the ribbon tool's panel, in the page's floating box
    # ------------------------------------------------------------------ #
    def _gce_panel_colour(self, body):
        """How the fibres are coloured: a Scaffold / Layer / Speed choice, and
        under it the colours of that choice (`_view_colour_update` fills it,
        so it follows the picture while it is open)."""
        var = tk.StringVar(value=self._view_mode)
        gui_widgets.SegmentedBar(
            body, options=[(m, view_tools.MODE_LABEL[m])
                           for m in view_tools.MODES],
            variable=var, command=self._view_cp_chosen).grid(
                row=0, column=0, sticky="w", pady=(0, 6))
        inner = ttk.Frame(body)
        inner.grid(row=1, column=0, sticky="ew")
        inner.columnconfigure(1, weight=1)
        coll = ttk.Frame(body)
        coll.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        coll.columnconfigure(1, weight=1)
        reset = ttk.Button(body, text="Reset colors",
                           command=self._view_reset_colors)
        reset.grid(row=3, column=0, sticky="w", pady=(8, 0))
        add_tip(reset, "Every color back to its default: both ramps, each "
                       "scaffold's own, and the collector, solid again.")
        self._view_cp = dict(var=var, inner=inner, coll=coll, why=None)
        self._view_cp_fill()
        self._view_cp_collector()

    def _view_cp_open(self):
        """The open panel's parts, or None."""
        cp = getattr(self, "_view_cp", None)
        try:
            return cp if cp and cp["inner"].winfo_exists() else None
        except tk.TclError:
            return None

    def _view_cp_chosen(self):
        cp = self._view_cp_open()
        mode = cp["var"].get()
        cp["why"] = self._view_mode_why(mode)
        if cp["why"]:
            cp["var"].set(self._view_mode)
            self._view_cp_fill()
        else:
            self._view_set_mode(mode)

    def _view_cp_rows(self):
        """What the panel lists for the colouring now: [(hex, words,
        set(hex))], one a scaffold or one end of a ramp."""
        panel = self._gce_pv_panel
        view, ok = self._view_state()
        if view is None or not ok:
            return []
        st = panel._state
        mode = self._view_mode
        if mode == "scaffold":
            cols = (st.get("aux_meta") or {}).get("scaffold_rgb") or []
            return [(self._view_hex(cols[min(i, len(cols) - 1)]), name,
                     lambda hx, i=i: self._view_set_scaffold(i, hx))
                    for i, name in enumerate(self._view_scaffold_names())
                    if cols]
        tab = view.table(mode)
        ends = view.custom.get(mode) or (tuple(tab[0]), tuple(tab[-1]))
        if mode == "layer":
            words = ("First layer (1)", f"Last layer ({len(self._view_layers)})")
        else:
            lo, hi = view.scale(st, "speed")
            words = (f"Slowest: {lo:.1f} mm/s (F{lo * 60:.0f})",
                     f"Fastest: {hi:.1f} mm/s (F{hi * 60:.0f})")
        return [(self._view_hex(ends[k]), words[k],
                 lambda hx, k=k: self._view_set_end(mode, k, hx))
                for k in (0, 1)]

    def _view_cp_fill(self):
        cp = self._view_cp_open()
        if cp is None:
            return
        inner = cp["inner"]
        for w in inner.winfo_children():
            w.destroy()
        mode = self._view_mode
        cp["var"].set(mode)
        ttk.Label(inner, text=cp["why"] or _COLOR_TIP[mode],
                  style="Hint.TLabel", wraplength=300, justify="left").grid(
                      row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        cp["why"] = None
        rows = self._view_cp_rows()
        view, _ok = self._view_state()
        row = 1
        if rows and mode != "scaffold":
            strip = tk.Canvas(inner, height=12, highlightthickness=0)
            strip.grid(row=row, column=0, columnspan=3, sticky="ew",
                       pady=(0, 6))
            strip.bind("<Configure>", lambda e, s=strip, t=view.table(mode):
                       self._view_cp_ramp(s, t, e.width), add="+")
            row += 1
        for hx, words, put in rows:
            self._view_cp_row(inner, row, hx, words, put)
            row += 1
        for w in self._gce_walk(inner):
            w.bind("<Escape>", self._view_cp_esc, add="+")
        self._gce_layout_soon()

    def _view_cp_collector(self):
        """The panel's collector rows, the same under every colouring: shown
        or not (the Collector row's own toggle), its colour, how see-through."""
        cp = self._view_cp_open()
        holder = getattr(self, "_gce_pv_holder", None)
        if cp is None or holder is None:
            return
        coll = cp["coll"]
        for w in coll.winfo_children():
            w.destroy()
        rgb, opacity = holder._look
        gui_widgets.hairline(coll).grid(row=0, column=0, columnspan=3,
                                        sticky="ew", pady=(0, 8))
        ttk.Label(coll, text="Collector", font="MewSection").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 4))
        shown = self._gce_c_eye_var()
        if isinstance(shown, tk.Variable):
            c = ttk.Checkbutton(coll, text="Show", variable=shown,
                                command=self._gce_vis_apply)
            gui_form.exempt(c, "display toggle")
            c.grid(row=1, column=2, sticky="e", pady=(0, 4))
        self._view_cp_row(coll, 2, self._view_hex(rgb), "Color",
                          self._view_set_collector)
        row = ttk.Frame(coll)
        row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text="Transparency").grid(row=0, column=0, sticky="w")
        pct = tk.StringVar(value=f"{(1.0 - opacity) * 100:.0f} %")

        def moved(value):
            t = float(value)
            pct.set(f"{t:.0f} %")
            holder.set_look(holder._look[0], 1.0 - t / 100.0)
        scale = ttk.Scale(row, from_=0, to=100, value=(1.0 - opacity) * 100,
                          command=moved)
        gui_form.exempt(scale, "collector transparency")
        scale.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        scale.bind("<ButtonRelease-1>", lambda _e: self._view_save_collector(),
                   add="+")
        add_tip(scale, "0 % is solid. Fibers behind the collector show "
                       "through as it goes up.")
        lab = ttk.Label(row, textvariable=pct, width=5, anchor="e")
        gui_form.mark(lab, "live")
        lab.grid(row=0, column=2, sticky="e")
        for w in self._gce_walk(coll):
            w.bind("<Escape>", self._view_cp_esc, add="+")
        self._gce_layout_soon()

    def _view_set_collector(self, hx):
        holder = self._gce_pv_holder
        holder.set_look(self._view_rgb(hx), holder._look[1])
        self._view_save_collector()
        self._view_cp_collector()

    def _view_save_collector(self):
        rgb, opacity = self._gce_pv_holder._look
        self._gce_prefs_set(collector_color=self._view_hex(rgb),
                            collector_opacity=opacity)

    def _view_load_collector(self):
        holder = getattr(self, "_gce_pv_holder", None)
        if holder is None:
            return
        prefs = self._gce_prefs()
        opacity = prefs.get("collector_opacity")
        holder.set_look(self._view_rgb(prefs.get("collector_color")),
                        1.0 if opacity is None else opacity)

    def _view_cp_esc(self, _e=None):
        self._gce_host_close()
        return "break"

    def _view_cp_ramp(self, canvas, table, wide):
        canvas.delete("all")
        n = len(table)
        for x in range(wide):
            canvas.create_line(x, 0, x, 12, fill=self._view_hex(
                table[min(int(x / wide * n), n - 1)]))

    def _view_cp_row(self, parent, row, hx, words, put):
        """A colour's row: its swatch (a click opens the colour dialog), what
        it colours, and its exact value, typed."""
        sw = tk.Label(parent, width=3, relief="solid", borderwidth=1,
                      background=hx, cursor="hand2")
        gui_form.mark(sw, "swatch")
        sw.grid(row=row, column=0, sticky="w", pady=2)

        def pick(_e=None):
            got = self._view_ask_color(hx)
            if got:
                put(got)
        sw.bind("<ButtonRelease-1>", pick, add="+")
        add_tip(sw, "Pick this color.")
        ttk.Label(parent, text=words).grid(row=row, column=1, sticky="w",
                                           padx=(8, 8))
        var = tk.StringVar(value=hx)
        ent = ttk.Entry(parent, textvariable=var, width=8)
        ent.grid(row=row, column=2, sticky="e")
        gui_form.exempt(ent, "color value")

        def typed(_e=None):
            got = var.get().strip().lower()
            if not got.startswith("#"):
                got = "#" + got
            if got != hx and self._view_rgb(got) is not None:
                put(got)
        ent.bind("<Return>", typed, add="+")
        ent.bind("<FocusOut>", typed, add="+")

    # -- user-chosen colours ------------------------------------------------- #
    @staticmethod
    def _view_hex(rgb):
        return "#%02x%02x%02x" % tuple(int(round(float(v) * 255))
                                       for v in rgb)

    @staticmethod
    def _view_rgb(hx):
        try:
            h = str(hx).strip().lstrip("#")
            if len(h) == 6:
                return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
                        int(h[4:6], 16) / 255.0)
        except ValueError:
            pass
        return None

    def _view_layer_table(self, nodes, clock):
        """The slider's and the colouring's layers (`view_tools.layer_table`):
        a scaffold per group. Calibrate counts a slide as one."""
        return view_tools.layer_table(nodes, clock)

    def _view_scaffold_names(self):
        names = []
        for r in self._view_layers:
            while r["scaffold"] > len(names):
                names.append(r["name"] if r["scaffold"] == len(names) + 1
                             else f"Scaffold {len(names) + 1}")
        return names

    def _view_load_custom(self, view):
        """The ramp ends the user chose, from the page's prefs (once)."""
        if view is None or getattr(view, "_custom_loaded", False):
            return
        view._custom_loaded = True
        prefs = self._gce_prefs()
        for mode in ("layer", "speed"):
            pair = prefs.get("color_" + mode)
            if isinstance(pair, list) and len(pair) == 2:
                a, b = self._view_rgb(pair[0]), self._view_rgb(pair[1])
                if a is not None and b is not None:
                    view.custom[mode] = (a, b)

    def _view_ask_color(self, initial):
        """The colour dialog the design's own colour row uses."""
        from tkinter import colorchooser
        _rgb, hx = colorchooser.askcolor(color=initial, parent=self.root)
        return hx or None

    def _view_note_color(self, rgbs):
        """Say so when a pick is close to the selection's yellow. The pick
        is kept as chosen."""
        yel = np.array((1.0, 0.95, 0.0))
        if any(float(np.linalg.norm(np.asarray(c) - yel)) < 0.3
               for c in rgbs):
            self._gce_pv_panel.set_status(
                "That color is close to the yellow a selected fiber is lit "
                "in, so a selection will be harder to see against it.")

    def _view_set_end(self, mode, which, hx):
        view, ok = self._view_state()
        if not ok:
            return
        tab = view.table(mode)
        new = list(view.custom.get(mode) or (tuple(tab[0]), tuple(tab[-1])))
        new[which] = self._view_rgb(hx)
        self._view_set_custom(mode, new)

    def _view_set_custom(self, mode, ends):
        """Use `ends` (two RGB triples) for a ramp, or the default (None):
        the picture and the legend change at once, and it is remembered."""
        view, _ok = self._view_state()
        if view is None:
            return
        view.set_custom(mode, ends)
        self._gce_prefs_set(**{"color_" + mode: None if ends is None else
                               [self._view_hex(c) for c in ends]})
        self._view_colour_update()
        if ends is not None:
            self._view_note_color(ends)

    def _view_set_scaffold(self, i, hx):
        sv = getattr(self, "gce_scaf_vars", None) or []
        if i < len(sv):
            sv[i]["color"].set(hx)          # the design's own colour value
        else:
            extra = dict(self._gce_prefs().get("color_scaf_extra") or {})
            extra[str(i)] = hx
            self._gce_prefs_set(color_scaf_extra=extra)
        self._view_scaf_refresh()
        self._view_note_color([self._view_rgb(hx)])

    def _view_scaf_refresh(self):
        panel = self._gce_pv_panel
        st = panel._state or {}
        meta = st.get("aux_meta")
        view, ok = self._view_state()
        if meta is None or view is None:
            return
        meta["scaffold_rgb"] = self._view_scaffold_colors(
            meta.get("nscaf", 1), st.get("color"))
        if self._view_mode == "scaffold":
            if not view.paint(st) and meta["scaffold_rgb"]:
                # one scaffold: the fibre is one colour, the actor's own
                st["color"] = meta["scaffold_rgb"][0]
                for act in (panel._actor, getattr(panel, "_tube_actor",
                                                  None)):
                    if act is not None:
                        act.GetProperty().SetColor(*st["color"])
        panel._render_soon()
        self._view_colour_update()

    def _view_reset_colors(self):
        """Every colour back to its default: both ramps, and each scaffold's
        colour to the one a new design gives it."""
        view, _ok = self._view_state()
        if view is not None:
            for mode in ("layer", "speed"):
                view.set_custom(mode, None)
        self._gce_prefs_set(color_layer=None, color_speed=None,
                            color_scaf_extra=None, collector_color=None,
                            collector_opacity=None)
        self._gce_pv_holder.set_look()
        self._view_cp_collector()
        from gui_common import SCAFFOLD_COLORS, SINGLE_SCAFFOLD_COLOR
        sv = getattr(self, "gce_scaf_vars", None) or []
        try:
            count = int(self.gce_s_count.get())
        except (AttributeError, ValueError, tk.TclError):
            count = len(sv)
        for i, v in enumerate(sv):
            v["color"].set(SINGLE_SCAFFOLD_COLOR if (i == 0 and count == 1)
                           else SCAFFOLD_COLORS[i % len(SCAFFOLD_COLORS)])
        self._view_scaf_refresh()
        self._view_colour_update()

    def _view_mode_why(self, mode):
        """Why `mode` cannot colour this picture, or None."""
        view, ok = self._view_state()
        if mode == "scaffold":
            return None
        if not ok:
            return self._view_why or "nothing is drawn yet"
        if mode == "layer" and len(self._view_layers) < 2:
            return "this print has one layer"
        if mode == "speed" and (self._view_state()[0] is None or
                                self._view_scale_of("speed") is None):
            return getattr(self, "_view_why_speed", None) or \
                "no feed (F) words in this text"
        return None

    def _view_scale_of(self, mode):
        view, _ok = self._view_state()
        return view.scale(self._gce_pv_panel._state, mode) if view else None

    def _view_set_mode(self, mode):
        if mode not in view_tools.MODES or self._view_mode_why(mode):
            return
        self._view_mode = mode
        self._gce_prefs_set(color_by=mode)
        view, _ok = self._view_state()
        if view is not None:
            view.set_mode(mode)
        self._view_colour_update()

    def _view_colour_update(self):
        """After anything that changes the colouring: the choice falls back
        quietly if the picture can no longer show it (a new text lost its F
        words), and the open panel follows."""
        view, ok = self._view_state()
        if ok and self._view_mode_why(self._view_mode):
            self._view_mode = "scaffold"
            view.set_mode("scaffold")
        self._view_cp_fill()

    # ------------------------------------------------------------------ #
    # measure
    # ------------------------------------------------------------------ #
    _view_measuring = False
    _view_why_speed = None

    def _view_measure_why(self):
        b = getattr(self, "_view_measure_btn", None)
        if b is None:
            return
        panel = self._gce_pv_panel
        ready = panel is not None and panel._win is not None \
            and panel._state is not None and bool(panel._state.get("paths"))
        b.set_why(None if ready else "Nothing is drawn yet to measure.")

    def _view_measure_toggle(self):
        self._view_measure_set(not self._view_measuring)

    def _view_measure_set(self, on):
        panel = self._gce_pv_panel
        if on and (panel is None or panel._win is None
                   or not (panel._state or {}).get("paths")):
            self._view_measure_why()
            return
        if on and getattr(self, "_jet", None) is not None:
            self._jet_stop()
        view = panel._view() if panel is not None else None
        if view is None:
            return
        self._view_measuring = bool(on)
        view.on_measure = self._view_on_measure if on else None
        view.set_measuring(on)
        self._view_measure_btn.set_on(on)
        panel._host.configure(cursor="plus" if on else "crosshair")
        if on:
            self._view_hover_text = ""
            self._view_measure_text()
        self._view_place()

    def _view_measure_clear(self):
        view, _ok = self._view_state()
        if view is not None:
            view.clear_points()
        self._view_hover_text = ""
        self._view_measure_text()

    def _view_on_measure(self, event, info):
        if event == "hover":
            self._view_hover_text = self._view_probe_text(info)
        elif event == "miss":
            self._view_hover_text = "Nothing there to snap to."
        self._view_measure_text()

    def _view_probe_text(self, info):
        if info is None:
            return "Nothing to snap to here."
        if info["kind"] == "plate":
            return "On the plate's top face."
        if info["kind"] == "rod":
            return "On the mandrel's surface."
        return "On " + self._view_fiber_text(info["hit"])

    def _view_fiber_text(self, hit):
        """'a fibre, layer 3 of 10, g-code line 5,812' for a pick."""
        panel = self._gce_pv_panel
        meta = (panel._state or {}).get("meta") or {}
        t = preview_pick.time_at(meta.get("ptimes"), hit)
        clock = self._view_clock
        if t is None or clock is None or not self._view_layers:
            return "a fiber."
        line = preview_pick.line_at_time(clock, t)
        if line is None:
            return "a fiber."
        li = self._view_layer_of_line(line)
        return (f"a fiber, {self._view_layer_text(li)}, g-code line "
                f"{_fmt_int(line)}.")

    def _view_measure_numbers(self):
        """dict of the measurement: the two points placed, or the first and
        the snap under the pointer."""
        view, _ok = self._view_state()
        pair = view.pair() if view is not None else None
        if pair is None:
            return None
        return view_tools.measure_numbers(pair[0], pair[1], view.rod_radius())

    def _view_measure_text(self):
        """The box: the distance and its parts first, then the two points,
        then what the next click does."""
        view, _ok = self._view_state()
        if view is None:
            return
        pts, probes = view.pts, view.probes
        nums = self._view_measure_numbers()
        self._view_m_dist.set(f"{_fmt_mm(nums['dist'])} mm" if nums else "–")
        for ax, var in self._view_m_d.items():
            var.set(f"{nums['d' + ax]:+.3f}" if nums else "–")
        what = {"plate": "On the plate", "rod": "On the mandrel"}
        for k, var in enumerate(self._view_m_pts):
            if k >= len(pts):
                var.set(f"Point {k + 1}: –")
                continue
            p, pr = pts[k], probes[k]
            where = (self._view_fiber_text(pr["hit"]).rstrip(".").replace(
                "a fiber, ", "").capitalize() if pr["kind"] == "fiber"
                else what[pr["kind"]])
            var.set(f"Point {k + 1}: {_fmt_mm(p[0])}, {_fmt_mm(p[1])}, "
                    f"{_fmt_mm(p[2])}\n{where}")
        note = {0: "Click a point on a fiber or the collector.",
                1: "Click a second point."}.get(len(pts),
                                                "Click again to start over.")
        hover = getattr(self, "_view_hover_text", "")
        self._view_m_note.set(hover if hover and len(pts) < 2 else note)
        self._view_mtext_last = self._view_copy_text(nums, pts)

    @staticmethod
    def _view_copy_text(nums, pts):
        if not nums or len(pts) < 2:
            return ""
        return (f"Point 1: ({pts[0][0]:.4f}, {pts[0][1]:.4f}, "
                f"{pts[0][2]:.4f}) mm\n"
                f"Point 2: ({pts[1][0]:.4f}, {pts[1][1]:.4f}, "
                f"{pts[1][2]:.4f}) mm\n"
                f"Distance: {nums['dist']:.4f} mm\n"
                f"dX: {nums['dx']:.4f} mm\ndY: {nums['dy']:.4f} mm\n"
                f"dZ: {nums['dz']:.4f} mm")

    def _view_copy(self):
        text = self._view_mtext_last
        if not text:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text.strip())
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    # fit
    # ------------------------------------------------------------------ #
    def _view_fit(self):
        """Z: frame the selected fibres (a layer, a scaffold, one move); with
        nothing selected, the whole print as it is shown. Keeps the view
        direction, so it works with the ViewCube and the orbit pivot, and on
        a tube."""
        panel = getattr(self, "_gce_pv_panel", None)
        if panel is None or panel._state is None:
            return False
        pts, what = [], None
        hl = getattr(panel, "_hl", None)
        if hl is not None and hl.get("data") and hl["data"][0]:
            # a lit FIBRE; a lone dot (the caret resting on a marker move)
            # is not a selection worth zooming to
            polys, dots = hl["data"]
            for p in polys:
                pts.append(np.asarray(p, np.float64).reshape(-1, 3))
            for d in dots:
                pts.append(np.asarray(d, np.float64).reshape(1, 3))
            if pts:
                what = "the selection"
        if not pts:
            st = panel._state
            allp = np.concatenate([np.asarray(p, np.float64)
                                   for p in st["paths"]], axis=0)
            aux = st.get("aux") or {}
            view = panel.view_tools
            if view is not None and view.window is not None \
                    and aux.get("mew_view_t") is not None \
                    and len(aux["mew_view_t"]) == len(allp):
                T = aux["mew_view_t"] + float(st.get("aux_base", 0.0))
                keep = (T >= view.window[0]) & (T <= view.window[1])
                if keep.any():
                    allp = allp[keep]
            pts.append(allp)
            what = "the whole print" if (view is None
                                         or view.window is None) \
                else "the layers shown"
        target = np.concatenate(pts)
        scene = getattr(self, "_jet", None)
        if scene is not None and scene.mandrel:
            target = scene.to_scene(target)
            rod = getattr(panel, "_mandrel_actor", None)
            if what != "the selection" and rod is not None and rod.GetVisibility():
                b = rod.GetBounds()
                target = np.vstack((target, (b[0], b[2], b[4]),
                                    (b[1], b[3], b[5])))
        ok = panel.look_at(target)
        panel.set_status(f"Fit: framed {what}." if ok
                         else "Fit: nothing to frame yet.")
        return ok

    # ------------------------------------------------------------------ #
    # keys
    # ------------------------------------------------------------------ #
    def _view_typing(self, e):
        try:
            return e.widget.winfo_class() in self._GCE_TYPING
        except (tk.TclError, AttributeError):
            return True

    def _view_key_letter(self, e):
        if not getattr(self, "_view_panel_ok", False) \
                or not self._gce_page_shown() or self._view_typing(e):
            return None
        if int(getattr(e, "state", 0) or 0) & _NOT_TYPING_MASK:
            return None
        key = (e.keysym or "").lower()
        if key == "i":
            self._view_measure_toggle()
        elif key == "z":
            self._view_fit()
        elif key == "k":
            self._gce_panel_open("colour")
        elif key == "c":
            order = [m for m in view_tools.MODES
                     if m == self._view_mode or not self._view_mode_why(m)]
            i = order.index(self._view_mode) if self._view_mode in order \
                else -1
            self._view_set_mode(order[(i + 1) % len(order)])
        else:
            return None
        return "break"

    def _view_has_focus(self, e):
        """The arrow keys (and Space) are the picture's only while it, or the
        bar's slider, holds the focus: the tree and the text use them for
        their own rows and lines."""
        panel = getattr(self, "_gce_pv_panel", None)
        w = e.widget
        host = panel._host if panel is not None else None
        return w is host or w is getattr(self, "_view_scale", None)

    def _view_key_arrow(self, e):
        """Up / Right and Down / Left step the slider a layer (Shift: ten),
        Home goes to layer 0 (the empty collector) and End to the whole
        print. While the jet runs the step pauses it and jumps there, as the
        slider does."""
        if not getattr(self, "_view_panel_ok", False) \
                or not self._gce_page_shown() or self._view_typing(e) \
                or not self._view_has_focus(e):
            return None
        ok = self._view_state()[1]
        n = len(self._view_layers)
        if not ok or n < 1 or not self._view_scale.enabled:
            return None
        sym = e.keysym
        step = 10 if int(getattr(e, "state", 0) or 0) & 0x1 else 1
        cur = self._view_cur_layer()
        if sym == "Home":
            k = 0
        elif sym == "End":
            k = n
        elif sym in ("Up", "Right"):
            k = cur + step
        elif sym in ("Down", "Left"):
            k = cur - step
        else:
            return None
        self._view_scale.set_pos(min(max(k, 0), n))
        return "break"

    def _view_key_esc(self, e):
        if not getattr(self, "_view_panel_ok", False) \
                or not self._gce_page_shown() or not self._view_measuring:
            return None
        self._view_measure_set(False)
        return "break"
