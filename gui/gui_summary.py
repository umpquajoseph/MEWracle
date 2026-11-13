#!/usr/bin/env python3
"""gui_summary.py -- MODERN-UI ONLY: the design's one line in the top row, a
slicer-style summary that stays in view on every page.

Three things, the third the one that matters:
  1. what Generate builds from ("Flat plate · 2 scaffolds", "G-code import",
     "Calibration print", ...) -- the same target `_active_mode` resolves, so on
     a non-input page it names the design page the bottom bar still points at;
  2. that design in one line, reusing the Scaffold designer's own Geometry tile
     summary (`_summary_geometry`) so the two never disagree -- less its
     "linking loops" tag, which the tile keeps. It rides in the tooltip;
  3. whether the last Generate still matches the form: "Not generated yet",
     "Up to date", or "Changed since Generate". Save g-code as…, Simulation
     and Send to printer all act on the LAST RESULT, not on the form, and
     nothing else on screen says the two have drifted apart.

How (3) is decided. When Generate is pressed the shell's `run` calls
`_summary_note_run`, which records a SIGNATURE of the design: the preset
snapshot (`_snapshot_for_kind`) of every key group that reaches that mode's
output, minus the display-only keys (the preview toggles, the viewer
colours). A result is recognised when the files it consists
of (`last_gcode`, `last_depo`, `last_stl`) change path or mtime, which every
successful Generate does; the recorded signature becomes the result's. A
Generate that is cancelled or fails leaves the note pending and the state
reads against the previous result. A one-second tick then
compares it with the live form. A full signature is ~1.5 ms, so the tick
costs nothing measurable.

Nothing here is persisted and nothing reaches a worker.

The classic (developer) UI never imports this module."""

from gui_common import *

import gui_tips

# Keys that change the picture but never the generated files. The preview's
# own toggles live in the scaffold key group for persistence, so without this
# a ticked "Sagging" preview box would read as a changed design.
_DISPLAY_PREFIXES = ("pv.",)
_DISPLAY_SUFFIXES = (".color",)

# Which preset key groups reach each input mode's output. The Physics page
# ("calibration" group) and the Machine page ride every run.
_MODE_GROUPS = {
    "scaffold": ("scaffold", "machine", "calibration"),
    "gcode": ("scaffold", "machine", "calibration"),
    "calibration": ("sweep", "machine", "calibration"),
}

# line 1 names the page Generate builds from, then its collector: the state
# beside it ("– Not generated yet") is about that page's design
_COLLECTOR_NAMES = {
    "plate": "Scaffold designer · plate",
    "mandrel": "Scaffold designer · mandrel",
}

#: where a result came from, as the page that made it is named
_RESULT_FROM = {"scaffold": "Scaffold designer", "gcode": "G-code window",
                "calibration": "Calibrate"}

_MODE_PAGES = {"scaffold": "scaffold", "gcode": "gcode",
               "calibration": "calibration"}

TICK_MS = 1000

_TIP = ("What Generate builds, and whether the last result still matches it. "
        "Save g-code as, Simulation and Send to printer all use the last "
        "result. Click to open the design.")


