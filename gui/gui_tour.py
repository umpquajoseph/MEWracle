#!/usr/bin/env python3
"""gui_tour.py -- MODERN-UI ONLY: a short guided tour of the software, started
from Home's "Take the tour".

Ten steps, in the order a print happens, left to right along the top row,
ending back on Home. Each
one goes to its page, rings one part of the window in the Ctrl+F finder's
highlighter yellow and puts a small callout beside it: a title, two or three
sentences, Back / Next and Close. Right or Enter steps on, Left steps back, Esc
closes.

Tkinter has no overlay or spotlight primitive, so the callout and the ring are
borderless Toplevels kept over the main window and re-placed when it moves. The
tour never starts on its own. Home is where the app opens, and the link sits
under the welcome line, so a first-time user sees it without a first-run flag
to persist. It changes no setting and launches no worker. Every step opens the
page it talks about (reported: Calibrate and Simulation stayed on the page
before); Simulation then shows its last result, or says there is none yet.

The classic (developer) UI never imports this module."""

from gui_common import *

import gui_fonts
import gui_tips
import gui_widgets

# (page id to show first or None, anchor resolver, title, text). The resolver
# is a method name on the app returning the widget to mark, or None to skip
# the step (a build without that page). Another tour (the G-code page's,
# `gui_gcode_tutorial`) passes its own steps to `start_tour`: there the
# resolver may also be a callable, and a fifth item, an action
# ``(button label, callable)``, adds a button that performs the step.
STEPS = (
    ("home", "_tour_anchor_nav",
     "The top row is the path",
     "MEWracle turns a scaffold design into g-code your printer can run, "
     "and shows how the fiber will land first. The row is the job, left to "
     "right: Setup, Calibrate, Design, Simulation."),
    ("machine", "_tour_anchor_machine",
     "Set up your printer once",
     "Describe the collector, the G-code zero and the g-code flavor. "
     "Instructions says what is left. Save it under Presets and it loads "
     "at every start."),
    ("physics", "_tour_anchor_physics",
     "Physics: this print's settings",
     "Gap, voltage, pressure and temperature for this print. The cards "
     "beside them hold the jet lag, sagging and fusion values, marked "
     "(default) while they are the lab's."),
    ("calibration", "_tour_anchor_calibrate",
     "Calibrate to your printer",
     "Optional for a first print. Print a test pattern, measure it, enter "
     "the number and press Apply: the Physics card of the same name then "
     "reads measured."),
    ("gcode", "_tour_anchor_design",
     "Design your print",
     "Parameters, in the ribbon, sets the scaffold's size, layers, angles "
     "and fiber spacing. The tree lists the print and the picture shows it. "
     "Tutorial, top right, builds a first scaffold with you."),
    ("gcode", "_tour_anchor_generate",
     "Generate",
     "Builds the g-code and the deposited fiber of the design. Ctrl+G does "
     "the same."),
    ("gcode", "_tour_anchor_summary",
     "Up to date, or not",
     "Says whether the last Generate still matches your settings. Saving "
     "and sending use that result."),
    ("view", "_tour_anchor_simulation",
     "Check it in Simulation",
     "When Generate finishes, Simulation replays the print in 3-D. Press "
     "Play to watch the printhead lay the fiber and check it lands where "
     "you meant."),
    ("gcode", "_tour_anchor_save",
     "Save it or send it",
     "Generated files are temporary. Save g-code as… keeps one; Send to "
     "printer uploads it through Printer link, under More."),
    ("home", "_tour_anchor_steps",
     "Home keeps your place",
     "Each step gets a ✓ when it is done, and Next points at the one to "
     "do. Ctrl+F finds any setting by name."),
)

WRAP = 300


