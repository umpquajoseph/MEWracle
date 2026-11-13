#!/usr/bin/env python3
"""gui_machine_link.py -- MODERN-UI ONLY: the "Machine link" page (physics build).

The GUI half of the live digital-twin link. `_MachineLinkMixin` adds one non-input
page in a "Print" sidebar section: a left control stack beside an embedded machine
viewer (the SAME ViewerPanel the Viewer page uses, launched as
`gcode_player --machine-live`, so it maximizes to native fullscreen for free). All
networking lives in moonraker_link (stdlib only); this module is pure Tk + that
engine, never VTK.

Flow: the Connect card stands up a `MoonrakerClient` + a `LinkPoller` daemon thread;
the poller pushes `LinkSnapshot`s onto a queue that a `root.after` pump drains on the
Tk thread (the gui_run reader-thread -> Queue -> after pattern), updating the
readouts, feeding the CSV recorder, and forwarding the toolhead position DOWN to the
viewer child. Nothing blocks the Tk loop: the connect probe and the file upload run on
short worker threads too.

Design rules honoured: NON-input page (it joins `_noninput_frames`, so the shared
Generate bar keeps pointing at the last design page); NO new persist-map keys
(host/port/api-key live in a per-user machine_link_prefs.json, the gui_prefs.json
pattern); the live view is a MODE of gcode_player, not a new viewer module.

Safety: nothing runs on connect; Upload-only is the default, with Upload & start and
Cancel behind a confirm dialog; E-stop is one immediate click, because a confirm would
defeat it; temperature setpoints are clamped to Klipper's configured max_temp; jog is
fenced on homed axes + not-printing. The Door-unlock button fires the rig's custom
DOOR_UNLOCK macro as a plain g-code line, with no confirm because unlocking is not
destructive.

Pause/resume are the one pair that is NOT a thin POST -- see the "print control"
section.
"""

import os
import gui_prefs
import queue
import re
import threading
import time

from gui_common import *  # ttk, tk, filedialog, messagebox, threading, queue, worker_cmd, _field, VScrollFrame, USER_DATA_DIR, ...

import gui_fonts
import gui_form
import gui_widgets
import mew_gcode
import gui_tips

import moonraker_link as ml

MACHINE_LINK_PREFS_PATH = os.path.join(USER_DATA_DIR, "machine_link_prefs.json")
JOG_FEED_MMPM = 600          # jog move feed, mm/min (10 mm/s)
# A custom Klipper [gcode_macro] some printers define (the lab's drives a door
# latch on GPIO21). The button shows only when the connected printer publishes
# it in /printer/gcode/help, so nobody is offered a command their printer lacks.
DOOR_UNLOCK_MACRO = "DOOR_UNLOCK"
FILES_PER_PAGE = 10                 # g-code-file browser page size (page x of y)


def _human_size(n):
    """A compact human byte size (B/KB/MB/GB) for the file list."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0 or unit == "GB":
            return "%d %s" % (int(n), unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


# The names a `[gcode_macro PAUSE]` / `[gcode_macro RESUME]` hands Klipper's ORIGINAL
# command back under via `rename_existing:`. Both orderings are in the wild (Mainsail
# and Fluidd ship `PAUSE_BASE`/`RESUME_BASE`; the Klipper docs' own example uses
# `BASE_PAUSE`/`BASE_RESUME`), so recognise either.
_BASE_CMD_CANDIDATES = {"pause": ("PAUSE_BASE", "BASE_PAUSE"),
                        "resume": ("RESUME_BASE", "BASE_RESUME")}


def _base_print_commands(help_table):
    """{"resume": "RESUME_BASE", …} for whichever renamed builtins this printer
    publishes in `/printer/gcode/help` — i.e. which of its PAUSE/RESUME are macro
    WRAPPERS rather than Klipper's own commands.

    Moonraker's /printer/print/resume runs the command NAMED `RESUME`, which on a
    wrapped rig is the macro. Those macros are `{% if %}`-heavy (the stock ones guard
    on the extruder, which a MEW rig does not have), and a guard that declines makes
    the resume a silent no-op. The base name is then the only way left to reach the
    real command, so it is worth knowing at connect time."""
    names = {str(k).upper() for k in (help_table or {})}
    out = {}
    for kind, candidates in _BASE_CMD_CANDIDATES.items():
        for cand in candidates:
            if cand in names:
                out[kind] = cand
                break
    return out


def _fmt_mtime(epoch):
    """A local date/time for a file's modified epoch, or an em dash."""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(epoch)))
    except (TypeError, ValueError, OSError):
        return "—"
# Queue-drain cadence on the Tk thread. Kept short so a fresh position sample is
# forwarded to the live view within ~one frame of arriving -- this latency is added
# on top of the ~0.25 s Moonraker feed and eats into the player's render-delay budget,
# so a tight pump keeps playback smooth (and cheap: draining an near-empty queue is
# trivial). See Player._LIVE_RENDER_DELAY, which is budgeted against this number: at
# 40 ms the delay could not safely go below ~0.31 s, and 16 ms is what buys the drop
# to 0.32 s with margin to spare. Not free-running -- it is one queue drain per tick.
_PUMP_MS = 16


# Saturated mid-tones that read on both a white and a dark plot surface, so the
# per-heater series colours stay fixed across the light/dark toggle (Fluidd does the
# same); only the background / grid / axis text follow the theme.
_GRAPH_SERIES_COLORS = ("#e8552f", "#2f7fe0", "#1fa85a", "#e0a020",
                        "#8f52e0", "#1fa8a8", "#e05fa8")
_GRAPH_MAXLEN = 5000     # per-series cap (~20 min at 4 Hz); oldest dropped past it
# Horizontal pixels per drawn envelope pair. The chart is redrawn on EVERY 4 Hz
# snapshot, so its cost is paid out of the same Tk main loop that forwards position
# samples to the live 3-D view -- an expensive redraw does not just make the chart
# sluggish, it delays the printhead feed. Drawing every retained point made that cost
# grow with uptime and eventually swamp the loop (measured, 3 series: 3.2 ms at 100
# points, 18.5 ms at 1000, 92.9 ms at 2000, 319.7 ms at the 5000 cap -- i.e. 127% of
# the 250 ms sample budget after ~20 min connected, which is the "live view drifts
# further behind the longer it runs" symptom). Screen resolution is the real limit on
# how much a strip chart can SHOW, so bucket to this pixel pitch and draw each
# bucket's min and max: the envelope (spikes included) is preserved exactly, and the
# cost becomes bounded by canvas width instead of by session length.
_GRAPH_PX_PER_BUCKET = 3


def _decimate(times, ys, buckets):
    """Reduce a (times, values) series to <= 2 points per bucket for drawing.

    Buckets are contiguous index ranges (the series is uniformly sampled at the poll
    rate, so index order is time order). Each bucket contributes its MIN and its MAX
    value, emitted in time order, so a transient spike survives decimation -- a plain
    stride would drop it. A bucket that is entirely None emits one None so a genuine
    data gap still breaks the line; a mixed bucket keeps its real points, its sub-pixel
    gap being invisible anyway.

    Pure and side-effect-free: `values` is never mutated, and the result is a new pair
    of lists. Returns the inputs unchanged when there is nothing to gain.
    """
    n = len(times)
    if buckets < 1 or n <= 2 * buckets:
        return times, ys
    out_t, out_y = [], []
    for b in range(buckets):
        lo = (b * n) // buckets
        hi = ((b + 1) * n) // buckets
        if hi <= lo:
            continue
        seg = ys[lo:hi]
        if None not in seg:
            # Fast path (the normal one -- a heater reports a number every sample).
            # min/max/index each run at C speed over the slice, so a bucket costs a
            # few native passes instead of a Python-level loop per point. This matters:
            # redraw happens 4x/s on the Tk thread that also feeds the live view, and
            # the per-point loop was itself the remaining cost once drawing was capped.
            mn, mx = min(seg), max(seg)
            lo_i, hi_i = lo + seg.index(mn), lo + seg.index(mx)
        else:
            lo_i = hi_i = None
            for i in range(lo, hi):
                v = ys[i]
                if v is None:
                    continue
                if lo_i is None or v < ys[lo_i]:
                    lo_i = i
                if hi_i is None or v > ys[hi_i]:
                    hi_i = i
        if lo_i is None:                      # bucket is all-None -> keep the gap
            out_t.append(times[lo])
            out_y.append(None)
            continue
        pair = sorted({lo_i, hi_i})
        for i in pair:
            out_t.append(times[i])
            out_y.append(ys[i])
    return out_t, out_y



def _connect_failure_text(host, port, exc):
    """A failed Connect, in the words of the person at the printer: what went
    wrong and what to check. The client's own message (a URL and a socket
    error number) stays at the end for a bug report."""
    detail = str(exc)
    low = detail.lower()
    status = getattr(exc, "status", None)
    if status in (401, 403):
        head = ("%s refused the connection. Check the API key, or leave it "
                "blank if the printer does not use one." % host)
    elif "getaddrinfo" in low or "11001" in low or "name or service" in low             or "nodename" in low:
        head = ("No printer named %s was found on the network. Check the "
                "host name, or type the printer's IP address instead." % host)
    elif "timed out" in low or "refused" in low or "10061" in low             or "10060" in low or "unreachable" in low:
        head = ("%s did not answer on port %d. Check the printer is on and "
                "on this network, and that its Moonraker uses port %d%s."
                % (host, port, port,
                   "" if port == 7125 else " (7125 is the usual one)"))
    else:
        head = "Could not connect to %s:%d." % (host, port)
    return "%s (%s)" % (head, detail)

def _link_lost_text(host, error):
    """A poll that failed, in plain words. The client's message is a URL with a
    300-character query string, which says nothing to the person at the
    printer; it stays in the console for a bug report, not on the line."""
    return ("Lost contact with %s: it stopped answering. Retrying… Check the "
            "printer is on and on this network." % (host or "the printer"))


def _upload_failure_text(host, name, exc):
    """A failed upload: that the file is NOT on the printer, why, and what to
    do. The printer's own reason ("the SD card is full") is the part a person
    can act on, so it is said when the printer gave one; the button named is
    the one that sends ("Upload" named no button anywhere)."""
    status = getattr(exc, "status", None)
    detail = str(getattr(exc, "detail", "") or "").strip().rstrip(".")
    if status is None:
        why = ("%s did not answer. Check the printer is on and on this "
               "network" % (host or "the printer"))
    elif status in (401, 403):
        why = "the printer refused it. Check the API key"
    elif detail:
        why = "the printer refused it and said: %s" % detail
    else:
        why = "the printer refused it (HTTP %s)" % status
    return ("Not sent: %s is not on the printer. %s%s. Then press Send to "
            "printer again." % (name or "the file", why[:1].upper(), why[1:]))


#: a line that sets the nozzle (or any) heater; a comment never counts
_HEAT_CMD = re.compile(r"^[ \t]*(?:M104|M109|SET_HEATER_TEMPERATURE)\b",
                       re.I | re.M)


#: `_ml_verb`'s client method -> the button that sent it
_VERB_ACTIONS = {"print_cancel": "Cancel"}


def _action_failure_text(action, host, exc, then=""):
    """Any other printer action that failed, in plain words: who did not answer
    or what the printer said, then `then` (what is left as it was). The client's
    message is a URL with a socket error, which says nothing at the printer; it
    goes to the console mirror instead (the "failed" queue event)."""
    status = getattr(exc, "status", None)
    detail = str(getattr(exc, "detail", "") or "").strip().rstrip(".")
    if status is None:
        why = ("%s did not answer. Check the printer is on and on this network"
               % (host or "the printer"))
    elif status in (401, 403):
        why = "the printer refused it. Check the API key"
    elif detail:
        why = "the printer said: %s" % detail
    else:
        why = "the printer refused it (HTTP %s)" % status
    return "%s failed: %s.%s" % (action, why, (" " + then) if then else "")