class _SummaryMixin:
    def _summary_mode(self):
        """`_active_mode`, with the Scaffold designer's two collectors folded
        into one: "mandrel" is the same page and the same key groups, and
        while its run dispatches the same call answers "scaffold"."""
        # while the G-code page dispatches the designer's own Generate,
        # `_active_mode` answers as the designer would; the design is the page's
        if getattr(self, "_gce_dispatch", None):
            return "gcode"
        mode = self._active_mode()
        return "scaffold" if mode == "mandrel" else mode

    # ------------------------------------------------------------------ #
    # build
    # ------------------------------------------------------------------ #
    def _build_design_summary(self, parent):
        """The line, a child of `parent` (the top row). Returns it."""
        box = ttk.Frame(parent, style="Bar.TFrame", cursor="hand2")
        self._sum_box = box              # the tour points at it
        self._sum_target = tk.StringVar(value="")
        self._sum_detail = tk.StringVar(value="")
        self._sum_state = tk.StringVar(value="")
        # the state packs first: a narrow window shortens the design's name
        s = ttk.Label(box, textvariable=self._sum_state,
                      style="BarHint.TLabel")
        s.pack(side="right")
        t = ttk.Label(box, textvariable=self._sum_target,
                      style="BarHint.TLabel", anchor="e")
        t.pack(side="right", padx=(0, 8))
        self._sum_target_lbl = t
        self._sum_state_lbl = s
        box.bind("<Configure>", self._summary_fit, add="+")
        self._sum_tips = []
        for w in (box, t, s):
            w.bind("<Button-1>", self._summary_open_design)
            self._sum_tips.append(gui_tips.Tooltip(w, _TIP))
        self._sum_result_key = None     # (path, mtime) of the result we know
        self._sum_result_sig = None     # the design signature that made it
        self._sum_result_mode = None
        self._sum_pending = None        # (mode, sig) noted when Generate ran
        self._sum_after = None
        self._summary_refresh()
        return box

    # ------------------------------------------------------------------ #
    # the signature
    # ------------------------------------------------------------------ #
    def _design_signature(self, mode):
        """A comparable snapshot of every input that reaches `mode`'s output,
        or None when this build cannot say (no preset engine, an unknown mode)."""
        groups = _MODE_GROUPS.get(mode)
        snap = getattr(self, "_snapshot_for_kind", None)
        if groups is None or snap is None:
            return None
        sig = {}
        for g in groups:
            try:
                part = snap(g)
            except Exception:  # noqa: BLE001  (a half-built page)
                return None
            for k, v in part.items():
                if k.startswith(_DISPLAY_PREFIXES) or \
                        k.endswith(_DISPLAY_SUFFIXES):
                    continue
                # the G-code import page's own fields reach only an import;
                # a scaffold run never reads them, and vice versa
                if g == "scaffold" and (mode == "gcode") != k.startswith("g."):
                    continue
                sig[k] = v
        if mode == "gcode":
            try:
                sig["__input__"] = self.in_var.get().strip()
            except (AttributeError, tk.TclError):
                pass
        return sig

    def _summary_note_run(self):
        """Generate was pressed: remember what it was asked to build."""
        try:
            mode = self._summary_mode()
        except Exception:  # noqa: BLE001
            return
        self._sum_pending = (mode, self._design_signature(mode))
        self._summary_refresh()

    def _summary_renote(self, mode, was, now):
        """A result (or a running Generate) noted as `was` is known to be
        `now`: the G-code page filled in with exactly the text an earlier
        press built from it while it was empty."""
        if self._sum_pending == (mode, was):
            self._sum_pending = (mode, now)
        if self._sum_result_mode == mode and self._sum_result_sig == was:
            self._sum_result_sig = now
        self._summary_refresh()

    # ------------------------------------------------------------------ #
    # refresh
    # ------------------------------------------------------------------ #
    def _summary_result_key(self):
        """(path, mtime) of each file the last result consists of, or None when
        there is no g-code. The .depo and STL are in it because a G-code import
        re-run leaves `last_gcode` -- the user's own input file -- untouched."""
        if not getattr(self, "last_gcode", None):
            return None
        key = []
        for attr in ("last_gcode", "last_depo", "last_stl"):
            path = getattr(self, attr, None)
            if not path:
                continue
            try:
                key.append((path, os.path.getmtime(path)))
            except OSError:
                if attr == "last_gcode":
                    return None
        return tuple(key)

    def _summary_target(self, mode):
        """(line 1, line 2) for `mode`."""
        if mode == "scaffold":
            coll = "plate"
            try:
                coll = self.s_collector.get() or "plate"
            except (AttributeError, tk.TclError):
                pass
            name = _COLLECTOR_NAMES.get(coll, "Scaffold designer")
            try:
                n = int(self.s_count.get())
            except (AttributeError, ValueError, tk.TclError):
                n = 1
            if coll == "plate" and n > 1:
                name = "Scaffold designer · %d scaffolds" % n
            detail = ""
            fmt = getattr(self, "_summary_geometry", None)
            if fmt is not None:
                try:
                    detail = fmt(self.scaf_vars[0], loops=False)
                except Exception:  # noqa: BLE001  (mid-keystroke)
                    detail = ""
            return name, detail
        if mode == "gcode":
            try:
                path = self.in_var.get().strip()
            except (AttributeError, tk.TclError):
                path = ""
            return "G-code import", (os.path.basename(path) if path
                                     else "No file chosen")
        if mode == "calibration":
            detail = ""
            sw = getattr(self, "sw_vars", None)
            names = getattr(self, "_PROTOCOL_NAMES", {})
            if sw is not None and "kind" in sw:
                try:
                    kind = sw["kind"].get()
                    detail = names.get(kind, kind)
                    # the names are written to follow "build ..." in a
                    # sentence ("the CTS speed ladder"); a line of its own
                    # drops the article
                    if detail.lower().startswith("the "):
                        detail = detail[4:]
                    detail = detail[:1].upper() + detail[1:]
                except tk.TclError:
                    pass
            return "Calibration print", detail
        return "", ""

    def _summary_refresh(self):
        """Recompute all three lines. Cheap; called by the tick and on page
        switches."""
        if not hasattr(self, "_sum_state"):
            return
        try:
            mode = self._summary_mode()
        except Exception:  # noqa: BLE001
            return
        target, detail = self._summary_target(mode)
        self._sum_target.set(target)
        self._sum_detail.set(detail)
        self._summary_fit()
        for tip in self._sum_tips:
            tip.text = detail + "\n\n" + _TIP if detail else _TIP

        # A run writes its files in stages (the g-code is announced before the
        # .depo exists), so a result is only read once the worker has exited.
        busy = getattr(self, "proc", None) is not None
        if busy:
            self._sum_state.set("Generating…")
            self._sum_state_lbl.configure(style="BarHint.TLabel")
            return
        # a new result landed: the signature noted at Generate is now its
        key = self._summary_result_key()
        if key != self._sum_result_key:
            self._sum_result_key = key
            if key is None:
                self._sum_result_sig = self._sum_result_mode = None
            elif self._sum_pending is not None:
                self._sum_result_mode, self._sum_result_sig = self._sum_pending
                self._sum_pending = None
            else:                       # a result we did not see requested
                self._sum_result_mode, self._sum_result_sig = mode, None

        if key is None:
            state, style = "– Not generated yet", "BarHint.TLabel"
        elif self._sum_result_mode != mode:
            # say WHICH page, or a user who generated on the Scaffold
            # designer and then looked in on the G-code window cannot tell
            # whose print Simulation is playing
            state = "– Last result: " + _RESULT_FROM.get(
                self._sum_result_mode, "another page")
            style = "BarHint.TLabel"
        elif self._sum_result_sig is None:
            state, style = "Generated", "BarHint.TLabel"
        elif self._design_signature(mode) == self._sum_result_sig:
            state, style = "✓ Up to date", "BarOk.TLabel"
        else:
            state, style = "! Changed since Generate", "BarWarn.TLabel"
        self._sum_state.set(state)
        try:
            self._sum_state_lbl.configure(style=style)
        except tk.TclError:
            pass
        self._summary_fit()

    def _summary_fit(self, _e=None):
        """A narrow row drops the design's name whole, not half of it, and a
        narrower one drops the state whole too: its tooltip's detail and the
        bottom row's buttons still say what the state said."""
        box, t, s = self._sum_box, self._sum_target_lbl, self._sum_state_lbl
        try:
            room = box.winfo_width()
            if room <= 1:
                return
            state = s.winfo_reqwidth()
            show_state = room >= state
            show_name = room >= t.winfo_reqwidth() + 8 + state
            if show_state and not s.winfo_manager():
                s.pack(side="right")
            elif not show_state and s.winfo_manager():
                s.pack_forget()
            if show_name and not t.winfo_manager():
                t.pack(side="right", padx=(0, 8))
            elif not show_name and t.winfo_manager():
                t.pack_forget()
        except tk.TclError:
            pass

    def _summary_tick(self):
        self._sum_after = None
        try:
            self._summary_refresh()
        finally:
            try:
                self._sum_after = self.root.after(TICK_MS, self._summary_tick)
            except tk.TclError:
                self._sum_after = None

    def _summary_start(self):
        if getattr(self, "_sum_after", None) is None:
            self._summary_tick()

    def _summary_open_design(self, _e=None):
        try:
            mode = self._summary_mode()
        except Exception:  # noqa: BLE001
            return
        pid = _MODE_PAGES.get(mode)
        if pid:
            self._nav_select(pid)