class _TourMixin:
    # ------------------------------------------------------------------ #
    # anchors
    # ------------------------------------------------------------------ #
    def _tour_anchor_nav(self):
        return getattr(self, "_nav_row", None)

    def _tour_anchor_machine(self):
        return getattr(self, "_machine_grid", None)

    def _tour_anchor_physics(self):
        return getattr(self, "_physics_grid", None)

    def _tour_anchor_steps(self):
        steps = getattr(self, "_home_steps", None)
        if not steps:
            return None
        return next(iter(steps.values())).master

    def _tour_anchor_calibrate(self):
        # the Calibrate ribbon (Type, Generate, Measurements, Apply), on its
        # page; the tab only when that page is not built
        lead = getattr(self, "_cal_groups", None)
        if lead is not None and lead.winfo_ismapped():
            return lead
        return self._nav_btns.get("calibrate")

    def _tour_anchor_design(self):
        return getattr(self, "_gce_ribbon", None)

    def _tour_anchor_generate(self):
        return getattr(self, "run_btn", None)

    def _tour_anchor_summary(self):
        # the words "Up to date" themselves, top right: ringing the whole
        # strip put the callout at its far left, mid-row (reported)
        lbl = getattr(self, "_sum_state_lbl", None)
        if lbl is not None and lbl.winfo_ismapped():
            return lbl
        return getattr(self, "_sum_box", None)

    def _tour_anchor_simulation(self):
        # the Play bar on the Simulation page, which the step tells you to use
        panel = getattr(self, "_unified_panel", None)
        bar = getattr(panel, "transport", None)
        if bar is not None and bar.winfo_ismapped():
            return bar
        return self._nav_btns.get("simulation")

    def _tour_anchor_save(self):
        btn = getattr(self, "save_gcode_btn", None)
        return btn.master if btn is not None else None

    # ------------------------------------------------------------------ #
    # the tour
    # ------------------------------------------------------------------ #
    def start_tour(self, _event=None, steps=None, on_step=None):
        """Open the tour on its first step. Safe to call while one is open.
        `steps` replaces Home's STEPS; `on_step(i)` runs before step i is
        shown (i None: the tour closed)."""
        self._tour_close()
        self._tour_on_step = on_step
        self._tour_steps = [s for s in (STEPS if steps is None else steps)
                            if callable(s[1])
                            or callable(getattr(self, s[1], None))]
        self._tour_i = 0
        if not getattr(self, "_tour_bound", False):
            self.root.bind("<Configure>", self._tour_reposition, add="+")
            self._tour_bound = True
        self._tour_show(0)

    def _tour_open(self):
        win = getattr(self, "_tour_win", None)
        try:
            return win is not None and bool(win.winfo_exists())
        except tk.TclError:
            return False

    def _tour_show(self, i):
        steps = self._tour_steps
        if not steps:
            return
        i = max(0, min(len(steps) - 1, i))
        self._tour_i = i
        pid, resolver, title, text = steps[i][:4]
        action = steps[i][4] if len(steps[i]) > 4 else None
        self._tour_unring()
        if getattr(self, "_tour_on_step", None) is not None:
            self._tour_on_step(i)
        self._tour_moving = True
        try:
            if pid is not None:
                self._nav_select(pid)
            self.root.update_idletasks()
        except tk.TclError:
            return
        finally:
            self._tour_moving = False
        self._tour_page = self._tour_where()
        self._tour_build_callout(i, len(steps), title, text, action)
        self._tour_reanchor()

    def _tour_where(self):
        """The page on screen; Design and Calibrate share one frame, so the
        Calibrate mode is part of it."""
        try:
            return (self.nb.select(), bool(getattr(self, "_cal_on", False)))
        except (AttributeError, tk.TclError):
            return None

    def _tour_follow_page(self):
        """The user changed page with a step open (the shell calls this on
        every page change). Off the step's page the callout and its ring
        hide, since what they point at is gone; back on it, the same step
        shows again."""
        if getattr(self, "_tour_moving", False) or not self._tour_open():
            return
        self._tour_reanchor()

    def _tour_reanchor(self):
        """Resolve the step's anchor again and re-place the ring and the
        callout: after an action has opened what the step points at."""
        if not self._tour_open():
            return
        resolver = self._tour_steps[self._tour_i][1]
        try:
            self.root.update_idletasks()
            anchor = (resolver if callable(resolver)
                      else getattr(self, resolver))()
        except tk.TclError:
            anchor = None
        self._tour_anchor = anchor
        self._tour_reposition()

    # -- the mark ------------------------------------------------------- #
    # Four thin borderless windows in SCREEN coordinates, just outside the
    # anchor, in the finder's highlighter yellow. Not `gui_finder.ring`: its
    # fallback band is placed inside the control's own master, which suits a
    # field, but on a whole card grid inside a scrolling page it landed over
    # the card titles. Drawn outside the anchor it can never cover what the
    # step points at, and it is clamped to the window.
    _RING_T, _RING_PAD = 3, 4

    def _tour_ring(self):
        self._tour_unring()
        a = getattr(self, "_tour_anchor", None)
        try:
            if a is None or not a.winfo_ismapped():
                return
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            t, pad = self._RING_T, self._RING_PAD
            x0 = max(rx, a.winfo_rootx() - pad - t)
            y0 = max(ry, a.winfo_rooty() - pad - t)
            x1 = min(rx + rw, a.winfo_rootx() + a.winfo_width() + pad + t)
            y1 = min(ry + rh, a.winfo_rooty() + a.winfo_height() + pad + t)
        except tk.TclError:
            return
        if x1 - x0 < 2 * t or y1 - y0 < 2 * t:
            return
        colour = gui_widgets.palette().get("find_hl", "#f2c200")
        bars = []
        for (x, y, w, h) in ((x0, y0, x1 - x0, t), (x0, y1 - t, x1 - x0, t),
                             (x0, y0, t, y1 - y0), (x1 - t, y0, t, y1 - y0)):
            try:
                bar = tk.Toplevel(self.root, background=colour)
                bar.wm_overrideredirect(True)
                bar.transient(self.root)
                bar.wm_geometry("%dx%d+%d+%d" % (w, h, x, y))
                bars.append(bar)
            except tk.TclError:
                break
        self._tour_bars = bars

    def _tour_unring(self):
        for bar in getattr(self, "_tour_bars", None) or ():
            try:
                bar.destroy()
            except tk.TclError:
                pass
        self._tour_bars = []

    def _tour_marked(self):
        return bool(getattr(self, "_tour_bars", None))

    def _tour_build_callout(self, i, n, title, text, action=None):
        win = getattr(self, "_tour_win", None)
        if self._tour_open():
            for c in win.winfo_children():
                c.destroy()
        else:
            win = tk.Toplevel(self.root)
            # withdrawn until _tour_reposition has placed it: a new borderless
            # window shown at once ignored its first geometry and sat at the
            # screen's corner
            win.withdraw()
            win.wm_overrideredirect(True)
            try:
                win.transient(self.root)
            except tk.TclError:
                pass
            self._tour_win = win
            win.bind("<Escape>", lambda _e: self._tour_close())
            win.bind("<Right>", lambda _e: self._tour_next())
            win.bind("<Return>", lambda _e: self._tour_next())
            win.bind("<Left>", lambda _e: self._tour_back())
        pal = gui_tips._TIP
        border = tk.Frame(win, background=pal["border"], padx=1, pady=1)
        border.pack()
        body = tk.Frame(border, background=pal["bg"], padx=14, pady=10)
        body.pack()
        fg, bg = pal["fg"], pal["bg"]
        tk.Label(body, text="%d of %d" % (i + 1, n), background=bg,
                 foreground=gui_widgets.palette().get("hint", "#646a71"),
                 font=gui_tips._font(win, "MewHint", ("Segoe UI", 9))
                 ).pack(anchor="w")
        tk.Label(body, text=title, background=bg, foreground=fg,
                 justify="left",
                 font=gui_tips._font(win, "MewSection",
                                     ("Segoe UI", 10, "bold"))
                 ).pack(anchor="w", pady=(2, 4))
        tk.Label(body, text=text, background=bg, foreground=fg,
                 justify="left", wraplength=WRAP,
                 font=gui_tips._font(win, "MewHint", ("Segoe UI", 9))
                 ).pack(anchor="w")
        row = tk.Frame(body, background=bg)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Close", command=self._tour_close).pack(
            side="left")
        if action is not None:
            label, fn = action
            self._tour_action_btn = ttk.Button(
                row, text=label, command=lambda: self._tour_act(fn, label))
            self._tour_action_btn.pack(side="left",
                                       padx=(gui_fonts.SP["s"], 0))
        last = i == n - 1
        ttk.Button(row, text="Done" if last else "Next",
                   style="Primary.TButton",
                   command=self._tour_close if last else self._tour_next
                   ).pack(side="right")
        back = ttk.Button(row, text="Back", command=self._tour_back)
        back.pack(side="right", padx=(0, gui_fonts.SP["s"]))
        if i == 0:
            back.state(["disabled"])
        win.focus_set()

    def _tour_reposition(self, _e=None):
        """Put the callout beside its anchor: right of it if there is room in
        the window, else left, else below, else above; always inside the
        window. Also re-run when the main window moves or resizes."""
        if not self._tour_open():
            return
        # every descendant's <Configure> reaches a binding on the toplevel;
        # only the window's own move or resize matters here
        if _e is not None and getattr(_e, "widget", None) is not self.root:
            return
        win = self._tour_win
        if self._tour_where() != getattr(self, "_tour_page", None):
            # the user went to another page: see _tour_follow_page
            self._tour_unring()
            try:
                win.withdraw()
            except tk.TclError:
                pass
            return
        self._tour_ring()
        try:
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            a = getattr(self, "_tour_anchor", None)
            gap = 14
            if a is not None and a.winfo_ismapped():
                ax, ay = a.winfo_rootx(), a.winfo_rooty()
                aw, ah = a.winfo_width(), a.winfo_height()
                if ax + aw + gap + w <= rx + rw:
                    x, y = ax + aw + gap, ay
                elif ax - gap - w >= rx:
                    x, y = ax - gap - w, ay
                elif ay + ah + gap + h <= ry + rh:
                    x, y = ax, ay + ah + gap
                else:
                    x, y = ax, ay - gap - h
            else:
                x, y = rx + (rw - w) // 2, ry + (rh - h) // 2
            x = max(rx + 8, min(x, rx + rw - w - 8))
            y = max(ry + 8, min(y, ry + rh - h - 8))
            win.wm_geometry("+%d+%d" % (x, y))
            # apply the move before raising: on Windows a lift or focus_set
            # with the move still pending dropped it, and every step's
            # callout stayed where the first one was put
            win.update_idletasks()
            if win.state() == "withdrawn":
                win.deiconify()
            win.lift()
            win.focus_set()
        except tk.TclError:
            pass

    def _tour_act(self, fn, label):
        """Run a step's action. It may finish later (it calls the `done`
        it is given); the button stays greyed until then."""
        btn = getattr(self, "_tour_action_btn", None)
        i = self._tour_i

        def done(ok=True):
            if not self._tour_open() or self._tour_i != i:
                return
            try:
                btn.configure(text="Done ✓" if ok else label)
                if not ok:
                    btn.state(["!disabled"])
            except tk.TclError:
                pass
            self._tour_reanchor()
        try:
            btn.state(["disabled"])
            btn.configure(text="Working…")
        except tk.TclError:
            pass
        fn(done)

    def _tour_next(self):
        if self._tour_i >= len(self._tour_steps) - 1:
            self._tour_close()
        else:
            self._tour_show(self._tour_i + 1)

    def _tour_back(self):
        if self._tour_i > 0:
            self._tour_show(self._tour_i - 1)

    def _tour_close(self):
        cb = getattr(self, "_tour_on_step", None)
        self._tour_on_step = None
        if cb is not None and self._tour_open():
            cb(None)
        self._tour_unring()
        win = getattr(self, "_tour_win", None)
        self._tour_win = None
        self._tour_anchor = None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass
