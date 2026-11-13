#!/usr/bin/env python3
"""gui_revert.py -- the per-field "revert to preset" arrow, on every page.

A slicer shows a small undo arrow beside any setting that has been hand-edited
away from the loaded profile; clicking it puts the profile's value back. This is
that, for every persisted field of the app.

**The baseline.** Each persist key has a baseline: the value it held right after
the last preset that owns it was applied (or saved over), else the factory value.
The baseline is stored per KEY, and `_keys_for_kind` already says which preset
kind owns which keys, so loading a Machine preset re-baselines only the Machine
fields and leaves a hand-edited scaffold field marked. A key's mark is lit while
its var differs from its baseline.

**The marks.** Pages are built with several different builders (gui_form rows,
hand-gridded widgets, the classic tabs), so no builder is hooked. Instead
`_revert_scan` walks the widget tree, and any entry / combobox / spinbox /
checkbutton / scale / SegmentedBar bound to a persisted var is recorded as a
target, and gets one small ↺ label `place`d just after it, on its right, the
first time its field differs from the baseline. Made lazily because a boot
records ~500 targets and a handful are ever edited: building every label and
tooltip up front held the just-shown window for 2-4 s. A placed widget
follows its target and is unmapped with it, so a hidden row or an unselected page
shows no arrow and no layout is touched. A radio group has one var and no single
home for a mark, so it is skipped.

A var edited while a preset is being applied is not a hand edit: marks refresh at
the end of the load, once the baseline has moved. Comparison is on the Tk string
(`"1.0"` vs `"1.00"` match when both parse as the same number); a var that cannot
be read (a half-typed number) counts as edited.
"""
import tkinter as tk
from tkinter import ttk

import gui_tips
import gui_units
import gui_widgets

GLYPH = "↺"                          # ↺
STYLE = "Revert.TLabel"
_BOUND = {"TEntry", "Entry", "TCombobox", "TSpinbox", "Spinbox", "TCheckbutton",
          "Checkbutton", "TScale", "Scale"}
_RESCAN_MS = 250


def _raw(w):
    """The comparison form of a persisted var: its Tk string, or None if unreadable."""
    try:
        return str(w.get())
    except Exception:  # noqa: BLE001  (an empty DoubleVar mid-typing)
        return None


def skip(widget):
    """`widget` and everything in it edit no persisted field: built on the fly
    (a list row, a form), they never get an arrow and mapping them never
    rescans the app."""
    stack = [widget]
    while stack:
        w = stack.pop()
        w._rv_done = True
        stack.extend(w.winfo_children())
    return widget


def _same(a, b):
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