class _TempGraph(ttk.Frame):
    """A dependency-free live strip chart of heater temperature (left axis, °C) and
    PWM power (right axis, 0-100%) versus WALL-CLOCK time since connect, drawn on a
    tk.Canvas (no matplotlib -- keeps the no-new-dependency posture, like
    moonraker_link). Follows the modern light/dark theme: it self-registers in
    gui_widgets' theme registry and recolours its surface in retheme().

    reset(t0) clears every series and rebases the clock to the connect epoch (called
    on each connect, so the x-axis restarts); add_sample(t, temps, powers) appends one
    frame; the canvas redraws on new data, on resize, and on a theme toggle. `series`
    is exposed for tests."""

    def __init__(self, master, height=190, **kw):
        super().__init__(master, **kw)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, height=height, highlightthickness=0, bd=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.series = {}       # name -> {"t":[], "temp":[], "pwm":[], "color":str}
        self._order = []       # first-seen name order (stable colours)
        self._t0 = None        # connect epoch (absolute), for the wall-clock axis
        self._tmax_hint = 0.0  # max target seen, so the temp axis doesn't jump
        # theme colours (set by retheme; sane light defaults until the first toggle)
        self._c_bg = "#ffffff"
        self._c_grid = "#d9dce1"
        self._c_axis = "#7a8087"
        gui_widgets.register(self)
        self.retheme()
        self.canvas.bind("<Configure>", lambda _e: self.redraw())

    def retheme(self):
        """Adopt the live modern palette (called on every dark/light toggle)."""
        pal = gui_widgets.palette()
        self._c_bg = pal.get("field", pal.get("card", self._c_bg))
        self._c_grid = pal.get("hairline", pal.get("border", self._c_grid))
        self._c_axis = pal.get("hint", self._c_axis)
        try:
            self.canvas.configure(background=self._c_bg)
        except tk.TclError:
            pass
        self.redraw()

    def reset(self, t0=None):
        self.series = {}
        self._order = []
        self._t0 = t0
        self._tmax_hint = 0.0
        self.redraw()

    def add_sample(self, t, temps, powers):
        if self._t0 is None:
            self._t0 = t
        elapsed = max(0.0, t - self._t0)
        names = list(temps.keys())
        for name in powers:               # a heater with power but no temp row (rare)
            if name not in temps:
                names.append(name)
        for name in names:
            s = self.series.get(name)
            if s is None:
                color = _GRAPH_SERIES_COLORS[len(self._order)
                                             % len(_GRAPH_SERIES_COLORS)]
                s = {"t": [], "temp": [], "pwm": [], "color": color}
                self.series[name] = s
                self._order.append(name)
            actual, target = (temps.get(name) or (None, None))
            s["t"].append(elapsed)
            s["temp"].append(None if actual is None else float(actual))
            pw = powers.get(name)
            s["pwm"].append(None if pw is None else float(pw) * 100.0)
            if target is not None:
                try:
                    self._tmax_hint = max(self._tmax_hint, float(target))
                except (TypeError, ValueError):
                    pass
            if len(s["t"]) > _GRAPH_MAXLEN:
                for key in ("t", "temp", "pwm"):
                    del s[key][0]
        self.redraw()

    # -- drawing -------------------------------------------------------------
    def _temp_axis_max(self):
        vals = [v for s in self.series.values() for v in s["temp"] if v is not None]
        peak = max(vals + [self._tmax_hint]) if (vals or self._tmax_hint) else 0.0
        peak = max(peak * 1.12, 40.0)             # headroom + a sane floor
        step = 20.0
        return step * (int(peak // step) + 1)     # round up to a tidy tick

    def _time_span(self):
        span = 0.0
        for s in self.series.values():
            if s["t"]:
                span = max(span, s["t"][-1])
        return max(span, 1.0)

    @staticmethod
    def _fmt_clock(epoch, span):
        """A wall-clock tick label for the absolute time `epoch`. Seconds resolution
        while the window is short (so the three ticks differ early on), minute
        resolution once it spans a couple of minutes: e.g. "9:47:30" -> "9:47am"."""
        lt = time.localtime(epoch)
        s = (time.strftime("%I:%M:%S", lt) if span < 120
             else time.strftime("%I:%M%p", lt).lower())
        return s[1:] if s.startswith("0") else s

    def redraw(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 60 or h < 40:
            return
        ml, mr, mt, mb = 42, 44, 22, 20
        x0, y0, x1, y1 = ml, mt, w - mr, h - mb
        pw, ph = x1 - x0, y1 - y0
        if pw < 10 or ph < 10:
            return
        if not self._order:
            c.create_text(w // 2, h // 2, text="Temperature + PWM will chart here "
                          "once connected.", fill=self._c_axis, font="MewMono")
            return
        tmax = self._temp_axis_max()
        span = self._time_span()

        def X(elapsed):
            return x0 + (elapsed / span) * pw

        def YT(temp):
            return y1 - (temp / tmax) * ph

        def YP(pct):
            return y1 - (pct / 100.0) * ph

        # grid + left (°C) and right (%) axes
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            gy = y1 - frac * ph
            c.create_line(x0, gy, x1, gy, fill=self._c_grid)
            c.create_text(x0 - 5, gy, text="%d" % round(frac * tmax),
                          fill=self._c_axis, anchor="e", font="MewMono")
            c.create_text(x1 + 5, gy, text="%d" % round(frac * 100),
                          fill=self._c_axis, anchor="w", font="MewMono")
        c.create_text(x0 - 5, mt - 12, text="°C", fill=self._c_axis, anchor="w",
                      font="MewMono")
        c.create_text(x1 + 5, mt - 12, text="PWM%", fill=self._c_axis, anchor="e",
                      font="MewMono")
        # wall-clock time ticks (labelled with the real clock, not seconds-elapsed):
        # the absolute time at a given elapsed offset is the connect epoch + offset.
        for frac, anchor in ((0.0, "nw"), (0.5, "n"), (1.0, "ne")):
            gx = x0 + frac * pw
            abs_t = (self._t0 or 0.0) + frac * span
            c.create_text(gx, y1 + 4, text=self._fmt_clock(abs_t, span),
                          fill=self._c_axis, anchor=anchor, font="MewMono")

        buckets = max(1, int(pw // _GRAPH_PX_PER_BUCKET))

        def _poly(times, ys, yfn, color, dash):
            # split into contiguous runs (gaps where a value is None); a run needs at
            # least two points (>= 4 flat coords) before create_line will accept it.
            times, ys = _decimate(times, ys, buckets)
            coords, run = [], []
            for tt, val in zip(times, ys):
                if val is None:
                    if len(run) >= 4:
                        coords.append(run)
                    run = []
                    continue
                run += [X(tt), yfn(val)]
            if len(run) >= 4:
                coords.append(run)
            for seg in coords:
                c.create_line(*seg, fill=color, width=2, dash=dash)

        for name in self._order:
            s = self.series[name]
            _poly(s["t"], s["temp"], YT, s["color"], None)      # temp: solid
            _poly(s["t"], s["pwm"], YP, s["color"], (3, 3))     # PWM: dashed

        # legend
        lx = x0 + 6
        for name in self._order:
            c.create_text(lx, mt + 2, text="● " + name, fill=self.series[name]["color"],
                          anchor="nw", font="MewMono")
            lx += 10 + len(name) * 7 + 14
        c.create_text(x1, mt + 2, text="solid °C · dashed PWM", fill=self._c_axis,
                      anchor="ne", font="MewMono")


class _MachineLinkMixin:
    """Composed into ModernApp (beside _SimMixin / _SweepsMixin). Supplies the
    Machine-link page; the shell mounts it via _build_machine_link_tab and lists it
    in the Print section."""

    # ------------------------------------------------------------------ #
    # page assembly (shell hook)
    # ------------------------------------------------------------------ #
    def _build_machine_link_tab(self, nb):
        """Build the Machine-link page into `nb` (the shell notebook) and return.
        No-op without the embed layer (a build with no modern shell), so the shell's
        tab-count-delta registration simply skips it -- exactly like the Print
        history hook. The shell owns the sidebar entry + _noninput_frames."""
        if not hasattr(self, "_make_embed_panel"):
            return
        self._ml_init_state()

        page = ttk.Frame(nb, padding=gui_fonts.PAGE_MARGIN)
        nb.add(page, text="Printer link")
        page.rowconfigure(0, weight=1)
        page.columnconfigure(0, weight=1)

        paned = ttk.PanedWindow(page, orient="horizontal")
        paned.grid(row=0, column=0, sticky="nsew")
        self._ml_paned = paned

        form_host = VScrollFrame(paned, padding=(0, 0, gui_fonts.SP["m"], 0))
        paned.add(form_host, weight=2)
        panel_host = ttk.Frame(paned)
        paned.add(panel_host, weight=3)

        self._ml_build_form(form_host.body)

        self._ml_panel = self._make_embed_panel(
            panel_host, "machine",
            "The live printer view appears here once you connect.\n\n"
            "It needs a printer model. Set one up on the "
            "Printer & collector page.",
            self._ml_relaunch_viewer, no_rail=True)
        self._ml_panel.pack(fill="both", expand=True)

        # seat the sash ~46% (form), or wider while the form's widest row
        # needs it (a narrow window), short of the live view's own button
        # bar; it follows the window until the user drags it, then it is
        # theirs
        self._ml_sash_set = False
        self._ml_form_host = form_host
        paned.bind("<Configure>", self._ml_init_sash)
        paned.bind("<ButtonRelease-1>",
                   lambda _e: setattr(self, "_ml_sash_set", True), add="+")

    def _ml_init_sash(self, _e=None):
        if self._ml_sash_set:
            return
        w = self._ml_paned.winfo_width()
        if w < 200:
            return
        fh = self._ml_form_host
        need = fh.body.winfo_reqwidth() + fh._vsb.winfo_reqwidth()
        # ...but never past what the live view needs for its window buttons
        bar = self._ml_panel.fs_btn.master.winfo_reqwidth() + 8
        pos = min(max(int(w * 0.46), need), w - bar)
        try:
            if abs(self._ml_paned.sashpos(0) - pos) > 1:
                self._ml_paned.sashpos(0, pos)
        except tk.TclError:
            return

    # ------------------------------------------------------------------ #
    # state + prefs
    # ------------------------------------------------------------------ #
    def _ml_init_state(self):
        host, port, key, rs, rg = self._ml_load_prefs()
        self.ml_reveal_scaffold = tk.BooleanVar(value=rs)
        self.ml_reveal_gcode = tk.BooleanVar(value=rg)
        self.ml_host = tk.StringVar(value=host)
        self.ml_port = tk.StringVar(value=str(port))
        self.ml_api_key = tk.StringVar(value=key)
        self.ml_jog_step = tk.StringVar(value="1.0")
        self.ml_upload_name = tk.StringVar(value="")   # rename the upload (blank = as-is)
        self.ml_speed_factor = tk.StringVar(value="100")
        self.ml_flow_factor = tk.StringVar(value="100")
        self.ml_console_entry = tk.StringVar(value="")
        self.ml_record = tk.BooleanVar(value=False)
        # readouts
        self.ml_conn_status = tk.StringVar(value="Not connected.")
        self.ml_pos_text = tk.StringVar(
            value="Printer position (mm):   X —      Y —      Z —")
        # editable work (relative) coordinates -- type a value + Enter to move
        self.ml_rel_x = tk.StringVar(value="")
        self.ml_rel_y = tk.StringVar(value="")
        self.ml_rel_z = tk.StringVar(value="")
        self.ml_state_text = tk.StringVar(value="State: —")
        self.ml_progress = tk.DoubleVar(value=0.0)
        self.ml_override_text = tk.StringVar(value="Speed 100% · Flow 100%")
        self.ml_safety_text = tk.StringVar(value="—")
        self.ml_viewer_status = tk.StringVar(value="")
        # runtime
        self._ml_client = None
        self._ml_poller = None
        self._ml_q = queue.Queue()
        self._ml_recorder = None
        self._ml_connected = False
        self._ml_pump_on = False
        self._ml_after_id = None
        self._ml_launched = False
        self._ml_autolaunch_viewer = True   # tests set False to stay subprocess-free
        self._ml_console_seen_empty = True
        self._ml_temp_actual = {}        # heater -> StringVar (actual/target readout)
        self._ml_temp_set = {}           # heater -> StringVar (target entry)
        self._ml_graph = None            # the live temperature/PWM strip chart
        self._ml_rel_entries = {}        # axis -> Entry (editable work coords)
        self._ml_last_gpos = None        # last work coords (for FocusOut revert)
        self._ml_last_state = None       # last print state (gate moves during a print)
        # -- print control (see the "pause / resume" section) --------------- #
        self._ml_last_paused = None      # pause_resume.is_paused (None = no such obj)
        self._ml_has_pause_resume = False   # does this rig HAVE [pause_resume]?
        self._ml_base_cmds = {}          # "resume"/"pause" -> the renamed builtin
        self._ml_pause_z = None          # work-frame Z the print paused at, mm
        self._ml_verb_busy = False       # one pause/resume in flight at a time
        self._ml_sticky_until = 0.0      # monotonic deadline for a held status line
        self._ml_pause_btn = None
        self._ml_resume_btn = None
        self._ml_cancel_btn = None
        self._ml_btn_state = None        # last (pause_on, resume_on) actually applied
        # -- the live-view motion gate (see _ml_motion_gate) ---------------- #
        self._ml_axis_limits = {}        # axis -> (min, max) travel, from the printer
        self._ml_gate_reasons = set()    # why the view is held ("homing", "frame")
        self._ml_gate_until = 0.0        # hard ceiling, monotonic (never wedge)
        self._ml_gate_settled = 0        # consecutive in-volume, stationary samples
        # g-code file browser (list / download / delete, paged)
        self._ml_files = []              # full list of {path, size, modified}
        self._ml_files_page = 0
        self._ml_files_per_page = FILES_PER_PAGE
        self._ml_files_tree = None
        self.ml_files_page_text = tk.StringVar(value="Connect to list files.")
        self._ml_live_controls = []      # widgets enabled only while connected
        # -- the live scaffold reveal (see _ml_sync_reveal_source) ---------- #
        self._ml_src_path = None         # local copy of the file the printer is running
        self._ml_src_key = None          # (filename, size) the copy was fetched for
        self._ml_src_busy = False        # a download is in flight (one at a time)
        self._ml_launched_src = None     # the source the running viewer was launched on
        self.ml_reveal_status = tk.StringVar(value="")
        self.ml_reveal_debug = tk.StringVar(value="")

    def _ml_load_prefs(self):
        """(host, port, api_key, reveal_scaffold, reveal_gcode) from
        machine_link_prefs.json; sensible defaults on a missing/corrupt file
        (tolerant, like gui_theme._load_theme_pref)."""
        # NO DEFAULT HOST. It was the lab's printer's name, which on anyone
        # else's network finds nothing and reads as the app's mistake. Blank,
        # Connect says what to type (`_ml_connect`); a saved host still loads.
        host, port, key = "", 7125, ""
        # BOTH REVEALS DEFAULT OFF. Passes draw progressively, and the bracket that
        # bounds the projection is derived per file (`_derive_live_bracket` in
        # gcode_player.py), but on a stack of rotated near-identical layers the reveal
        # still loses monotonicity. Nor has the reveal been run against real
        # hardware -- every number behind it comes from a fake or simulated rig. The
        # rest of this page is unaffected and solid, so it must not open
        # with an untrustworthy layer switched on.
        rs, rg = False, False
        try:
            with open(MACHINE_LINK_PREFS_PATH) as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                host = str(raw.get("host", host) or host)
                port = int(raw.get("port", port) or port)
                key = str(raw.get("api_key", key) or "")
                rs = bool(raw.get("reveal_scaffold", rs))
                rg = bool(raw.get("reveal_gcode", rg))
        except (OSError, ValueError, TypeError):
            pass
        return host, port, key, rs, rg

    def _ml_save_prefs(self):
        try:
            port = int(self.ml_port.get() or 7125)
        except (ValueError, tk.TclError):
            port = 7125
        try:
            with open(MACHINE_LINK_PREFS_PATH, "w") as fh:
                json.dump({"host": self.ml_host.get().strip(),
                           "port": port,
                           "api_key": self.ml_api_key.get().strip(),
                           "reveal_scaffold": bool(self.ml_reveal_scaffold.get()),
                           "reveal_gcode": bool(self.ml_reveal_gcode.get())},
                          fh, indent=2)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # form
    # ------------------------------------------------------------------ #
    def _ml_build_form(self, body):
        body.columnconfigure(0, weight=1)
        pad = dict(padx=4, pady=3)
        r = 0

        ttk.Label(body, text="Printer link", style="Title.TLabel").grid(
            row=r, column=0, sticky="w"); r += 1
        gui_form.page_intro(
            body, "Connect to the printer's Klipper/Moonraker to watch it "
                  "print, send files, set temperatures and jog.",
            row=r, wrap=380); r += 1

        # --- Connection card ------------------------------------------- #
        conn = ttk.Frame(body)
        conn.grid(row=r, column=0, sticky="ew", pady=(0, gui_fonts.SP["m"])); r += 1
        conn.columnconfigure(1, weight=1)
        gui_widgets.section_header(conn, "Connection", row=0, column=0,
                                   columnspan=4, sticky="w")
        # one field per row on the shared edges; the API key's "leave blank"
        # note is its tooltip, not grey text beside it
        C = gui_form.Form(conn, row=1)
        C.entry("Host", self.ml_host, "The printer's address on your network: "
                "the name or IP address your browser opens Mainsail or Fluidd "
                "at, such as klipper.local or 192.168.1.50.", width="text")
        C.entry("Port", self.ml_port, "Moonraker's port, 7125 unless you "
                "changed it.")
        C.entry("API key", self.ml_api_key, "Leave blank if the browser opens "
                "the printer's page without a login.", width="text")
        btnrow = ttk.Frame(conn)
        btnrow.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self._ml_connect_btn = ttk.Button(btnrow, text="Connect",
                                          style="Primary.TButton",
                                          command=self._ml_connect)
        self._ml_connect_btn.pack(side="left")
        self._ml_estop_btn = ttk.Button(btnrow, text="■  EMERGENCY STOP",
                                        command=self._ml_estop, state="disabled")
        self._ml_estop_btn.pack(side="right")
        # Door unlock -- fires the rig's custom Klipper macro (GPIO21). Gated on the
        # connection like every other live control; not destructive, so no confirm.
        self._ml_door_btn = ttk.Button(btnrow, text="Door unlock",
                                       command=self._ml_door_unlock, state="disabled")
        gui_tips.add_tip(self._ml_door_btn, "Runs your printer's DOOR_UNLOCK macro. "
                "Shown because the connected printer has one.")
        self._ml_live_controls.append(self._ml_door_btn)
        # packed on connect, and only for a printer with the macro
        # (`_ml_show_door`); absent, there is nothing for it to run
        # wraps: a failed Connect says what to check, which is wider than
        # the form, and a clipped instruction is no instruction
        gui_form.mark(ttk.Label(conn, textvariable=self.ml_conn_status,
                                style="Hint.TLabel", wraplength=560,
                                justify="left"), "live").grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # --- Status & progress ----------------------------------------- #
        d = gui_widgets.Disclosure(body, "Status & progress", open=True)
        d.grid(row=r, column=0, sticky="ew"); r += 1
        sb = d.body
        sb.columnconfigure(0, weight=1)
        # the printer's state and position: live readouts, tagged as such
        gui_form.mark(ttk.Label(sb, textvariable=self.ml_state_text,
                                style="Section.TLabel"), "live").grid(
            row=0, column=0, sticky="w")
        # absolute (raw machine) coords -- a read-only readout, then the relative/work
        # coords (the frame G92 zeroes) as EDITABLE boxes: type a value + Enter to move
        # the name in the body face, as "Work position (mm):" under it; only
        # the numbers in the mono face, so they hold still as they change.
        # ml_pos_text keeps its whole sentence for anything that reads it
        posrow = ttk.Frame(sb)
        posrow.grid(row=1, column=0, sticky="w", pady=(2, 0))
        gui_form.mark(ttk.Label(posrow, text="Printer position (mm):"),
                      "label").pack(side="left")
        shown = tk.StringVar()

        def _pos_shown(*_a):
            shown.set(self.ml_pos_text.get().split(":", 1)[-1].strip())
        self.ml_pos_text.trace_add("write", _pos_shown)
        _pos_shown()
        gui_form.mark(ttk.Label(posrow, textvariable=shown, font="MewMono"),
                      "live").pack(side="left", padx=(8, 0))
        relhost = ttk.Frame(sb)
        relhost.grid(row=2, column=0, sticky="w", pady=(1, 0))
        rel_tip = ("The work coordinates, measured from the work origin that "
                   "Zero sets. Edit one and press Enter to move that axis there "
                   "(feed %d mm/min)." % JOG_FEED_MMPM)
        relrow = gui_form.Form(relhost).group("Work position", rel_tip,
                                              unit="mm")
        rel_vars = {"x": self.ml_rel_x, "y": self.ml_rel_y, "z": self.ml_rel_z}
        for ax in ("x", "y", "z"):
            e = relrow.add_entry(ax.upper(), rel_vars[ax], rel_tip,
                                 width="coord", font="MewMono",
                                 justify="right", state="disabled")
            e.bind("<Return>", lambda _e, a=ax: self._ml_move_rel(a))
            e.bind("<FocusOut>", lambda _e, a=ax: self._ml_rel_refresh(a))
            self._ml_rel_entries[ax] = e
            self._ml_live_controls.append(e)
        # a form too narrow for the label and X/Y/Z on one line (a small
        # window) puts X/Y/Z on the line under the label
        inner = relrow._inline
        home = dict(inner.grid_info())
        home.pop("in", None)

        def fit_rel(_e=None):
            form = inner.master
            lab = max(int(form.columnconfigure(0).get("minsize") or 0),
                      form.grid_slaves(column=0)[0].winfo_reqwidth()) + 16
            narrow = lab + inner.winfo_reqwidth() + 16 > sb.winfo_width() > 1
            if narrow == (int(inner.grid_info().get("row", 0))
                          != int(home["row"])):
                return
            if narrow:
                inner.grid(row=int(home["row"]) + 1, column=0, columnspan=2,
                           sticky="w", padx=home.get("padx", 0), pady=(0, 2))
            else:
                inner.grid(**home)
        sb.bind("<Configure>", fit_rel, add="+")
        ttk.Progressbar(sb, variable=self.ml_progress, maximum=1.0).grid(
            row=4, column=0, sticky="ew", pady=(0, 2))
        row = ttk.Frame(sb)
        row.grid(row=5, column=0, sticky="w")
        # Pause/Resume are NOT blanket live controls: they follow the job's real
        # state (_ml_update_print_buttons). Pause greying out the moment the printer
        # reports "paused" is what stops a second press stacking another macro z-hop
        # on the first. Cancel is a safety control and must not be locked out by a
        # stale or unknown state (it has its own confirm), so it greys only on a
        # state KNOWN to have no job: live while printing, paused or unknown.
        self._ml_pause_btn = ttk.Button(row, text="Pause", command=self._ml_pause,
                                        state="disabled")
        self._ml_pause_btn.pack(side="left", padx=(0, 4))
        self._ml_resume_btn = ttk.Button(row, text="Resume", command=self._ml_resume,
                                         state="disabled")
        self._ml_resume_btn.pack(side="left", padx=(0, 4))
        self._ml_cancel_btn = ttk.Button(row, text="Cancel print", command=self._ml_cancel,
                                         state="disabled")
        self._ml_cancel_btn.pack(side="left", padx=(0, 4))

        # --- Live view ---------------------------------------------------- #
        d = gui_widgets.Disclosure(body, "Live view (beta)", open=True, info=(
            "Both boxes below are off by default. Live view lays the "
            "scaffold in the 3-D view as the printer prints it, from the file "
            "the printer downloads when a print starts, pass by pass. It has "
            "not been tried on a mandrel print, and on a plate print it does "
            "not yet cover every case: on a stack of rotated, nearly identical "
            "layers the drawing can jump ahead of the print. It has been run "
            "only against simulated printers, not a real one. Treat what it "
            "draws as an illustration, not a measurement. Everything else on "
            "this page is unaffected."))
        d.grid(row=r, column=0, sticky="ew"); r += 1
        vb = d.body
        vb.columnconfigure(0, weight=1)
        # The section header carries "(beta)": these are off by default, and
        # someone who ticks one anyway should know the drawing is not yet
        # trustworthy before they read anything into it.
        V = gui_form.Form(vb)
        V.check("Deposition (+ jet)", self.ml_reveal_scaffold,
                "Lay the scaffold in the 3-D view as the printer prints it. "
                "Unverified: an illustration, not a measurement.",
                command=self._ml_toggle_reveal, row=0)
        V.check("G-code path", self.ml_reveal_gcode,
                "Draw the printing file's path in the 3-D view. Unverified: an "
                "illustration, not a measurement.",
                command=self._ml_toggle_reveal, row=1)
        # Throw the deposited scaffold out of the view. NOT the same as unticking the
        # box above -- that hides a mesh it keeps built, this discards it -- which is
        # why it is a button and not a third checkbox: a finished print leaves the
        # whole scaffold standing on the plate, and this is the one click that clears
        # it without relaunching the viewer.
        ttk.Button(vb, text="Clear scaffold",
                   command=self._ml_clear_scaffold).grid(
            row=2, column=0, sticky="w", pady=(4, 0))
        gui_form.mark(ttk.Label(vb, textvariable=self.ml_reveal_status,
                                style="Hint.TLabel", wraplength=360,
                                justify="left"), "live").grid(
            row=3, column=0, sticky="w", pady=(2, 0))
        # Seating readout, straight from the viewer child. Where the overlay sits in
        # Z relative to the collector is a question three world-Z numbers answer, and
        # they are only knowable inside the rendered scene -- so the child reports
        # them and they are shown here rather than reasoned about from outside.
        gui_form.mark(ttk.Label(vb, textvariable=self.ml_reveal_debug,
                                font="MewMono", style="Hint.TLabel"),
                      "live").grid(row=4, column=0, sticky="w", pady=(2, 0))

        # --- Send a print ---------------------------------------------- #
        d = gui_widgets.Disclosure(body, "Send a print", open=True, tip=(
            "Send the g-code you last generated to the printer. Send to printer "
            "only stores it there, as the bottom row's button does; Send and "
            "start begins printing after a confirmation."))
        d.grid(row=r, column=0, sticky="ew"); r += 1
        pb = d.body
        pb.columnconfigure(0, weight=1)
        gui_form.mark(ttk.Label(pb, textvariable=self._ml_print_name_var(),
                                style="Hint.TLabel", wraplength=360,
                                justify="left"), "live").grid(
            row=0, column=0, sticky="w")
        # Out of date: the file on the printer path is older than what the
        # G-code page or the designer now shows. Takes space only then; the
        # two buttons are the way forward (the page's own version, or the
        # page that made the file).
        self.ml_stale_var = tk.StringVar(value="")
        stale = ttk.Frame(pb)
        self._ml_stale_box = stale
        gui_form.mark(ttk.Label(stale, textvariable=self.ml_stale_var,
                                style="Warn.TLabel", wraplength=360,
                                justify="left"), "live").grid(
            row=0, column=0, columnspan=2, sticky="w")
        sb1 = ttk.Button(stale, text="Send the G-code page's version",
                         command=self._ml_send_page_version)
        sb1.grid(row=1, column=0, sticky="w", pady=(4, 0))
        gui_tips.add_tip(sb1, "Send the g-code exactly as the G-code "
                              "page shows it now (the file Save would "
                              "write), then ask before starting.")
        self._ml_stale_page_btn = sb1
        sb2 = ttk.Button(stale, text="Go to the page that made it",
                         command=self._ml_open_maker)
        sb2.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(4, 0))
        gui_tips.add_tip(sb2, "Open the page you pressed Generate on, to "
                              "generate again.")
        self._ml_stale_why = None
        prow = ttk.Frame(pb)
        prow.grid(row=2, column=0, sticky="w", pady=(4, 0))
        b1 = ttk.Button(prow, text="Send to printer", command=lambda: self._ml_upload(False),
                        state="disabled")
        b1.pack(side="left", padx=(0, 4))
        b2 = ttk.Button(prow, text="Send and start",
                        command=lambda: self._ml_upload(True), state="disabled")
        b2.pack(side="left")
        self._ml_live_controls += [b1, b2]
        # Rename the file the printer will store it as (defaults to the generated
        # basename -- all "scaffold.gcode" otherwise). Blank = send it as-is. Always
        # enabled so a name can be set before connecting; the upload reads it live.
        nrow = ttk.Frame(pb)
        nrow.grid(row=3, column=0, sticky="w", pady=(6, 0))
        name_row = gui_form.Form(nrow).entry(
            "Name", self.ml_upload_name, "The name the printer stores the file "
            "under. Blank sends it as generated.", width="text")
        ent = name_row.field
        ent.bind("<Return>", lambda _e: self._ml_apply_rename())
        name_row.add_button("Rename", self._ml_apply_rename)

        # --- G-code files on the printer ------------------------------- #
        d = gui_widgets.Disclosure(body, "G-code files", open=False, tip=(
            "Browse, download, and delete the g-code files stored on the printer. "
            "Refreshes on connect and after an upload; paged if there are many."))
        d.grid(row=r, column=0, sticky="ew"); r += 1
        fb = d.body
        fb.columnconfigure(0, weight=1)
        cols = ("name", "size", "modified")
        tree = ttk.Treeview(fb, columns=cols, show="headings", selectmode="browse",
                            height=self._ml_files_per_page)
        # each heading sits over its column's values, as the values align
        tree.heading("name", text="Name", anchor="w")
        tree.heading("size", text="Size", anchor="e")
        tree.heading("modified", text="Modified", anchor="w")
        from gui_fonts import dpi_px     # sized as the text is
        tree.column("name", width=dpi_px(fb, 190), anchor="w")
        tree.column("size", width=dpi_px(fb, 64), anchor="e")
        tree.column("modified", width=dpi_px(fb, 120), anchor="w")
        tree.grid(row=0, column=0, sticky="ew")
        gui_widgets.scroll_guard(tree)   # wheel scrolls the list, not the whole page
        tree.bind("<Double-1>", lambda _e: self._ml_download_selected())
        self._ml_files_tree = tree
        pager = ttk.Frame(fb)
        pager.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._ml_files_prev_btn = ttk.Button(pager, text="◀ Prev", width=8,
                                             command=self._ml_files_prev,
                                             state="disabled")
        self._ml_files_prev_btn.pack(side="left")
        gui_form.mark(ttk.Label(pager, textvariable=self.ml_files_page_text),
                      "live").pack(side="left", padx=8)
        self._ml_files_next_btn = ttk.Button(pager, text="Next ▶", width=8,
                                             command=self._ml_files_next,
                                             state="disabled")
        self._ml_files_next_btn.pack(side="left")
        facts = ttk.Frame(fb)
        facts.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        b_ref = ttk.Button(facts, text="Refresh", command=self._ml_refresh_files,
                           state="disabled")
        b_ref.pack(side="left")
        b_dl = ttk.Button(facts, text="Download…", command=self._ml_download_selected,
                          state="disabled")
        b_dl.pack(side="left", padx=(6, 0))
        b_go = ttk.Button(facts, text="Start print", command=self._ml_start_selected,
                          state="disabled")
        b_go.pack(side="left", padx=(6, 0))
        gui_tips.add_tip(b_go, "Start printing the selected file. Set the "
                               "temperatures first if the file does not.")
        self._ml_live_controls.append(b_go)
        b_del =ttk.Button(facts, text="Delete", command=self._ml_delete_selected,
                           state="disabled")
        b_del.pack(side="right")
        self._ml_live_controls += [b_ref, b_dl, b_del]

        # --- Temperatures (rows filled at connect time) ---------------- #
        d = gui_widgets.Disclosure(body, "Temperatures", open=False, tip=(
            "Read and set the printer's heaters, such as the melt heater and "
            "a heated collector. The melt temperature sets how freely the "
            "polymer flows, so it changes the fibre you print. A target above "
            "the heater's limit in the printer's Klipper config (max_temp) is "
            "lowered to that limit."))
        d.grid(row=r, column=0, sticky="ew"); r += 1
        tb = d.body
        tb.columnconfigure(0, weight=1)
        # rows live in their own frame (rebuilt at connect); the graph sits below it
        # in the disclosure body, so a heater-row rebuild never destroys the chart.
        self._ml_temp_host = ttk.Frame(tb)
        self._ml_temp_host.grid(row=0, column=0, sticky="ew")
        self._ml_temp_host.columnconfigure(0, weight=1)
        gui_form.mark(ttk.Label(self._ml_temp_host, style="Hint.TLabel",
                                text="Connect to list the printer's heaters."),
                      "live").grid(row=0, column=0, sticky="w")
        self._ml_graph = _TempGraph(tb)
        self._ml_graph.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        # the graph's legend: read, not hovered, so it stays text
        gui_form.mark(ttk.Label(
            tb, style="Hint.TLabel", wraplength=360, justify="left",
            text="Temperature (solid, °C) and heater PWM power (dashed, %) "
                 "since connect. The clock resets when you reconnect."),
            "caption").grid(row=2, column=0, sticky="w", pady=(3, 0))

        # --- Jog & home ------------------------------------------------- #
        d = gui_widgets.Disclosure(body, "Jog & home", open=False)
        d.grid(row=r, column=0, sticky="ew"); r += 1
        jb = d.body
        gui_form.Form(jb).entry("Step", self.ml_jog_step,
                                "How far one jog button moves.", unit="mm")
        grid = ttk.Frame(jb)
        grid.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        # a compact jog pad: +X/-X, +Y/-Y, +Z/-Z
        for c, (label, axis, sign) in enumerate((
                ("-X", "x", -1), ("+X", "x", +1), ("-Y", "y", -1),
                ("+Y", "y", +1), ("-Z", "z", -1), ("+Z", "z", +1))):
            b = ttk.Button(grid, text=label, width=4,
                           command=lambda a=axis, s=sign: self._ml_jog(a, s),
                           state="disabled")
            b.grid(row=0, column=c, padx=2)
            self._ml_live_controls.append(b)
        hrow = ttk.Frame(jb)
        hrow.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        for label, which in (("Home all", "all"), ("Home X", "x"),
                             ("Home Y", "y"), ("Home Z", "z")):
            b = ttk.Button(hrow, text=label,
                           command=lambda w=which: self._ml_home(w),
                           state="disabled")
            b.pack(side="left", padx=(0, 4))
            self._ml_live_controls.append(b)
        # Set-work-origin row (G92): make the current position read zero on the chosen
        # axes -- the 'rel' readout above is measured from here.
        zrow = ttk.Frame(jb)
        zrow.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        for label, which in (("Zero all", "all"), ("Zero X", "x"),
                             ("Zero Y", "y"), ("Zero Z", "z")):
            b = ttk.Button(zrow, text=label,
                           command=lambda w=which: self._ml_zero(w),
                           state="disabled")
            b.pack(side="left", padx=(0, 4))
            self._ml_live_controls.append(b)
        for b in zrow.winfo_children():
            gui_tips.add_tip(b, "Zero sets the current position as the work "
                                "origin (G92). It moves nothing, it just "
                                "re-bases the work position above.")

        # --- Overrides -------------------------------------------------- #
        d = gui_widgets.Disclosure(body, "Overrides", open=False, tip=(
            "Speed up or slow down the running print (M220), or change its "
            "flow (M221), as a percent of what the file asks for. 100% runs "
            "the file as written."))
        d.grid(row=r, column=0, sticky="ew"); r += 1
        ob = d.body
        gui_form.mark(ttk.Label(ob, textvariable=self.ml_override_text,
                                font="MewMono"), "live").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 3))
        O = gui_form.Form(ob, row=1)
        b = O.entry("Speed", self.ml_speed_factor, "Live feedrate override "
                    "(M220), as a percent of the file's speed.",
                    unit="%").add_button(
            "Set", lambda: self._ml_set_override("speed"), state="disabled")
        self._ml_live_controls.append(b)
        b = O.entry("Flow", self.ml_flow_factor, "Live flow override (M221), "
                    "as a percent.", unit="%").add_button(
            "Set", lambda: self._ml_set_override("flow"), state="disabled")
        self._ml_live_controls.append(b)

        # --- Console ---------------------------------------------------- #
        d = gui_widgets.Disclosure(body, "Console", open=False)
        d.grid(row=r, column=0, sticky="ew"); r += 1
        cb = d.body
        cb.columnconfigure(0, weight=1)
        self._ml_console = tk.Text(cb, height=7, wrap="none", font="MewMono",
                                   state="disabled", background="#111",
                                   foreground="#ddd")
        self._ml_console.grid(row=0, column=0, columnspan=2, sticky="ew")
        e = gui_form.exempt(ttk.Entry(cb, textvariable=self.ml_console_entry),
                            "a console command line, not a parameter")
        e.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        e.bind("<Return>", lambda _e: self._ml_send_console())
        b = ttk.Button(cb, text="Send", command=self._ml_send_console,
                       state="disabled")
        b.grid(row=1, column=1, sticky="w", padx=(4, 0), pady=(3, 0))
        self._ml_live_controls.append(b)

        # --- Telemetry recording --------------------------------------- #
        d = gui_widgets.Disclosure(body, "Telemetry recording", open=False, tip=(
            "Log every telemetry frame (position, temps, overrides, state) to a "
            "timestamped CSV, a lab-notebook trace of the print."))
        d.grid(row=r, column=0, sticky="ew"); r += 1
        rb = d.body
        gui_form.Form(rb).check(
            "Record telemetry to CSV while connected", self.ml_record,
            "Log every telemetry frame (position, temps, overrides, state) to "
            "a timestamped CSV.", command=self._ml_toggle_record)

        # --- Safety ----------------------------------------------------- #
        d = gui_widgets.Disclosure(body, "Safety", open=False)
        d.grid(row=r, column=0, sticky="ew"); r += 1
        gui_form.mark(ttk.Label(d.body, textvariable=self.ml_safety_text,
                                style="Hint.TLabel", wraplength=360,
                                justify="left"), "live").grid(
            row=0, column=0, sticky="w")

        # viewer status line under the form
        gui_form.mark(ttk.Label(body, textvariable=self.ml_viewer_status,
                                style="Hint.TLabel", wraplength=380,
                                justify="left"), "live").grid(
            row=r, column=0, sticky="w", pady=(gui_fonts.SP["m"], 0)); r += 1

    def _ml_print_name_var(self):
        """A StringVar tracking the g-code the Upload buttons will send (the last
        one generated). Refreshed lazily from self.last_gcode when the pump runs."""
        if not hasattr(self, "_ml_print_name"):
            self._ml_print_name = tk.StringVar(value="No g-code generated yet.")
        return self._ml_print_name

    # ------------------------------------------------------------------ #
    # connect / disconnect + pump
    # ------------------------------------------------------------------ #
    def _ml_connect(self):
        if self._ml_connected:
            return self._ml_disconnect()
        host = self.ml_host.get().strip()
        if not host:
            self.ml_conn_status.set(
                "Type your printer's address in Host first: the name or IP "
                "address your browser opens Mainsail or Fluidd at (such as "
                "klipper.local or 192.168.1.50).")
            return
        try:
            port = int(self.ml_port.get() or 7125)
        except (ValueError, tk.TclError):
            self.ml_conn_status.set("Port must be a number.")
            return
        key = self.ml_api_key.get().strip() or None
        self._ml_save_prefs()
        # discard any events left on the shared queue from a previous session, so a
        # stale snapshot / file list can't be applied before this connect completes.
        try:
            while True:
                self._ml_q.get_nowait()
        except queue.Empty:
            pass
        self.ml_conn_status.set("Connecting to %s…" % host)
        self._ml_connect_btn.configure(state="disabled")
        client = ml.MoonrakerClient(host, port, api_key=key, timeout=4.0)

        def probe():
            try:
                client.info()
                heaters = client.discover_heaters()
                sensors = client.discover_sensors()
                limits = client.heater_limits()
                # the machine's own travel range -- the live view's frame-redefinition
                # test (see _ml_motion_gate). Best-effort: an older config without
                # position_min/max just leaves the detector off.
                try:
                    axes = client.axis_limits()
                except ml.MoonrakerError:
                    axes = {}
                # How this rig's pause/resume are actually wired -- read once, here,
                # because both answers change what the Resume button must send (see
                # the "pause / resume" section). Best-effort on both counts.
                help_table = client.gcode_help()
                caps = {"pause_resume": client.has_object("pause_resume"),
                        "base_cmds": _base_print_commands(help_table),
                        "door_unlock": DOOR_UNLOCK_MACRO in {
                            str(k).upper() for k in (help_table or {})}}
                self._ml_q.put(("connected", client, heaters, sensors, limits, axes,
                                caps))
            except ml.MoonrakerError as exc:
                self._ml_q.put(("error", _connect_failure_text(host, port, exc)))
        threading.Thread(target=probe, daemon=True, name="MLConnect").start()
        self._ml_pump_on = True
        self._ml_arm_pump()

    def _ml_disconnect(self):
        if self._ml_poller is not None:
            self._ml_poller.stop()
            self._ml_poller = None
        self._ml_stop_record()
        if self._ml_panel is not None:
            self._ml_panel.stop()
        self._ml_launched = False
        self._ml_client = None
        self._ml_gate_reasons.clear()
        self._ml_gate_settled = 0
        self._ml_connected = False
        # a fresh session re-discovers the rig's pause wiring; nothing here survives
        self._ml_last_state = None
        self._ml_last_paused = None
        self._ml_has_pause_resume = False
        self._ml_base_cmds = {}
        self._ml_pause_z = None
        self._ml_pump_on = False
        self._ml_set_controls(False)
        self._ml_show_door(False)
        self._ml_connect_btn.configure(text="Connect", state="normal")
        self._ml_estop_btn.configure(state="disabled")
        self._ml_on_files([])         # clear the file browser + reset its pager
        self.ml_conn_status.set("Disconnected.")

    def _ml_on_connected(self, client, heaters, sensors, limits, axis_limits=None,
                         caps=None):
        self._ml_client = client
        self._ml_heaters = heaters
        self._ml_limits = limits
        self._ml_axis_limits = dict(axis_limits or {})
        caps = caps or {}
        self._ml_has_pause_resume = bool(caps.get("pause_resume"))
        self._ml_base_cmds = dict(caps.get("base_cmds") or {})
        self._ml_last_paused = None
        self._ml_pause_z = None
        self._ml_gate_reasons.clear()      # a fresh session starts trusting the feed
        self._ml_gate_settled = 0
        self._ml_connected = True
        self._ml_show_door(bool(caps.get("door_unlock")))
        self._ml_build_temp_rows(heaters, sensors)
        if self._ml_graph is not None:
            self._ml_graph.reset(t0=time.time())   # clock restarts at each connect
        self._ml_poller = ml.LinkPoller(client, self._ml_q,
                                        temp_objects=list(heaters) + list(sensors),
                                        pause_resume=self._ml_has_pause_resume)
        self._ml_poller.start()
        self._ml_set_controls(True)
        self._ml_connect_btn.configure(text="Disconnect", state="normal")
        self._ml_estop_btn.configure(state="normal")
        self.ml_conn_status.set("Connected to %s." % self.ml_host.get().strip())
        if self.ml_record.get():
            self._ml_start_record()
        self._ml_refresh_files()      # populate the g-code file browser
        if self._ml_autolaunch_viewer:
            self._ml_launch_viewer()

    def _ml_arm_pump(self):
        if self._ml_after_id is None:
            self._ml_after_id = self.root.after(_PUMP_MS, self._ml_pump)

    def _ml_pump(self):
        self._ml_after_id = None
        drained = 0
        while drained < 200:
            try:
                item = self._ml_q.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if isinstance(item, tuple):
                self._ml_handle_event(item)
            else:
                self._ml_handle_snapshot(item)
        # keep the last-generated print name fresh
        self._ml_refresh_print_name()
        if self._ml_pump_on:
            self._ml_arm_pump()

    def _ml_handle_event(self, item):
        kind = item[0]
        if kind == "connected":
            _k, client, heaters, sensors, limits = item[:5]
            self._ml_on_connected(client, heaters, sensors, limits,
                                  item[5] if len(item) > 5 else None,
                                  item[6] if len(item) > 6 else None)
        elif kind == "uploaded":
            _k, name, started = item
            text = ("Sent %s to %s%s." % (
                name, self.ml_host.get().strip() or "the printer",
                " and started printing it" if started else ""))
            self._ml_tell(text)
            self._ml_send_said(text)
            self._ml_refresh_files()   # the new file should appear in the list
        elif kind == "uploaded_all":
            # several prints went up: say what to do with them, last. The
            # note was written before the send ("Sending 3 files…"); now
            # they are up, so it says so.
            text = item[1]
            if text.startswith("Sending "):
                text = "Sent " + text[len("Sending "):]
            self._ml_tell(text)
            self._ml_send_said(text)
            self._ml_refresh_files()
        elif kind == "upload_failed":
            # a send that did not happen: said where Send was pressed too,
            # because the Send button is on every page and this line is not
            _k, name, text = item
            self._ml_say(text, sticky=True, error=True)
            self._ml_send_said(text)
        elif kind == "started":
            text = "Started %s on %s." % (item[1], self.ml_host.get().strip()
                                          or "the printer")
            self._ml_tell(text)
            self._ml_send_said(text)
        elif kind == "start_failed":
            _k, name, exc = item
            self._ml_append_console("! starting %s failed: %s" % (name, exc))
            text = _action_failure_text(
                "Starting %s" % name, self.ml_host.get().strip(), exc,
                "%s is on the printer but is not printing. Start it from "
                "G-code files on the Printer link page." % name)
            self._ml_say(text, sticky=True, error=True)
            self._ml_send_said(text)
        elif kind == "files":
            if self._ml_connected:      # ignore a late list that outlived its session
                self._ml_on_files(item[1])
        elif kind == "src":
            # the local copy of the printing file arrived -- relaunch onto it
            _k, key, path, err = item
            self._ml_src_busy = False
            if err or not path:
                self._ml_src_key = key      # don't retry the same file in a loop
                self.ml_reveal_status.set("Could not fetch the printing file: %s"
                                          % (err or "unknown error"))
                return
            self._ml_src_key, self._ml_src_path = key, path
            self.ml_reveal_status.set("Showing %s" % os.path.basename(path))
            if self._ml_launched:
                self._ml_launch_viewer()
        elif kind == "downloaded":
            _k, name, dest = item
            self._ml_tell("Downloaded %s → %s"
                                    % (name, os.path.basename(dest)))
        elif kind == "deleted":
            self._ml_tell("Deleted %s." % item[1])
            self._ml_refresh_files()   # reflect the removal
        elif kind == "status":
            # a worker's own verdict (pause/resume) -- held against the poll rate
            self._ml_say(item[1], sticky=True)
        elif kind == "console":
            # a worker's audit line (which resume path was tried, and what it did):
            # it belongs in the mirror beside the printer's own responses
            self._ml_append_console(item[1])
        elif kind == "failed":
            # a worker's MoonrakerError: plain words on the line (the host is
            # read here, on the Tk thread), the client's own text in the console
            _k, action, exc, then = item
            self._ml_append_console("! %s failed: %s" % (action, exc))
            self._ml_say(_action_failure_text(
                action, self.ml_host.get().strip(), exc, then),
                sticky=True, error=True)
        elif kind == "error":
            if not self._ml_connected:
                self._ml_pump_on = False
                self._ml_connect_btn.configure(state="normal")
            self._ml_say("%s" % item[1], sticky=True, error=True)

    # How long a `sticky` status message survives the snapshot handler below, which
    # otherwise rewrites that line to "Connected to …" at the 4 Hz poll rate -- long
    # enough to read the outcome of a pause/resume, short enough that the line goes
    # back to reporting the link. A LINK ERROR always wins immediately.
    _ML_STICKY_S = 8.0

    def _ml_say(self, msg, sticky=False, error=False):
        """Put `msg` on the connection status line, optionally holding it against the
        next few snapshots (see _ML_STICKY_S). A held `error` also outlasts a
        link-error snapshot: it is the more specific news (an upload that did
        not happen), and the link error takes the line once it expires."""
        self.ml_conn_status.set(msg)
        self._ml_sticky_until = (time.monotonic() + self._ML_STICKY_S) if sticky else 0.0
        self._ml_sticky_error = bool(sticky and error)

    def _ml_heat_note(self, text):
        """A sentence for a "start printing now?" prompt, or "": a g-code
        file from Design sets no temperature, so the print runs at whatever
        the printer is at, while Simulation drew it at the Physics page's
        temperature. A cold melt at the start of a print costs the print;
        the prompt is the last place to say so. The same goes for the
        voltage and pressure, which no built-in flavor writes. What the
        pre-flight dialog said a moment ago is not said again; the heater's
        live reading, which it could not know, still is."""
        if not text:
            return ""
        try:
            flavor = self._gcode_export_flavor()
        except AttributeError:
            flavor = "klipper"
        sp = mew_gcode.process_setpoints(text, flavor)
        acc = getattr(self, "_preflight_accepted", None)
        accepted = acc() if callable(acc) else set()
        said = getattr(self, "_preflight_said", None) or set()
        vp = [n for n in ("voltage", "pressure")
              if not sp[n]["written"] and n not in accepted | said]
        hv = ("This file does not set the %s: set %s on the printer before "
              "it starts (Setup's G-code card says how to add your "
              "printer's commands)."
              % (" or ".join(vp), "it" if len(vp) == 1 else "them")
              if vp else "")
        if _HEAT_CMD.search(text) or sp["temperature"]["written"] \
                or "temperature" in accepted:
            return hv
        heat = self._ml_heat_only_note(brief="temperature" in said)
        return " ".join(s for s in (hv, heat) if s)

    def _ml_heat_only_note(self, brief=False):
        """The temperature half of `_ml_heat_note`. `brief`: the pre-flight
        already said the file sets no temperature, so only the readings."""
        temps = getattr(self, "_ml_last_temps", None) or {}
        reading = temps.get("extruder")
        if reading is None and temps:
            reading = next(iter(temps.values()))
        now = ""
        if reading is not None and reading[0] is not None:
            actual, target = reading
            now = " The nozzle heater reads %.0f °C" % actual
            if target is not None:
                now += " (target %.0f °C)" % target
            now += "."
        sim = getattr(self, "sim_temperature", None)
        try:
            sim = float(sim.get()) if sim is not None else None
        except (ValueError, tk.TclError):
            sim = None
        assumed = (" Simulation assumed %g °C." % sim) if sim is not None else ""
        if brief:
            return (now + assumed).strip()
        return ("This file sets no temperature: it prints at the printer's "
                "own setting.%s%s Set it under Temperatures on the Printer "
                "link page first." % (now, assumed))

    def _ml_send_said(self, text):
        """A send's outcome, also on the status line under the bottom row.
        Send to printer is on every page and this page's own line is not:
        a send pressed from Simulation that failed was told to nobody."""
        status = getattr(self, "status", None)
        if status is not None:
            status.set(" ".join(str(text).split()))

    def _ml_tell(self, msg):
        """An outcome or refusal said while connected. Held, because otherwise
        the next poll (a quarter second later) rewrites the line to
        "Connected to …" and the user never reads it: an upload that worked
        was reported to nobody."""
        self._ml_say(msg, sticky=True)

    def _ml_handle_snapshot(self, snap):
        if not snap.ok:
            if (getattr(self, "_ml_sticky_error", False)
                    and time.monotonic() < self._ml_sticky_until):
                return
            if not getattr(self, "_ml_lost", False):
                # once per outage: the raw error, for a bug report
                self._ml_lost = True
                self._ml_append_console("link error: %s" % (snap.error or ""))
            self._ml_say(_link_lost_text(self.ml_host.get().strip(),
                                         snap.error))
            return
        self._ml_lost = False
        if time.monotonic() >= self._ml_sticky_until:
            self.ml_conn_status.set("Connected to %s." % self.ml_host.get().strip())
        held = self._ml_gate_step(snap)
        if snap.pos is not None:
            self.ml_pos_text.set("Printer position (mm):   X %8.2f  Y %8.2f  Z %8.2f"
                                 % (snap.pos[0], snap.pos[1], snap.pos[2]))
            if self._ml_launched and self._ml_panel is not None:
                # forward the POLLER's sample time (snap.t) so the viewer interpolates
                # on an evenly-spaced timeline, immune to this pump's batching jitter.
                # `hold` carries the motion gate: the child stops the bodies rather
                # than rendering a coordinate that is not a position (see
                # _ml_motion_gate). The numeric readout above keeps updating either
                # way, so a held view never hides what the machine is reporting.
                self._ml_panel.send_child(
                    t="livepos", ts=snap.t,
                    x=snap.pos[0], y=snap.pos[1], z=snap.pos[2],
                    state=snap.state, progress=snap.progress, spd=snap.speed,
                    hold=1 if held else 0)
        # The reveal rides its own message, independent of `livepos`: where the
        # printer is IN THE FILE plus the work offset H. Sent every snapshot (not only
        # while a position is reported), so a paused or just-started print still
        # places the overlay. `hold` deliberately does NOT gate it -- holding the
        # BODIES through a homing frame redefinition is right, freezing the scaffold
        # is not.
        if self._ml_launched and self._ml_panel is not None and self._ml_launched_src:
            H = ml.work_offset(snap) or (0.0, 0.0, 0.0)
            self._ml_panel.send_child(
                t="filepos", pos=snap.file_position, state=snap.state,
                hx=H[0], hy=H[1], hz=H[2])  # noqa: E501
        self._ml_sync_reveal_source(snap)
        self._ml_refresh_reveal_debug()
        if snap.gpos is not None:
            self._ml_last_gpos = snap.gpos
            self._ml_rel_update(snap.gpos)
        was = getattr(self, "_ml_last_state", None)
        self._ml_last_state = snap.state
        self._ml_last_paused = snap.is_paused
        self._ml_update_print_buttons()
        if snap.state == "error" and was in ("printing", "paused"):
            # a print that died on the printer is the most expensive news this
            # page carries: said once, on both lines, with Klipper's reason
            why = (snap.message or "").strip().rstrip(".")
            text = ("The print stopped on an error%s. Nothing more will print "
                    "until the cause is fixed and the printer is restarted."
                    % ((": " + why) if why else
                       ". The Console shows the printer's last lines"))
            self._ml_say(text, sticky=True, error=True)
            self._ml_send_said(text)
        # Klipper's own word, as a person writes it ("printing" -> "Printing")
        state = (snap.state or "—")
        state = state[:1].upper() + state[1:]
        if snap.state == "paused" and snap.is_paused is not True:
            # The job is stopped but Klipper's own pause flag is NOT set (False), or
            # the rig has no [pause_resume] to set it (None) -- either way this pause
            # came from a bare `M25`, not from Klipper, and `RESUME` will refuse it
            # with a console line and no motion. Say so on the readout rather than
            # leave the operator guessing why the machine looks paused but isn't.
            state = "Paused by M25 (Resume cannot restart it)"
        if held:
            # say WHY the 3-D view stopped tracking, so a held view never reads as a
            # dropped link (the coordinates beside it are still live)
            self.ml_state_text.set(
                "State: %s · homing (3-D view held)" % state
                if "homing" in self._ml_gate_reasons else
                "State: %s · re-homing (3-D view held)" % state)
        elif snap.filename and snap.state in ("printing", "paused"):
            self.ml_state_text.set("State: %s · %s"
                                   % (state, os.path.basename(snap.filename)))
        elif snap.state == "error" and (snap.message or "").strip():
            # Klipper's reason, not a bare "Error" the operator cannot act on
            self.ml_state_text.set("State: Error · %s"
                                   % snap.message.strip().rstrip("."))
        else:
            self.ml_state_text.set("State: %s" % state)
        if snap.progress is not None:
            self.ml_progress.set(float(snap.progress))
        if snap.speed_factor is not None or snap.extrude_factor is not None:
            self.ml_override_text.set(
                "Speed %d%% · Flow %d%%"
                % (round((snap.speed_factor or 1.0) * 100),
                   round((snap.extrude_factor or 1.0) * 100)))
        if snap.temps:
            self._ml_last_temps = dict(snap.temps)   # for `_ml_heat_note`
        for name, (actual, target) in (snap.temps or {}).items():
            var = self._ml_temp_actual.get(name)
            if var is not None:
                if target is None:
                    var.set("%.1f°C" % (actual or 0.0))
                else:
                    var.set("%.1f / %.0f°C" % (actual or 0.0, target or 0.0))
        if self._ml_graph is not None and (snap.temps or snap.powers):
            self._ml_graph.add_sample(snap.t, snap.temps or {}, snap.powers or {})
        if snap.throttled is not None:
            self._ml_update_safety(snap.throttled)
        for line in snap.console:
            self._ml_append_console(line)
        if self._ml_recorder is not None:
            try:
                self._ml_recorder.write(snap)
            except (OSError, ValueError) as exc:
                # the CSV went away (USB stick pulled, disk full). Swallowing
                # this every tick left the box ticked and the status saying
                # "Recording" over a trace that had quietly ended.
                path = self._ml_recorder.path
                try:
                    self._ml_stop_record()
                except (OSError, ValueError):
                    self._ml_recorder = None
                self.ml_record.set(False)
                self._ml_say("Telemetry recording stopped: could not write %s "
                             "(%s)" % (os.path.basename(path), exc), sticky=True)

    # ------------------------------------------------------------------ #
    # the live-view motion gate
    # ------------------------------------------------------------------ #
    # A `G28` makes Klipper call `set_position`, which REDEFINES the coordinate frame.
    # `motion_report.live_position` then steps discontinuously while nothing physical
    # teleports, and the live view renders those steps as travel: 647.9 mm of it over
    # one homing burst at 231-449 mm/s (rig capture) -- the printhead yank. So while
    # the reported coordinates are not positions, HOLD the 3-D view (the numeric
    # readouts keep updating) and glide back on when the machine settles.
    # Two ways in: "homing", where the page sent the G28 itself, and "frame", the
    # sample leaving the configured travel volume. The console mirror is NOT a
    # detector: it is read every 5 s, ~20 samples after the yank is already drawn.
    _ML_GATE_MAX_S = 180.0        # ceiling: a lost end-signal must not wedge the view
    _ML_GATE_SETTLE_N = 2         # consecutive good samples before releasing
    _ML_GATE_MARGIN = 1.0         # mm of slack on the travel volume

    def _ml_arm_gate(self, reason, max_s=None):
        """Hold the live view. Idempotent; each reason releases independently."""
        self._ml_gate_reasons.add(reason)
        self._ml_gate_settled = 0
        self._ml_gate_until = max(self._ml_gate_until,
                                  time.monotonic()
                                  + (self._ML_GATE_MAX_S if max_s is None else max_s))

    def _ml_release_gate(self, reason):
        self._ml_gate_reasons.discard(reason)

    def _ml_outside_travel(self, pos):
        """True if `pos` is not a place this machine can physically be, per its own
        `[stepper_*] position_min/max`. Unknown limits -> False (never guess).

        Tested against the RAW machine coordinate, never `gcode_position`: the
        earlier rejected prototype's companion heuristic rejected work coordinates
        below the origin and duly froze the view on a legitimate jog to (5, -3, 0).
        In the machine frame that same jog is comfortably inside the volume."""
        if not self._ml_axis_limits or pos is None:
            return False
        for i, axis in enumerate("xyz"):
            rng = self._ml_axis_limits.get(axis)
            if rng is None or i >= len(pos) or pos[i] is None:
                continue
            if not (rng[0] - self._ML_GATE_MARGIN <= pos[i]
                    <= rng[1] + self._ML_GATE_MARGIN):
                return True
        return False

    def _ml_gate_step(self, snap):
        """Fold one snapshot into the gate and return True if the view must hold.

        Releasing on the FIRST in-volume sample would let the view chase the machine
        back and forth through a multi-axis homing sequence (the rig's burst returns
        inside the volume between axes), so release needs `_ML_GATE_SETTLE_N`
        consecutive samples that are both in-volume and stationary."""
        if self._ml_outside_travel(snap.pos):
            self._ml_arm_gate("frame")
        elif "frame" in self._ml_gate_reasons:
            moving = (snap.speed or 0.0) > 1e-6
            self._ml_gate_settled = 0 if moving else self._ml_gate_settled + 1
            if self._ml_gate_settled >= self._ML_GATE_SETTLE_N:
                self._ml_release_gate("frame")
        if self._ml_gate_reasons and time.monotonic() > self._ml_gate_until:
            self._ml_gate_reasons.clear()      # ceiling: never wedge the view
        return bool(self._ml_gate_reasons)

    def _ml_update_safety(self, throttled):
        flags = throttled.get("flags") if isinstance(throttled, dict) else None
        if flags:
            self.ml_safety_text.set("⚠ " + ", ".join(flags))
        else:
            self.ml_safety_text.set("OK: no undervoltage or throttle flags.")

    def _ml_set_controls(self, on):
        st = "normal" if on else "disabled"
        for w in self._ml_live_controls:
            try:
                w.configure(state=st)
            except tk.TclError:
                pass
        self._ml_update_print_buttons()

    def _ml_update_print_buttons(self):
        """Pause/Resume follow the JOB, not just the connection.

        Pause is live only while a job is actually running and not already paused --
        the guard that stops a second click stacking another z-hop from the rig's
        PAUSE macro. Resume is live only while it is paused (`pause_resume.is_paused`
        when the rig publishes it, else `print_stats.state`), so a click always has
        something to act on."""
        if self._ml_pause_btn is None:
            return                      # form not built yet
        if not self._ml_connected:
            paused = printing = cancel = False
        else:
            paused = (bool(self._ml_last_paused)
                      or self._ml_last_state == "paused")
            printing = self._ml_last_state == "printing"
            # Klipper's print_stats states with no job to cancel (the fake's
            # "ready" too). Anything else, unknown included, keeps Cancel live:
            # reported, Cancel print sat live beside an idle printer.
            cancel = self._ml_last_state not in (
                "standby", "ready", "complete", "cancelled", "error")
        want = (printing and not paused, paused, cancel)
        if want == self._ml_btn_state:
            return          # this runs on every 4 Hz snapshot: don't re-configure
        self._ml_btn_state = want
        for btn, on in zip((self._ml_pause_btn, self._ml_resume_btn,
                            self._ml_cancel_btn), want):
            try:
                btn.configure(state="normal" if on else "disabled")
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ #
    # temperature rows (built at connect from discovery)
    # ------------------------------------------------------------------ #
    def _ml_build_temp_rows(self, heaters, sensors):
        for child in self._ml_temp_host.winfo_children():
            child.destroy()
        self._ml_temp_actual = {}
        self._ml_temp_set = {}
        settables = set(heaters)
        row = 0
        # one row per heater on the shared edges: its name, its live reading,
        # and -- for a heater, not a bare sensor -- a setpoint with Set / Off
        F = gui_form.Form(self._ml_temp_host)
        for name in list(heaters) + [s for s in sensors if s not in settables]:
            av = tk.StringVar(value="—")
            self._ml_temp_actual[name] = av
            r = F.group(name, "The heater's reading now, in °C."
                        + (" Type a target and press Set; Off sets 0."
                           if name in settables else ""))
            r.add_widget(ttk.Label(r.host(), textvariable=av, font="MewMono",
                                   width=12), kind="live")
            if name in settables:
                sv = tk.StringVar(value="0")
                self._ml_temp_set[name] = sv
                r.add_entry("Target", sv, "The temperature to heat to. A target "
                            "above the heater's max_temp is lowered to it.", unit="°C", width="small")
                r.add_button("Set", lambda n=name: self._ml_set_temp(n),
                             width=4)
                r.add_button("Off", lambda n=name: self._ml_set_temp(n, 0.0),
                             width=4)
            row += 1

    # ------------------------------------------------------------------ #
    # g-code script builders (pure) + threaded senders
    # ------------------------------------------------------------------ #
    def _ml_jog_script(self, axis, sign):
        try:
            step = float(self.ml_jog_step.get())
        except (ValueError, tk.TclError):
            step = 1.0
        return "G91\nG1 %s%.3f F%d\nG90" % (axis.upper(), sign * step, JOG_FEED_MMPM)

    def _ml_home_script(self, which):
        return "G28" if which == "all" else "G28 %s" % which.upper()

    def _ml_zero_script(self, which):
        """G92 so the current toolhead position reads zero on the chosen axes -- sets
        the work origin the relative ('rel') readout is measured from. Moves nothing."""
        if which == "all":
            return "G92 X0 Y0 Z0"
        return "G92 %s0" % which.upper()

    def _ml_temp_script(self, heater, target):
        limit = getattr(self, "_ml_limits", {}).get(heater)
        t = max(0.0, float(target))
        if limit is not None:
            t = min(t, float(limit))
        return "SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.1f" % (heater, t)

    def _ml_override_script(self, kind, value):
        code = "M220" if kind == "speed" else "M221"
        return "%s S%d" % (code, round(float(value)))

    def _ml_door_unlock_script(self):
        """The rig's door-unlock Klipper macro, sent verbatim as one g-code line."""
        return DOOR_UNLOCK_MACRO

    def _ml_gcode(self, script, timeout=None, gate=None):
        """Send a g-code script to the printer on a short worker thread (so a slow
        POST never freezes Tk); errors surface on the connection status line.

        `gate`, if given, is a motion-gate reason held for exactly as long as the
        request is in flight. Klipper answers a script only once it has finished
        running it, so for a blocking command (`G28`) that window IS the duration of
        the move -- an exact, inference-free signal, no classifier needed. `timeout`
        must then be long enough to cover the move (homing took ~21 s on the rig; the
        client's 4 s default would abort mid-home and report a false link error)."""
        client = self._ml_client
        if client is None:
            return
        if gate:
            self._ml_arm_gate(gate)

        def worker():
            try:
                client.gcode(script, timeout=timeout)
            except ml.MoonrakerError as exc:
                self._ml_q.put(("failed", "The command", exc, ""))
            finally:
                if gate:
                    # the gate falls back to the settle detector from here: the head
                    # must also be in-volume and stationary before the view re-seats
                    self._ml_release_gate(gate)
        threading.Thread(target=worker, daemon=True, name="MLgcode").start()

    def _ml_jog(self, axis, sign):
        self._ml_gcode(self._ml_jog_script(axis, sign))

    def _ml_home(self, which):
        self._ml_gcode(self._ml_home_script(which),
                       timeout=self._ML_GATE_MAX_S, gate="homing")

    def _ml_zero(self, which):
        self._ml_gcode(self._ml_zero_script(which))

    def _ml_show_door(self, on):
        """Door unlock is on the page only while connected to a printer that
        has the macro."""
        btn = getattr(self, "_ml_door_btn", None)
        if btn is None:
            return
        try:
            if on and not btn.winfo_manager():
                btn.pack(side="left", padx=(6, 0))
            elif not on and btn.winfo_manager():
                btn.pack_forget()
        except tk.TclError:
            pass

    def _ml_door_unlock(self):
        """Fire the printer's DOOR_UNLOCK macro (one click, no confirm)."""
        self._ml_gcode(self._ml_door_unlock_script())
        self._ml_tell("Sent %s." % DOOR_UNLOCK_MACRO)

    # -- editable work coordinates (type X/Y/Z + Enter to move) --------- #
    def _ml_rel_var(self, axis):
        return {"x": self.ml_rel_x, "y": self.ml_rel_y, "z": self.ml_rel_z}[axis]

    def _ml_entry_focused(self, widget):
        """True if `widget` currently owns keyboard focus (so live telemetry must not
        overwrite what the user is typing). focus_get can raise mid-teardown."""
        try:
            return self.root.focus_get() is widget
        except (KeyError, tk.TclError):
            return False

    def _ml_rel_update(self, gpos):
        """Refresh the editable work-coordinate boxes from live telemetry -- but never
        clobber a box the user is actively editing (it has focus)."""
        for i, ax in enumerate(("x", "y", "z")):
            e = self._ml_rel_entries.get(ax)
            if e is None or self._ml_entry_focused(e):
                continue
            self._ml_rel_var(ax).set("%.2f" % gpos[i])

    def _ml_rel_refresh(self, axis):
        """Revert one box to the last live value (on FocusOut / a rejected move), so an
        uncommitted edit doesn't linger."""
        g = self._ml_last_gpos
        if g is not None:
            self._ml_rel_var(axis).set("%.2f" % g["xyz".index(axis)])

    def _ml_move_rel_script(self, axis, target):
        """An ABSOLUTE move in the g-code (work) frame. gcode_position IS the frame G1
        commands use, so G90 + G1 <axis><target> lands the 'rel' readout exactly on the
        typed value (honouring any G92 / SET_GCODE_OFFSET work origin)."""
        return "G90\nG1 %s%.3f F%d" % (axis.upper(), float(target), JOG_FEED_MMPM)

    def _ml_move_rel(self, axis):
        """Enter in a work-coordinate box: move that one axis to the typed value.
        Blocked while a print runs; a non-number reverts the box."""
        if not self._ml_connected:
            return
        if self._ml_last_state in ("printing", "paused"):
            self._ml_tell("Can't move an axis while a print is running.")
            self._ml_rel_refresh(axis)
            return
        try:
            target = float(self._ml_rel_var(axis).get())
        except (ValueError, tk.TclError):
            self._ml_tell("Coordinate must be a number.")
            self._ml_rel_refresh(axis)
            return
        self._ml_gcode(self._ml_move_rel_script(axis, target))
        self._ml_tell("Moving %s to %.2f (work)…" % (axis.upper(), target))
        try:                       # drop focus so live telemetry resumes updating
            self.root.focus_set()
        except tk.TclError:
            pass

    def _ml_set_temp(self, heater, target=None):
        if target is None:
            try:
                target = float(self._ml_temp_set[heater].get())
            except (ValueError, tk.TclError, KeyError):
                self._ml_tell("Temperature must be a number.")
                return
        self._ml_gcode(self._ml_temp_script(heater, target))

    def _ml_set_override(self, kind):
        var = self.ml_speed_factor if kind == "speed" else self.ml_flow_factor
        try:
            value = float(var.get())
        except (ValueError, tk.TclError):
            self._ml_tell("Override must be a number.")
            return
        self._ml_gcode(self._ml_override_script(kind, value))

    def _ml_send_console(self):
        script = self.ml_console_entry.get().strip()
        if not script:
            return
        self._ml_append_console("> " + script)
        self._ml_gcode(script)
        self.ml_console_entry.set("")

    # ------------------------------------------------------------------ #
    # print control -- pause / resume
    # ------------------------------------------------------------------ #
    # Pause and resume are not symmetric one-shot POSTs, because on a real rig they are
    # not one-shot commands: Moonraker's /printer/print/{pause,resume} run the G-CODE
    # COMMANDS named PAUSE and RESUME, macros included. So (1) a rig's PAUSE macro can
    # lift Z and nothing lowers it again -- on a MEW rig the nozzle-collector distance
    # IS the process, so the pause records the work-frame Z and the resume restores it;
    # (2) that macro re-fires on EVERY press, so Pause is greyed AND refuses in code,
    # since the button state can be one poll behind; (3) a resume can fail SILENTLY,
    # because Klipper's RESUME declines with HTTP 200 and no motion whenever
    # `pause_resume.is_paused` is false, so the resume ESCALATES and VERIFIES.
    _ML_VERB_SETTLE_S = 6.0     # per attempt: how long to wait for the state to move
    _ML_VERB_POLL_S = 0.25      # checked at the printer's own ~4 Hz status cadence
    _ML_Z_RESTORE_EPS = 0.05    # mm; a drift under this is not a pause hop

    def _ml_print_state(self, client):
        """(state, is_paused) read FRESH from the printer, on the calling worker.

        Deliberately not the poller's last snapshot: that is up to a poll old and is
        delivered on the Tk thread, and after sending a verb the entire question is
        whether the machine has moved on YET. `pause_resume` is asked for only when
        the rig actually has it -- Klipper fails a whole query on one unknown object.
        `is_paused` is None when unavailable, which is never the same as False."""
        objs = {"print_stats": ["state"]}
        if self._ml_has_pause_resume:
            objs["pause_resume"] = ["is_paused"]
        st = client.query(objs)
        return (st.get("print_stats", {}).get("state"),
                st.get("pause_resume", {}).get("is_paused"))

    def _ml_wait_state(self, client, want, timeout):
        """Poll until print_stats.state is one of `want`; False if it never gets
        there. A dropped tick is treated as 'not yet', never as failure."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                state, _paused = self._ml_print_state(client)
            except ml.MoonrakerError:
                state = None
            if state in want:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._ML_VERB_POLL_S)

    def _ml_read_work_z(self, client):
        """The toolhead's work-frame (`gcode_position`) Z, or None. That is the frame
        a plain `G1 Z…` targets, so it is the one worth remembering across a pause."""
        try:
            st = client.query({"gcode_move": ["gcode_position"]})
        except ml.MoonrakerError:
            return None
        pos = st.get("gcode_move", {}).get("gcode_position")
        try:
            return float(pos[2])
        except (TypeError, ValueError, IndexError, KeyError):
            return None

    def _ml_pause(self):
        client = self._ml_client
        if client is None or self._ml_verb_busy:
            return
        if bool(self._ml_last_paused) or self._ml_last_state == "paused":
            self._ml_say("Already paused. Press Resume to carry on.", sticky=True)
            return
        if self._ml_last_state != "printing":
            self._ml_say("Nothing is printing.", sticky=True)
            return
        self._ml_verb_busy = True
        self._ml_say("Pausing…")

        def worker():
            try:
                # Read Z BEFORE the macro can lift it. A MEW layer holds Z constant,
                # so a sample from ~one poll before the stop is the print height.
                z = self._ml_read_work_z(client)
                client.print_pause()
                self._ml_pause_z = z
                if self._ml_wait_state(client, ("paused",), self._ML_VERB_SETTLE_S):
                    self._ml_q.put(("status", "Paused."))
                else:
                    self._ml_q.put(("error", "Pause was sent but the printer never "
                                             "reported 'paused'."))
            except ml.MoonrakerError as exc:
                self._ml_q.put(("failed", "Pause", exc, ""))
            finally:
                self._ml_verb_busy = False
        threading.Thread(target=worker, daemon=True, name="MLpause").start()

    def _ml_resume_attempts(self, paused_flag):
        """The resume paths to try, in order, for a job in this pause condition.

        `paused_flag` is `pause_resume.is_paused`: True (Klipper owns the pause, its
        RESUME will honour it), False (the job is stopped WITHOUT that flag, so
        RESUME will decline and only a raw `M24` restarts the SD stream), or None
        (the rig has no `[pause_resume]` at all -- try its own RESUME first, then the
        raw stream). A renamed builtin, if the rig publishes one, is the last resort:
        it is the only way past a wrapper macro that swallows the call."""
        resume = ("RESUME", lambda c: c.print_resume())
        sd = ("M24", lambda c: c.gcode("M24"))
        out = [sd, resume] if paused_flag is False else \
              ([resume] if paused_flag is True else [resume, sd])
        base = self._ml_base_cmds.get("resume")
        if base:
            out.append((base, lambda c, b=base: c.gcode(b)))
        return out

    def _ml_resume(self):
        client = self._ml_client
        if client is None or self._ml_verb_busy:
            return
        if not (bool(self._ml_last_paused) or self._ml_last_state == "paused"):
            self._ml_say("Nothing is paused.", sticky=True)
            return
        self._ml_verb_busy = True
        self._ml_say("Resuming…")
        z_pause = self._ml_pause_z
        threading.Thread(target=self._ml_resume_worker, args=(client, z_pause),
                         daemon=True, name="MLresume").start()

    def _ml_resume_worker(self, client, z_pause):
        try:
            state, paused_flag = self._ml_print_state(client)
            if state != "paused":
                self._ml_q.put(("status", "Nothing is paused."))
                return
            # Undo the PAUSE macro's z-hop ourselves when nothing else will. Klipper's
            # own RESUME ends in `RESTORE_GCODE_STATE MOVE=1` and puts the head back
            # unaided -- but only when `is_paused` is set. A job parked by a bare M25
            # saved no state, so there the lift would survive into the resumed print.
            restored = None
            if z_pause is not None and paused_flag is not True:
                z_now = self._ml_read_work_z(client)
                if z_now is not None and z_now > z_pause + self._ML_Z_RESTORE_EPS:
                    client.gcode("G90\nG1 Z%.3f F%d" % (z_pause, JOG_FEED_MMPM))
                    restored = z_pause
            for label, send in self._ml_resume_attempts(paused_flag):
                try:
                    send(client)
                except ml.MoonrakerError as exc:
                    self._ml_q.put(("console", "! resume via %s: %s" % (label, exc)))
                    continue
                if self._ml_wait_state(client, ("printing", "complete"),
                                       self._ML_VERB_SETTLE_S):
                    self._ml_pause_z = None
                    self._ml_q.put(("status", "Resumed%s%s." % (
                        "" if label == "RESUME" else " via %s" % label,
                        "" if restored is None else " (Z put back to %.2f)" % restored)))
                    return
                self._ml_q.put(("console",
                                "! %s left the print paused; trying the next path"
                                % label))
            self._ml_q.put(("error",
                            "Resume failed: the printer is still paused. Klipper's "
                            "RESUME refuses unless pause_resume.is_paused is set. "
                            "Check the printer's [pause_resume] section and its PAUSE "
                            "macro (see the console mirror)."))
        except ml.MoonrakerError as exc:
            self._ml_q.put(("failed", "Resume", exc, ""))
        finally:
            self._ml_verb_busy = False

    def _ml_cancel(self):
        if not messagebox.askyesno(
                "Cancel print",
                "Cancel the running print on %s?" % self.ml_host.get().strip()):
            return
        self._ml_verb("print_cancel")

    def _ml_verb(self, name):
        client = self._ml_client
        if client is None:
            return

        def worker():
            try:
                getattr(client, name)()
            except ml.MoonrakerError as exc:
                self._ml_q.put(("failed", _VERB_ACTIONS.get(name, name), exc,
                                "Check the printer: the print may still be "
                                "running." if name == "print_cancel" else ""))
        threading.Thread(target=worker, daemon=True, name="MLverb").start()

    def _ml_estop(self):
        """Immediate emergency stop (M112) -- one click, no confirm (a confirm would
        defeat an E-stop). Runs on its own thread so it never queues behind anything."""
        client = self._ml_client
        if client is None:
            return
        host = self.ml_host.get().strip()      # read on the Tk thread

        def worker():
            # "sent" only once the printer took it: a swallowed failure used
            # to read "EMERGENCY STOP sent." with the printer still moving
            try:
                client.emergency_stop()
                self._ml_q.put(("status", "EMERGENCY STOP sent."))
            except ml.MoonrakerError as exc:
                self._ml_q.put(("console", "! EMERGENCY STOP failed: %s" % exc))
                self._ml_q.put(("error", (
                    "EMERGENCY STOP may not have reached %s: %s. If the "
                    "printer is still moving, switch it off." % (
                        host or "the printer",
                        "it did not answer" if exc.status is None else
                        "it refused it (HTTP %s)" % exc.status))))
        threading.Thread(target=worker, daemon=True, name="MLestop").start()
        self._ml_tell("Sending EMERGENCY STOP…")

    # -- upload --------------------------------------------------------- #
    def _ml_default_upload_name(self):
        """The filename the printer would store if nothing is renamed -- the
        generated file's basename (usually "scaffold.gcode")."""
        path = getattr(self, "last_gcode", None)
        if path and getattr(self, "_last_gcode_is_calibration", False) \
                and hasattr(self, "_sw_default_name"):
            # the calibration's own name, as Save and Send give it, not the
            # scratch file's "sweep_print.gcode"
            return self._sw_default_name() + ".gcode"
        return os.path.basename(path) if path else "scaffold.gcode"

    @staticmethod
    def _ml_sanitize_filename(name, fallback):
        """A safe .gcode filename from user text: strip any path components (no
        directory traversal onto the printer), replace filesystem-hostile chars, and
        force a .gcode extension. Blank/invalid -> `fallback`."""
        base = (name or "").strip().replace("\\", "/").split("/")[-1].strip()
        base = re.sub(r"[^A-Za-z0-9._ ()\-]+", "_", base)
        base = re.sub(r"\s+", " ", base).strip()
        if not base or base in (".", ".."):
            return fallback
        if not base.lower().endswith(".gcode"):
            base += ".gcode"
        return base

    def _ml_effective_upload_name(self):
        """The name the upload will actually use: the sanitized rename box if the
        user typed one, else the generated file's basename."""
        fallback = self._ml_default_upload_name()
        typed = self.ml_upload_name.get().strip()
        return self._ml_sanitize_filename(typed, fallback) if typed else fallback

    def _ml_apply_rename(self):
        """The Rename button: normalize the box in place (so the user sees exactly
        what will be sent) and refresh the 'Will send' preview. Blank stays blank
        (meaning 'send as generated')."""
        if self.ml_upload_name.get().strip():
            self.ml_upload_name.set(
                self._ml_sanitize_filename(self.ml_upload_name.get(),
                                           self._ml_default_upload_name()))
        self._ml_refresh_print_name()

    def _ml_stale_reason(self):
        """None, or {"text", "kind"}: the file Upload would send is older
        than what the G-code page or the designer now shows
        (`_gw_stale_reason`). A result nobody stamped is never stale."""
        fn = getattr(self, "_gw_stale_reason", None)
        return fn() if fn is not None else None

    def _ml_refresh_print_name(self):
        path = getattr(self, "last_gcode", None)
        var = self._ml_print_name_var()
        n = (self._sw_rung_count()
             if getattr(self, "_last_gcode_is_calibration", False)
             and hasattr(self, "_sw_rung_count") else 0)
        if path and os.path.isfile(path) and n:
            stem = os.path.splitext(self._ml_effective_upload_name())[0]
            var.set("Will send:  %d files, %s_print1 to _print%d.gcode, one "
                    "per temperature. None starts on its own."
                    % (n, stem, n))
        elif path and os.path.isfile(path):
            var.set("Will send:  %s" % self._ml_effective_upload_name())
        else:
            var.set("No g-code generated yet.")
        box = getattr(self, "_ml_stale_box", None)
        if box is None:
            return
        why = self._ml_stale_reason() if path and os.path.isfile(path)             else None
        self._ml_stale_why = why
        if why:
            self.ml_stale_var.set(
                "Out of date. %s Send to printer and Send and start will not send "
                "it. Send the G-code page's version, or generate again."
                % why["text"])
            box.grid(row=1, column=0, sticky="w", pady=(4, 0))
            can = hasattr(self, "_gce_send") and bool(
                getattr(self, "gce", None) is not None
                and self.gce.get_text().strip())
            self._ml_stale_page_btn.configure(
                state="normal" if can else "disabled")
        else:
            box.grid_remove()

    def _ml_send_page_version(self):
        """The way forward from an out-of-date file: send the G-code
        page's own text (what Save writes), speaking here."""
        if not hasattr(self, "_gce_send"):
            return
        self._gce_send(report=lambda text, buttons=(): self.ml_conn_status
                       .set(text))

    def _ml_open_maker(self):
        why = self._ml_stale_why or {}
        self._nav_select("gcode" if why.get("kind") == "page"
                         else "scaffold")

    def _ml_upload(self, start):
        path = getattr(self, "last_gcode", None)
        if not path or not os.path.isfile(path):
            self._ml_tell("Generate a scaffold or g-code first.")
            return
        why = self._ml_stale_reason()
        if why:
            self._ml_refresh_print_name()
            self._ml_tell(
                "Not sent: %s Use “Send the G-code page's version” "
                "or generate again." % why["text"])
            return
        name = self._ml_effective_upload_name()
        # a temperature ladder goes up one file per rung, and none of them
        # starts: the operator sets each rung's temperature first
        rungs = (self._sw_rung_files(name)
                 if getattr(self, "_last_gcode_is_calibration", False)
                 and hasattr(self, "_sw_rung_files") else None)
        if rungs:
            if not self._sw_rung_confirm(rungs, self.ml_host.get().strip()):
                return
            note = self._sw_rung_note(rungs, self.ml_host.get().strip())
            if self._ml_upload_files(rungs, os.path.dirname(path), note):
                self._ml_tell(note)
            return
        # the bytes Save g-code writes (the Machine page's flavor, start and
        # end, origin...), never the solver's scratch copy of them
        fn = getattr(self, "_gcode_save_text", None)
        text = fn(name) if fn is not None else None
        shown = text
        if shown is None:
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    shown = fh.read()
            except OSError:
                shown = None
        # what Save would ask about these bytes, Send asks too
        ok = getattr(self, "_preflight_ok", None)
        if callable(ok) and not ok(shown, designed=self._gcode_is_designer_output(),
                                   send=True):
            self._ml_tell("Not sent.")
            return
        if start:
            heat = self._ml_heat_note(shown)
            if not messagebox.askyesno(
                    "Send and start",
                    "Send %s to %s and start printing it now?%s"
                    % (name, self.ml_host.get().strip(),
                       ("\n\n" + heat) if heat else "")):
                return
        if text is not None:
            path = os.path.join(os.path.dirname(path), "upload.gcode")
            try:
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
            except OSError as exc:
                self._ml_tell(
                    "Not sent: the file could not be prepared (%s)." % exc)
                return
        self._ml_do_upload(path, start, name)

    def _ml_upload_files(self, files, folder, note=""):
        """Upload several prints, `files` = [(name, text, ...)], one after
        another on one worker thread, none of them started. Each is written
        to `folder` first; False (and a status line) if one cannot be.
        `note` is the status once the last one is up."""
        client = self._ml_client
        if client is None:
            return False
        staged = []
        for i, f in enumerate(files, 1):
            path = os.path.join(folder, "upload_%d.gcode" % i)
            try:
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(f[1])
            except OSError as exc:
                self._ml_tell(
                    "Not sent: the file could not be prepared (%s)." % exc)
                return False
            staged.append((path, f[0]))
        host = self.ml_host.get().strip()      # read on the Tk thread

        def worker():
            for path, name in staged:
                try:
                    client.upload(path, filename=name, start=False)
                    self._ml_q.put(("uploaded", name, False))
                except ml.MoonrakerError as exc:
                    self._ml_q.put(("console", "! upload of %s failed: %s"
                                    % (name, exc)))
                    self._ml_q.put(("upload_failed", name,
                                    _upload_failure_text(host, name, exc)))
                    return
            if note:
                self._ml_q.put(("uploaded_all", note))
        t = threading.Thread(target=worker, daemon=True, name="MLupload")
        t.start()
        return t

    def _ml_do_upload(self, path, start, name=None):
        """Upload `path` to the printer on a worker thread, storing it as `name`
        (defaults to the effective rename); status posts back via the queue. Returns
        the thread (tests join it)."""
        client = self._ml_client
        if client is None:
            return None
        name = name or self._ml_effective_upload_name()
        host = self.ml_host.get().strip()     # read here: the worker is off Tk
        self._ml_tell("Uploading %s…" % name)

        def worker():
            try:
                client.upload(path, filename=name, start=start)
                self._ml_q.put(("uploaded", name, start))
            except ml.MoonrakerError as exc:
                self._ml_q.put(("console", "! upload of %s failed: %s"
                                % (name, exc)))
                self._ml_q.put(("upload_failed", name,
                                _upload_failure_text(host, name, exc)))
        t = threading.Thread(target=worker, daemon=True, name="MLupload")
        t.start()
        return t

    # -- g-code file browser (list / download / delete, paged) --------- #
    def _ml_refresh_files(self):
        """Fetch the printer's stored g-code list on a worker thread; the result
        rides the queue back to the Tk thread as a ('files', [...]) event."""
        client = self._ml_client
        if client is None:
            return

        def worker():
            try:
                self._ml_q.put(("files", client.list_files()))
            except ml.MoonrakerError as exc:
                self._ml_q.put(("failed", "Listing the printer's files", exc,
                                ""))
        threading.Thread(target=worker, daemon=True, name="MLfiles").start()

    def _ml_on_files(self, files):
        """Adopt a fresh file list (newest first), clamp the page, and re-render."""
        def _mtime(f):
            try:
                return float(f.get("modified") or 0.0)
            except (TypeError, ValueError):
                return 0.0
        self._ml_files = sorted(
            [f for f in files if isinstance(f, dict) and f.get("path")],
            key=_mtime, reverse=True)
        self._ml_render_files_page()

    def _ml_pages(self):
        per = self._ml_files_per_page
        return max(1, (len(self._ml_files) + per - 1) // per)

    def _ml_render_files_page(self):
        tree = self._ml_files_tree
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)
        n = len(self._ml_files)
        per = self._ml_files_per_page
        pages = self._ml_pages()
        page = max(0, min(self._ml_files_page, pages - 1))
        self._ml_files_page = page
        for f in self._ml_files[page * per:page * per + per]:
            name = f.get("path", "")
            tree.insert("", "end", iid=name,
                        values=(name, _human_size(f.get("size")),
                                _fmt_mtime(f.get("modified"))))
        if not self._ml_connected:
            self.ml_files_page_text.set("Connect to list files.")
        elif n:
            self.ml_files_page_text.set("Page %d of %d  (%d file%s)"
                                        % (page + 1, pages, n, "" if n == 1 else "s"))
        else:
            self.ml_files_page_text.set("No files on the printer.")
        self._ml_files_prev_btn.configure(
            state="normal" if page > 0 else "disabled")
        self._ml_files_next_btn.configure(
            state="normal" if page < pages - 1 else "disabled")

    def _ml_files_prev(self):
        if self._ml_files_page > 0:
            self._ml_files_page -= 1
            self._ml_render_files_page()

    def _ml_files_next(self):
        if self._ml_files_page < self._ml_pages() - 1:
            self._ml_files_page += 1
            self._ml_render_files_page()

    def _ml_selected_file(self):
        tree = self._ml_files_tree
        sel = tree.selection() if tree is not None else ()
        return sel[0] if sel else None

    def _ml_download_selected(self):
        name = self._ml_selected_file()
        if not name:
            self._ml_tell("Select a file in the list to download.")
            return
        client = self._ml_client
        if client is None:
            return
        dest = filedialog.asksaveasfilename(
            **gui_prefs.save_dir_kw(),
            title="Save g-code as…", initialfile=os.path.basename(name),
            defaultextension=".gcode",
            filetypes=[("G-code", "*.gcode"), ("All files", "*.*")])
        if not dest:
            return
        self._ml_tell("Downloading %s…" % name)

        def worker():
            try:
                client.download_file(name, dest)
                self._ml_q.put(("downloaded", name, dest))
            except ml.MoonrakerError as exc:
                self._ml_q.put(("failed", "Download of %s" % name, exc, ""))
            except OSError as exc:
                self._ml_q.put(("error", "Download failed: %s could not be "
                                "written (%s)." % (dest, exc.strerror or exc)))
        threading.Thread(target=worker, daemon=True, name="MLdownload").start()

    def _ml_start_selected(self):
        """Start a file already on the printer. Send's "it has not been
        started" and a temperature ladder's "start that file on the printer"
        both send the operator here, and before this button the page had no
        way to do it."""
        name = self._ml_selected_file()
        if not name:
            self._ml_tell("Select a file in the list to start.")
            return
        if getattr(self, "_ml_last_state", None) in ("printing", "paused"):
            self._ml_tell("Not started: a print is running. Cancel it first.")
            return
        client = self._ml_client
        if client is None:
            return
        if not messagebox.askyesno(
                "Start print",
                "Start printing %s on %s now?"
                % (name, self.ml_host.get().strip())):
            return
        self._ml_tell("Starting %s…" % name)

        def worker():
            try:
                client.print_start(name)
                self._ml_q.put(("started", name))
            except ml.MoonrakerError as exc:
                self._ml_q.put(("start_failed", name, exc))
        threading.Thread(target=worker, daemon=True, name="MLstart").start()

    def _ml_delete_selected(self):
        name = self._ml_selected_file()
        if not name:
            self._ml_tell("Select a file in the list to delete.")
            return
        if not messagebox.askyesno(
                "Delete file",
                "Delete %s from %s?\nThis cannot be undone."
                % (name, self.ml_host.get().strip())):
            return
        client = self._ml_client
        if client is None:
            return
        self._ml_tell("Deleting %s…" % name)

        def worker():
            try:
                client.delete_file(name)
                self._ml_q.put(("deleted", name))
            except ml.MoonrakerError as exc:
                self._ml_q.put(("failed", "Deleting %s" % name, exc, ""))
        threading.Thread(target=worker, daemon=True, name="MLdelete").start()

    # -- console + recording ------------------------------------------- #
    def _ml_append_console(self, line):
        w = getattr(self, "_ml_console", None)
        if w is None:
            return
        try:
            w.configure(state="normal")
            w.insert("end", line.rstrip("\n") + "\n")
            w.see("end")
            w.configure(state="disabled")
        except tk.TclError:
            pass

    def _ml_toggle_record(self):
        if self.ml_record.get():
            if self._ml_connected:
                self._ml_start_record()
        else:
            self._ml_stop_record()

    def _ml_start_record(self):
        if self._ml_recorder is not None:
            return
        path = filedialog.asksaveasfilename(
            **gui_prefs.save_dir_kw(),
            title="Record telemetry to…", defaultextension=".csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            self.ml_record.set(False)
            return
        try:
            self._ml_recorder = ml.TelemetryRecorder(
                path, heaters=list(getattr(self, "_ml_heaters", [])))
            self._ml_tell("Recording telemetry to %s"
                                    % os.path.basename(path))
        except OSError as exc:
            self.ml_record.set(False)
            self._ml_tell("Could not open CSV: %s" % exc)

    def _ml_stop_record(self):
        if self._ml_recorder is not None:
            self._ml_recorder.close()
            self._ml_recorder = None

    # ------------------------------------------------------------------ #
    # viewer
    # ------------------------------------------------------------------ #
    def _ml_launch_viewer(self):
        """Launch the machine-only live view into the embedded panel, fed by the
        livepos stream. Uses the configured printer model (Machine & collector) when
        there is one; otherwise falls back to the bundled printhead so there is always
        a moving nozzle to watch. The live feed is RAW machine coordinates, so no
        g-code origin re-base is applied (see the rig-day frame-alignment check)."""
        margs = self._machine_args() if hasattr(self, "_machine_args") else []
        using_fallback = False
        if not margs:
            margs = self._ml_fallback_machine_args()
            using_fallback = bool(margs)
        if not margs:
            self.ml_viewer_status.set(
                "Live 3-D view unavailable (no printer model and no bundled "
                "printhead). Telemetry + control still work.")
            return
        # the reveal source: the printer's own file (see _ml_reveal_source_args).
        # [] keeps the bodies-only view, exactly as before.
        src = self._ml_reveal_source_args()
        self._ml_launched_src = src[0] if src else None
        argv = worker_cmd("worker:player", unbuffered=False) \
            + ["--machine-live"] + margs + src
        self._ml_panel.launch(argv, subtitle=self.ml_host.get().strip())
        self._ml_launched = True
        self.ml_viewer_status.set(
            "Showing the printhead only. Set up a full printer model on the "
            "Printer & collector page to see the whole printer." if using_fallback
            else "")

    def _ml_fallback_machine_args(self):
        """A minimal one-body machine profile (the bundled printhead STL) written to
        the session dir, so the live view shows a moving nozzle even when no full
        printer model is configured. [] if the asset is missing."""
        try:
            from app_paths import resource_path
            head = resource_path("machine_models", "MEWron_0.2",
                                 "Printhead STL.stl")
            if not os.path.isfile(head):
                return []
            os.makedirs(SESSION_DIR, exist_ok=True)
            path = os.path.join(SESSION_DIR, "machine_link_fallback.json")
            with open(path, "w") as fh:
                json.dump({"kinematics": "mewron", "parts": {"head": head},
                           "scale": 1.0}, fh)
            return ["--machine", path]
        except Exception:  # noqa: BLE001
            return []

    def _ml_relaunch_viewer(self):
        """The panel's Relaunch button (shown if the child crashes)."""
        if self._ml_connected:
            self._ml_launch_viewer()

    # ------------------------------------------------------------------ #
    # the live scaffold reveal
    # ------------------------------------------------------------------ #
    # Where the local copy of the printing file is kept. It is a COPY OF THE
    # PRINTER'S FILE, not whatever this GUI generated last -- that distinction is the
    # whole point of the download. An earlier attempt fed `self.last_gcode` to the
    # viewer with no check that the printer was running the same thing; a stale or
    # unrelated scaffold there means different geometry AND a different timeline, so
    # every reveal cursor built on top of it is meaningless.
    _ML_SRC_DIRNAME = "machine_link_src"

    def _ml_reveal_wanted(self):
        return bool(self.ml_reveal_scaffold.get() or self.ml_reveal_gcode.get())

    def _ml_sync_reveal_source(self, snap):
        """Keep a local copy of the file the printer is running, fetching it when the
        printer starts a different one. Downloads on a short thread (never on the Tk
        loop); the result comes back as an ("src", …) event. No-op when neither reveal
        box is ticked, while a fetch is in flight, or when nothing is printing."""
        if not self._ml_reveal_wanted() or self._ml_src_busy:
            return
        name = snap.filename
        if not name or snap.state not in ("printing", "paused"):
            return
        key = (name, snap.file_size)
        if key == self._ml_src_key:
            return
        client = self._ml_client
        if client is None:
            return
        self._ml_src_busy = True
        self.ml_reveal_status.set("Fetching %s from the printer…"
                                  % os.path.basename(name))
        threading.Thread(target=self._ml_fetch_source, args=(client, name, key),
                         daemon=True).start()

    def _ml_fetch_source(self, client, name, key):
        """Download the printing g-code to the session dir (worker thread)."""
        try:
            dest_dir = os.path.join(SESSION_DIR, self._ML_SRC_DIRNAME)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(name))
            client.download_file(name, dest)
            self._ml_q.put(("src", key, dest, None))
        except Exception as exc:  # noqa: BLE001
            self._ml_q.put(("src", key, None, str(exc)))

    def _ml_refresh_reveal_debug(self):
        """Mirror the viewer child's seating numbers into the form.

        `base` is where the first deposited layer is actually drawn, `surf` is the
        print surface it was seated on and `bed` is the highest point of the bed
        assembly -- all world Z, all measured inside the rendered scene. If the
        overlay ever looks sunk into the collector again, these three numbers say so
        directly instead of it having to be inferred from a screenshot."""
        panel = self._ml_panel
        st = getattr(panel, "_last_state", None) if panel is not None else None
        if not st or not st.get("reveal"):
            self.ml_reveal_debug.set("")
            return
        def _n(k):
            v = st.get(k)
            return "—" if v is None else ("%.3f" % v)
        gap = ("—" if st.get("base") is None or st.get("surf") is None
               else "%+.3f" % (st["base"] - st["surf"]))
        self.ml_reveal_debug.set(
            "Z  base %s  surface %s  gap %s  bed-max %s"
            % (_n("base"), _n("surf"), gap, _n("bedtop")))

    def _ml_reveal_source_args(self):
        """Player args that load the reveal source, or [] for the bodies-only view.

        The source is the DOWNLOADED file. When our last export happens to be
        byte-identical to it we also pass that export's `.depo` sidecar and fiber
        params, so the mesh is the exported geometry (physics shaping and all)
        instead of a re-derived one; when they differ we pass the file alone and let
        the player rebuild the mesh from it. Never `--origin`: machine-live renders
        raw machine coordinates and registers the overlay with the streamed work
        offset H, not the export datum."""
        src = self._ml_src_path
        if not (self._ml_reveal_wanted() and src and os.path.isfile(src)):
            return []
        args = [src]
        # The fiber + physics knobs describe HOW to build the mesh, so they go every
        # time -- they are the settings on screen, and they apply to whatever the
        # printer is running. Gating them on the cache match (as this first did) meant
        # that printing a file we had not just exported dropped `--physics-deposit`,
        # and the live view quietly drew the COMMANDED fiber instead of the deposited
        # one: right-looking geometry with no jet-lag and no corner rounding.
        if getattr(self, "last_fiber_params", None):
            args += list(self.last_fiber_params)
        for hook in ("_sim_play_args", "_sim_cone_args"):
            if hasattr(self, hook):
                args += getattr(self, hook)()
        # The `.depo` sidecar is the one thing that IS file-specific: it is the
        # exported geometry for OUR export, so it is attached only when the printer is
        # running exactly those bytes. Otherwise the player re-derives the mesh from
        # the downloaded file (with the physics transform installed, per the args
        # above), which is correct for the file actually printing but pays the whole
        # sag solve before the first frame -- 6.0 s on a 10x10 mm 20-layer scaffold
        # and 67.1 s on 20x20x40, which is the "Starting the viewer..." wait.
        depo = self._ml_depo_for_source(src)
        if depo:
            args += ["--depo", depo]
        args += ["--render", "tube:12"]      # smooth-but-fast tubes for the live view
        if self.ml_reveal_scaffold.get():
            args.append("--reveal-scaffold")
        if self.ml_reveal_gcode.get():
            args.append("--reveal-gcode")
        return args

    def _ml_export_matches(self, src, gco=None):
        """True when a local export is byte-identical to `src` -- i.e. the printer is
        running exactly what we exported, so that run's .depo cache describes this
        print. Defaults to the LAST export; `_ml_depo_for_source` passes each indexed
        one in turn.

        Byte-identity, not a name or a timestamp, and that is the safety property the
        whole feature rests on: a `.depo` describes one specific set of bytes, and
        attaching the wrong one draws a different scaffold on a different timeline
        while looking entirely plausible."""
        gco = gco or getattr(self, "last_gcode", None)
        if not (gco and os.path.isfile(gco)):
            return False
        try:
            if os.path.getsize(gco) != os.path.getsize(src):
                return False
            with open(gco, "rb") as a, open(src, "rb") as b:
                return a.read() == b.read()
        except OSError:
            return False

    def _ml_depo_for_source(self, src):
        """The `.depo` of ANY export this session that is byte-identical to `src`.

        Not just the last one. `last_gcode`/`last_depo` track the newest export, so the
        old single check silently stopped working the moment anything else was
        generated -- which is the normal way to work: generate, upload, print, carry on
        tweaking. Measured on a 10x10 mm 20-layer scaffold, the viewer opened in 2.7 s
        with the cache attached and 8.2 s without, all of it the sag solve; on 20x20x40
        that difference is 67 s.

        Still bounded work: `EXPORT_INDEX_MAX` entries, each rejected on a size
        comparison before anything is read. NOT covered, and it cannot be from here: a
        file already on the printer from an EARLIER session, whose scratch folder is
        gone -- the viewer re-solves, which is correct rather than merely slow."""
        if not (src and os.path.isfile(src)):
            return None
        seen = set()
        cands = [(getattr(self, "last_gcode", None), getattr(self, "last_depo", None))]
        cands += list(getattr(self, "export_index", []))
        for gco, depo in cands:
            if not (gco and depo) or gco in seen:
                continue
            seen.add(gco)
            if not os.path.isfile(depo):
                continue                     # scratch folder cleaned out from under us
            if self._ml_export_matches(src, gco):
                return depo
        return None

    def _ml_clear_scaffold(self):
        """"Clear scaffold": drop the deposited mesh from the live view.

        Sent to the child BEFORE the checkbox is cleared, because `_ml_toggle_reveal`
        may then decide the viewer no longer wants a source and relaunch it -- the
        clear has to reach the process that is holding the mesh. The checkbox goes
        with it so the form keeps telling the truth about what is drawn."""
        if self._ml_launched and self._ml_panel is not None:
            self._ml_panel.send_child(t="reveal", clear=True, scaffold=False)
        self.ml_reveal_scaffold.set(False)
        self._ml_toggle_reveal()

    def _ml_toggle_reveal(self):
        """A reveal checkbox changed: persist it and push it to the live view. Toggled
        in place when the running viewer already has the source; otherwise relaunch so
        the source can come in (or drop out)."""
        self._ml_save_prefs()
        if not (self._ml_launched and self._ml_panel is not None):
            return
        want_src = bool(self._ml_reveal_source_args())
        if want_src != bool(self._ml_launched_src):
            self._ml_launch_viewer()
            return
        self._ml_panel.send_child(
            t="reveal",
            scaffold=bool(self.ml_reveal_scaffold.get()),
            gcode=bool(self.ml_reveal_gcode.get()))