class _RevertMixin:
    """Mixed into the preset mixin; state is created lazily on first use."""

    # -- state -------------------------------------------------------------- #
    def _rv(self):
        st = getattr(self, "_rv_state", None)
        if st is None:
            st = self._rv_state = {
                "base": {},          # key -> (typed, raw) at the last preset load
                "by_var": {},        # tk var name -> [key, ...]
                "targets": {},       # tk var name -> [widget it marks, ...]
                "scan_after": None,
                "on": False,
            }
        return st

    def _revert_vars(self):
        """tk var name -> keys, rebuilt each scan (a scaffold can add keys)."""
        out = {}
        for k, w in self._persist_map().items():
            name = getattr(w, "_name", None)
            if name:
                out.setdefault(str(name), []).append(k)
        return out

    # -- baseline ------------------------------------------------------------ #
    def _revert_rebaseline(self, keys=None):
        """Record the current value of `keys` (default: all) as their baseline,
        then clear/relight their marks."""
        st = self._rv()
        pmap = self._persist_map()
        for k in (pmap if keys is None else keys):
            w = pmap.get(k)
            if w is None:
                continue
            try:
                typed = w.get()
            except Exception:  # noqa: BLE001
                typed = None
            st["base"][k] = (typed, _raw(w))
        self._revert_refresh_all()

    def _revert_differs(self, key):
        st = self._rv()
        w = self._persist_map().get(key)
        base = st["base"].get(key)
        if w is None or base is None:
            return False
        cur = _raw(w)
        return cur is None or not _same(cur, base[1])

    def _revert_key(self, key):
        """Put one field back to its baseline."""
        st = self._rv()
        w = self._persist_map().get(key)
        base = st["base"].get(key)
        if w is None or base is None:
            return
        typed, raw = base
        try:
            w.set(typed if typed is not None else raw)
        except Exception:  # noqa: BLE001
            try:
                w.set(raw)
            except Exception:  # noqa: BLE001
                pass
        self._revert_refresh_all()

    def _revert_all(self):
        """Revert every edited field (a test and a future 'Reset all' use this)."""
        for k in [k for k in self._rv()["base"] if self._revert_differs(k)]:
            self._revert_key(k)

    def _revert_edited_keys(self):
        return [k for k in self._rv()["base"] if self._revert_differs(k)]

    # -- marks --------------------------------------------------------------- #
    def _revert_refresh_all(self):
        st = self._rv()
        for name in list(st["targets"]):
            self._revert_refresh_var(name)

    def _revert_refresh_var(self, name):
        st = self._rv()
        targets = st["targets"].get(name)
        if not targets:
            return
        keys = st["by_var"].get(name, [])
        lit = [k for k in keys if self._revert_differs(k)]
        for t in list(targets):
            m = getattr(t, "_rv_mark", None)
            try:
                if lit:
                    home = self._revert_home(t)
                    if m is not None and m.master is not home:
                        m.destroy()                # Tk cannot reparent
                        m = None
                    if m is None:
                        m = self._revert_mark(t, home)
                    m._rv_key = lit[0]
                    m._rv_tip.text = m._rv_text(lit[0])
                    self._revert_show(m)
                elif m is not None:
                    m._rv_key = None
                    m.place_forget()
            except tk.TclError:
                targets.remove(t)

    @staticmethod
    def _revert_show(m):
        """Just right of the field (or of a checkbutton's text)."""
        m.place(in_=m._rv_target, relx=1.0, x=2, rely=0.5, anchor="w")
        m.lift()

    def _revert_style(self):
        # only when the theme or palette changed: a ttk style configure
        # restyles every ttk widget in the app, and this runs on every rescan
        try:
            pal = gui_widgets.palette()
            style = ttk.Style(self.root)
            key = (style.theme_use(), pal.get("st_info", "#2f6fd6"))
            st = self._rv()
            if st.get("styled_for") == key:
                return
            st["styled_for"] = key
            style.configure(
                STYLE, foreground=pal.get("st_info", "#2f6fd6"),
                font=("Segoe UI Symbol", 13, "bold"))
        except tk.TclError:
            pass

    def _revert_text(self, key):
        base = self._rv()["base"].get(key)
        raw = base[1] if base else ""
        if raw is not None and len(raw) <= 40 and "\n" not in raw:
            return f"Edited from the preset. Click to revert to {raw!r}."
        return "Edited from the preset. Click to revert."

    def _revert_target_var(self, w):
        """The persisted tk var name a widget edits, or None."""
        names = []
        if isinstance(w, gui_widgets.SegmentedBar):
            v = getattr(w, "_var", None)
            names.append(str(getattr(v, "_name", "")))
        elif w.winfo_class() in _BOUND:
            for opt in ("textvariable", "variable"):
                try:
                    names.append(str(w.cget(opt)))
                except tk.TclError:
                    pass
        for n in names:
            n = gui_units._display_of.get(n, n)      # a unit-converted field
            if n:
                yield n

    def _revert_scan(self):
        """Give every not-yet-marked persisted-var widget its arrow."""
        st = self._rv()
        st["scan_after"] = None
        st["by_var"] = by_var = self._revert_vars()
        self._revert_style()
        stack = [self.root]
        while stack:
            w = stack.pop()
            try:
                stack.extend(w.winfo_children())
                if getattr(w, "_rv_done", False):
                    continue
                for name in self._revert_target_var(w):
                    if name in by_var:
                        st["targets"].setdefault(name, []).append(w)
                        break
                w._rv_done = True
            except tk.TclError:
                continue
        self._revert_refresh_all()

    #: room, in px, the mark needs right of its field
    MARK_ROOM = 22

    @classmethod
    def _revert_home(cls, target):
        """The window the mark is a child of: the nearest ancestor with room
        for it right of `target`.

        A placed widget is clipped to its parent. The field's own parent is
        often exactly as wide as its widest field, so that field's arrow was
        cut to a sliver (Process parameters' Polymer row, 184 px beside 86 px
        numbers). It climbs no further than a canvas, so a mark in a scrolled
        page stays inside its scroll viewport, and falls back to the field's
        parent while nothing is laid out yet (a later rescan re-homes it)."""
        home = target.master
        try:
            need = target.winfo_rootx() + target.winfo_width() + cls.MARK_ROOM
            w = home
            while w is not None and not isinstance(w, (tk.Canvas, tk.Tk,
                                                        tk.Toplevel)):
                if w.winfo_rootx() + w.winfo_width() >= need:
                    return w
                w = w.master
        except tk.TclError:
            pass
        return home

    def _revert_mark(self, target, home=None):
        """Build `target`'s mark, on its first edit (see the module docstring)."""
        m = ttk.Label(home if home is not None else target.master,
                      text=GLYPH, style=STYLE, cursor="hand2")
        m._rv_target = target
        m._rv_key = None
        m._rv_text = self._revert_text
        m._rv_tip = gui_tips.Tooltip(m, "Edited from the preset.")
        m._rv_done = True          # mapping it brings nothing new to scan
        m.bind("<Button-1>", lambda _e, m=m: (
            self._revert_key(m._rv_key) if m._rv_key else None, "break")[1])
        target._rv_mark = m
        return m

    def _revert_schedule_scan(self, e=None, *_a):
        # a widget already scanned, or one `skip` says edits no preset field,
        # brings nothing new to mark: no walk of the whole app (180 ms) for it
        if getattr(getattr(e, "widget", None), "_rv_done", False):
            return
        st = self._rv()
        if st["scan_after"] is None:
            try:
                st["scan_after"] = self.root.after(_RESCAN_MS, self._revert_scan)
            except tk.TclError:
                pass

    def _revert_install(self):
        """Boot-time: baseline everything, trace every var, scan, and keep
        scanning as pages map. Safe to call once `_persist_map` is complete."""
        st = self._rv()
        if st["on"]:
            return
        st["on"] = True
        self._revert_rebaseline()
        seen = set()
        for w in self._persist_map().values():
            name = str(getattr(w, "_name", ""))
            if not name or name in seen:
                continue
            seen.add(name)
            try:
                w.trace_add("write", self._revert_make_cb(name))
            except Exception:  # noqa: BLE001  (a non-Tk var)
                pass
        self.root.bind_all("<Map>", self._revert_schedule_scan, add="+")
        self._revert_schedule_scan()

    def _revert_make_cb(self, name):
        return lambda *_a: self._revert_refresh_var(name)
