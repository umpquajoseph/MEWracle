#!/usr/bin/env python3
"""gui_gcode_editor.py -- MODERN-UI ONLY: the G-code page.

The Scaffold designer seen "under the hood". The page holds ONE text, and it is
exactly the file Save g-code writes: the designer's output with the printer's
output options applied (travels as G1, the GX/G0X spelling, the M109
line, the dropped layer Z, the custom line), in the machine's flavor. Nothing
is edited in a hidden form and translated later. The designer's own preview
panel class sits beside it, on the same `pv_*` option vars, drawing the whole
file from this text.

Two sources, one page:

  * LINKED -- the text follows the Scaffold designer. Whenever the page is
    shown and the design has changed, the designer's own argv (the one Generate
    builds, minus the STL stage) runs in a subprocess of its own and the new
    g-code replaces the text. A hand-edited text is kept, and the header says
    the design moved under it.
  * FILE -- "Open file..." loads a g-code file from anywhere. This is the old
    "G-code import" page folded in: its conversion settings live in the
    collapsed "Convert to STL" section, on the same vars and preset keys. The
    original file is never written to.

Generate on this page converts THIS text: a linked, unedited text runs the
designer's own Generate (byte-identical to pressing it there); anything else
goes through the converter on a scratch copy of the text. Save writes the
text.

The engine (`mew_gcode.py`, built on its own branch) owns the document model,
the line map, structure, problems, the line explainer and the anchored
changes. This page calls it where it exists and degrades to the plain text
editor where it does not; `_gce_engine()` is the one switch.

The classic (developer) UI never imports this module and keeps its read-only
G-code text tab."""

from gui_common import *

import difflib
import json
import math
import queue
import re
import threading
import time
from bisect import bisect_right
from tkinter import font as tkfont

import numpy as np

import gui_fonts
import gui_form
import gui_widgets
from gui_preview import PREVIEW_DISABLED_STATUS, PREVIEW_POLL_MS
from gui_tips import add_tip


#: the code panes' palettes, one per theme, so the text is not the one dark slab
#: on a light page. Each tint is chosen against its own background: the dark set is the
#: pane's original one, the light set keeps the same hues readable on white.
CODE_DARK = {
    "bg": "#111111", "fg": "#dddddd", "insert": "#ffffff",
    "select": "#264f78",
    "gutter_bg": "#0b0b0b", "gutter_fg": "#666666",
    "curline": "#1d2733",
    "edited": "#2e2a12",         # a hand-changed or added line
    "mark": "#e0b84a",
    "focus": "#4a3a10",          # the lines of the edit being looked at
    "find": "#4a3f00", "findcur": "#8a6d00",
    "del": "#3a1717", "add": "#173a1d", "chg": "#2e2a12",   # Compare
    "gc_cmd": "#7fb7ff",         # G0..G3: motion
    "gc_mcmd": "#d7a6ff",        # every other command word (M, G4, T, ...)
    "gc_axis": "#dddddd",        # X Y Z A B C ... with their numbers
    "gc_feed": "#9fd39f",        # F, and S/P/E parameters
    "gc_cmt": "#6a8f5a",         # ; comments and ( ) comments
}
CODE_LIGHT = {
    "bg": "#ffffff", "fg": "#1f2328", "insert": "#1f2328",
    "select": "#cfe0fb",
    "gutter_bg": "#f3f5f8", "gutter_fg": "#8b929b",
    "curline": "#eef3fb",
    "edited": "#fff4d1",
    "mark": "#b07a12",
    "focus": "#ffe7a6",
    "find": "#fff1a8", "findcur": "#ffd34d",
    "del": "#fde2e1", "add": "#dcf5e1", "chg": "#fff4d1",
    "gc_cmd": "#1f5fbf",
    "gc_mcmd": "#8a3ffc",
    "gc_axis": "#1f2328",
    "gc_feed": "#2a7a3a",
    "gc_cmt": "#6a7f5a",
}
#: the syntax tags, in the order `_colour_visible` applies them
_SYNTAX = ("gc_cmd", "gc_mcmd", "gc_axis", "gc_feed", "gc_cmt")


def _shift_key(shift):
    """A preview shift as a comparable tuple (None for no shift)."""
    if shift is None:
        return None
    return tuple(round(float(v), 9) + 0.0 for v in shift)


def code_palette(app):
    """The code panes' palette for `app`'s theme."""
    var = getattr(app, "dark_mode", None)
    try:
        dark = bool(var is not None and var.get())
    except tk.TclError:
        dark = False
    return CODE_DARK if dark else CODE_LIGHT

#: the gutter's marks. Never colour alone (accessibility, spec 12.6): each
#: mark is a different character too.
_MARK_CHANGED = "~"
_MARK_ADDED = "+"
_MARK_REMOVED = "−"         # a line was deleted just above this one

#: how long typing must pause before the slower passes run: the gutter marks,
#: the syntax colour of the visible lines and the undo snapshot.
_EDIT_IDLE_MS = 350
#: the designer's own preview debounce (PREVIEW_DEBOUNCE_MS = 150) is for a
#: field edit; a text edit is many keystrokes, so the picture waits a little
#: longer for the typing to stop.
_PREVIEW_IDLE_MS = 450
#: a finished picture waits until typing has paused this long before it is
#: swapped in, so the swap never lands between two keystrokes
_PREVIEW_HOLD_S = 0.4
#: with no picture to draw, the worker still works out a text up to this
#: long (the edits half records the typing there, ~60 ms per 3k lines)
_RECORD_ONLY_MAX_LINES = 60000
#: ...after a longer pause than a picture waits for: the worker holds the
#: interpreter while it works, and typing resumed meanwhile would stutter
_RECORD_IDLE_MS = 1200
#: over this many lines the preview stops following every edit and waits for
#: "Update preview" instead, and says so. A whole-file parse + relay + drape
#: of a large print is seconds of work per keystroke otherwise.
_PREVIEW_AUTO_MAX_LINES = 60000
#: a diff of two texts with a changed middle larger than this many lines marks
#: the whole middle changed rather than running difflib over it.
_DIFF_MAX_MIDDLE = 20000
_UNDO_DEPTH = 200

_RE_COMMENT = re.compile(r";.*$|\([^)]*\)")
_RE_WORD = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
#: index pairs per Tk `tag add` call (`_tag_ranges`)
_TAG_PAIRS_PER_CALL = 2000


def _tag_ranges(widget, tag, pairs):
    """`widget.tag_add(tag, a, b)` for every (a, b) in `pairs`, many pairs
    to one Tk call. One call per line cost 570k Tcl round trips (3 s) when
    a Move lit, tinted and marked every line of a 20x20 mm scaffold.
    Tk stops a call at its first EMPTY range, so a caller passes none
    before a pair it means (one per line, a pair past the end comes last)."""
    flat = [i for ab in pairs for i in ab]
    step = 2 * _TAG_PAIRS_PER_CALL
    for k in range(0, len(flat), step):
        widget.tag_add(tag, *flat[k:k + step])


def _whole_lines(lines):
    """The (start, end) Tk indices covering 1-based `lines` whole, each
    line's newline included, one pair per run of consecutive lines: line
    i's `lineend+1c` is line i+1's start, so a run's pair tags exactly the
    characters its lines' own pairs would."""
    ln = sorted({int(i) for i in lines})
    out = []
    k = 0
    while k < len(ln):
        j = k
        while j + 1 < len(ln) and ln[j + 1] == ln[j] + 1:
            j += 1
        out.append((f"{ln[k]}.0", f"{ln[j]}.0 lineend+1c"))
        k = j + 1
    return out


# ---------------------------------------------------------------------------
# the editor widget
# ---------------------------------------------------------------------------
class GcodeEditor(ttk.Frame):
    """A g-code text editor: a line-number gutter with change marks, the text,
    both scrollbars, an inline Find / Replace bar and an inline Go-to-line bar.

    Undo is the DOCUMENT's, not Tk's (`Text(undo=False)`): a re-render after
    an output option or a design change replaces the whole text, and Tk's own
    stack would then let Ctrl+Z walk back through it as if it were typing.
    Here every step is a line-range replacement recorded once typing pauses,
    and `set_text(..., undoable=False)` resets the history (a new document).

    `on_change()` fires on every edit (cheap; debounce in the caller);
    `on_idle()` once typing has paused; `on_caret(line, col)` when the caret
    moves; `on_focus()` when the lit lines (`set_focus`) change."""

    def __init__(self, parent, app, on_change=None, on_idle=None,
                 on_caret=None, on_step=None, on_focus=None):
        super().__init__(parent)
        self.app = app
        self._on_focus = on_focus
        self.focus_lines = []
        self._on_change = on_change
        self._on_idle = on_idle
        self._on_caret = on_caret
        self._on_step = on_step
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        try:
            base = tkfont.nametofont("MewMono")
        except tk.TclError:     # an app built without gui_fonts.init_fonts
            base = tkfont.nametofont("TkFixedFont")
        self._font = tkfont.Font(family=base.cget("family"),
                                 size=base.cget("size"))
        self._base_size = int(base.cget("size"))

        # -- the find bar (row 0) and go-to-line bar (row 1), hidden ------- #
        self._build_find_bar()
        self._build_goto_bar()

        # -- gutter | text | scrollbar ------------------------------------- #
        body = ttk.Frame(self)
        body.grid(row=2, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        self.gutter = tk.Text(body, width=7, wrap="none", font=self._font,
                              state="disabled", takefocus=0, cursor="arrow",
                              borderwidth=0, highlightthickness=0, padx=4)
        self.gutter.grid(row=0, column=0, sticky="ns")
        self.text = tk.Text(body, wrap="none", font=self._font, undo=False,
                            borderwidth=0, highlightthickness=0, padx=4)
        self.text.grid(row=0, column=1, sticky="nsew")
        gui_form.exempt(self.text, "the g-code editor's text")
        gui_form.exempt(self.gutter, "the g-code editor's line numbers")

        def _yview(*a):
            self.text.yview(*a)
            self.gutter.yview_moveto(self.text.yview()[0])
        self._sby = ttk.Scrollbar(body, command=_yview)
        self._sby.grid(row=0, column=2, sticky="ns")

        def _on_yscroll(lo, hi):
            self._sby.set(lo, hi)
            self.gutter.yview_moveto(lo)
            self._schedule_syntax()
        self.text["yscrollcommand"] = _on_yscroll
        sbx = ttk.Scrollbar(body, orient="horizontal", command=self.text.xview)
        sbx.grid(row=1, column=1, sticky="ew")
        self.text["xscrollcommand"] = sbx.set

        # tag order is priority order: later tags draw over earlier ones
        # (created here in that order; `retheme` gives them their colours)
        for tag in ("edited", "curline", "focus") + _SYNTAX + (
                "findhit", "findcur"):
            self.text.tag_configure(tag)
        self.text.tag_raise("sel")
        self.retheme()
        # lines folded away by the tree's eyes (`set_hidden`)
        self._hidden = []
        self.text.tag_configure("hidden", elide=True)
        self.gutter.tag_configure("hidden", elide=True)

        self._lines = [""]          # the text at the last undo snapshot
        self._undo = []             # [(first line index, old lines, new lines)]
        self._redo = []
        self._idle_id = None
        self._syntax_id = None
        self._marks = {}            # 1-based line -> mark char
        self._gutter_n = -1
        self._gutter_marks_shown = {}
        self._muted = 0             # >0 while the widget itself rewrites text
        self._caret = (1, 0)
        self._find_hits = []        # [(start index, end index)]
        self._find_cur = -1

        t = self.text
        t.bind("<<Modified>>", self._on_modified)
        for seq in ("<KeyRelease>", "<ButtonRelease-1>", "<FocusIn>"):
            t.bind(seq, self._caret_moved, add="+")
        self.gutter.bind("<MouseWheel>", self._gutter_wheel)
        self.gutter.bind("<Button-1>", self._gutter_click)
        self._bind_keys()

    # -- keys ------------------------------------------------------------- #
    def _bind_keys(self):
        """The editor's own shortcuts, each returning "break" so the Text CLASS
        binding (Tk's own meaning) and the app-wide one never also run.

        Ctrl+F is Find in THIS text while the editor has focus -- the control
        finder is class-bound on Text (gui_shell._bind_shell_keys), and this
        widget binding runs before the class one. Ctrl+G stays Generate, so Go
        to line is Ctrl+L. Ctrl+D is the app's dark-mode toggle but Tk's Text
        class reads it as delete-forward, so it is handled here: the toggle,
        and no deleted character. Ctrl+/ is Tk's select-all on Text; here it
        comments the selected lines. Ctrl+H is Tk's backspace; here it opens
        Replace."""
        t = self.text
        t.bind("<Control-f>", lambda e: self.open_find())
        t.bind("<Control-h>", lambda e: self.open_find(replace=True))
        t.bind("<Control-l>", lambda e: self.open_goto())
        t.bind("<Control-z>", lambda e: self._key(self.undo))
        t.bind("<Control-y>", lambda e: self._key(self.redo))
        t.bind("<Control-Z>", lambda e: self._key(self.redo))
        t.bind("<Control-slash>", lambda e: self._key(self.toggle_comment))
        t.bind("<Control-d>", self._key_dark)
        t.bind("<Control-a>", lambda e: self._key(self.select_all))
        t.bind("<Control-equal>", lambda e: self._key(lambda: self.zoom(1)))
        t.bind("<Control-plus>", lambda e: self._key(lambda: self.zoom(1)))
        t.bind("<Control-minus>", lambda e: self._key(lambda: self.zoom(-1)))
        t.bind("<Control-0>", lambda e: self._key(lambda: self.zoom(0)))
        t.bind("<Control-MouseWheel>", self._ctrl_wheel)
        t.bind("<Tab>", lambda e: self._key(lambda: self.indent(1)))
        t.bind("<Shift-Tab>", lambda e: self._key(lambda: self.indent(-1)))
        t.bind("<F3>", lambda e: self._key(lambda: self.find_step(1)))
        t.bind("<Shift-F3>", lambda e: self._key(lambda: self.find_step(-1)))
        # Esc in the TEXT closes an open bar; with none open it falls through
        # to the app (which cancels a run) exactly as before.
        t.bind("<Escape>", self._text_escape)

    @staticmethod
    def _key(fn):
        fn()
        return "break"

    def _key_dark(self, _e=None):
        fn = getattr(self.app, "_kb_dark", None)
        if callable(fn):
            fn()
        return "break"

    def _text_escape(self, _e=None):
        if self._find_bar.winfo_ismapped() or self._goto_bar.winfo_ismapped():
            self.close_find()
            self.close_goto()
            return "break"
        return None

    def _ctrl_wheel(self, e):
        self.zoom(1 if e.delta > 0 else -1)
        return "break"

    def _gutter_wheel(self, ev):
        self.text.yview_scroll(int(-1 * (ev.delta / 120)), "units")
        self.gutter.yview_moveto(self.text.yview()[0])
        return "break"

    def _gutter_click(self, ev):
        line = int(self.gutter.index(f"@{ev.x},{ev.y}").split(".")[0])
        self.goto_line(line)
        return "break"

    # -- content ---------------------------------------------------------- #
    def get_text(self):
        return self.text.get("1.0", "end-1c")

    def line_count(self):
        return int(self.text.index("end-1c").split(".")[0])

    #: a text to read: Calibrate's slide is the engine's, not the page's
    read_only = False

    def set_read_only(self, on):
        self.read_only = bool(on)
        self.text.configure(state="disabled" if on else "normal")

    def _unlock(self):
        """Tk's disabled state refuses a program's writes too."""
        if self.read_only:
            self.text.configure(state="normal")

    def _relock(self):
        if self.read_only:
            self.text.configure(state="disabled")

    def set_text(self, text, undoable=True, keep_view=True):
        """Replace the whole text. `undoable` makes it ONE undo step (a flavor
        conversion, a Replace all, taking the new design); False starts a
        fresh history (a new document)."""
        self.commit()
        top = self.text.yview()[0]
        line, col = self._caret
        new = (text or "").split("\n")
        # only the lines that differ are replaced: a whole-text swap made Tk
        # redraw every visible line uncoloured, then colour it -- a flash on
        # every edit, option flip or new design
        step = self._step(self._lines, new)
        if step is not None:
            self._replace_lines(step[0], step[1], step[2])
        if undoable:
            self._push_step(self._lines, new)
        else:
            self._undo.clear()
            self._redo.clear()
        self._lines = new
        if keep_view:
            self.text.yview_moveto(top)
            self._place_caret(min(line, len(new)), col)
        else:
            self._place_caret(1, 0)
        self._after_programmatic_change()

    def replace_range(self, start, end, text):
        """Replace a Tk index range as ONE undo step (Replace, comment,
        indent, an Insert form)."""
        self.commit()
        self._muted += 1
        self._unlock()
        try:
            self.text.delete(start, end)
            self.text.insert(start, text)
            self.text.edit_modified(False)
        finally:
            self._relock()
            self._muted -= 1
        self.commit()
        self._after_programmatic_change()

    def _after_programmatic_change(self):
        self._sync_gutter()
        # coloured now, in this callback, so the redraw never shows the
        # changed lines plain first
        if self._syntax_id is not None:
            try:
                self.after_cancel(self._syntax_id)
            except tk.TclError:
                pass
        self._colour_visible()
        self._caret_moved()
        if self._on_change:
            self._on_change()
        if self._on_idle:
            self._on_idle()

    # -- undo, recorded as line-range steps ------------------------------- #
    @staticmethod
    def _step(old, new):
        """(first index, old middle, new middle) between two line lists, or
        None when they are equal. Prefix/suffix trim only: linear, and exact
        for the single-hunk edits that typing produces."""
        n0, n1 = len(old), len(new)
        p = 0
        lim = min(n0, n1)
        while p < lim and old[p] == new[p]:
            p += 1
        if p == n0 == n1:
            return None
        s = 0
        while (s < n0 - p and s < n1 - p
               and old[n0 - 1 - s] == new[n1 - 1 - s]):
            s += 1
        return (p, old[p:n0 - s], new[p:n1 - s])

    def _push_step(self, old, new):
        step = self._step(old, new)
        if step is None:
            return
        self._undo.append(list(step) + [None])
        del self._undo[:-_UNDO_DEPTH]
        self._redo.clear()

    def push_meta_step(self, meta):
        """An undo step that changes no line, only what `meta` carries
        (`on_step` puts it back): a design edit whose g-code came out the
        same. Without it Undo skipped that edit and took back an older
        one."""
        self.commit()
        self._undo.append([0, [], [], meta])
        del self._undo[:-_UNDO_DEPTH]
        self._redo.clear()

    def commit(self):
        """Record the typing since the last snapshot as one undo step."""
        if self._idle_id is not None:
            try:
                self.after_cancel(self._idle_id)
            except tk.TclError:
                pass
            self._idle_id = None
        new = self.get_text().split("\n")
        self._push_step(self._lines, new)
        self._lines = new

    def attach_meta(self, meta):
        """Pair the newest undo step with `meta` (the page's document
        snapshots, before and after), unless it already has one. Undo and
        Redo hand it back through `on_step` before they touch the text."""
        if self._undo and self._undo[-1][3] is None:
            self._undo[-1][3] = meta

    def _apply_step(self, step, forward):
        p, old, new, meta = step
        if meta is not None and self._on_step:
            self._on_step(meta, forward)
        src, dst = (old, new) if forward else (new, old)
        if src or dst:          # a step of `push_meta_step` has no lines
            self._replace_lines(p, src, dst)
        # the text is the truth: re-read rather than reconstruct
        self._lines = self.get_text().split("\n")
        self._place_caret(p + 1, 0)
        self.text.see(f"{p + 1}.0")
        self._after_programmatic_change()

    def _replace_lines(self, p, src, dst):
        """Replace the `src` lines starting at 0-based line `p` with `dst`,
        touching nothing else in the widget."""
        self._muted += 1
        self._unlock()
        try:
            start = f"{p + 1}.0"
            if src:
                end = f"{p + len(src)}.end"
                # include the newline after the block, unless it is the last
                if p + len(src) < len(self._lines):
                    end = f"{p + len(src) + 1}.0"
                    self.text.delete(start, end)
                    if dst:
                        self.text.insert(start, "\n".join(dst) + "\n")
                else:
                    if p > 0 and not dst:
                        start = f"{p}.end"
                    self.text.delete(start, end)
                    if dst:
                        self.text.insert(start, "\n".join(dst))
            else:
                if p < len(self._lines):
                    self.text.insert(start, "\n".join(dst) + "\n")
                else:
                    self.text.insert("end-1c", "\n" + "\n".join(dst))
            self.text.edit_modified(False)
        finally:
            self._relock()
            self._muted -= 1

    def _before_step(self):
        # an edit's form open on the page is kept first, so the step undoes
        # its whole session
        fn = getattr(self.app, "_gce_before_step", None)
        if fn is not None and getattr(self, "_group", None) is not None:
            fn()

    def undo(self):
        self._before_step()
        self.commit()
        if not self._undo:
            return
        step = self._undo.pop()
        self._redo.append(step)
        self._apply_step(step, forward=False)

    def redo(self):
        self._before_step()
        self.commit()
        if not self._redo:
            return
        step = self._redo.pop()
        self._undo.append(step)
        self._apply_step(step, forward=True)

    def can_undo(self):
        return not self.read_only and (
            bool(self._undo) or self.get_text().split("\n") != self._lines)

    def can_redo(self):
        return not self.read_only and bool(self._redo)

    # -- edit plumbing ---------------------------------------------------- #
    def _on_modified(self, _e=None):
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self._muted:
            return
        # cheap per keystroke: the line count for the gutter, nothing else
        if self.line_count() != self._gutter_n:
            self._sync_gutter()
        if self._on_change:
            self._on_change()
        if self._idle_id is not None:
            try:
                self.after_cancel(self._idle_id)
            except tk.TclError:
                pass
        self._idle_id = self.after(_EDIT_IDLE_MS, self._typing_paused)

    def _typing_paused(self):
        self._idle_id = None
        self.commit()
        self._schedule_syntax()
        if self._on_idle:
            self._on_idle()

    # -- gutter ----------------------------------------------------------- #
    def set_marks(self, marks):
        """{1-based line: mark char} -- the change marks the gutter shows."""
        self._marks = dict(marks)
        self._sync_gutter(force=True)

    def _sync_gutter(self, force=False):
        n = self.line_count()
        if not force and n == self._gutter_n \
                and self._gutter_marks_shown == self._marks:
            return
        width = max(4, len(str(n)))
        marks = self._marks
        rows = [f"{i:>{width}}{marks.get(i, ' ')}" for i in range(1, n + 1)]
        g = self.gutter
        g.configure(state="normal", width=width + 2)
        g.delete("1.0", "end")
        g.insert("1.0", "\n".join(rows))
        _tag_ranges(g, "mark", [(f"{i}.{width}", f"{i}.{width + 1}")
                                for i in sorted(marks) if 1 <= i <= n])
        for a, b in self._hidden:
            g.tag_add("hidden", f"{a}.0", f"{b + 1}.0")
        g.configure(state="disabled")
        g.yview_moveto(self.text.yview()[0])
        self._gutter_n = n
        self._gutter_marks_shown = dict(marks)

    def set_hidden(self, ranges):
        """Fold these 1-based, inclusive line ranges out of view, in the text
        and its line numbers alike, so the two stay row for row. Display
        only: the text, Save and Generate are unchanged."""
        self._hidden = [(int(a), int(b)) for a, b in ranges]
        for w in (self.text, self.gutter):
            w.tag_remove("hidden", "1.0", "end")
            for a, b in self._hidden:
                w.tag_add("hidden", f"{a}.0", f"{b + 1}.0")
        self.gutter.yview_moveto(self.text.yview()[0])

    def set_tinted(self, lines):
        """Tint these 1-based lines as hand-changed."""
        t = self.text
        t.tag_remove("edited", "1.0", "end")
        _tag_ranges(t, "edited", _whole_lines(lines))

    def set_focus(self, lines, see=False):
        """Light up these 1-based lines (one edit's); `see` scrolls the
        first into view."""
        t = self.text
        t.tag_remove("focus", "1.0", "end")
        _tag_ranges(t, "focus", _whole_lines(lines))
        if see and lines:
            t.see(f"{lines[0]}.0")
        lines = list(lines)
        if lines != self.focus_lines:
            self.focus_lines = lines
            if self._on_focus:
                self._on_focus()

    # -- one undo step for a whole form session --------------------------- #
    def begin_group(self):
        """Every change from here to `end_group` becomes ONE undo step (an
        edit's form applies live while it is open)."""
        self.commit()
        self._group = (len(self._undo), list(self._lines))

    def end_group(self, meta=None, keep=True):
        """Close the group: its steps fold into one (with `meta`), or with
        `keep` False vanish (Cancel already put the text back)."""
        group = getattr(self, "_group", None)
        self._group = None
        if group is None:
            return
        self.commit()
        n, lines0 = group
        del self._undo[n:]
        if keep:
            self._push_step(lines0, self._lines)
            if meta is not None and len(self._undo) > n:
                self.attach_meta(meta)
        self._redo.clear()

    # -- caret ------------------------------------------------------------ #
    def _place_caret(self, line, col=0):
        self.text.mark_set("insert", f"{max(1, line)}.{max(0, col)}")

    def _caret_moved(self, _e=None):
        idx = self.text.index("insert")
        line, col = (int(x) for x in idx.split("."))
        t = self.text
        t.tag_remove("curline", "1.0", "end")
        t.tag_add("curline", f"{line}.0", f"{line}.0 lineend+1c")
        if (line, col) != self._caret or _e is None:
            self._caret = (line, col)
            if self._on_caret:
                self._on_caret(line, col)

    def caret(self):
        return self._caret

    def goto_line(self, line, focus=True):
        """Caret to `line`, scrolled into view. `focus` False leaves the
        keyboard where it is: a picture click must not hand the next key
        (G, which opens this text) to the text instead."""
        line = max(1, min(int(line), self.line_count()))
        self._place_caret(line, 0)
        self.text.see(f"{line}.0")
        if focus:
            self.text.focus_set()
        self._caret_moved()

    def current_line_text(self):
        return self.text.get("insert linestart", "insert lineend")

    def select_all(self):
        self.text.tag_add("sel", "1.0", "end-1c")

    # -- syntax colour, visible lines only -------------------------------- #
    def _schedule_syntax(self):
        if self._syntax_id is None:
            try:
                self._syntax_id = self.after_idle(self._colour_visible)
            except tk.TclError:
                self._syntax_id = None

    def retheme(self):
        """Colour the pane for the app's theme (`code_palette`). Called at
        build and by the theme switch."""
        p = code_palette(self.app)
        try:
            self.gutter.configure(background=p["gutter_bg"],
                                  foreground=p["gutter_fg"])
            self.gutter.tag_configure("mark", foreground=p["mark"])
            self.text.configure(background=p["bg"], foreground=p["fg"],
                                insertbackground=p["insert"],
                                selectbackground=p["select"],
                                selectforeground=p["fg"],
                                inactiveselectbackground=p["select"])
            for tag in ("edited", "curline", "focus"):
                self.text.tag_configure(tag, background=p[tag])
            for tag in _SYNTAX:
                self.text.tag_configure(tag, foreground=p[tag])
            self.text.tag_configure("findhit", background=p["find"])
            self.text.tag_configure("findcur", background=p["findcur"])
        except tk.TclError:
            pass

    def _colour_visible(self):
        self._syntax_id = None
        t = self.text
        try:
            first = int(t.index("@0,0").split(".")[0])
            last = int(t.index(f"@0,{t.winfo_height()}").split(".")[0])
        except tk.TclError:
            return
        first = max(1, first - 2)
        last = last + 2
        for tag in _SYNTAX:
            t.tag_remove(tag, f"{first}.0", f"{last}.end")
        for ln in range(first, last + 1):
            s = t.get(f"{ln}.0", f"{ln}.end")
            if not s:
                continue
            code = s
            for m in _RE_COMMENT.finditer(s):
                t.tag_add("gc_cmt", f"{ln}.{m.start()}", f"{ln}.{m.end()}")
                code = code[:m.start()] + " " * (m.end() - m.start()) \
                    + code[m.end():]
            for m in _RE_WORD.finditer(code):
                letter = m.group(1).upper()
                a, b = m.start(), m.end()
                if letter in "GMT":
                    num = m.group(2)
                    try:
                        motion = letter == "G" and float(num) in (0, 1, 2, 3)
                    except ValueError:
                        motion = False
                    tag = "gc_cmd" if motion else "gc_mcmd"
                elif letter in "XYZABCUVWIJK":
                    tag = "gc_axis"
                else:
                    tag = "gc_feed"
                t.tag_add(tag, f"{ln}.{a}", f"{ln}.{b}")

    # -- editing helpers -------------------------------------------------- #
    def _selected_lines(self):
        try:
            a = int(self.text.index("sel.first").split(".")[0])
            b_idx = self.text.index("sel.last")
            b = int(b_idx.split(".")[0])
            if b_idx.endswith(".0") and b > a:
                b -= 1
        except tk.TclError:
            a = b = self._caret[0]
        return a, b

    def toggle_comment(self, comment_char=";"):
        """Comment or uncomment the selected lines (the caret's line with no
        selection): all already commented -> uncomment, else comment."""
        a, b = self._selected_lines()
        rows = self.text.get(f"{a}.0", f"{b}.end").split("\n")
        done = all(r.lstrip().startswith(comment_char) or not r.strip()
                   for r in rows)
        out = []
        for r in rows:
            if not r.strip():
                out.append(r)
            elif done:
                i = r.index(comment_char)
                rest = r[i + 1:]
                out.append(r[:i] + (rest[1:] if rest.startswith(" ") else rest))
            else:
                out.append(comment_char + " " + r)
        self.replace_range(f"{a}.0", f"{b}.end", "\n".join(out))
        self.text.tag_add("sel", f"{a}.0", f"{b}.end")

    def indent(self, direction):
        """Indent (+1) or outdent (-1) the selected lines by two spaces. Duet
        meta-command blocks are scoped by indentation."""
        try:
            self.text.index("sel.first")
            has_sel = True
        except tk.TclError:
            has_sel = False
        if direction > 0 and not has_sel:
            self.text.insert("insert", "  ")
            return
        a, b = self._selected_lines()
        rows = self.text.get(f"{a}.0", f"{b}.end").split("\n")
        if direction > 0:
            rows = ["  " + r if r else r for r in rows]
        else:
            rows = [r[2:] if r.startswith("  ") else r.lstrip(" ")
                    for r in rows]
        self.replace_range(f"{a}.0", f"{b}.end", "\n".join(rows))
        self.text.tag_add("sel", f"{a}.0", f"{b}.end")

    def zoom(self, step):
        size = self._base_size if step == 0 else max(
            6, min(32, int(self._font.cget("size")) + step))
        self._font.configure(size=size)

    # -- find / replace --------------------------------------------------- #
    def _build_find_bar(self):
        bar = ttk.Frame(self, padding=(0, 0, 0, 4))
        self._find_bar = bar
        self.find_var = tk.StringVar()
        self.repl_var = tk.StringVar()
        self.find_case = tk.BooleanVar(value=False)
        self.find_word = tk.BooleanVar(value=False)
        self.find_regex = tk.BooleanVar(value=False)
        self._find_msg = tk.StringVar(value="")
        # sized for the narrow g-code panel: the words on one row, the
        # options and the match count on the next, the entries taking what
        # width is left (one long row clipped after its first checkbox)
        r0 = ttk.Frame(bar)
        r0.pack(fill="x")
        r0.columnconfigure(1, weight=1)
        ttk.Label(r0, text="Find").grid(row=0, column=0, sticky="w")
        self._find_entry = ttk.Entry(r0, textvariable=self.find_var, width=12)
        self._find_entry.grid(row=0, column=1, sticky="ew", padx=(6, 4))
        prev = ttk.Button(r0, text="Prev", width=5,
                          command=lambda: self.find_step(-1))
        prev.grid(row=0, column=2)
        add_tip(prev, "The match before (Shift+Enter).")
        nxt = ttk.Button(r0, text="Next", width=5,
                         command=lambda: self.find_step(1))
        nxt.grid(row=0, column=3, padx=2)
        add_tip(nxt, "The next match (Enter).")
        close = ttk.Button(r0, text="✕", width=3, command=self.close_find)
        close.grid(row=0, column=4)
        add_tip(close, "Close Find (Esc).")
        r0b = ttk.Frame(bar, padding=(0, 2, 0, 0))
        r0b.pack(fill="x")
        for txt, var, tip in (
                ("Match case", self.find_case,
                 "Only matches with the same capitals: G1 but not g1."),
                ("Whole word", self.find_word,
                 "Only whole words: X1 but not inside X10."),
                ("Regex", self.find_regex,
                 "Read the search as a regular expression, e.g. G[23] "
                 "finds every arc.")):
            cb = ttk.Checkbutton(r0b, text=txt, variable=var,
                                 command=self._find_refresh)
            cb.pack(side="left", padx=(0, 8))
            add_tip(cb, tip)
        gui_form.mark(ttk.Label(r0b, textvariable=self._find_msg,
                                style="Hint.TLabel"), "live").pack(
            side="right")
        r1 = ttk.Frame(bar, padding=(0, 4, 0, 0))
        self._repl_row = r1
        r1.columnconfigure(1, weight=1)
        ttk.Label(r1, text="With").grid(row=0, column=0, sticky="w")
        self._repl_entry = ttk.Entry(r1, textvariable=self.repl_var, width=12)
        self._repl_entry.grid(row=0, column=1, sticky="ew", padx=(6, 4))
        rb = ttk.Button(r1, text="Replace", width=8, command=self.replace_one)
        rb.grid(row=0, column=2)
        add_tip(rb, "Replace this match and go to the next (Enter).")
        ra = ttk.Button(r1, text="All", width=5, command=self.replace_all)
        ra.grid(row=0, column=3, padx=2)
        add_tip(ra, "Replace every match in the text.")
        self.find_var.trace_add("write", lambda *_a: self._find_refresh())
        for e in (self._find_entry, self._repl_entry):
            e.bind("<Escape>", lambda _e: self._key(self.close_find))
            e.bind("<Control-f>", lambda _e: "break")
        self._find_entry.bind("<Return>", lambda _e: self._key(
            lambda: self.find_step(1)))
        self._find_entry.bind("<Shift-Return>", lambda _e: self._key(
            lambda: self.find_step(-1)))
        self._repl_entry.bind("<Return>", lambda _e: self._key(
            self.replace_one))

    def _build_goto_bar(self):
        bar = ttk.Frame(self, padding=(0, 0, 0, 4))
        self._goto_bar = bar
        self.goto_var = tk.StringVar()
        ttk.Label(bar, text="Go to line").pack(side="left")
        ent = ttk.Entry(bar, textvariable=self.goto_var, width=10)
        ent.pack(side="left", padx=(6, 4))
        self._goto_entry = ent
        ttk.Button(bar, text="Go", width=5,
                   command=self._goto_from_bar).pack(side="left")
        self._goto_msg = tk.StringVar(value="")
        gui_form.mark(ttk.Label(bar, textvariable=self._goto_msg,
                                style="Hint.TLabel"), "live").pack(
            side="left", padx=(10, 0))
        close = ttk.Button(bar, text="✕", width=3, command=self.close_goto)
        close.pack(side="right")
        add_tip(close, "Close Go to line (Esc).")
        ent.bind("<Return>", lambda _e: self._key(self._goto_from_bar))
        ent.bind("<Escape>", lambda _e: self._key(self.close_goto))

    def open_find(self, replace=False):
        self.close_goto()
        self._find_bar.grid(row=0, column=0, sticky="ew")
        if replace:
            self._repl_row.pack(fill="x")
        else:
            self._repl_row.pack_forget()
        try:
            sel = self.text.get("sel.first", "sel.last")
            if sel and "\n" not in sel:
                self.find_var.set(sel)
        except tk.TclError:
            pass
        self._find_entry.focus_set()
        self._find_entry.select_range(0, "end")
        self._find_refresh()
        return "break"

    def close_find(self):
        self._find_bar.grid_remove()
        self.text.tag_remove("findhit", "1.0", "end")
        self.text.tag_remove("findcur", "1.0", "end")
        self._find_hits = []
        self.text.focus_set()

    def open_goto(self):
        self.close_find()
        self._goto_bar.grid(row=1, column=0, sticky="ew")
        self._goto_msg.set(f"1 to {self.line_count()}")
        self._goto_entry.focus_set()
        self._goto_entry.select_range(0, "end")
        return "break"

    def close_goto(self):
        self._goto_bar.grid_remove()

    def _goto_from_bar(self):
        try:
            n = int(self.goto_var.get().strip())
        except ValueError:
            self._goto_msg.set("Type a line number.")
            return
        self.close_goto()
        self.goto_line(n)

    def _find_pattern(self):
        pat = self.find_var.get()
        if not pat:
            return None
        if not self.find_regex.get():
            pat = re.escape(pat)
        if self.find_word.get():
            pat = r"\b(?:" + pat + r")\b"
        flags = 0 if self.find_case.get() else re.IGNORECASE
        try:
            return re.compile(pat, flags | re.MULTILINE)
        except re.error as exc:
            self._find_msg.set(f"That pattern is not valid: {exc}.")
            return None

    def _offsets_to_index(self, text):
        starts = [0]
        for m in re.finditer("\n", text):
            starts.append(m.end())

        def idx(off):
            ln = bisect_right(starts, off) - 1
            return f"{ln + 1}.{off - starts[ln]}"
        return idx

    def _find_refresh(self):
        t = self.text
        t.tag_remove("findhit", "1.0", "end")
        t.tag_remove("findcur", "1.0", "end")
        self._find_hits = []
        self._find_cur = -1
        rx = self._find_pattern()
        if rx is None:
            if not self.find_var.get():
                self._find_msg.set("")
            return
        text = self.get_text()
        idx = self._offsets_to_index(text)
        hits = []
        for m in rx.finditer(text):
            if m.end() == m.start():
                continue
            hits.append((idx(m.start()), idx(m.end())))
            if len(hits) >= 20000:
                break
        self._find_hits = hits
        for a, b in hits[:5000]:
            t.tag_add("findhit", a, b)
        if not hits:
            self._find_msg.set("No matches.")
            return
        # start at the first hit at or after the caret
        caret = t.index("insert")
        self._find_cur = next(
            (i for i, (a, _b) in enumerate(hits)
             if t.compare(a, ">=", caret)), 0)
        self._show_find_cur()

    def _show_find_cur(self):
        t = self.text
        t.tag_remove("findcur", "1.0", "end")
        if not self._find_hits or self._find_cur < 0:
            return
        a, b = self._find_hits[self._find_cur]
        t.tag_add("findcur", a, b)
        t.see(a)
        n = len(self._find_hits)
        more = "+" if n >= 20000 else ""
        self._find_msg.set(f"{self._find_cur + 1} of {n}{more}")

    def find_step(self, direction):
        if not self._find_hits:
            self._find_refresh()
            if not self._find_hits:
                return
        else:
            self._find_cur = (self._find_cur + direction) % len(self._find_hits)
        self._show_find_cur()
        a, _b = self._find_hits[self._find_cur]
        self._place_caret(*(int(x) for x in a.split(".")))
        self._caret_moved()

    def replace_one(self):
        if not self._find_hits or self._find_cur < 0:
            self._find_refresh()
            if not self._find_hits:
                return
        a, b = self._find_hits[self._find_cur]
        rx = self._find_pattern()
        if rx is None:
            return
        found = self.text.get(a, b)
        new = rx.sub(self.repl_var.get(), found, count=1) \
            if self.find_regex.get() else self.repl_var.get()
        self.replace_range(a, b, new)
        self._place_caret(*(int(x) for x in a.split(".")))
        self._find_refresh()

    def replace_all(self):
        rx = self._find_pattern()
        if rx is None:
            return
        text = self.get_text()
        repl = self.repl_var.get()
        if not self.find_regex.get():
            repl = repl.replace("\\", "\\\\")
        new, n = rx.subn(repl, text)
        if n:
            self.set_text(new, undoable=True)
        self._find_refresh()
        self._find_msg.set(f"Replaced {n}." if n else "No matches.")


def _line_renumber(src, dst):
    """int array: 1-based line of `src` -> its line in `dst`, where `dst` is
    `src` through the output options (travels respelled G1, layer Z lines
    dropped). A dropped line takes the line before it."""
    def norm(t):
        return [re.sub(r"^\s*G0\b", "G1", ln.strip())
                for ln in t.split("\n")]
    a, b = norm(src), norm(dst)
    to = np.zeros(len(a) + 2, np.int64)
    for tag, i0, i1, j0, j1 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        for k in range(i1 - i0):
            to[i0 + k + 1] = (j0 + k + 1 if k < j1 - j0
                              else max(j0 + (j1 - j0), 1))
    for i in range(1, len(to)):
        if to[i] == 0:
            to[i] = to[i - 1] or 1
    return to


# ---------------------------------------------------------------------------
# change marks: what differs between the designer's text and the buffer
# ---------------------------------------------------------------------------
def diff_marks(base_lines, lines):
    """({1-based line in `lines`: mark}, [hunks]) for the gutter and the
    Changes list. A hunk is (kind, first line in `lines`, count, removed
    lines) with kind "changed" / "added" / "removed". Prefix/suffix trim
    first, so an edit in a 100k-line file diffs only the middle."""
    n0, n1 = len(base_lines), len(lines)
    p = 0
    lim = min(n0, n1)
    while p < lim and base_lines[p] == lines[p]:
        p += 1
    if p == n0 == n1:
        return {}, []
    s = 0
    while s < n0 - p and s < n1 - p \
            and base_lines[n0 - 1 - s] == lines[n1 - 1 - s]:
        s += 1
    old = base_lines[p:n0 - s]
    new = lines[p:n1 - s]
    marks, hunks = {}, []
    if max(len(old), len(new)) > _DIFF_MAX_MIDDLE:
        ops = [("replace", 0, len(old), 0, len(new))]
    else:
        ops = difflib.SequenceMatcher(None, old, new,
                                      autojunk=False).get_opcodes()
    for tag, i0, i1, j0, j1 in ops:
        if tag == "equal":
            continue
        first = p + j0 + 1
        if tag == "replace":
            k = min(i1 - i0, j1 - j0)
            for j in range(j0, j0 + k):
                marks[p + j + 1] = _MARK_CHANGED
            for j in range(j0 + k, j1):
                marks[p + j + 1] = _MARK_ADDED
            hunks.append(("changed", first, j1 - j0, old[i0:i1]))
            if i1 - i0 > j1 - j0:
                marks.setdefault(p + j1 + 1, _MARK_REMOVED)
        elif tag == "insert":
            for j in range(j0, j1):
                marks[p + j + 1] = _MARK_ADDED
            hunks.append(("added", first, j1 - j0, []))
        elif tag == "delete":
            marks.setdefault(first, _MARK_REMOVED)
            hunks.append(("removed", first, 0, old[i0:i1]))
    return marks, hunks


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------
class _GcodeEditorMixin:
    """The G-code page. Sits BEFORE _ShellMixin in ModernApp's MRO: it
    overrides `run`, `_active_mode`, `save_gcode_as` and
    `_refresh_result_controls`, each chaining to super() everywhere but this
    page, and the shell's own versions then still run around it."""

    _GCE_LINKED = "designer"
    _GCE_FILE = "file"

    # ------------------------------------------------------------------ #
    # build
    # ------------------------------------------------------------------ #
    def _build_gcode_editor_page(self, page):
        """Build the page into `page` (a plain frame the shell registered as
        `tab_gcode`). The old G-code import form is built inside the
        collapsed Convert-to-STL section, so `in_var` / `diameter` / `faces` /
        `sag_mode` / `mesh_tol` keep their widgets, validators and preset
        keys. The old G-code text page's widget names (`gcode_text`,
        `gcode_text_status`, `gcode_find_btn`,
        `view_gcode_btn`) are set here too, so the shared code that refreshes
        them keeps working with nothing to know."""
        margin = gui_fonts.PAGE_MARGIN
        self._gce_doc = dict(source=None, raw="", base="", path=None,
                             sig=None, mode="g1", diameter=None,
                             stale=False, error=None)
        self._gce_regen = None          # the running regeneration, or None
        self._gce_regen_q = queue.Queue()
        self._gce_regen_poll = None
        self._gce_dispatch = None
        self._gce_run_text = None
        self._gce_hunks = []
        self._gce_pv_after = None
        self._gce_pv_gen = 0
        self._gce_pv_state = None
        self._gce_pv_results = queue.Queue()
        self._gce_pv_cv = threading.Condition()
        self._gce_pv_mail = None
        self._gce_pv_worker = None
        self._gce_pv_poll = None
        # the document's flavor (a label from _gce_flavor_labels). It follows
        # the Machine page's flavor until it is picked here.
        self.gce_flavor = tk.StringVar(value=self._gce_machine_flavor())
        # a session, undo step or design saved under a flavor's old name
        # shows its new one (`flavor_presets.by_label` knows former names)
        self.gce_flavor.trace_add("write", lambda *_a: self._gce_flavor_current())
        self._gce_flavor_picked = False
        mvar = getattr(self, "gcode_flavor", None)
        if mvar is not None:
            mvar.trace_add("write", lambda *_a: self._gce_follow_machine())

        page.columnconfigure(0, weight=1)
        page.rowconfigure(4, weight=1)   # the picture (row 3 is left empty)

        # -- Undo, Redo, Tutorial and Help sit in the top row, shown with
        #    this page. The page has no title row: the way to the Scaffold
        #    designer is the ribbon's, and where the text comes from sits on
        #    the tree's title bar ------------------------------------------- #
        from gui_gcode_cad import GlyphButton
        tools = self._nav_context
        self._gce_undo_btn = GlyphButton(tools, "undo",
                                         command=lambda: self._gce_undo())
        self._gce_undo_btn.pack(side="left")
        add_tip(self._gce_undo_btn, "Undo (Ctrl+Z).")
        self._gce_redo_btn = GlyphButton(tools, "redo",
                                         command=lambda: self._gce_redo())
        self._gce_redo_btn.pack(side="left", padx=(2, 10))
        add_tip(self._gce_redo_btn, "Redo (Ctrl+Y).")
        self.gce_link_var = tk.StringVar(value="")
        helpb = ttk.Button(tools, text="Help", style="BarLink.TButton",
                           command=lambda: self._gce_help_toggle())
        helpb.pack(side="right")
        self._gce_help_btn = helpb
        add_tip(helpb, "An index of g-code commands (a dwell, an arc, a "
                       "temperature…) in this text's flavor, and how this "
                       "page works.")
        # the shared code still refreshes this one by name
        self.gcode_text_status = tk.StringVar(value="")
        if hasattr(self, "_gw_build_state"):
            self._gw_build_state()          # unsaved state, job stats

        # -- the ribbon: Fusion's captioned tool groups -------------------- #
        from gui_gcode_cad import ToolButton, ribbon_group, GROUP_TINT
        rib = ttk.Frame(page, style="Bar.TFrame", padding=(margin - 4, 0,
                                                           margin, 0))
        rib.grid(row=1, column=0, sticky="ew")
        gui_widgets.hairline(page).grid(row=1, column=0, sticky="sew")
        self._gce_ribbon = rib

        def tool(row, grp, glyph, text, tip, command=None, menu=None):
            b = ToolButton(row, glyph, text, command=command, menu=menu,
                           tint=GROUP_TINT[grp], tip=tip)
            b.pack(side="left", padx=1, pady=(3, 0))
            # a click on a greyed tool puts its reason on the status line
            b.say = lambda text: self.status.set(text)
            return b
        self._gce_tool = tool
        if hasattr(self, "_build_gce_tool_groups"):
            self._build_gce_tool_groups(rib, ribbon_group)
        ins = ribbon_group(rib, "Inspect")
        self._gce_code_btn = tool(
            ins, "inspect", "code", "G-code\n(G)",
            "Show or hide the g-code text (G). Type in it directly if you "
            "prefer g-code: what you type shows up in the tree.",
            command=lambda: self._gce_toggle_code())
        tool(ins, "inspect", "find", "Find",
             "Find and replace in the g-code. In the g-code text, Ctrl+F "
             "and Ctrl+H open it too; elsewhere Ctrl+F finds a setting.",
             command=self._gce_find)
        self._gce_wc_btn = tool(
            ins, "inspect", "compare", "Compare",
            "This design's plain g-code beside this text, each "
            "difference labelled with what made it. Press again to go back.",
            command=self._gce_toggle_whatchanged)
        self._gce_inspect = ins         # the view tools join it
        fil = ribbon_group(rib, "File", last=True)
        self._build_gce_relink_tool(fil, tool)
        if hasattr(self, "_build_gce_work_tools"):
            self._build_gce_work_tools(fil, tool)   # Save design, Open, Send
        self._gce_syntax_btn = tool(
            fil, "file", "flavor", "Syntax",
            "The syntax this text is written in: your printer firmware's "
            "flavor of g-code. It starts as the Printer & collector page's choice; "
            "changing it respells the commands (a pause, a wait, a "
            "message), never the nozzle's path. Edit flavors… makes, "
            "imports and exports your own.",
            menu=self._gce_flavor_menu)
        # Quality, Export (which holds what Output options did) and Presets:
        # the designer's own cards (gui_gcode_panels)
        if hasattr(self, "_build_gce_panel_tools"):
            self._build_gce_panel_tools(fil)
        # too wide for the window: compact, then fold groups from the right
        from gui_gcode_cad import RibbonFit
        self._gce_ribfit = RibbonFit(rib)

        # -- the inline notice (no popups: spec 12.4) ---------------------- #
        self._gce_notice = tk.Frame(page, background="#3a3214", padx=10,
                                    pady=6)
        self.gce_notice_var = tk.StringVar(value="")
        tk.Label(self._gce_notice, textvariable=self.gce_notice_var,
                 background="#3a3214", foreground="#f0e2b0", anchor="w",
                 justify="left", font="MewHint").pack(side="left", fill="x",
                                                      expand=True)
        self._gce_notice_btns = ttk.Frame(self._gce_notice)
        self._gce_notice_btns.pack(side="right")

        # -- the picture fills the page; everything else floats over it ---- #
        self._build_gce_preview(page)
        vp = self._gce_viewport
        self._gce_code_shown = bool(self._gce_prefs().get("code_shown"))
        # the browser: one tree of the print (and, for an opened file, how
        # Generate converts it)
        browser = ttk.Frame(page, padding=(6, 4, 6, 6), borderwidth=1,
                            relief="solid")
        browser.columnconfigure(0, weight=1)
        tree_box = ttk.Frame(browser)
        tree_box.grid(row=0, column=0, sticky="nsew")
        tree_box.columnconfigure(0, weight=1)
        props = ttk.Frame(browser)
        props.grid(row=1, column=0, sticky="ew")
        props.columnconfigure(0, weight=1)
        self._gce_props = props
        # the open edit: its own box beside the tree, as Fusion floats a
        # command's dialog, sized to its fields and scrolling only past the
        # window's height
        # The box's own 4 px padding holds its resize strips, clear of the
        # scrollbar; the body's padding makes up the rest of the margin
        self._gce_formbox = VScrollFrame(page, padding=(4, 2, 4, 4),
                                         borderwidth=1, relief="solid")
        self._gce_formbox.configure(padding=self._GCE_FORM_PAD)
        self._gce_formbox.body.columnconfigure(0, weight=1)
        self._gce_form_edges = self._gce_resizable(
            self._gce_formbox, self._gce_form_size_key,
            "form", min_size=(240, 120), laid_from_nw=True)
        self._build_gce_side(props, tree_box)
        self._gce_browser = browser
        # what the next tool works on: the tree's selection shows it; the
        # words stay for the status line
        self.gce_sel_var = tk.StringVar(value="")
        # the g-code text, one key away (G) and never forced on screen
        code = ttk.Frame(page, padding=(6, 4, 6, 4), borderwidth=1,
                         relief="solid")
        self._build_gce_editor(code)
        self._gce_code = code
        # the display toggles, a strip under the ribbon
        self._gce_disp = self._build_gce_display(page)
        # a small status box in the bottom-left corner
        self.gce_corner_var = tk.StringVar(value="Press G for the g-code")
        self._gce_corner = ttk.Label(page, textvariable=self.gce_corner_var,
                                     style="Hint.TLabel", padding=(8, 3),
                                     borderwidth=1, relief="solid")
        gui_form.mark(self._gce_corner, "live")
        pv = getattr(self, "_gce_pv_panel", None)
        if pv is not None:
            pv._status_var.trace_add(
                "write", lambda *_a: self._gce_corner_refresh())
        # the selection: lines selected in the text, a click on the picture
        # (the panel's pick seam: it calls `on_pick` on a click that did
        # not drag, and asks `pick_outline` for the print's layers)
        if hasattr(self, "_gce_on_text_select"):
            self.gce.text.bind("<<Selection>>", self._gce_on_text_select,
                               add="+")
            if pv is not None:
                pv.on_pick = self._gce_on_pick
                pv.pick_outline = self._gce_pick_outline
        # how the page works and the command index: behind Help, never on
        # screen by themselves
        self._gce_intro = self._build_gce_intro(page)
        self._gce_help = self._build_gce_help(page)
        self.gce_flavor.trace_add("write", lambda *_a: self._gce_help_fill()
                                  if self._gce_help_shown else None)
        for name in ("pv_tube_sides", "s_sag_reset",
                     "s_sag_reset_layers", "pv_fill_per_tau",
                     "pv_curve_quality", "pv_auto_corner_grid"):
            var = getattr(self, name, None)
            if var is not None:
                var.trace_add("write", lambda *_a: self._gce_pv_schedule()
                              if not getattr(self, "_pv_suppress", False) else None)
        self._gce_layout_after = None
        vp.bind("<Configure>", self._gce_layout_soon, add="+")
        for w in (browser, self._gce_formbox.body):
            w.bind("<Configure>", self._gce_layout_soon, add="+")
        self._gce_toggle_code(self._gce_code_shown)
        self.root.bind_all("<KeyPress-g>", self._gce_key_g, add="+")

        # -- problems bar (takes space only with something to say) --------- #
        self._gce_problems = ttk.Frame(page, padding=(margin, 2, margin, 2))
        self.gce_problem_var = tk.StringVar(value="")
        gui_form.mark(ttk.Label(self._gce_problems,
                                textvariable=self.gce_problem_var,
                                foreground="#c0392b", justify="left"),
                      "live").pack(side="left")

        # the old Refresh button: no job on this page, but the shared
        # _sync_result_buttons still enables it by name
        self.view_gcode_btn = ttk.Button(self._legacy_holder_for_gce(),
                                         state="disabled")
        self._gcode_text_frame = page
        self._gce_set_link_label()

    _GCE_PREFS = "gcode_page.json"

    def _gce_prefs(self):
        try:
            with open(os.path.join(USER_DATA_DIR, self._GCE_PREFS),
                      encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _gce_prefs_set(self, **kw):
        prefs = self._gce_prefs()
        prefs.update(kw)
        try:
            os.makedirs(USER_DATA_DIR, exist_ok=True)
            with open(os.path.join(USER_DATA_DIR, self._GCE_PREFS), "w",
                      encoding="utf-8") as fh:
                json.dump(prefs, fh)
        except OSError:
            pass

    def _build_gce_intro(self, page):
        """How the page works, in three short lines: an overlay shown by
        the title row's "?" and closed by Got it. Never on screen by
        itself."""
        box = tk.Frame(page, background="#1d2733", padx=12, pady=8)
        box.columnconfigure(0, weight=1)
        steps = (
            "Select a part of the print: a layer in the tree or the "
            "picture, or lines in the g-code.",
            "Pick a tool above. The edit lands in the tree under that part, "
            "with only its own fields; the picture follows.",
            "Press G for the g-code text. What you type there shows up in "
            "the tree too.")
        for i, t in enumerate(steps):
            tk.Label(box, text=f"{i + 1}.  {t}", background="#1d2733",
                     foreground="#dde6f0", anchor="w", justify="left",
                     font="MewHint").grid(row=i, column=0, sticky="w")
        ttk.Button(box, text="Got it", command=self._gce_intro_hide).grid(
            row=0, column=1, rowspan=3, sticky="e", padx=(12, 0))
        return box

    def _gce_intro_hide(self):
        self._gce_intro.place_forget()
        self._gce_prefs_set(intro_seen=True)

    def _gce_intro_show(self):
        self._gce_intro.place(in_=self._gce_viewport, relx=0.5, y=48,
                              anchor="n")
        self._gce_intro.lift()

    def _build_gce_help(self, page):
        """Help: an index of g-code commands, in the text's flavor
        (`gui_gcode_cad.gcode_index`, the engine's own spellings and words).
        A search box, the list, and the picked command's example and what it
        does; Copy, and Insert for a command the Insert tool makes."""
        box = ttk.Frame(page, padding=(8, 6, 8, 8), borderwidth=1,
                        relief="solid")
        box.columnconfigure(0, weight=1)
        head = ttk.Frame(box)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)
        htitle = ttk.Label(head, text="G-code commands", font="MewSection")
        htitle.grid(row=0, column=0, sticky="w")
        self.gce_help_fl_var = tk.StringVar(value="")
        hflavor = gui_form.mark(ttk.Label(
            head, textvariable=self.gce_help_fl_var, style="Hint.TLabel"),
            "live")
        hflavor.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(head, text="✕", width=3,
                   command=lambda: self._gce_help_toggle(False)).grid(
            row=0, column=3, sticky="e", padx=(4, 0))
        self.gce_help_q = tk.StringVar(value="")
        q = ttk.Entry(box, textvariable=self.gce_help_q)
        q.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        add_tip(q, "Type to narrow the list: dwell, arc, temperature…")
        self.gce_help_q.trace_add("write", lambda *_a: self._gce_help_fill())
        tree = ttk.Treeview(box, show="tree", selectmode="browse",
                            columns=("ex",), height=8)
        from gui_fonts import dpi_px     # sized as the text is (150% cut both)
        tree.column("#0", width=dpi_px(box, 215), stretch=True)
        tree.column("ex", width=dpi_px(box, 150), stretch=False)
        tree.grid(row=2, column=0, sticky="nsew")
        box.rowconfigure(2, weight=1)       # a taller box: a longer list
        tree.bind("<<TreeviewSelect>>", lambda _e: self._gce_help_pick())
        self._gce_help_tree = tree
        self.gce_help_ex_var = tk.StringVar(value="")
        self.gce_help_what_var = tk.StringVar(value="")
        det = ttk.Frame(box)
        det.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        det.columnconfigure(0, weight=1)
        gui_form.mark(ttk.Label(det, textvariable=self.gce_help_ex_var,
                                font="TkFixedFont"), "live").grid(
            row=0, column=0, sticky="w")
        self._gce_help_copy = ttk.Button(det, text="Copy",
                                         command=self._gce_help_do_copy)
        self._gce_help_copy.grid(row=0, column=1, sticky="e")
        add_tip(self._gce_help_copy, "Copy the example line.")
        self._gce_help_ins = ttk.Button(det, text="Insert",
                                        command=self._gce_help_do_insert)
        self._gce_help_ins.grid(row=0, column=2, sticky="e", padx=(4, 0))
        add_tip(self._gce_help_ins, "Add this command after what is "
                                    "selected, as the Insert tool does.")
        what = gui_form.mark(ttk.Label(det, textvariable=self.gce_help_what_var,
                                       style="Hint.TLabel", justify="left",
                                       wraplength=320), "live")
        what.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))
        det.bind("<Configure>", lambda e: what.configure(
            wraplength=max(200, e.width - 4)), add="+")
        ttk.Button(box, text="How this page works", style="Link.TButton",
                   command=self._gce_intro_show).grid(row=4, column=0,
                                                      sticky="w",
                                                      pady=(6, 0))
        self._gce_help_rows = []
        self._gce_help_shown = False
        # it hangs from the top-right corner until its title row moves it,
        # as the page's other floating boxes; any edge or corner resizes it,
        # the side opposite staying put (its place lands under "help")
        self._gce_help_edges = self._gce_resizable(
            box, "help", "help", min_size=(260, 200))
        self._gce_drag_bind("help", box, head, htitle, hflavor)
        return box

    def _gce_help_toggle(self, show=None):
        show = (not self._gce_help_shown) if show is None else bool(show)
        self._gce_help_shown = show
        if show:
            self._gce_help_fill()
        self._gce_layout()

    def _gce_help_fill(self):
        """The list, for the flavor now and the search words."""
        from gui_gcode_cad import gcode_index
        eng = self._gce_engine() if hasattr(self, "_gce_engine") else None
        if eng is None:
            return
        fl = self.gce_flavor_name()
        self.gce_help_fl_var.set(self.gce_flavor.get())
        words = self.gce_help_q.get().lower().split()
        rows = [r for r in gcode_index(eng, fl)
                if all(w in (r["topic"] + " " + r["example"] + " "
                             + r["what"]).lower() for w in words)]
        self._gce_help_rows = rows
        tree = self._gce_help_tree
        tree.delete(*tree.get_children(""))
        for i, r in enumerate(rows):
            tree.insert("", "end", iid=str(i), text=r["topic"],
                        values=(r["example"].split("\n")[0] or "—",))
        if rows:
            tree.selection_set("0")
        self._gce_help_pick()

    def _gce_help_row(self):
        sel = self._gce_help_tree.selection()
        try:
            return self._gce_help_rows[int(sel[0])] if sel else None
        except (ValueError, IndexError):
            return None

    def _gce_help_pick(self):
        r = self._gce_help_row()
        self.gce_help_ex_var.set(r["example"] if r else "")
        self.gce_help_what_var.set(
            (r["what"] + ("  " + r["note"] if r["note"] else "")) if r else
            "Nothing matches.")
        st = "normal" if r and r["example"] else "disabled"
        self._gce_help_copy.configure(state=st)
        self._gce_help_ins.configure(
            state="normal" if r and r["kind"] else "disabled")

    def _gce_help_do_copy(self):
        r = self._gce_help_row()
        if r and r["example"]:
            self.root.clipboard_clear()
            self.root.clipboard_append(r["example"])

    def _gce_help_do_insert(self):
        r = self._gce_help_row()
        if r and r["kind"] and hasattr(self, "_gce_tool_insert"):
            self._gce_tool_insert(r["kind"])

    def _legacy_holder_for_gce(self):
        holder = getattr(self, "_gce_holder", None)
        if holder is None:
            holder = ttk.Frame(self.root)     # never gridded -> hidden
            self._gce_holder = holder
        return holder

    def _gce_output_menu(self):
        """Output options: the printer's output options (Setup's), on the
        SAME vars, so flipping one here flips it there."""
        menu = getattr(self, "_gce_out_menu", None)
        if menu is not None:
            return menu
        menu = self._gce_out_menu = tk.Menu(self.root, tearoff=False)
        if hasattr(self, "s_travel_as_g1"):
            menu.add_checkbutton(label="Write travel moves as G1",
                                 variable=self.s_travel_as_g1)
        if hasattr(self, "s_gcode_convention"):
            menu.add_separator()
            menu.add_radiobutton(label="Spell commands G0, G1, G2",
                                 variable=self.s_gcode_convention, value="GX")
            menu.add_radiobutton(label="Spell commands G00, G01, G02",
                                 variable=self.s_gcode_convention, value="G0X")
        if hasattr(self, "s_wait_for_temp"):
            menu.add_separator()
            menu.add_checkbutton(
                label="Wait for the nozzle temperature first (M109)",
                variable=self.s_wait_for_temp)
        if hasattr(self, "no_layer_z"):
            menu.add_checkbutton(label="Don't change height between layers",
                                 variable=self.no_layer_z)
        return menu

    def _gce_flavor_menu(self):
        """Syntax (the flavor): the firmware the text is written for, one radio each.
        Built on every post, so the label list is the current one."""
        menu = getattr(self, "_gce_fl_menu", None)
        if menu is None:
            menu = self._gce_fl_menu = tk.Menu(self.root, tearoff=False)
        menu.delete(0, "end")
        for lab in self._gce_flavor_labels():
            menu.add_radiobutton(label=lab, variable=self.gce_flavor,
                                 value=lab,
                                 command=self._gce_flavor_selected)
        if hasattr(self, "_open_flavor_editor"):
            menu.add_separator()
            menu.add_command(label="Edit flavors\u2026",
                             command=self._gce_edit_flavors)
        return menu

    def _gce_edit_flavors(self):
        """The flavor editor the Machine page opens too, on this text's
        flavor; its Use button switches this text to the one picked."""
        def use(label):
            self.gce_flavor.set(label)
            self._gce_flavor_selected()
        return self._open_flavor_editor(self.gce_flavor.get(), use,
                                        "Use for this g-code")

    def _build_gce_side(self, props, tree_box):
        """The browser: the tree in `tree_box`, the open edit's fields in
        `props` under it (in `_gce_changes`' place, where the edits half
        opens its form), and, for an opened file only, how Generate turns
        it into fibers."""
        self._gce_changes = ttk.Frame(props)
        self._gce_changes.grid(row=1, column=0, sticky="ew")
        self._gce_changes.columnconfigure(0, weight=1)
        if hasattr(self, "_build_gce_quadrants"):
            self._build_gce_quadrants(self._legacy_holder_for_gce(),
                                      tree_box)

        # only an opened file is converted with these; a designer text uses
        # the designer's own settings, so the section shows for a file only
        conv = gui_widgets.Disclosure(
            props, "Convert to STL", open=False,
            tip="How Generate turns this opened file into fibers: the fiber "
                "diameter, the tube facets, the sag solver and the mesh "
                "detail.")
        conv.grid(row=9, column=0, sticky="ew")
        gform = ttk.Frame(conv.body)
        gform.grid(row=0, column=0, sticky="ew")
        conv.body.columnconfigure(0, weight=1)
        self._build_gcode_tab(gform)
        self._gce_convert = conv
        conv.grid_remove()

    def _gce_toggle_header(self):
        hidden = not self._gce_hide_header.get()
        self._gce_hide_header.set(hidden)
        self._gce_header_button.configure(text="Show header" if hidden else "Hide header")
        self._gce_vis_apply()

    def _build_gce_editor(self, mid):
        """The g-code text, in its overlay: a title row (caret position,
        close), the editor and the line explainer. Find / Go to line /
        Comment are its keys (Ctrl+F, Ctrl+L, Ctrl+/) and its right-click
        menu."""
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(2, weight=1)
        head = ttk.Frame(mid)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        title = ttk.Label(head, text="G-code", font="MewSection")
        self.gce_caret_var = tk.StringVar(value="")
        # a fixed width in the one-row layout, so the row never grows under
        # the buttons
        caret = gui_form.mark(ttk.Label(head, textvariable=self.gce_caret_var,
                                        style="Hint.TLabel", width=20), "live")
        look = ttk.Button(head, text="Look at", command=self._gce_look_at)
        add_tip(look, "Zoom the picture onto the caret's line, or the "
                      "selected lines, from where you are looking now.")
        self._gce_look_btn = look
        close = ttk.Button(head, text="✕", width=3,
                           command=lambda: self._gce_toggle_code(False))
        add_tip(close, "Hide the g-code text (G).")
        self._gce_code_close = close
        # one row while it fits; a narrow box puts the caret and Look at on
        # a second row, so the box can be as thin as its title and close
        self._gce_head = head
        self._gce_head_parts = (title, caret, look, close)
        sp = gui_fonts.SP["m"]
        self._gce_head_need = (title.winfo_reqwidth() + caret.winfo_reqwidth()
                               + look.winfo_reqwidth()
                               + close.winfo_reqwidth() + 3 * sp)
        self._gce_head_wide = None
        self._gce_head_reflow()
        head.bind("<Configure>", self._gce_head_reflow, add="+")
        self._gce_hide_header = tk.BooleanVar(value=False)
        self._gce_header_button = ttk.Button(
            mid, text="Hide header", command=self._gce_toggle_header)
        self._gce_header_button.grid(row=1, column=0, sticky="w")
        # the title row drags the box (not its buttons)
        self._gce_drag_bind("code", mid, head,
                            *[w for w in head.winfo_children()
                              if isinstance(w, ttk.Label)])

        self.gce = GcodeEditor(mid, self, on_change=self._gce_on_change,
                               on_idle=self._gce_on_idle,
                               on_caret=self._gce_on_caret,
                               on_step=self._gce_on_step,
                               on_focus=self._gce_hl_schedule)
        self.gce.grid(row=2, column=0, sticky="nsew")
        self.gcode_text = self.gce.text
        self.gcode_gutter = self.gce.gutter
        self._gcode_selected_line = None
        menu = tk.Menu(self.gce.text, tearoff=False)
        for label, fn in (("Find…  Ctrl+F", lambda: self.gce.open_find()),
                          ("Replace…  Ctrl+H",
                           lambda: self.gce.open_find(replace=True)),
                          ("Go to line…  Ctrl+L", lambda: self.gce.open_goto()),
                          ("Comment / uncomment  Ctrl+/",
                           lambda: self.gce.toggle_comment())):
            menu.add_command(label=label, command=fn)
        self.gce.text.bind("<Button-3>", lambda e: menu.tk_popup(
            e.x_root, e.y_root), add="+")

        # Explain this line: plain words for the caret's line (engine)
        self.gce_explain_var = tk.StringVar(value="")
        self._gce_explain = gui_form.mark(
            ttk.Label(mid, textvariable=self.gce_explain_var,
                      style="Hint.TLabel", justify="left", wraplength=400),
            "live")
        self._gce_explain.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        mid.bind("<Configure>", lambda e: self._gce_explain.configure(
            wraplength=max(200, e.width - 16)), add="+")
        # no drawn grip: the box is resized from any edge or corner, through
        # invisible strips laid over its padding (the cursor says so)
        self._gce_code_edges = self._gce_resizable(
            mid, "code", "code",
            min_size=lambda: (self._gce_code_min_width(), 140),
            done=lambda: setattr(self, "_gce_code_fitted", True))

    def _gce_find(self):
        """The ribbon's Find: the bar with its Replace row (the ribbon has
        no separate Replace button)."""
        self._gce_toggle_code(True)
        self.gce.open_find(replace=True)

    def _build_gce_preview(self, page):
        """The designer's own preview panel class, a second instance, filling
        the page (row 3). Its own chrome -- heading, subtitle, options row,
        status line -- is hidden: the page's overlays carry the same things
        (`_build_gce_display`) on the same vars, so a toggle flipped on
        either page flips both. It draws the whole file from this page's
        text; the designer's panel draws the active scaffold from its spec.
        Two instances because the designer's draws one scaffold
        (`_pv_states`, one per pill) and this one draws all of them.

        The picture renders GPU-direct into the viewport frame's own window
        (gui_preview's docstring, GPU-DIRECT). The overlays are siblings of
        the panel lifted above it, so Windows clips the GL under them.
        `_gce_viewport` is the widget the overlays are placed in."""
        if not hasattr(self, "_build_scaffold_preview_options"):
            self._gce_pv_holder = None
            self._gce_pv_panel = None
            vp = ttk.Frame(page)
            vp.grid(row=4, column=0, sticky="nsew")
            self._gce_viewport = vp
            return
        from gui_mandrel_preview import MandrelPreviewPanel
        # GPU-direct: this page is full-window (gui_preview's docstring)
        holder = MandrelPreviewPanel(page, self, direct=True)
        panel = holder.widget
        # the Viewer's ViewCube, upper right (built with the scene)
        panel.want_view_cube = True
        # a new picture's tubes are built off the window's thread, the last
        # picture staying up meanwhile (`PreviewPanel._tubes_async`)
        panel.async_tubes = True
        panel.grid(row=4, column=0, sticky="nsew")
        panel.configure(padding=0)
        for w in panel.grid_slaves():
            if w is not panel._host:
                w.grid_remove()
        # the map hands this panel's scene to THIS page, not to whichever
        # collector half the designer's panel serves
        panel._host.bind("<Map>", self._gce_pv_on_map)
        holder.set_collector_visible(False)
        holder.set_plate_visible(self._pv_plate_wanted())
        self._gce_pv_holder = holder
        self._gce_pv_panel = panel
        self._gce_viewport = panel._host
        self._gce_pv_state = self._pv_new_state()

    def _build_gce_display(self, page):
        """The display strip: the preview's display toggles (the designer's
        own builder, on one row), Reset view, and Update preview when a large
        text needs it. It sits in the bottom row, after Generate and Cancel,
        while this page shows (`_gce_full_window`): that row is otherwise
        empty between Cancel and Save, and the picture keeps the height a
        strip above it would take."""
        from gui_gcode_cad import OneRow
        # a child of the window, not the page: it is packed into the shared
        # action bar (built after the pages) while the picture shows; the
        # text-only view has no strip
        disp = ttk.Frame(self.root, style="Bar.TFrame")
        disp.columnconfigure(0, weight=1)
        flow = OneRow(disp)
        flow.configure(style="Bar.TFrame")
        flow.grid(row=0, column=0, sticky="ew")
        side = ttk.Frame(disp, style="Bar.TFrame")
        side.grid(row=0, column=1, sticky="e", padx=(6, 0))
        panel = self._gce_pv_panel
        # for a text past the auto-update size; packed only then
        self._gce_pv_btn = ttk.Button(side, text="Update preview",
                                      command=self._gce_pv_kick_now)
        add_tip(self._gce_pv_btn,
                "Rebuild the picture from the text now. A large text updates "
                "only when you ask, so typing stays quick.")
        if panel is not None:
            self._build_scaffold_preview_options(flow)
            # stopping the picture is the designer's switch, kept there
            flow.drop("Disable preview")
            self._apply_collector_mode()
        for it in flow._items:
            if isinstance(it.widget, ttk.Checkbutton):
                it.widget.configure(style="Bar.TCheckbutton")
        if panel is not None:
            # the row's last item, so it folds into More with the toggles
            rb = ttk.Button(flow, text="Reset view", style="BarLink.TButton",
                            command=panel.reset_view)
            add_tip(rb, "Frame the whole print again.")
            self._gce_reset_item = flow.add(rb)
        self._gce_flow = flow
        # display toggles, not design fields: no revert arrows beside them
        import gui_revert
        gui_revert.skip(disp)
        return disp

    def _gce_corner_refresh(self):
        """The corner box: the picture's status when it has one, else the
        one hint the page keeps on screen."""
        pv = getattr(self, "_gce_pv_panel", None)
        st = pv._status_var.get().strip() if pv is not None else ""
        self.gce_corner_var.set(st or ("Press G for the g-code"
                                       if not self._gce_code_shown else ""))
        self._gce_layout_soon()

    # ------------------------------------------------------------------ #
    # the overlays, the G key and the full window
    # ------------------------------------------------------------------ #
    _GCE_OVERLAY_GAP = 10
    _GCE_FORM_PAD = 4           # the edit box's own padding: its resize strips
    _gce_code_fitted = False    # the box has been sized to its text once
    _gce_layout_after = None
    def _gce_layout_soon(self, _e=None):
        if self._gce_layout_after is None:
            self._gce_layout_after = self.root.after_idle(self._gce_layout)

    def _gce_text_only(self):
        """The Legacy UI's G-code text: the page is its text, nothing else. A
        calibration slide keeps the whole window, the only place it exists."""
        return getattr(self, "_legacy_on", False) \
            and not getattr(self, "_cal_on", False)

    def _gce_sync_text_only(self):
        on = self._gce_text_only()
        if on != getattr(self, "_gce_text_shown", False):
            self._gce_text_shown = on
            if on:
                self._gce_ribbon.grid_remove()
                self._gce_explain.grid_remove()
                self._gce_code_edges.forget()
                self._gce_look_btn.grid_remove()
                self._gce_code_close.grid_remove()
            else:
                self._gce_ribbon.grid()
                self._gce_explain.grid()
                self._gce_code_edges.place()
                self._gce_look_btn.grid()
                self._gce_code_close.grid()
            self._gce_layout_soon()

    def _gce_layout(self):
        """Place the overlays over the viewport, mockup B's arrangement:
        the tree box top-left sized to its content, the selection pill top
        centre, the g-code text (when shown) down the right, the display
        strip bottom centre, the status box bottom-left. Re-run on a resize
        of the viewport or of any overlay."""
        self._gce_layout_after = None
        vp = self._gce_viewport
        try:
            W, H = vp.winfo_width(), vp.winfo_height()
        except tk.TclError:
            return
        if W < 160 or H < 160:
            return
        if self._gce_text_only():
            for w in (self._gce_browser, self._gce_formbox, self._gce_corner,
                      self._gce_help):
                w.place_forget()
            self._gce_code.place(in_=vp, x=0, y=0, width=W, height=H)
            self._gce_code.lift()
            return
        m = self._GCE_OVERLAY_GAP
        corner = self._gce_corner
        cbh = corner.winfo_reqheight() if self.gce_corner_var.get() else 0
        avail = H - 2 * m - (cbh + m if cbh else 0)   # above the status box
        # the tree as tall as its rows, but never past the window
        if hasattr(self, "_gce_tree_fit"):
            cap = max(3, (avail - 60) // self._gce_tree_rowheight())
            if cap != getattr(self, "_gce_tree_cap", None):
                self._gce_tree_cap = cap
                self._gce_tree_fit()
        bw = max(min(330, max(240, int(W * 0.26))),
                 min(getattr(self, "_gce_tree_want", 0), W // 2))
        bh = min(self._gce_browser.winfo_reqheight(), avail)
        self._gce_browser.place(in_=vp, x=m, y=m, width=bw, height=bh)
        dragged = self._gce_dragged()
        right = 0
        if self._gce_code_shown:
            cw = min(460, max(340, int(W * 0.36)))
            if not self._gce_code_fitted:
                fit = self._gce_code_fit_width()
                if fit is not None:
                    self._gce_code_fitted = True
                    self._gce_sized()["code"] = (fit, None)
            cw, ch = self._gce_sized().get("code", (cw, None))
            ch = avail if ch is None else ch
            cw = min(max(self._gce_code_min_width(), cw), max(1, W - 2 * m))
            ch = min(max(140, ch), max(1, avail))
            x, y = self._gce_drag_spot("code", W - m - cw, m, cw, ch, W, H)
            self._gce_code.place(in_=vp, x=x, y=y, width=cw, height=ch)
            if "code" not in dragged:
                right = cw + m
        else:
            self._gce_code.place_forget()
        # the open edit's box, beside the tree -- or where it was dragged
        mx0 = bw + 2 * m
        fb = self._gce_formbox
        host = getattr(self, "_gce_form", None)
        if host is not None:
            self._gce_drag_bind_form(host)
            body = fb.body
            room = (W - 2 * m) if "form" in dragged else (W - mx0 - right - m)
            # the box's border and padding, and the panel's own height (the
            # body's minimum row, set below for a resized box, would count)
            chrome = 2 + 2 * self._GCE_FORM_PAD
            try:
                _l, pt, _r, pb = [int(float(str(v))) for v in
                                  fb.tk.splitlist(body.cget("padding"))]
            except (tk.TclError, ValueError):
                pt = pb = 0
            need = host.winfo_reqheight() + pt + pb
            sized = self._gce_sized().get(self._gce_form_size_key())
            if sized is not None:
                # the size the user dragged it to, inside the picture
                fw = max(200, min(sized[0], W - 2 * m))
                fh = max(80, min(sized[1], H - 2 * m))
                room = fw
            else:
                fh = min(need + chrome + 2, avail)
            # a box shorter than its content scrolls, and its scrollbar
            # takes width from the content: leave room for it, or every
            # row's right edge is cut by the bar's width
            inside = fh - chrome
            bar = fb._vsb.winfo_reqwidth() if inside < need else 0
            if sized is None:
                fw = max(200, min(max(320, body.winfo_reqwidth() + chrome
                                      + 2 + bar), room))
            self._gce_form_room = room      # Calibrate's cards wrap to it
            # a box taller than its panel: the panel fills it
            fill = max(0, inside - pt - pb) if sized is not None else 0
            if body.grid_rowconfigure(1)["minsize"] != fill:
                body.rowconfigure(1, minsize=fill, weight=1 if fill else 0)
            x, y = self._gce_drag_spot("form", max(m, min(mx0, W - m - fw)),
                                       m, fw, fh, W, H)
            fb.place(in_=vp, x=x, y=y, width=fw, height=fh)
            fb.lift()
        else:
            fb.place_forget()
        if self.gce_corner_var.get():
            corner.place(in_=vp, x=m, y=H - m, anchor="sw")
        else:
            corner.place_forget()
        for w in (self._gce_browser, self._gce_code, corner):
            w.lift()
        if getattr(self, "_gce_form", None) is not None:
            fb.lift()
        # the box dragged last stays on top of the other
        top = {"code": self._gce_code, "form": fb}.get(
            getattr(self, "_gce_drag_top", None))
        if top is not None and top.winfo_ismapped():
            top.lift()
        # Help, top right, over the g-code text when that is shown
        hb = self._gce_help
        if self._gce_help_shown:
            hw, hh = self._gce_sized().get(
                "help", (hb.winfo_reqwidth(), hb.winfo_reqheight()))
            hw, hh = min(hw, W - 2 * m), min(hh, avail)
            x, y = self._gce_drag_spot("help", W - m - hw, m, hw, hh, W, H)
            hb.place(in_=vp, x=x, y=y, anchor="nw", width=hw, height=hh)
            hb.lift()
        else:
            hb.place_forget()
        if self._gce_intro.winfo_ismapped():
            self._gce_intro.lift()

    # -- dragging and resizing the boxes over the picture ------------------- #
    def _gce_head_reflow(self, _e=None):
        """The box's title row: one row while the box is wide enough for
        title, caret position, Look at and close; narrower, the caret and
        Look at drop to a second row under the title and close."""
        head = self._gce_head
        w = head.winfo_width()
        wide = w <= 1 or w >= self._gce_head_need
        if wide == self._gce_head_wide:
            return
        self._gce_head_wide = wide
        title, caret, look, close = self._gce_head_parts
        sp = gui_fonts.SP["m"]
        for part in self._gce_head_parts:
            part.grid_forget()
        for col in range(4):
            head.columnconfigure(col, weight=0)
        head.columnconfigure(3 if wide else 1, weight=1)
        title.grid(row=0, column=0, sticky="w")
        close.grid(row=0, column=3, sticky="e")
        if wide:
            caret.configure(width=20)
            caret.grid(row=0, column=1, sticky="w", padx=(sp, 0))
            look.grid(row=0, column=2, padx=(sp, 0))
        else:
            caret.configure(width=1)        # fills the room, clipped if short
            look.grid(row=1, column=0, sticky="w", pady=(4, 0))
            caret.grid(row=1, column=1, columnspan=3, sticky="ew",
                       padx=(sp, 0), pady=(4, 0))
        if getattr(self, "_gce_text_shown", False):
            look.grid_remove()
            close.grid_remove()

    def _gce_code_min_width(self):
        """Narrowest the box goes: its title with the close button on one
        row, Look at with a little of the caret position under it."""
        try:
            title, _c, look, close = self._gce_head_parts
            row0 = title.winfo_reqwidth() + close.winfo_reqwidth()                 + gui_fonts.SP["m"]
            row1 = look.winfo_reqwidth() + 60
            return max(row0, row1) + 14
        except tk.TclError:
            return 220

    def _gce_code_fit_width(self):
        """The box width that shows the widest line of the text in full (the
        gutter, the scrollbar and the box's chrome included), or None while
        there is no text to fit. At least as wide as the title row."""
        ed = self.gce
        lines = ed._lines
        widest = max(lines, key=len) if lines else ""
        if not widest.strip():
            return None
        try:
            # the text's padx, and three characters of air past the line
            text_px = (ed._font.measure(widest) + 2 * 4
                       + 3 * ed._font.measure("0"))
            chrome = (ed.gutter.winfo_reqwidth() + ed._sby.winfo_reqwidth()
                      + 6 + 6 + 2 + 2)       # box padding, border, slack
        except tk.TclError:
            return None
        return max(self._gce_code_min_width(), text_px + chrome)

    def _gce_form_size_key(self):
        """The open panel's own key in `_gce_sized`: each panel of the edit
        box keeps the size it was dragged to (Presets is not Move's size),
        while the box keeps one place for them all."""
        if getattr(self, "_gce_form", None) is None:
            return None
        # "Footprint · scaffold 2" is Footprint's size too
        title = getattr(self, "_gce_form_title", "") or ""
        return ("form", title.split(" · ")[0])

    def _gce_sized(self):
        """The size the user dragged each box to, ``{key: (w, h)}`` (an `h`
        of None: the room's height). Like `_gce_dragged`, this session
        only, and closing a box keeps its size for next time."""
        d = getattr(self, "_gce_size_at", None)
        if d is None:
            d = self._gce_size_at = {}
        return d

    def _gce_resizable(self, box, size_key, pos_key=None,
                       sides=gui_widgets.RESIZE_SIDES, min_size=(240, 120),
                       relayout=None, done=None, laid_from_nw=False):
        """Let a drag on any edge or corner in `sides` resize `box`, a panel
        floating over the picture (`gui_widgets.ResizeEdges`). The side
        opposite the one dragged stays where it is, and the box never
        leaves the picture or goes under `min_size` (a pair, or a callable
        giving one). The size lands in `_gce_sized()[size_key]` (a callable
        `size_key` names the panel open at the press), the place in
        `_gce_dragged()[pos_key]` when the box has one; `relayout()` (the
        page's layout by default) then lays the box at its new size.
        `laid_from_nw`: the layout places the box by its top-left corner,
        so a drag on the right or bottom moves nothing and leaves the box
        where the layout puts it (the edit box's place is shared by every
        panel it shows, and pinning it there moved the next one).
        Returns the strips, so a caller can hide them (`forget`)."""
        def press(_sides):
            if self._gce_text_only():
                return None
            vp = self._gce_viewport
            try:
                x = box.winfo_rootx() - vp.winfo_rootx()
                y = box.winfo_rooty() - vp.winfo_rooty()
                w, h = box.winfo_width(), box.winfo_height()
            except tk.TclError:
                return None
            if w <= 1 or h <= 1:
                return None
            key = size_key() if callable(size_key) else size_key
            if key is None:
                return None
            if pos_key is not None:
                self._gce_drag_top = pos_key
            box.lift()
            return key, x, y, w, h

        def drag(st, sides, dx, dy):
            key, x, y, w, h = st
            vp = self._gce_viewport
            W, H = vp.winfo_width(), vp.winfo_height()
            m = self._GCE_OVERLAY_GAP
            minw, minh = min_size() if callable(min_size) else min_size
            minw, minh = min(minw, W - 2 * m), min(minh, H - 2 * m)
            if "w" in sides:
                right = x + w
                x = max(m, min(x + dx, right - minw))
                w = right - x
            elif "e" in sides:
                w = max(minw, min(w + dx, W - m - x))
            if "n" in sides:
                bottom = y + h
                y = max(m, min(y + dy, bottom - minh))
                h = bottom - y
            elif "s" in sides:
                h = max(minh, min(h + dy, H - m - y))
            self._gce_sized()[key] = (w, h)
            pin = pos_key is not None and (
                not laid_from_nw or "w" in sides or "n" in sides
                or pos_key in self._gce_dragged())
            if pin:
                rw, rh = self._gce_drag_room(w, h, W, H)
                self._gce_dragged()[pos_key] = (
                    min(1.0, max(0.0, (x - m) / rw)) if rw else 0.0,
                    min(1.0, max(0.0, (y - m) / rh)) if rh else 0.0)
            if done is not None:
                done()
            (relayout or self._gce_layout)()

        return gui_widgets.ResizeEdges(box, press, drag, sides=sides)

    def _gce_dragged(self):
        """Where the user dragged each box, ``{key: (rx, ry)}``: fractions
        of the room the box has to move in the viewport, so a dragged box
        keeps its place through a resize and can never leave the window.
        This session only; closing a box keeps its place for next time."""
        d = getattr(self, "_gce_drag_at", None)
        if d is None:
            d = self._gce_drag_at = {}
        return d

    def _gce_drag_room(self, w, h, W, H):
        m = self._GCE_OVERLAY_GAP
        return max(0, W - 2 * m - w), max(0, H - 2 * m - h)

    def _gce_drag_spot(self, key, x, y, w, h, W, H):
        """The box's place: its laid-out `x, y`, or where it was dragged."""
        pos = self._gce_dragged().get(key)
        if pos is None:
            return x, y
        m = self._GCE_OVERLAY_GAP
        rw, rh = self._gce_drag_room(w, h, W, H)
        return m + round(pos[0] * rw), m + round(pos[1] * rh)

    def _gce_drag_bind(self, key, box, *handles, strip=None):
        """Let `handles` drag `box`. `strip(e)`, when given, says whether a
        press on a handle is on the title row at all."""
        def start(e):
            if self._gce_text_only() or (strip is not None and not strip(e)):
                return None
            try:
                x, y = int(box.place_info()["x"]), int(box.place_info()["y"])
            except (KeyError, ValueError, tk.TclError):
                return None
            self._gce_drag_now = (key, box, e.x_root, e.y_root, x, y)
            self._gce_drag_top = key
            box.lift()
            return "break"

        def move(e):
            st = getattr(self, "_gce_drag_now", None)
            if st is None or st[0] != key:
                return None
            _k, _b, px, py, x0, y0 = st
            vp = self._gce_viewport
            W, H = vp.winfo_width(), vp.winfo_height()
            rw, rh = self._gce_drag_room(box.winfo_width(),
                                         box.winfo_height(), W, H)
            m = self._GCE_OVERLAY_GAP
            x = min(max(x0 + e.x_root - px, m), m + rw)
            y = min(max(y0 + e.y_root - py, m), m + rh)
            self._gce_dragged()[key] = ((x - m) / rw if rw else 0.0,
                                        (y - m) / rh if rh else 0.0)
            box.place_configure(x=x, y=y)
            return "break"

        def stop(_e):
            if getattr(self, "_gce_drag_now", None) is not None:
                self._gce_drag_now = None
                self._gce_layout_soon()

        for w in handles:
            if strip is None:
                w.configure(cursor="fleur")
            w.bind("<ButtonPress-1>", start, add="+")
            w.bind("<B1-Motion>", move, add="+")
            w.bind("<ButtonRelease-1>", stop, add="+")

    def _gce_drag_bind_form(self, host):
        """The edit box's title row is the open panel's first grid row
        (its title, built with the panel); bind it once per panel. The
        rest of that row is the panel's own background, so a press there
        drags only above the row's foot, and the cursor says so."""
        if getattr(self, "_gce_drag_host", None) is host:
            return
        self._gce_drag_host = host
        fb = self._gce_formbox
        title = [w for w in host.grid_slaves(row=0)
                 if isinstance(w, ttk.Label)]
        self._gce_drag_bind("form", fb, *title)

        def on_strip(e):
            try:
                return e.y < host.grid_bbox(0, 0)[3]
            except (tk.TclError, IndexError):
                return False

        def hover(e):
            want = "fleur" if on_strip(e) else ""
            if str(host.cget("cursor")) != want:
                host.configure(cursor=want)
        self._gce_drag_bind("form", fb, host, strip=on_strip)
        host.bind("<Motion>", hover, add="+")

    def _gce_undo(self):
        """The page's Undo: the buttons, the tree and the keys all come
        here (`gui_gcode_work` adds a design change that is waiting)."""
        self._gce_step_said(self.gce.undo, "Undone.")

    def _gce_redo(self):
        self._gce_step_said(self.gce.redo, "Redone.")

    def _gce_step_said(self, step, said):
        """Take one undo step, and when it took one, say so on the status
        line: the line still described what was just taken back ("Scaffold
        3 is beside the others" over a print with two)."""
        before = (len(self.gce._undo), len(self.gce._redo))
        step()
        if (len(self.gce._undo), len(self.gce._redo)) != before:
            try:
                self.status.set(said)
            except (AttributeError, tk.TclError):
                pass

    def _gce_toggle_code(self, show=None):
        """Show or hide the g-code text overlay (G, or the ribbon's G-code
        tool). Remembered between sessions."""
        show = (not self._gce_code_shown) if show is None else bool(show)
        if show != self._gce_code_shown:
            self._gce_prefs_set(code_shown=show)
        self._gce_code_shown = show
        self._gce_code_btn.set_on(show)
        self._gce_corner_refresh()
        self._gce_layout()

    _GCE_TYPING = ("Text", "Entry", "TEntry", "TCombobox", "TSpinbox",
                   "Spinbox", "Listbox")

    def _gce_key_g(self, e):
        """G toggles the g-code text, as A toggles appearances in Fusion --
        unless the key is typing into something."""
        if not self._gce_page_shown() or self._gce_text_only():
            return None
        try:
            if e.widget.winfo_class() in self._GCE_TYPING:
                return None
        except (tk.TclError, AttributeError):
            return None
        self._gce_toggle_code()
        return "break"

    def _gce_full_window(self, on):
        """The page takes the whole window: its tools join the top row, and
        the preview's display strip joins the bottom row after Cancel, so
        the picture gets the full height under the ribbon. The status line
        under the bar stays as every other page has it: this page's own
        messages (`_gce_status`) and Generate's progress and refusals are
        written there, so folding it away hid them. All of it goes back on
        any other page."""
        st = getattr(self, "_shell_status", None)
        bar = getattr(self, "_shell_bar", None)
        disp = getattr(self, "_gce_disp", None)
        if st is not None:
            st.grid()
        cad = on and not self._gce_text_only()
        self._nav_context.pack(side="right", after=self._nav_gear) if cad \
            else self._nav_context.pack_forget()
        if disp is not None:
            if cad and bar is not None:
                # after Cancel: the progress bar, while a run shows it, packs
                # after Cancel too and so lands between the two
                cancel = getattr(self, "cancel_btn", None)
                disp.pack(in_=bar, side="left", padx=(8, 0),
                          **({"after": cancel} if cancel is not None
                             and cancel.master is bar else {}))
                disp.lift()
                if not getattr(self, "_gce_disp_bound", False):
                    bar.bind("<Configure>", self._gce_disp_fit, add="+")
                    self._gce_disp_bound = True
                self.root.after_idle(self._gce_disp_fit)
            else:
                disp.pack_forget()

    def _gce_disp_fit(self, _e=None):
        """Fit the display row to what the bottom row leaves it: the items
        that do not fit fold into its More link (`OneRow.fit`), so a narrow
        window never cuts a toggle mid-word. The room is counted with the
        progress bar on the row at its full length even while it is off, so
        a run starting neither shortens the bar nor shuffles the toggles."""
        flow = getattr(self, "_gce_flow", None)
        bar = getattr(self, "_shell_bar", None)
        disp = getattr(self, "_gce_disp", None)
        if flow is None or bar is None or disp is None                 or disp.winfo_manager() != "pack":
            return
        try:
            if _e is None:
                # called after a pack change, not a resize: the requests up
                # the chain to the bar settle in later idle passes
                bar.update_idletasks()
            have = bar.winfo_width()
            if have <= 1:
                return
            # the bar's request less the row's: what everything else asks
            other = bar.winfo_reqwidth() - flow.winfo_reqwidth()
            prog = getattr(self, "progress", None)
            if prog is not None and prog.winfo_manager() != "pack":
                other += prog.winfo_reqwidth() + 24     # its padx=12 each side
            flow.fit(have - other)
        except tk.TclError:
            return

    # ------------------------------------------------------------------ #
    # page show / the shared glue's hooks
    # ------------------------------------------------------------------ #
    def _gce_page_shown(self):
        try:
            return (self.nb.select() == str(self.tab_gcode)
                    and getattr(self, "gce", None) is not None)
        except (tk.TclError, AttributeError):
            return False

    def _page_arrived(self):
        """A linked page whose design moved fills its tree from a regeneration
        in a subprocess. The picture is not waited for."""
        if self._gce_page_shown() and self._gce_regen is not None:
            return False
        return super()._page_arrived()

    def _on_tab_changed(self, _event=None):
        # Out of sight, the picture gives back its tubes before anything the
        # switch starts (Simulation's viewer) sizes itself to free memory.
        panel = getattr(self, "_gce_pv_panel", None)
        if panel is not None and hasattr(panel, "hold_mesh"):
            panel.hold_mesh(not self._gce_page_shown())
        super()._on_tab_changed(_event)
        shown = self._gce_page_shown()
        if getattr(self, "gce", None) is not None:
            self._gce_full_window(shown)
        if shown:
            self._gce_on_show()

    def _gce_on_show(self):
        """The page came up: a linked text checks whether the design moved
        and regenerates if so; a never-filled page links to the designer."""
        doc = self._gce_doc
        if doc["source"] is None:
            doc["source"] = self._GCE_LINKED
            self._gce_set_link_label()
        if doc["source"] == self._GCE_LINKED:
            self._gce_check_design()
        self._gce_pv_refresh_if_stale()
        self._gce_refresh_controls()
        self._gce_say_arrival()

    #: Design's line on arrival, as Calibrate has its own: where to start
    _GCE_ARRIVAL = ("Design: the tree on the left is your print. Press "
                    "Parameters to set a scaffold's size, layers, layer "
                    "angles and fiber spacing, or New scaffold to add one. "
                    "Then press Generate.")

    def _gce_say_arrival(self):
        """Say where to start, in the status line, on arriving at Design
        from another page or from Calibrate. Calibrate's own line is not
        Design's: a stranger read "Calibrate is optional…" on this page."""
        if getattr(self, "_cal_on", False):
            return                      # Calibrate says its own
        if self._gce_doc.get("source") == self._GCE_FILE:
            return                      # an opened file is not a design
        text = self._GCE_ARRIVAL
        boot = getattr(self, "_boot_design_preset", None)
        work = getattr(self, "gce_work_var", None)
        if boot and work is not None \
                and work.get().endswith("starting design"):
            # the startup preset is on the page, untouched: say it is a
            # place to start from, not the user's own work
            text = ("Design: the print on the left is the starting design "
                    "“%s”, to change into yours. " % boot.get("name", "")
                    + text.split(". ", 1)[1])
        try:
            self.status.set(text)
        except (AttributeError, tk.TclError):
            pass

    def _load_gcode_text(self):
        """The shared refresh after a run or a viewer-tab visit. The page owns
        its text now, so a refresh never overwrites it; it only re-reads the
        design when linked (a Generate may have followed a design edit)."""
        if not hasattr(self, "gce"):
            return super()._load_gcode_text()
        self._gce_refresh_controls()
        if self._gce_page_shown() \
                and self._gce_doc["source"] == self._GCE_LINKED:
            self._gce_check_design()

    def _set_gcode_text(self, text):
        if not hasattr(self, "gce"):
            return super()._set_gcode_text(text)
        self.gce.set_text(text, undoable=False)

    def _refresh_gcode_display_on_toggle(self):
        """An output option flipped (here or on the Export card): re-render
        the designer's text with it. Nothing regenerates -- the options act
        on the text only."""
        if getattr(self, "_loading_preset", False) or not hasattr(self, "gce"):
            return
        doc = self._gce_doc
        if doc["source"] != self._GCE_LINKED or not doc["raw"]:
            self._gce_render_changes()
            return
        self._gce_take_design(doc["raw"], doc["sig"])

    def _copy_gcode_text(self):
        self._copy_text(self.gce.get_text() if hasattr(self, "gce")
                        else self.gcode_text.get("1.0", "end-1c"))
        self.status.set("G-code copied to clipboard.")

    # ------------------------------------------------------------------ #
    # the linked source: regenerate from the designer
    # ------------------------------------------------------------------ #
    def _gce_designer_mode(self):
        coll = getattr(self, "s_collector", None)
        coll = coll.get() if coll is not None else "plate"
        return "mandrel" if coll == "mandrel" else "scaffold"

    def _gce_scratch(self, name):
        d = os.path.join(self.session_dir, "editor")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, name)

    def _gce_design_argv(self):
        """(argv, signature, error): the designer's own Generate argv with its
        output pointed at this page's scratch file and the STL stage off.

        Built by the designer's own builders (`_build_args_scaffold` and the
        mandrel inversion of it), prefix included, so the text is
        the file Generate writes. The prefix is NOT swapped for the plain
        worker: with a physics law on it is mew_export, and Generate's argv
        carries flags only that wrapper reads (`--auto-corner-grid`,
        `--curve-quality`), which the plain scaffold worker refuses. The signature is the argv with the scaffolds-json path
        replaced by that file's content, so a design edit is seen and an
        unchanged design never regenerates. Never clobbers a running
        Generate's scaffolds json."""
        mode = self._gce_designer_mode()
        validate = {"scaffold": "_validate_scaffold",
                    "mandrel": "_validate_mandrel"}[mode]
        fn = getattr(self, validate, None)
        try:
            err = fn() if callable(fn) else None
        except Exception as exc:  # noqa: BLE001 -- a half-typed field
            err = str(exc)
        if err:
            return None, None, err
        out = self._gce_scratch("design.gcode")
        saved = (self.s_gcode_out.get(), self.s_make_stl.get(),
                 self._scaffolds_json_tmp)
        flag = {"mandrel": "_mandrel_dispatch"}.get(mode)
        if flag:
            setattr(self, flag, True)
        self._pv_suppress = True
        try:
            self.s_gcode_out.set(out)
            self.s_make_stl.set(False)
            argv = list(self._build_args_scaffold())
            json_path = self._scaffolds_json_tmp \
                if self._scaffolds_json_tmp != saved[2] else None
        except Exception as exc:  # noqa: BLE001
            return None, None, f"the design could not be read ({exc})"
        finally:
            self._pv_suppress = False
            if flag:
                setattr(self, flag, False)
            self.s_gcode_out.set(saved[0])
            self.s_make_stl.set(saved[1])
            self._scaffolds_json_tmp = saved[2]
        sig = list(argv)
        if json_path and os.path.isfile(json_path):
            try:
                with open(json_path, encoding="utf-8") as fh:
                    content = fh.read()
                sig = [content if a == json_path else a for a in sig]
            except OSError:
                pass
        sig.append(f"collector={mode}")
        return argv, tuple(sig), None

    def _gce_check_design(self):
        argv, sig, err = self._gce_design_argv()
        doc = self._gce_doc
        if err:
            doc["error"] = err
            gap = getattr(self, "_gce_tube_gap", None) or \
                getattr(self, "_mandrel_length_gap", None)
            if (self._gce_designer_mode() == "mandrel" and callable(gap)
                    and gap() is not None):
                # a tube length that does not close: the Parameters table
                # says so and offers the nearest that do (not
                # up here). The text is not remade, so no preview kick
                # will say it.
                import gui_mandrel
                self._gce_notice_hide_if("design-error")
                self._gce_pv_gap = True
                panel = getattr(self, "_gce_pv_panel", None)
                if panel is not None:
                    panel.set_status(gui_mandrel._MANDREL_GAP_STATUS_PAGE,
                                     error=True)
                return
            # the page owns its scaffolds, and the Parameters table already
            # marks the bad cell: the reason goes on the preview's own status
            # line, not a banner or popup sending the user to the designer
            self._gce_pv_gap = False
            self._gce_notice_hide_if("design-error")
            panel = getattr(self, "_gce_pv_panel", None)
            if panel is not None:
                panel.set_status(f"Not redrawn: {err}", error=True)
            return
        doc["error"] = None
        if sig == doc["sig"] and doc["raw"]:
            self._gce_notice_hide_if("design-error")
            return
        self._gce_start_regen(argv, sig)

    def _gce_start_regen(self, argv, sig):
        """Run the designer's argv in a subprocess of its own -- never
        `self.proc`, which belongs to Generate."""
        self._gce_stop_regen()
        self.gce_link_var.set("This page's design · updating…")
        creationflags = 0x08000000 if IS_WINDOWS else 0
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=HERE, creationflags=creationflags, **WORKER_PIPE_UTF8)
        except Exception as exc:  # noqa: BLE001
            self._gce_notice_show(f"The g-code could not be made: {exc}", [])
            self._gce_set_link_label()
            return
        out = argv[argv.index("-o") + 1] if "-o" in argv else None
        job = dict(proc=proc, sig=sig, out=out)
        self._gce_regen = job

        def _wait(job=job):
            try:
                log = job["proc"].communicate()[0] or ""
            except Exception as exc:  # noqa: BLE001
                log = str(exc)
            self._gce_regen_q.put((job, job["proc"].returncode, log))
        threading.Thread(target=_wait, daemon=True).start()
        self._gce_arm_regen_poll()

    def _gce_arm_regen_poll(self):
        if self._gce_regen_poll is None:
            self._gce_regen_poll = self.root.after(120, self._gce_regen_tick)

    def _gce_regen_tick(self):
        self._gce_regen_poll = None
        try:
            while True:
                job, code, log = self._gce_regen_q.get_nowait()
                if job is not self._gce_regen:
                    continue                    # overtaken by a newer one
                self._gce_regen = None
                self._gce_regen_done(job, code, log)
        except queue.Empty:
            pass
        if self._gce_regen is not None:
            self._gce_arm_regen_poll()

    def _gce_stop_regen(self):
        job, self._gce_regen = self._gce_regen, None
        if job is not None and job["proc"].poll() is None:
            try:
                job["proc"].terminate()
            except Exception:  # noqa: BLE001
                pass

    def _gce_regen_done(self, job, code, log):
        if code != 0 or not (job["out"] and os.path.isfile(job["out"])):
            said = [ln.strip() for ln in log.splitlines()
                    if ln.strip() and not ln.startswith("[")]
            reason = said[-1][:300] if said else f"exit code {code}"
            self._gce_notice_show(
                f"This design's g-code could not be made. It said: "
                f"{reason}", [("Show the log",
                               lambda: self._nav_select("log"))])
            self._gce_set_link_label()
            return
        try:
            with open(job["out"], encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError as exc:
            self._gce_notice_show(f"The new g-code could not be read: {exc}",
                                  [])
            self._gce_set_link_label()
            return
        self._gce_take_design(raw, job["sig"])
        self._gce_restamp_unfilled(job["sig"])

    def _gce_restamp_unfilled(self, sig):
        """The first fill of a page that Generate built while it was still
        empty (`run`). That result was noted against the empty text, so the
        top row read "Changed since Generate" for a build of this very text.
        A fill from the same design argv is what was built: re-note it."""
        built = getattr(self, "_gce_unfilled_build", None)
        self._gce_unfilled_build = None
        renote = getattr(self, "_summary_renote", None)
        if built is None or built != sig or not callable(renote):
            return
        now = self._design_signature("gcode")
        if now is not None:
            renote("gcode", dict(now, __text__=hash("")), now)

    def _gce_render_design(self, raw):
        """The designer's raw g-code -> the text Save writes: the Export
        card's output options, through the one seam Save itself uses
        (`_gcode_for_display_export`), read as a scaffold's own output."""
        saved = (getattr(self, "_last_gcode_is_scaffold", False),
                 getattr(self, "_last_gcode_is_calibration", False))
        self._last_gcode_is_scaffold = True
        self._last_gcode_is_calibration = False
        try:
            return self._gcode_for_display_export(raw)
        finally:
            (self._last_gcode_is_scaffold,
             self._last_gcode_is_calibration) = saved

    def _gce_take_design(self, raw, sig):
        """A new designer g-code arrived (or an output option changed).
        Unedited text is replaced; edited text is kept, and the header says
        the design moved under it, with the one-click way to take the new
        one."""
        doc = self._gce_doc
        old_base = doc["base"]
        edited = doc["source"] == self._GCE_LINKED and old_base \
            and self.gce.get_text() != old_base
        new_base = self._gce_render_design(raw)
        doc.update(source=self._GCE_LINKED, raw=raw, sig=sig, path=None,
                   mode="g1", stale=False)
        doc["diameter"] = self._gce_designer_diameter()
        if edited and new_base != old_base:
            doc["pending_base"] = new_base
            n = len(self._gce_hunks)
            self._gce_notice_show(
                "The design changed since you edited this text. Your "
                f"{n} hand edit{'s' if n != 1 else ''} are kept below, on "
                "the old g-code.",
                [("Take the new g-code (drops your edits)",
                  self._gce_take_pending),
                 ("Keep editing", lambda: self._gce_notice_hide())],
                kind="design-moved")
        else:
            doc["base"] = new_base
            doc.pop("pending_base", None)
            if not edited:
                self.gce.set_text(new_base, undoable=bool(old_base))
            self._gce_notice_hide_if("design-moved")
        self._gce_set_link_label()
        self._gce_render_changes()
        self._gce_pv_schedule(now=True)
        self._gce_refresh_controls()

    def _gce_take_pending(self):
        doc = self._gce_doc
        new = doc.pop("pending_base", None)
        if new is None:
            return
        doc["base"] = new
        self.gce.set_text(new, undoable=True)
        self._gce_notice_hide()
        self._gce_render_changes()
        self._gce_pv_schedule(now=True)

    def _gce_designer_diameter(self):
        try:
            return float(self.gce_scaf_vars[0]["diameter"].get())
        except (AttributeError, IndexError, KeyError, ValueError,
                tk.TclError):
            return 0.02

    def _gce_relink(self):
        """Follow the designer again. One undo step away from the opened
        text."""
        doc = self._gce_doc
        doc.update(source=self._GCE_LINKED, sig=None, raw="", path=None)
        self._gce_flavor_reset()
        self._gce_set_link_label()
        self._gce_check_design()

    # the designer's app-wide edits (Quality, Export, the base...) reach the
    # page; its per-scaffold design does not -- the page has its own
    # (`gui_gcode_twodesigns`), and an unchanged signature never regenerates
    def _pv_all_dirty(self):
        if not getattr(self, "_pv_suppress", False):
            self._gce_export_preview_cancel()
        super()._pv_all_dirty()
        self._gce_design_touched()

    def _gce_design_touched(self):
        if getattr(self, "_pv_suppress", False) or not hasattr(self, "gce"):
            return
        # an app-wide input of the picture moved (`_pv_all_dirty`): the
        # picture is stale even when the text it regenerates to is not
        self._gce_pv_missed = True
        if not self._gce_page_shown():
            return
        if self._gce_doc["source"] != self._GCE_LINKED:
            # an opened file or a detached text is never regenerated, but
            # its picture is drawn at g-code zero and with the app-wide
            # physics and quality all the same: redraw it
            self._gce_pv_schedule()
            return
        aid = getattr(self, "_gce_touch_after", None)
        if aid is not None:
            try:
                self.root.after_cancel(aid)
            except (ValueError, tk.TclError):
                pass
        self._gce_touch_after = self.root.after(400, self._gce_touch_fire)

    def _gce_touch_fire(self):
        self._gce_touch_after = None
        if self._gce_page_shown():
            self._gce_check_design()
            self._gce_pv_refresh_if_stale()

    # ------------------------------------------------------------------ #
    # the file source: Open file...
    # ------------------------------------------------------------------ #
    def _gce_open_file(self):
        path = filedialog.askopenfilename(
            title="Open a g-code file",
            filetypes=[("G-code", "*.gcode *.gco *.g *.nc *.txt"),
                       ("All files", "*.*")])
        if path:
            self.in_var.set(path)
            self._gce_load_file(path)

    def browse_input(self):
        """The Convert-to-STL section's file row: the same as Open file."""
        before = self.in_var.get()
        super().browse_input()
        path = self.in_var.get().strip()
        if path and path != before and hasattr(self, "gce"):
            self._gce_load_file(path)

    def _gce_load_file(self, path):
        # read the way a file from another tool needs: its encoding honoured
        # (Notepad's "Unicode" is UTF-16) and its line numbers, checksums and
        # packed words tidied, so every reader downstream -- the STL converter,
        # the simulation, the viewer -- sees the same moves this page shows
        import mew_gcode
        pre = self._gce_prepared_for(path)
        try:
            text, notes = ((pre["text"], pre["notes"]) if pre is not None
                           else mew_gcode.read_foreign_gcode(path))
        except OSError as exc:
            self._gce_notice_show(f"That file could not be opened: {exc}", [])
            return
        self._gce_stop_regen()
        try:
            dia = float(self.diameter.get())
        except (ValueError, tk.TclError):
            dia = 0.02
        self._gce_doc.update(source=self._GCE_FILE, raw=text, base=text,
                             path=path, sig=None, mode="auto", diameter=dia,
                             stale=False)
        self._gce_doc.pop("pending_base", None)
        self._gce_flavor_reset()
        self.gce.set_text(text, undoable=True, keep_view=False)
        if notes:
            self._gce_notice_show(
                "Opened %s and made it readable: %s."
                % (os.path.basename(path), "; ".join(notes)),
                [("OK", self._gce_notice_hide)], kind="file-tidied",
                hide_key="file-tidied")
        else:
            self._gce_notice_hide()
        # the arrival line ("the print on the left is the starting design
        # ...") stayed under an opened file, naming a design not on the page
        self._gce_status("Opened %s. Generate turns it into fibers for "
                         "Simulation; Back to this design returns to your "
                         "design." % os.path.basename(path))
        self._gce_set_link_label()
        self._gce_render_changes()
        self._gce_pv_schedule(now=True)
        self._gce_refresh_controls()
        if not self._gce_page_shown():
            self._nav_select("gcode")

    def _gce_prepared_for(self, path):
        """The worker's preparation of `path` (`_gce_prepare_file`, handed
        over in `_gce_prepared` by File > Open), when it is of this file as it
        is on disk now; else None, and the load reads the file itself."""
        pre = getattr(self, "_gce_prepared", None)
        if pre is None or pre.get("path") != path:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None
        return pre if pre.get("stat") == (st.st_mtime_ns, st.st_size) else None

    # ------------------------------------------------------------------ #
    # header state, notice, controls
    # ------------------------------------------------------------------ #
    def _gce_set_link_label(self):
        doc = self._gce_doc
        if doc["source"] == self._GCE_FILE:
            self.gce_link_var.set("● File: "
                                  + os.path.basename(doc["path"] or ""))
            self._gce_src_tool(self._gce_relink_btn, True)
            self._gce_convert.grid()
        else:
            self.gce_link_var.set("● This page's design")
            self._gce_src_tool(self._gce_relink_btn, False)
            self._gce_convert.grid_remove()
        n = self.gce.line_count() if self.gce.get_text() else 0
        self.gcode_text_status.set(
            f"{n} lines · {self.gce_flavor.get()}" if n else "")

    def _gce_src_tool(self, btn, on):
        """Back to this design shows in the File group only while the text
        is not this design's own."""
        if bool(btn.winfo_manager()) == on:
            return
        if on:
            first = [w for w in btn.master.pack_slaves() if w is not btn]
            btn.pack(side="left", padx=1, pady=(3, 0),
                     **({"before": first[0]} if first else {}))
        else:
            btn.pack_forget()
        fit = getattr(self, "_gce_ribfit", None)
        if fit is not None:
            fit.fit()

    def _build_gce_relink_tool(self, fil, tool):
        self._gce_relink_btn = tool(
            fil, "file", "design", "Back to\nthis design",
            "Replace this text with the g-code of this page's own design, "
            "and update it whenever that design changes.",
            command=self._gce_relink)
        self._gce_relink_btn.pack_forget()

    def _gce_notice_show(self, text, buttons, kind=None, hide_key=None):
        """Put `text` and its `buttons` in the page's notice strip.
        `hide_key`: a notice that only informs adds "Don't show again",
        remembered under that key in gui_prefs.json; once it is hidden the
        call shows nothing and returns False (Settings brings it back)."""
        import gui_prefs
        if hide_key and gui_prefs.is_hidden(hide_key):
            # it would have replaced its own kind's notice ("Starting…"
            # before "Started"): that one is out of date now too
            if kind:
                self._gce_notice_hide_if(kind)
            return False
        self.gce_notice_var.set(text)
        for w in self._gce_notice_btns.winfo_children():
            w.destroy()
        for label, cmd in buttons:
            ttk.Button(self._gce_notice_btns, text=label,
                       command=cmd).pack(side="left", padx=(6, 0))
        self._gce_notice_kind = kind = kind or "design-error"
        if hide_key:
            def never():
                gui_prefs.hide(hide_key)
                self._gce_notice_hide_if(kind)
            ttk.Button(self._gce_notice_btns, text="Don't show again",
                       command=never).pack(side="left", padx=(6, 0))
        self._gce_notice.grid(row=2, column=0, sticky="ew",
                              padx=gui_fonts.PAGE_MARGIN, pady=(0, 4))
        return True

    def _gce_notice_hide(self):
        self._gce_notice.grid_remove()
        self._gce_notice_kind = None

    def _gce_notice_hide_if(self, kind):
        if getattr(self, "_gce_notice_kind", None) == kind:
            self._gce_notice_hide()

    def _gce_refresh_controls(self):
        has = bool(self.gce.get_text())
        self._gce_undo_btn.configure(
            state="normal" if self.gce.can_undo() else "disabled")
        self._gce_redo_btn.configure(
            state="normal" if self.gce.can_redo() else "disabled")
        n = self.gce.line_count() if has else 0
        self.gcode_text_status.set(
            f"{n} lines · {self.gce_flavor.get()}" if n else "")
        if hasattr(self, "save_gcode_btn"):
            self._refresh_result_controls()

    # ------------------------------------------------------------------ #
    # editing callbacks
    # ------------------------------------------------------------------ #
    def _gce_on_change(self):
        if not self._gce_code_fitted:       # the first text sizes the box
            self._gce_layout_soon()
        self._gce_last_key = time.monotonic()
        self._gce_pv_schedule()

    def _gce_on_idle(self):
        """Typing paused: the gutter and the typed row only. The list of
        edits is not rebuilt here (see `_gce_render_changes`)."""
        self._gce_refresh_marks()
        self._gce_refresh_controls()

    def _gce_on_caret(self, line, col):
        self._gcode_selected_line = line
        self._gce_hl_off = False        # a caret that moved lights its line
        self.gce_caret_var.set(f"Line {line}, column {col + 1}")
        self._gce_explain_line(line)
        self._gce_hl_schedule()

    def _gce_explain_line(self, line):
        eng = self._gce_engine()
        text = self.gce.current_line_text()
        msg = ""
        if eng is not None and hasattr(eng, "explain"):
            try:
                msg = eng.explain(text, flavor=self.gce_flavor_name())
            except Exception:  # noqa: BLE001 -- an explainer never breaks typing
                msg = ""
        self.gce_explain_var.set(f"Line {line}: {msg}" if msg else "")

    # ------------------------------------------------------------------ #
    # flavor: the Machine page's, until it is picked on this page
    # ------------------------------------------------------------------ #
    def _gce_flavor_labels(self):
        eng = self._gce_engine()
        if eng is None:
            return ["Klipper"]
        if hasattr(self, "_flavor_labels"):
            return self._flavor_labels()
        return [f.label for f in eng.FLAVORS.values()]

    def _gce_machine_flavor(self):
        labels = self._gce_flavor_labels()
        var = getattr(self, "gcode_flavor", None)
        try:
            label = var.get() if var is not None else ""
        except tk.TclError:
            label = ""
        try:
            # the box may still hold an old or renamed flavor's name: this
            # runs from its write trace, before the box is normalised
            import flavor_presets
            fl = flavor_presets.by_label(label)
            if fl is not None:
                label = fl.label
        except Exception:  # noqa: BLE001 -- a build without the engine
            pass
        return label if label in labels else labels[0]

    def _gce_flavor_current(self):
        """The page's flavor label as the flavor shows it today: an old
        built-in label or a renamed flavor's former name is rewritten."""
        try:
            import flavor_presets
            v = self.gce_flavor.get()
            fl = flavor_presets.by_label(v)
        except Exception:  # noqa: BLE001 -- a build without the engine
            return
        if fl is not None and fl.label != v:
            self.gce_flavor.set(fl.label)

    def gce_flavor_name(self):
        """The engine's name for the document's flavor ("klipper", ...)."""
        eng = self._gce_engine()
        label = self.gce_flavor.get()
        try:
            import flavor_presets
            fl = flavor_presets.by_label(label)
            if fl is not None:
                return fl.name
        except Exception:  # noqa: BLE001 -- a build without the engine
            pass
        if eng is not None:
            label = eng.LEGACY_FLAVOR_LABELS.get(label, label)
            for name, f in eng.FLAVORS.items():
                if f.label == label:
                    return name
        return "klipper"

    def _gce_follow_machine(self):
        """The Machine page's flavor changed: a document that never had its
        own picked follows it."""
        if not self._gce_flavor_picked:
            self.gce_flavor.set(self._gce_machine_flavor())
            self._gce_flavor_changed()

    def _gce_flavor_selected(self, _e=None):
        self._gce_flavor_picked = (self.gce_flavor.get()
                                   != self._gce_machine_flavor())
        self._gce_flavor_changed()
        # a linked tube's feeds follow the page's flavor (`_mandrel_rotary_feed`),
        # so a pick may change the design's argv: its signature decides
        if self._gce_doc.get("source") == self._GCE_LINKED \
                and self._gce_page_shown():
            self._gce_check_design()

    def _gce_flavor_reset(self):
        """A new document (linked again, or a file opened) starts in the
        Machine page's flavor."""
        self._gce_flavor_picked = False
        self.gce_flavor.set(self._gce_machine_flavor())

    def _gce_flavor_changed(self):
        line = getattr(self, "_gcode_selected_line", None)
        if line and hasattr(self, "gce"):
            self._gce_explain_line(line)

    @staticmethod
    def _gce_engine():
        """The g-code engine module, or None on a build without it."""
        try:
            import mew_gcode
        except Exception:  # noqa: BLE001
            return None
        if mew_gcode.STRUCTURE_DIR is None:
            # where a saved file's layers are kept, out of band
            # (`mew_gcode.recall_structure`)
            mew_gcode.STRUCTURE_DIR = os.path.join(USER_DATA_DIR,
                                                   "gcode_structure")
        return mew_gcode

    # ------------------------------------------------------------------ #
    # Changes: what makes this text differ from the designer's plain output
    # ------------------------------------------------------------------ #
    def _gce_output_chips(self):
        """[(label, where, var)] for each output option that is on and
        applies to this text. A file you opened is never rewritten, so it has
        none."""
        if self._gce_doc["source"] != self._GCE_LINKED:
            return []
        chips = []
        v = getattr(self, "s_travel_as_g1", None)
        if v is not None and v.get():
            chips.append(("Travel moves written as G1", "every travel", v))
        conv = getattr(self, "s_gcode_convention", None)
        if conv is not None and conv.get() == "G0X":
            chips.append(("Commands spelled G00, G01, G02", "every move",
                          None))
        v = getattr(self, "s_wait_for_temp", None)
        if v is not None and v.get() and self._gce_wait_line():
            chips.append(("Wait for temperature: " + self._gce_wait_line(),
                          "at start", v))
        v = getattr(self, "no_layer_z", None)
        if v is not None and v.get():
            chips.append(("No height change between layers",
                          "every layer", v))
        if hasattr(self, "custom_gcode"):
            try:
                spec = self._custom_gcode_spec()
            except Exception:  # noqa: BLE001
                spec = None
            if spec is not None:
                chips.append((f"Your printer line: {spec[0]}",
                              f"after line {spec[1]} · set on the "
                              "Printer & collector page", None))
        return chips

    def _gce_wait_line(self):
        saved = getattr(self, "_last_gcode_is_scaffold", False)
        self._last_gcode_is_scaffold = True
        try:
            return self._gcode_wait_temp_line().strip()
        except Exception:  # noqa: BLE001
            return ""
        finally:
            self._last_gcode_is_scaffold = saved

    def _gce_render_changes(self):
        """The document changed (an edit added, ticked, moved, removed; a
        new design; an opened file): the gutter here; the edits half
        refreshes its library list and the tree, each keeping the rows
        that did not change, so nothing flashes."""
        if not hasattr(self, "gce"):
            return
        self._gce_refresh_marks()

    def _gce_refresh_marks(self):
        marks, hunks, tinted = self._gce_marks()
        self._gce_hunks = hunks
        self.gce.set_marks(marks)
        self.gce.set_tinted(tinted)

    # -- hooks the Changes half (gui_gcode_changes) overrides --------------- #
    def _gce_marks(self):
        """(gutter marks, hand-edit hunks, tinted lines): the plain diff of
        the text against the rendered base."""
        base = self._gce_doc.get("base") or ""
        lines = self.gce.get_text().split("\n")
        if not base:
            return {}, [], []
        marks, hunks = diff_marks(base.split("\n"), lines)
        return marks, hunks, [i for i, m in marks.items()
                              if m != _MARK_REMOVED]

    def _gce_on_step(self, meta, forward):
        """Undo/Redo reached a step carrying document snapshots."""

    def _gce_parse_text(self):
        """What the parser, converter and player read for this text."""
        return self._gce_preview_text()

    def _gce_parse_line(self, line):
        """The parse-text line (1-based) for text line `line`."""
        return line

    def _gce_doc_carries_edits(self):
        """True when the designer's Generate carries this page's changes
        itself (the engine's --gcode-doc), so an edited linked text still
        Generates through the designer."""
        return False

    def _gce_toggle_whatchanged(self):
        pass

    def _gce_detach(self):
        pass

    def _gce_edited(self):
        doc = self._gce_doc
        return bool(doc["base"]) and self.gce.get_text() != doc["base"]

    # ------------------------------------------------------------------ #
    # the preview: the whole text, on the designer's panel class
    # ------------------------------------------------------------------ #
    @staticmethod
    def _gce_preview_shift(paths, meta, shift):
        """Display rebased plate g-code back in the fixed physical plate frame.
        The shift applied rides the meta (`preview_shift`), so a picture that
        inherits pieces of this one (`_gce_depo_picture`'s overlay) can tell
        the zero they were drawn at."""
        applied = _shift_key(shift) or (0.0, 0.0, 0.0)
        meta = dict(meta, preview_shift=applied)
        if not any(applied):
            return paths, meta
        delta = np.asarray(applied, dtype=np.float64)
        paths = [np.asarray(path) + delta for path in paths]
        # both boxes: the camera frames on `frame_bbox` (`gui_preview.
        # _frame_bbox`), so left unshifted it aimed at the file's numbers and
        # a zero off the bottom-left showed an empty picture
        for key in ("bbox", "frame_bbox"):
            if meta.get(key):
                meta[key] = tuple(tuple(float(v) for v in np.asarray(p) + delta)
                                  for p in meta[key])
        if meta.get("gcode_paths"):
            meta["gcode_paths"] = [np.asarray(path) + delta
                                   for path in meta["gcode_paths"]]
        return paths, _GcodeEditorMixin._gce_shift_clock(meta, applied)

    @staticmethod
    def _gce_shift_clock(meta, shift):
        """`meta` with its line clock moved by `shift`, which it records as
        `preview_shift` (a tube's paths are moved by its builder)."""
        applied = _shift_key(shift) or (0.0, 0.0, 0.0)
        meta = dict(meta, preview_shift=applied)
        clock = meta.get("line_clock")
        if any(applied) and clock is not None and len(clock.get("verts", ())):
            clock = dict(clock)
            verts = np.asarray(clock["verts"]).copy()
            verts[:, :3] += np.asarray(applied, dtype=np.float64)
            clock["verts"] = verts
            meta["line_clock"] = clock
        return meta

    def _gce_export_preview_cancel(self):
        """Discard an obsolete export-quality solve or return to live geometry."""
        proc = getattr(self, "_gce_eq_proc", None)
        if proc is not None:
            self._gce_eq_proc = None
            try:
                proc.terminate()
            except OSError:
                pass
            if getattr(self, "proc", None) is None:
                self._progress_idle()
        previous = getattr(self, "_gce_eq_previous", None)
        self._gce_eq_previous = None
        if previous is not None and getattr(self, "_gce_pv_panel", None) is not None:
            self._gce_pv_apply(previous[0], previous[1], None)

    def _gce_export_preview_args(self):
        """Snapshot the same worker and options Generate uses, to private files."""
        stem = self._gce_scratch("quality_%d" % time.time_ns())
        gcode, stl = stem + ".gcode", stem + ".stl"
        doc = self._gce_doc
        linked = (doc["source"] == self._GCE_LINKED and doc["raw"] and
                  (not self._gce_edited() or self._gce_doc_carries_edits()))
        old_suppress = getattr(self, "_pv_suppress", False)
        old_json = self._scaffolds_json_tmp
        old_doc = getattr(self, "_gce_gen_doc", None)
        old_dispatch = self._gce_dispatch
        swaps = []
        self._pv_suppress = True
        try:
            if linked:
                mode = self._gce_designer_mode()
                flag = {"mandrel": "_mandrel_dispatch"}.get(mode)
                old_flag = getattr(self, flag, False) if flag else None
                if flag:
                    setattr(self, flag, True)
                self._gce_dispatch = mode
                swaps = [(self.s_gcode_out, gcode), (self.s_stl_out, stl),
                         (self.s_make_stl, True)]
                saved = [(v, v.get()) for v, _ in swaps]
                for var, val in swaps:
                    var.set(val)
                try:
                    with self._gce_page_scope():
                        validate = {"scaffold": "_validate_scaffold",
                                    "mandrel": "_validate_mandrel"}[mode]
                        err = getattr(self, validate)()
                        if err:
                            raise ValueError(err)
                        args = list(self._build_args_scaffold())
                        # The document file must not be overwritten by Generate.
                        if "--gcode-doc" in args:
                            import shutil
                            n = args.index("--gcode-doc") + 1
                            target = stem + ".doc.json"
                            shutil.copyfile(args[n], target)
                            args[n] = target
                finally:
                    if flag:
                        setattr(self, flag, old_flag)
            else:
                text = self._gce_parse_text()
                with open(gcode, "w", encoding="utf-8") as fh:
                    fh.write(text)
                swaps = [(self.in_var, gcode), (self.out_var, stl)]
                if doc["source"] != self._GCE_FILE:
                    swaps += [(self.diameter, str(self._gce_designer_diameter())),
                              (self.faces, self.s_faces.get()),
                              (self.sag_mode, self.s_sag_mode.get())]
                saved = [(v, v.get()) for v, _ in swaps]
                for var, val in swaps:
                    var.set(val)
                err = self._validate_gcode()
                if err:
                    raise ValueError(err)
                args = list(self._build_args_gcode())
            if "--no-mesh" not in args:
                args.append("--no-mesh")
            options = self._gce_pv_options()
            # this one preview is the export's, so it takes the export's facets
            # and solver
            faces, solver = self._gce_export_solver_vars()
            options["tube_sides"] = max(3, int(faces.get()))
            if self._sag_solver_value(solver) == "off":
                options["sag"] = None
            return args, gcode, stem + ".depo.npz", options
        finally:
            for var, val in locals().get("saved", ()):
                var.set(val)
            self._gce_dispatch = old_dispatch
            self._scaffolds_json_tmp = old_json
            self._gce_gen_doc = old_doc
            self._pv_suppress = old_suppress

    def _gce_export_preview_start(self):
        """Run the export solver without replacing the user's Generate result."""
        if getattr(self, "proc", None) is not None:
            self._gce_pv_panel.set_status("Wait for Generate to finish first.")
            return
        if getattr(self, "_gce_regen", None) is not None:
            self._gce_pv_panel.set_status("Wait for the design to finish updating.")
            return
        if getattr(self, "_gce_pv_full", None) is None:
            self._gce_pv_panel.set_status("Wait for the live preview first.")
            return
        self._gce_export_preview_cancel()
        shown = self.gce.get_text()
        if not shown.strip():
            self._gce_pv_panel.set_status("Nothing to draw yet. Press New scaffold, or open a g-code file, and it draws here.")
            return
        try:
            args, gcode, depo, options = self._gce_export_preview_args()
            err = getattr(self, "_validate_calibration", lambda: None)()
            if err:
                raise ValueError(err)
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, cwd=HERE,
                creationflags=0x08000000 if IS_WINDOWS else 0,
                **WORKER_PIPE_UTF8)
        except Exception as exc:  # noqa: BLE001 -- show a bad field or launch failure
            self._gce_pv_panel.set_status(f"Cannot preview at export quality: {exc}",
                                          error=True)
            return
        self._gce_eq_proc = proc
        self._gce_pv_gen += 1       # discard any live job already in flight
        if self._gce_pv_after is not None:
            self.root.after_cancel(self._gce_pv_after)
            self._gce_pv_after = None
        if self._gce_pv_poll is not None:
            self.root.after_cancel(self._gce_pv_poll)
            self._gce_pv_poll = None
        self._gce_pv_active = self._gce_pv_bar = False
        self._gce_eq_reporting = False
        self._gce_pv_panel.set_status("Building export-quality preview…")
        events = queue.Queue()
        previous = getattr(self, "_gce_pv_full", None)
        basis = dict(previous[1]) if previous else {}

        def read_worker():
            tail = []
            for line in proc.stdout:
                if line.startswith("[PROGRESS]"):
                    events.put(("progress", line[len("[PROGRESS]"):].strip()))
                else:
                    tail.append(line.strip())
                    tail = tail[-5:]
            code = proc.wait()
            result = None
            if not code:
                try:
                    result = self._gce_depo_picture(depo, gcode, shown,
                                                    options, basis)
                except Exception as exc:  # noqa: BLE001 -- cache missing or invalid
                    code, tail = 1, [str(exc)]
            events.put(("done", (code, " ".join(tail), result)))

        threading.Thread(target=read_worker, daemon=True).start()
        if getattr(self, "_sub_draft", None) is None:
            self._gce_host_close()
        self.root.after(100, lambda: self._gce_export_preview_poll(
            proc, events, shown, gcode, depo, options))

    def _gce_depo_picture(self, depo, gcode, shown, options, basis):
        """(paths, meta) for the picture of an export's `.depo` cache: the
        solved centrelines with their times and radii, the canonical text's
        line clock, and the `basis` meta (the live picture's) it inherits the
        collector, overlay and framing from. Run off the Tk thread. Raises
        on a missing or invalid cache. The drawing is thinned past the
        display budget exactly as a live picture is."""
        with np.load(depo, allow_pickle=False) as data:
            cuts = np.asarray(data["splits"], dtype=np.int64)[:-1]
            paths = np.split(np.asarray(data["xyz"]), cuts)
            ptimes = np.split(np.asarray(data["t"]), cuts)
            diameter = float(data["meta"][0])
            if "diam_t" in data and "diam_d" in data:
                radii = [np.interp(t, data["diam_t"], data["diam_d"]) / 2
                         for t in ptimes]
            elif "pdia" in data and len(data["pdia"]) == len(paths):
                radii = [np.full(len(p), d / 2)
                         for p, d in zip(paths, data["pdia"])]
            else:
                radii = None
        if not paths:
            raise ValueError("no deposited fibers were produced")
        points = np.concatenate(paths)
        with open(gcode, encoding="utf-8") as fh:
            canonical = fh.read()
        command_times = None
        if options.get("subsection_layers"):
            try:
                import gcode_to_fibers as g2f
                frame = self._gce_subsection_clock_frame(options, basis)
                _commanded, candidate = g2f.parse_gcode(
                    gcode, mode="auto", with_times=True,
                    frame=frame)
                if len(candidate) == len(paths):
                    command_times = candidate
            except (OSError, ValueError):
                pass
        import gui_preview
        import mew_frame
        # the radii ride with the times, so one mask thins all three alike
        rider = (ptimes if radii is None else
                 [np.column_stack([t, r]) for t, r in zip(ptimes, radii)])
        paths, rider, thin_bound = gui_preview.thin_for_display(
            paths, rider, max_bound=max(gui_preview._DISPLAY_STEP_MM,
                                        2.0 * diameter))
        if radii is None:
            ptimes = rider
        else:
            ptimes = [np.ascontiguousarray(a[:, 0]) for a in rider]
            radii = [np.ascontiguousarray(a[:, 1]) for a in rider]
        basis.pop("tube_diameter", None)
        basis.update(ptimes=ptimes, n_paths=len(paths), point_radii=radii,
                     command_ptimes=command_times,
                     fiber_diameter=diameter,
                     bbox=(tuple(points.min(axis=0)),
                           tuple(points.max(axis=0))),
                     # on the rod, as the live picture's clock is: a tube's
                     # A words are degrees, and a flat clock read them as
                     # millimetres (a 20-layer graft's play bar said 1:06:00
                     # beside Simulation's 1:17:39)
                     line_clock=self._gce_hl_clock_of(
                         canonical, gcode, shown,
                         frame=basis.get("frame") or (
                             mew_frame.CylinderFrame(basis["radius"])
                             if basis.get("radius") else None),
                         flavor=options.get("preview_flavor", "klipper")),
                     tube_sides=options.get("tube_sides"),
                     export_quality=True,
                     decimated=thin_bound is not None, thin_bound=thin_bound,
                     n_pts=int(sum(len(p) for p in paths)))
        # The live overlay is already in plate coordinates, at the zero the
        # live picture was drawn with (its `preview_shift`). The exported
        # centerlines and clock need today's shift, and so does the overlay
        # when zero moved since: else the yellow path stays where the last
        # live picture had it while the fiber moves.
        overlay = basis.pop("gcode_paths", None)
        was = np.asarray(_shift_key(basis.get("preview_shift"))
                         or (0.0, 0.0, 0.0))
        if basis.get("frame_bbox"):
            # the live box is shifted already: back to the file's numbers,
            # so the one shift below puts it where the solve is drawn
            basis["frame_bbox"] = tuple(
                tuple(float(v) for v in np.asarray(p) - was)
                for p in basis["frame_bbox"])
        result = self._gce_preview_shift(
            paths, basis, options.get("preview_shift"))
        if overlay is not None:
            delta = np.asarray(result[1]["preview_shift"]) - was
            if np.any(np.abs(delta) > 1e-9):
                overlay = [np.asarray(p) + delta for p in overlay]
            result[1]["gcode_paths"] = overlay
        return result

    def _gce_result_ready(self):
        """A Generate succeeded (`gui_run`). Remember which text it solved and
        where its `.depo` is, then draw: a live picture held while it ran is
        built now, and a picture drawn without sag because the tube was too
        large for the live drape is replaced by the run's own solved result
        (`_gce_result_try`) -- no second solve in this process."""
        depo = getattr(self, "last_depo", None)
        gcode = getattr(self, "last_gcode", None)
        self._gce_result_for = (
            (self.gce.get_text(), depo, gcode)
            if depo and gcode and os.path.isfile(depo) else None)
        if getattr(self, "_gce_pv_run_hold", False):
            self._gce_pv_run_hold = False
            self._gce_pv_schedule(now=True)
            return
        self._gce_result_try()

    def _gce_result_try(self):
        """Draw the last Generate's `.depo` in place of the live picture, when
        that picture is the commanded tube drawn without sag and the text on
        screen is the text the run solved."""
        res = getattr(self, "_gce_result_for", None)
        full = getattr(self, "_gce_pv_full", None)
        panel = getattr(self, "_gce_pv_panel", None)
        if res is None or full is None or panel is None:
            return
        shown, depo, gcode = res
        meta = full[1] or {}
        if (not meta.get("sag_refused") or meta.get("export_quality")
                or shown != self.gce.get_text() or not os.path.isfile(depo)
                or getattr(self, "_gce_eq_proc", None) is not None
                or getattr(self, "_gce_result_busy", False)):
            return
        try:
            options = self._gce_pv_options()
        except Exception:  # noqa: BLE001 -- a half-typed field
            options = {}
        basis = dict(meta)
        basis.pop("sag_refused", None)
        self._gce_result_busy = True
        panel.set_status("Drawing the generated result…")
        box = {}

        def load():
            try:
                box["ok"] = self._gce_depo_picture(depo, gcode, shown,
                                                   options, basis)
            except Exception as exc:  # noqa: BLE001 -- cache missing or invalid
                box["err"] = str(exc)

        th = threading.Thread(target=load, daemon=True)
        th.start()

        def poll():
            if th.is_alive():
                self.root.after(100, poll)
                return
            self._gce_result_busy = False
            if shown != self.gce.get_text():
                return              # edited meanwhile: the live picture rules
            if "err" in box:
                panel.set_status("Could not draw the generated result: "
                                 + box["err"], error=True)
                return
            self._gce_eq_previous = getattr(self, "_gce_pv_full", None)
            self._gce_pv_apply(box["ok"][0], box["ok"][1], None)

        self.root.after(100, poll)

    def _gce_export_preview_poll(self, proc, events, shown, gcode, depo, options):
        if proc is not getattr(self, "_gce_eq_proc", None):
            return
        done = None
        try:
            while True:
                kind, data = events.get_nowait()
                if kind == "progress":
                    if not self._gce_eq_reporting:
                        self._progress_idle()
                        self._gce_eq_reporting = True
                    self._on_progress(data)
                else:
                    done = data
        except queue.Empty:
            pass
        if done is None:
            self.root.after(100, lambda: self._gce_export_preview_poll(
                proc, events, shown, gcode, depo, options))
            return
        self._gce_eq_proc = None
        self._progress_idle()
        code, log, result = done
        if code or shown != self.gce.get_text():
            self._gce_pv_panel.set_status(
                "Export-quality preview failed: " + log if code else
                "The design changed; showing the live preview.", error=bool(code))
            return
        self._gce_eq_previous = getattr(self, "_gce_pv_full", None)
        self._gce_pv_apply(result[0], result[1], None)
        self.status.set("Export-quality preview ready")

    def _gce_pv_on_map(self, _e=None):
        panel = self._gce_pv_panel
        panel.retheme()
        state = self._gce_pv_state
        if state is not None and state.get("paths") is not None:
            panel.apply_state(state, self._gce_pv_subtitle())
        self._gce_pv_schedule(now=True)

    def _gce_pv_key_now(self):
        """(text, zero shift) the picture would be drawn from now: the pair
        `_gce_pv_sent` records at each kick. The picture depends on both,
        so a zero change with the text unchanged (an opened file, a
        detached text) is a picture to redraw."""
        try:
            options = self._gce_pv_options()
        except Exception:  # noqa: BLE001 -- a half-typed field
            options = {}
        return (self.gce.get_text(),
                _shift_key(options.get("preview_shift")))

    #: an input of the picture besides its text and zero moved since it was
    #: drawn (a Physics value, a Calibrate Apply), or a redraw was asked for
    #: while the page was hidden. The text can be unchanged (a fusion φ), so
    #: the key alone kept the old picture and its "not calibrated" line.
    _gce_pv_missed = False

    def _gce_pv_stale(self):
        """The picture is not what the page would draw now."""
        return (self._gce_pv_missed
                or self._gce_pv_key_now() != getattr(self, "_gce_pv_sent", None))

    def _gce_pv_refresh_if_stale(self):
        """Redraw when the picture was drawn from another text or another
        zero, or a redraw was missed while the page was hidden, unless a
        regeneration is under way (its text redraws it)."""
        if getattr(self, "_gce_pv_panel", None) is None \
                or getattr(self, "_gce_regen", None) is not None \
                or not self._gce_page_shown():
            return
        if self._gce_pv_stale():
            self._gce_pv_schedule()

    def _gce_pv_waiting(self):
        """An edit form is open: the picture waits for Done. A rebuild on
        every change of an open edit (a Repeat every line, say) could eat
        gigabytes before the edit was even finished."""
        return getattr(self, "_gce_fs", None) is not None

    def _gce_pv_hold_status(self):
        panel = self._gce_pv_panel
        if not getattr(self, "_gce_pv_holding", False):
            self._gce_pv_holding = True
            self._gce_pv_status0 = panel._status_var.get()
        panel.set_status("The picture updates when you press Done.")

    def _gce_pv_release(self, rebuild):
        """The form closed: rebuild the picture once, or (nothing to
        draw anew) put back the status it had."""
        if getattr(self, "_gce_pv_holding", False):
            self._gce_pv_holding = False
            if not rebuild and self._gce_pv_panel is not None:
                self._gce_pv_panel.set_status(self._gce_pv_status0)
        if not rebuild and self._gce_pv_after is not None:
            # the text put back on Cancel queued a rebuild of what is shown
            try:
                self.root.after_cancel(self._gce_pv_after)
            except (ValueError, tk.TclError):
                pass
            self._gce_pv_after = None
        if rebuild:
            self._gce_pv_schedule(now=True)

    def _gce_pv_schedule(self, now=False, force=False):
        """`force`: Update preview, the one rebuild an open form allows."""
        if getattr(self, "_gce_pv_panel", None) is None:
            return
        if not getattr(self, "_pv_suppress", False):
            self._gce_export_preview_cancel()
        if self._gce_pv_after is not None:
            try:
                self.root.after_cancel(self._gce_pv_after)
            except (ValueError, tk.TclError):
                pass
            self._gce_pv_after = None
        if self._gce_pv_waiting() and not force:
            self._gce_pv_hold_status()
            return
        if self.gce.line_count() > _PREVIEW_AUTO_MAX_LINES and not now:
            self._gce_pv_btn.pack(side="left", padx=(8, 0))
            self.root.after_idle(self._gce_disp_fit)
            self._gce_pv_panel.set_status(
                "This text is large, so the picture updates when you press "
                "Update preview.")
            self._gce_pv_after = self.root.after(
                _RECORD_IDLE_MS, lambda: self._gce_pv_kick(draw=False))
            return
        if self._pv_preview_disabled() and not now:
            self._gce_pv_after = self.root.after(
                _RECORD_IDLE_MS, lambda: self._gce_pv_kick(draw=False))
            return
        self._gce_pv_after = self.root.after(
            1 if now else _PREVIEW_IDLE_MS,
            lambda: self._gce_pv_kick(force=force))

    def _gce_pv_kick_now(self):
        self._gce_pv_schedule(now=True, force=True)

    def _gce_pv_kick(self, draw=True, force=False):
        """Hand the worker the next job. `draw` False (no picture this time:
        switched off, or a text too large to draw unasked) still sends the
        text's source, so what the worker works out besides the picture
        (`_gce_pv_adopt`) never runs on this thread."""
        self._gce_pv_after = None
        panel = self._gce_pv_panel
        if panel is None:
            return
        if not self._gce_page_shown():
            # drawn when the page shows again (`_gce_pv_refresh_if_stale`)
            self._gce_pv_missed = self._gce_pv_missed or draw
            return
        if draw and not force and getattr(self, "_run_in_flight",
                                          lambda: False)():
            # The live drape runs in this process, the export worker in its
            # own, and one 3.5 GB budget holds both: a tube that drapes in
            # ~1 GB beside a worker solving the same tube is the two at once.
            # Held, and drawn when the run ends (`_gce_result_ready`).
            self._gce_pv_run_hold = True
            panel.set_status("Generate is running: the picture is drawn "
                             "when it finishes.")
            return
        if self._gce_pv_waiting() and not force:
            # queued before the form opened
            self._gce_pv_hold_status()
            return
        if self._pv_preview_disabled():
            panel.set_status(PREVIEW_DISABLED_STATUS)
            draw = False
        shown = self.gce.get_text()
        if not shown.strip():
            panel.set_status("Nothing to draw yet. Press New scaffold, or open a g-code file, and it draws here.")
            return
        if not draw and self.gce.line_count() > _RECORD_ONLY_MAX_LINES:
            return
        # the text it draws is worked out on the worker, never here: on the
        # window's thread it froze typing for a third of a second per pause
        source = self._gce_pv_source()
        try:
            options = self._gce_pv_options()
        except Exception:  # noqa: BLE001 -- a half-typed field: no physics
            options = {}
        doc = self._gce_doc
        dia = doc.get("diameter") or self._gce_designer_diameter()
        arc = self._gce_pv_arc(options)
        tube = None
        self._gce_pv_gap = False            # a plate, or a tube that closes
        if draw and self._gce_pv_is_tube():
            try:
                snap = getattr(self, "_gce_tube_snapshot",
                               self._mandrel_snapshot)
                tube = snap()
            except Exception as exc:  # noqa: BLE001 -- a half-typed field
                import gui_mandrel
                gap = isinstance(exc, gui_mandrel._LengthGap)
                self._gce_pv_gap = gap      # nothing in flight lands over it
                panel.set_status(gui_mandrel._MANDREL_GAP_STATUS_PAGE if gap else
                                 f"waiting for valid parameters: {exc}",
                                 error=gap)
                return
            # the designer's rule (`gui_mandrel`): a tube whose last build took
            # a second or more shows its commanded shape first, then the sag
            import gui_preview
            last = self._gce_pv_state.get("meta") or {}
            options["toolpath_first"] = bool(
                last.get("provisional")
                or float(last.get("elapsed", math.inf))
                >= gui_preview.PREVIEW_TOOLPATH_FIRST_S)
        self._gce_pv_gen += 1
        job = (self._gce_pv_gen, source, options, dia,
               doc.get("mode") or "g1", arc, draw, shown, tube)
        if draw:
            panel.set_status("updating…")
            # what the picture shows next: the text, at this zero
            self._gce_pv_sent = (shown,
                                 _shift_key(options.get("preview_shift")))
            self._gce_pv_missed = False
            self._gce_pv_active = True
            self._gce_pv_began = (self._gce_pv_gen, time.monotonic())
        with self._gce_pv_cv:
            self._gce_pv_mail = job
            self._gce_pv_cv.notify()
        if self._gce_pv_worker is None:
            self._gce_pv_worker = threading.Thread(
                target=self._gce_pv_loop, daemon=True)
            self._gce_pv_worker.start()
        if self._gce_pv_poll is None:
            self._gce_pv_poll = self.root.after(PREVIEW_POLL_MS,
                                                self._gce_pv_tick)

    def _gce_pv_source(self):
        """A function the preview WORKER runs to get the text it draws: it
        returns the text, or ``(text, adopt)`` where `adopt` is handed back
        to `_gce_pv_adopt` on this thread. It must only read what is
        captured here, never the live page."""
        text = self._gce_preview_text()
        return lambda: text

    @staticmethod
    def _gce_subsection_clock_frame(options, meta):
        frame = meta.get("frame")
        tube = options.get("subsection_mandrel_frame")
        if frame is None and tube:
            import mew_frame
            frame = mew_frame.CylinderFrame(tube[0], surface_z=tube[1])
        return frame

    def _gce_pv_options(self):
        """The worker's options for the picture. `"after"`, when given, runs
        on the worker with the build's meta (`gui_gcode_calibrate`)."""
        options = self._pv_snapshot_options(0)
        # the picture's own facets (Settings > Quality > Live preview), not the
        # export's: "Preview at export quality" swaps the export's in
        # (`_gce_export_preview_args`)
        try:
            options["tube_sides"] = max(3, int(self.pv_tube_sides.get()))
        except (ValueError, AttributeError):
            options["tube_sides"] = 12
        options["preview_flavor"] = self.gce_flavor_name()
        options["subsection_layers"] = bool(getattr(self, "_sub_draft", None)) or any(
            s.get("enabled", True) and s.get("z_mode") == "layers"
            for s in getattr(self, "_subsections", lambda: ())())
        if options["subsection_layers"] and self._gce_pv_is_tube():
            try:
                tube = self._subsection_tube_frame({})
                options["subsection_mandrel_frame"] = (
                    tube["mandrel_radius"], tube["mandrel_surface_z"])
            except (ValueError, AttributeError, tk.TclError):
                pass  # The ordinary preview reports incomplete mandrel inputs.
        dx, dy, dz = self._origin_offset()
        if options.get("plate"):
            x, y, width, height, round_plate = options["plate"]
            options["plate"] = (x + dx, y + dy, width, height, round_plate)
        options["preview_shift"] = (-dx, -dy, -(self._plate_top_z() + dz))
        if self._gce_pv_is_tube():
            # a tube's text is re-based along the rod only (`_mandrel_origin_
            # offset`); its picture is drawn on the rod, so it is moved back
            # by where X 0 sits there
            options["preview_shift"] = (self._gce_tube_origin_u(), 0.0, 0.0)
        return options

    def _gce_tube_origin_u(self):
        """Where on the rod the page's tube text has X 0: the axial zero,
        read as the page's text was made (scaffold 1's scope, the rod that
        covers every tube)."""
        try:
            scope = getattr(self, "_gce_tube_scope", None)
            if scope is None:
                return -float(self._mandrel_origin_offset()[0]) + 0.0
            with scope(0):
                return -float(self._mandrel_origin_offset()[0]) + 0.0
        except Exception:  # noqa: BLE001 -- a half-typed tube: no re-base
            return 0.0

    def _gce_export_solver_vars(self):
        """(facets var, sag-solver var) the EXPORT of this text runs on."""
        is_file = self._gce_doc.get("source") == self._GCE_FILE
        return ((self.faces, self.sag_mode) if is_file
                else (self.s_faces, self.s_sag_mode))

    def _gce_pv_arc(self, options):
        """Segments per half-turn of a G2/G3 in the picture."""
        try:
            arc = int(self.gce_scaf_vars[0]["arc_segments"].get())
        except (AttributeError, IndexError, KeyError, ValueError,
                tk.TclError):
            arc = 8
        return arc * 2 if options.get("g2g3") else arc

    def _gce_pv_color(self):
        return self._pv_parse_color(self.gce_scaf_vars[0]["color"])

    def _gce_pv_is_tube(self):
        """The text is the designer's 4-axis tube: X along the rod, A around
        it. Drawn flat it is one straight line, so the picture wraps it on
        the designer's rod instead (`build_mandrel_preview`)."""
        return (self._gce_designer_mode() == "mandrel"
                and self._gce_doc["source"] != self._GCE_FILE
                and hasattr(self, "_mandrel_snapshot"))

    def _gce_pv_adopt(self, adopt):
        """What the worker worked out besides the picture (the edits half
        takes its recording of the typing)."""

    def _gce_preview_text(self):
        """The text the picture is built from. An unedited linked text is
        drawn from the designer's RAW output: "Write travel moves as G1"
        respells every travel as a printing move, and the parser would then
        draw the travels as fiber, which the designer's own preview does not
        (the Render-travel box decides that, for both)."""
        doc = self._gce_doc
        text = self.gce.get_text()
        if doc["source"] == self._GCE_LINKED and doc["raw"] \
                and text == doc["base"]:
            return doc["raw"]
        return text

    def _gce_pv_loop(self):
        from gui_preview import build_preview_paths_from_text
        from preview_sag import BuildSuperseded
        path = self._gce_scratch("preview.gcode")
        while True:
            with self._gce_pv_cv:
                while self._gce_pv_mail is None:
                    self._gce_pv_cv.wait()
                job, self._gce_pv_mail = self._gce_pv_mail, None
            gen, source, options, dia, mode, arc, draw, shown, tube = job
            after = options.pop("after", None)
            first = options.pop("toolpath_first", False)
            adopt = None
            paths = meta = err = None

            def stage(words, gen=gen):
                # the build's stage, for the window's progress line; a plain
                # attribute, read there, written only here
                self._gce_pv_stage = (gen, words)
            stage("reading the g-code")
            try:
                text = source()
                if isinstance(text, tuple):
                    text, adopt = text
                if draw and tube is not None:
                    from gui_mandrel_preview import build_mandrel_preview
                    import mew_frame
                    if "build" in tube:     # a tube with its own builder
                        paths, meta = tube["build"](text, path)
                    else:
                        # a newer job in the mail: this picture would be
                        # thrown away on landing, so the build may stop
                        def stop():
                            return self._gce_pv_mail is not None

                        def sink(tp, tmeta, gen=gen):
                            # the commanded tube, up while relay and drape run
                            if not stop():
                                self._gce_pv_results.put(
                                    (gen, tp, tmeta, None, None, True))
                        paths, meta = build_mandrel_preview(
                            **dict(tube, gcode_path=path, gcode_text=text,
                                   text_origin_u=(options.get("preview_shift")
                                                  or (0.0,))[0],
                                   on_toolpath=sink if first else None,
                                   should_stop=stop))
                    # the player's clock on the same rod and the SAME text
                    # as the picture: on a tube Z is the ply's radius, so a
                    # clock of the shown text (layer Z dropped) runs at the
                    # wrong radius. Its lines are then renumbered to the
                    # shown text's.
                    stage("matching the picture to the lines")
                    clock = self._gce_hl_clock_of(
                        text, path, shown,
                        frame=meta.get("frame")
                        or mew_frame.CylinderFrame(meta["radius"]),
                        flavor=options.get("preview_flavor", "klipper"))
                    meta["line_clock"] = clock
                elif draw:
                    stage("reading the toolpath")
                    paths, meta = build_preview_paths_from_text(
                        text, path, dict(options, on_stage=stage),
                        fiber_diameter=dia, mode=mode, arc_segments=arc)
                    stage("matching the picture to the lines")
                    meta["line_clock"] = self._gce_hl_clock_of(
                        text, path, shown,
                        flavor=options.get("preview_flavor", "klipper"))
                    if after is not None:
                        after(meta, path, mode, arc)
                if draw and meta is not None:
                    if options.get("subsection_layers"):
                        try:
                            import gcode_to_fibers as g2f
                            frame = self._gce_subsection_clock_frame(options, meta)
                            _commanded, candidate = g2f.parse_gcode(
                                path, mode=mode, with_times=True,
                                arc_segments=arc, frame=frame)
                            if len(candidate) == len(paths):
                                meta["command_ptimes"] = candidate
                        except (OSError, ValueError):
                            pass
                    meta["tube_sides"] = options.get("tube_sides")
                    if tube is None:
                        paths, meta = self._gce_preview_shift(
                            paths, meta, options.get("preview_shift"))
                    elif "build" not in tube:
                        # the builder put the tube back on the rod
                        # (`text_origin_u`); the clock read the text, so it
                        # moves the same way
                        meta = self._gce_shift_clock(
                            meta, options.get("preview_shift"))
            except BuildSuperseded:
                continue        # the newer job's own picture replaces it
            except Exception as exc:  # noqa: BLE001
                paths, meta, err = None, None, str(exc)
            self._gce_pv_results.put((gen, paths, meta, err, adopt, draw))

    def _gce_pv_tick(self):
        self._gce_pv_poll = None
        landed = getattr(self, "_gce_pv_held", None)
        self._gce_pv_held = None
        try:
            while True:
                landed = self._gce_pv_results.get_nowait()
        except queue.Empty:
            pass
        typing = (time.monotonic() - getattr(self, "_gce_last_key", 0.0)
                  < _PREVIEW_HOLD_S)
        if landed is not None and landed[0] == self._gce_pv_gen and typing:
            # swapping the picture in takes the window a tenth of a second
            # or more: never in the middle of a word
            self._gce_pv_held, landed = landed, None
        provisional = False
        if landed is not None:
            gen, paths, meta, err, adopt, draw = landed
            # the toolpath-first tube: drawn, but its build is still running
            provisional = bool((meta or {}).get("provisional"))
            if gen == self._gce_pv_gen:
                if not provisional:
                    self._gce_pv_active = False
                    if getattr(self, "_gce_pv_bar", False):
                        self._gce_pv_bar = False
                        if getattr(self, "proc", None) is None:
                            self._progress_idle()
                if adopt is not None:
                    self._gce_pv_adopt(adopt)
                if draw:
                    self._gce_pv_apply(paths, meta, err)
        if landed is None or landed[0] != self._gce_pv_gen or provisional:
            if landed is None or landed[0] != self._gce_pv_gen:
                self._gce_pv_progress()
            self._gce_pv_poll = self.root.after(PREVIEW_POLL_MS,
                                                self._gce_pv_tick)

    #: a build still running after this long says what it is doing and for
    #: how long, so a slow picture reads as progress, never as a hang
    _GCE_PV_PROGRESS_S = 1.5

    def _gce_pv_progress(self):
        """The status line while the current picture builds: its stage and
        the seconds so far, once it has taken long enough to wonder about.
        Set only when the words change (once a second at most)."""
        began = getattr(self, "_gce_pv_began", None)
        if not getattr(self, "_gce_pv_active", False) or began is None                 or began[0] != self._gce_pv_gen:
            return
        secs = time.monotonic() - began[1]
        if secs < self._GCE_PV_PROGRESS_S:
            return
        st = getattr(self, "_gce_pv_stage", None)
        words = st[1] if st is not None and st[0] == self._gce_pv_gen             else "waiting for the last picture"
        meta = self._gce_pv_state.get("meta") or {}
        head = "toolpath only · " if meta.get("provisional") else ""
        line = "%supdating… %s · %d s" % (head, words, int(secs))
        # compared with the last line written, never read back from Tk: a
        # Tk call here waits for the busy worker to hand over the GIL
        if getattr(self, "_gce_pv_progress_line", None) != (began, line):
            self._gce_pv_progress_line = (began, line)
            self._gce_pv_panel.set_status(line)

    def _gce_pv_apply(self, paths, meta, err):
        panel = self._gce_pv_panel
        state = self._gce_pv_state
        if getattr(self, "_gce_pv_gap", False):
            return      # a build from before the tube stopped closing
        if err:
            # the last good picture stays (spec 10.4)
            msg = ("nothing in this text deposits fiber"
                   if "no deposition" in err else err)
            panel.set_status(f"Not redrawn: {msg}.", error=True)
            return
        pts = np.concatenate(paths, axis=0)
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        state.update(paths=paths, meta=meta, lines_pd=None, tubes_pd=None,
                     gcode_pd=None, display_mode=None,
                     diag=float(np.linalg.norm(hi - lo)),
                     color=self._gce_pv_color())
        if getattr(self, "_gce_pv_reframe", False):
            # the first picture of a loaded preset frames it
            # (`gui_gcode_work._preset_load`)
            self._gce_pv_reframe = False
            state.update(framed_diag=None, camera=None)
        tube = "radius" in meta
        bits = [f"{meta.get('n_paths', len(paths))} fibers"]
        if tube:
            bits.append(f"mandrel Ø{2 * meta['radius']:.3f} mm")
        if meta.get("physics"):
            shipped = getattr(self, "_physics_is_shipped", None)
            bits.append("lab physics (not calibrated)"
                        if callable(shipped) and shipped() else "physics on")
        if meta.get("export_quality"):
            bits.append("export-quality preview")
        elif meta.get("sag_exact"):
            bits.append("exact sag")
        elif meta.get("sag") or meta.get("preview_sagged"):
            bits.append("approximate sag")
        elif meta.get("sag_refused"):
            # first, not last: at the end of a long line it was missed, and
            # it is the one bit that says the picture is not the landing
            bits.insert(0, "NOT the real landing, drawn without sag: "
                        f"{meta['sag_refused']}")
        import gui_preview
        thin = gui_preview.display_thin_note(meta)
        if thin:
            bits.append(thin)
        # the plate check the Scaffold designer's summary makes
        # (`gui_preview._design_frame`): this page is where Generate, Save
        # and Send are pressed, so a print off the plate is said here, first
        off = self._gce_off_plate(meta)
        if off:
            bits.insert(0, "part of the print is off the plate (Setup: "
                        "Collector size, Position, G-code zero)")
        state["status"] = " · ".join(bits)
        if meta.get("provisional"):
            state["status"] = (f"toolpath only · {meta.get('layers')} layers"
                               " · updating…")
        state["status_error"] = off
        self._gce_pv_full = (paths, meta)
        self._gce_pv_show()
        if self._gce_pv_holder is not None:
            self._gce_pv_holder._size_plate(panel, state)
            self._gce_pv_holder.set_collector_visible(self._gce_rod_wanted())
            self._gce_pv_holder.set_plate_visible(self._pv_plate_wanted())
        if meta.get("sag_refused") and not meta.get("export_quality"):
            self.root.after_idle(self._gce_result_try)
        if self._gce_pv_btn.winfo_manager():
            # the strip lost its Update preview button: refit it. Only then,
            # since the fit flushes every idle task in the window first
            self._gce_pv_btn.pack_forget()
            self.root.after_idle(self._gce_disp_fit)
        self._gce_hl_take(paths, meta)

    def _gce_off_plate(self, meta):
        """Whether the picture's print leaves the plate (`meta["off_plate"]`,
        the designer summary's check). An opened file carries its end-of-
        print exit as a printing move, and that run off the plate's edge is
        meant: the file is judged on its commanded path without the final
        straight run."""
        if not meta.get("off_plate") or meta.get("provisional"):
            return False
        if self._gce_doc.get("source") != self._GCE_FILE:
            return True
        import gui_preview
        paths = [np.asarray(p, dtype=np.float64)
                 for p in meta.get("gcode_paths") or () if len(p)]
        plate = self._pv_snapshot_options(0).get("plate")
        if not paths or not plate:
            return True
        last = paths[-1]
        k = len(last) - 1
        if k >= 1:
            d = last[k, :2] - last[k - 1, :2]
            while k >= 1:
                e = last[k, :2] - last[k - 1, :2]
                if abs(d[0] * e[1] - d[1] * e[0]) > 1e-6 * (
                        np.hypot(*d) * np.hypot(*e) + 1e-12) \
                        or float(np.dot(d, e)) < 0:
                    break
                k -= 1
        paths[-1] = last[:k + 1]
        return gui_preview.off_plate(np.concatenate(paths, axis=0), plate)

    def _gce_pv_show(self):
        """Put the picture up with the tree's hidden rows cut out (their
        print-time windows, from the picture's own line clock). The pick
        reads the same cut paths, so a hidden fiber is never picked. The
        framing stays the whole print's."""
        panel = getattr(self, "_gce_pv_panel", None)
        full = getattr(self, "_gce_pv_full", None)
        if panel is None or full is None:
            return
        from gui_gcode_cad import cut_hidden
        import preview_pick
        paths, meta = full
        state = self._gce_pv_state
        ranges = getattr(self, "_gce_vis_ranges", None) or []
        clock = meta.get("line_clock")
        ptimes = meta.get("ptimes")
        shown, meta2 = paths, meta
        if ranges and clock is not None and ptimes is not None:
            wins = [w for w in (preview_pick.line_window(clock, a, b)
                                for a, b in ranges) if w is not None]
            shown, pt = cut_hidden(paths, ptimes, wins)
            meta2 = dict(meta, ptimes=pt)
            radii = meta.get("point_radii")
            if radii is not None and len(radii) == len(paths):
                cut_radii, _ = cut_hidden([r[:, None] for r in radii], ptimes, wins)
                meta2["point_radii"] = [r[:, 0] for r in cut_radii]
            # the G-code path overlay hides with the fiber
            gp, gt = meta.get("gcode_paths"), meta.get("gcode_ptimes")
            if gp and gt is not None and len(gp) == len(gt):
                meta2["gcode_paths"], meta2["gcode_ptimes"] = cut_hidden(
                    gp, gt, wins)
        shown, meta2 = self._gce_pv_clip(shown, meta2)
        state.update(meta=meta2, lines_pd=None, tubes_pd=None, gcode_pd=None,
                     display_mode=None)
        if not shown:
            # everything hidden: an empty picture, the camera kept
            state["paths"] = paths
            panel.clear()
            return
        state["paths"] = shown
        self._gce_pv_aux(state, shown, meta2)
        panel.apply_state(state, self._gce_pv_subtitle())

    def _gce_pv_clip(self, shown, meta):
        """Hook: the picture is about to go up with `shown` paths (after the
        eyes' cut). Return (paths, meta) cut further, the meta's per-fibre
        arrays cut with them (the Subsection tool's view, `gui_gcode_subsection`).
        The framing stays the whole print's. A page with no such view returns
        both unchanged."""
        return shown, meta

    def _gce_pv_aux(self, state, shown, meta):
        """Hook: the picture is about to go up with `shown` paths and `meta`
        (after the eyes' cut). The view tools put the print clock and the
        colour arrays on `state` here (`gui_gcode_view`)."""

    # ------------------------------------------------------------------ #
    # the highlight: the caret's line (or the open edit's lines) in yellow
    # ------------------------------------------------------------------ #
    def _gce_hl_clock_of(self, text, path, shown, frame=None, flavor="klipper"):
        """WORKER: the player's own line clock (`gcode_player.parse_path`,
        the clock `parse_gcode` keeps in step with). Parse the same canonical
        text as the picture, then map its lines to the machine-spelled text
        the page shows. None when it cannot be had; the picture never waits
        on it. `frame` is a
        tube's rotary frame (the clock then carries A, in the rod's frame)."""
        try:
            import gcode_player
            verts, times, _d, _f, seg_line, _df, _r = \
                gcode_player.parse_path(path, frame=frame)
            if text != shown:
                from mew_gcode import read_line
                normal = "\n".join(read_line(line, flavor)
                                   for line in shown.split("\n"))
                if text != normal:
                    to = _line_renumber(text, normal)
                    seg_line = to[np.asarray(seg_line, np.int64)]
        except Exception:  # noqa: BLE001 -- no clock, no highlight
            return None
        return dict(text=shown, verts=verts, times=times, seg_line=seg_line,
                    rotary=_r)

    def _gce_hl_take(self, paths, meta):
        """After a new picture: its points by print time, for the lookup --
        each fiber on its own (`fibers`, ordered by start), and all of them
        merged (`T`, `P`) for the one-point lookup. A lit stretch is cut
        from the fibers, never from the merge: consecutive fibers can
        overlap in time (a physics-deposited tube's next ply starts ~0.3 s
        before the last one ends), and the merge interleaves the two plies'
        points there -- cut from it, the lit ply came out in dashes."""
        pt = meta.get("ptimes")
        hl = dict(clock=meta.get("line_clock"), T=None, P=None, fibers=None)
        if pt is not None and len(pt) == len(paths):
            fibers = sorted(
                ((np.asarray(t, np.float64), p) for p, t in zip(paths, pt)
                 if len(p)), key=lambda f: f[0][0])
            T = np.concatenate([np.asarray(t, np.float64) for t in pt])
            P = np.concatenate(paths, axis=0)
            if len(T) > 1 and np.any(np.diff(T) < 0):
                order = np.argsort(T, kind="stable")
                T, P = T[order], P[order]
            hl.update(T=T, P=P, fibers=fibers)
        self._gce_hl = hl
        self._gce_hl_refresh()

    def _gce_hl_schedule(self):
        if getattr(self, "_gce_hl_after", None) is None:
            self._gce_hl_after = self.root.after(30, self._gce_hl_refresh)

    def _gce_hl_refresh(self):
        """Light the open edit's lines, else the caret's, in the picture.
        Nothing while the text differs from the one the picture was built
        from: its line numbers would point at the wrong fiber."""
        self._gce_hl_after = None
        panel = getattr(self, "_gce_pv_panel", None)
        if panel is None:
            return
        hl = getattr(self, "_gce_hl", None)
        if hl is None or hl["clock"] is None \
                or hl["clock"]["text"] != self.gce.get_text():
            panel.set_highlight()
            return
        lines = self.gce.focus_lines or (
            [] if getattr(self, "_gce_hl_off", False)
            else [self.gce.caret()[0]])
        if not lines:               # clicked off: the caret lights nothing
            panel.set_highlight()
            return
        polys, dots = self._gce_hl_geometry(hl, lines)
        panel.set_highlight(polys, dots)

    def _gce_look_at(self):
        """Look at: the picture frames the selected lines, else the caret's
        line -- the same fiber the highlight lights."""
        panel = getattr(self, "_gce_pv_panel", None)
        hl = getattr(self, "_gce_hl", None)
        if panel is None or hl is None or hl["clock"] is None \
                or hl["clock"]["text"] != self.gce.get_text():
            self.gce_explain_var.set(
                "Look at: the picture is still catching up with the text.")
            return
        a, b = self.gce._selected_lines()
        lines = list(range(a, b + 1)) if b > a else (
            self.gce.focus_lines or [self.gce.caret()[0]])
        polys, dots = self._gce_hl_geometry(hl, lines)
        pts = [np.asarray(p, np.float64).reshape(-1, 3) for p in polys]
        pts += [np.asarray(d, np.float64).reshape(1, 3) for d in dots]
        if not pts or not panel.look_at(np.concatenate(pts)):
            self.gce_explain_var.set(
                "Look at: nothing in the picture comes from that line.")

    @staticmethod
    def _gce_hl_geometry(hl, lines):
        """``(polylines, points)`` for 1-based `lines`: a move's fiber (the
        drawn points laid while it runs, from the point at or before its
        start to the one at or after its end, cut where one fiber ends), or
        its commanded segment when no fiber was laid (a travel); a line that
        does not move (a dwell, a feed or pressure change) is a point -- the
        fiber where the print is when it runs.

        A run of consecutive lines is lit as ONE window of the fiber, from
        its first move's start to its last move's end, so a whole scaffold
        selected (thousands of lines) is a handful of pieces -- and none of
        its lines is left out. (A per-line loop capped at 5000 lines used
        to drop the end of any longer selection: its last passes stayed
        unlit.)"""
        c = hl["clock"]
        sl, t, v = np.asarray(c["seg_line"]), c["times"], c["verts"]
        T, P, fibers = hl["T"], hl["P"], hl["fibers"] or []
        have = T is not None and len(T) > 0
        # the print-time window the view tools show (layer slider, move
        # scrubber), if any: a selection is lit only inside it
        win = hl.get("window")
        polys, dots = [], []
        ln = np.unique(np.asarray(list(lines), np.int64))
        if not len(ln):
            return polys, dots
        J0 = np.searchsorted(sl, ln, side="left")
        J1 = np.searchsorted(sl, ln, side="right")
        moving = J1 > J0

        def fiber(j0, j1):
            out = []
            for Tf, Pf in fibers:
                if Tf[0] > t[j1]:
                    break
                i0 = max(int(np.searchsorted(Tf, t[j0], "right")) - 1, 0)
                i1 = min(int(np.searchsorted(Tf, t[j1], "left")), len(Tf) - 1)
                if win is not None:
                    i0 = max(i0, int(np.searchsorted(Tf, win[0], "right")) - 1,
                             0)
                    i1 = min(i1, int(np.searchsorted(Tf, win[1], "left")),
                             len(Tf) - 1)
                if i1 > i0:
                    out.append(Pf[i0:i1 + 1])
            return out

        # runs of consecutive selected lines; each run's moves light as one
        run = np.r_[0, np.cumsum(np.diff(ln) > 1)]
        mv = np.flatnonzero(moving)
        bare = np.ones(len(ln), bool)       # a move that laid no fiber
        if have and len(mv):
            tt = np.asarray(t, np.float64)
            ta, tb = tt[J0[mv]], tt[J1[mv]]     # both ascending
            laid = np.zeros(len(mv), bool)
            for Tf, _Pf in fibers:
                # only the moves this fiber's time span can reach
                k0 = int(np.searchsorted(tb, Tf[0], "left"))
                k1 = int(np.searchsorted(ta, Tf[-1], "right"))
                if k1 <= k0:
                    continue
                I0 = np.maximum(
                    np.searchsorted(Tf, ta[k0:k1], "right") - 1, 0)
                I1 = np.minimum(np.searchsorted(Tf, tb[k0:k1], "left"),
                                len(Tf) - 1)
                laid[k0:k1] |= I1 > I0
            bare[mv] = ~laid
        if len(mv):
            starts = np.r_[0, np.flatnonzero(np.diff(run[mv])) + 1]
            ends = np.r_[starts[1:], len(mv)]
            for s0, s1 in zip(starts, ends):
                k = mv[s0:s1]
                got = fiber(int(J0[k[0]]), int(J1[k[-1]]))
                polys += got
                # a move that laid no fiber (a travel): its commanded path
                for q in (k if not got else k[bare[k]]):
                    if win is not None and (t[int(J1[q])] < win[0]
                                            or t[int(J0[q])] > win[1]):
                        continue
                    polys.append(v[int(J0[q]):int(J1[q]) + 1])
        for q in np.flatnonzero(~moving):
            j0 = int(J0[q])
            tt = t[j0] if j0 < len(sl) else t[-1]
            if win is not None and not win[0] <= tt <= win[1]:
                continue
            if have:
                i = int(np.searchsorted(T, tt))
                i = min(max(i, 0), len(T) - 1)
                if i > 0 and abs(T[i - 1] - tt) <= abs(T[i] - tt):
                    i -= 1
                dots.append(P[i])
            else:
                dots.append(v[min(j0, len(v) - 1)])
        return polys, dots

    def _gce_pv_subtitle(self):
        if self._gce_doc["source"] == self._GCE_FILE:
            return "The whole file · commanded path, unless the physics " \
                   "and sag switches say otherwise."
        return "The whole print, every scaffold · drawn from this text."

    # the designer's display-toggle fan-out reaches this panel too
    def _pv_refresh_display(self):
        super()._pv_refresh_display()
        panel = getattr(self, "_gce_pv_panel", None)
        if panel is not None:
            panel._update_display_mode(force=True)
            panel._update_gcode_overlay()
            panel._render_soon()

    def _pv_relight(self):
        super()._pv_relight()
        panel = getattr(self, "_gce_pv_panel", None)
        if panel is not None:
            panel.on_studio_light_changed()

    def _pv_on_show_plate(self, *_a):
        super()._pv_on_show_plate(*_a)
        holder = getattr(self, "_gce_pv_holder", None)
        if holder is not None:
            holder.set_plate_visible(self._pv_plate_wanted())

    def _gce_rod_wanted(self):
        """The rod under this page's picture: only around a tube it drew, and
        only with the Collector box ticked."""
        full = getattr(self, "_gce_pv_full", None)
        if full is None or "radius" not in full[1]:
            return False
        var = getattr(self, "pv_show_collector", None)
        return True if var is None else bool(var.get())

    def _pv_on_show_collector(self, *_a):
        super()._pv_on_show_collector(*_a)
        holder = getattr(self, "_gce_pv_holder", None)
        if holder is not None:
            holder.set_collector_visible(self._gce_rod_wanted())

    def _pv_trace_dark(self):
        super()._pv_trace_dark()
        if getattr(self, "_gce_dark_traced", False):
            return
        var = getattr(self, "dark_mode", None)
        panel = getattr(self, "_gce_pv_panel", None)
        if var is None or panel is None:
            return
        var.trace_add("write", lambda *_a: panel.retheme())
        self._gce_dark_traced = True

    # ------------------------------------------------------------------ #
    # Generate and Save -- both from THIS text
    # ------------------------------------------------------------------ #
    def _gce_on_page(self):
        return self._gce_page_shown()

    def _active_mode(self):
        """While the page dispatches the designer's own Generate, answer as
        the designer would -- including the mandrel inversion, which asks
        for "scaffold" once its own run has validated."""
        d = getattr(self, "_gce_dispatch", None)
        if d:
            if d == "mandrel" and getattr(self, "_mandrel_dispatch", False):
                return "scaffold"
            return d
        return super()._active_mode()

    def run(self):
        self._gce_export_preview_cancel()
        if not hasattr(self, "gce") or self._gce_dispatch \
                or self._gce_run_text is not None:
            return super().run()
        if self._active_mode() != "gcode":
            return super().run()
        if self._gce_run_waits():
            return None
        doc = self._gce_doc
        text = self.gce.get_text()
        if not text.strip() and doc["source"] in (None, self._GCE_LINKED) \
                and not self._gce_edited():
            # never filled in: the page has not been opened yet, or its first
            # fill is still running. Its text would be the designer's own
            # Generate, so build that now. The first press from Setup used to
            # open Design and build nothing
            if doc["source"] is None:
                doc["source"] = self._GCE_LINKED
                self._gce_set_link_label()
            _argv, sig, err = self._gce_design_argv()
            self._gce_unfilled_build = None if err else sig
            return self._gce_run_designer()
        if not text.strip():
            # an opened file that is empty is not "nothing opened yet"
            opened = doc["source"] == self._GCE_FILE and doc.get("path")
            self._gce_notice_show(
                ("%s is empty: there is no g-code in it to generate from. "
                 "Open another file (File), or add a scaffold with New "
                 "scaffold." % os.path.basename(doc["path"])) if opened else
                "There is no g-code here yet. Open a file (File), or add "
                "a scaffold with New scaffold.", [])
            return
        if doc["source"] == self._GCE_LINKED and doc["raw"] and (
                not self._gce_edited() or self._gce_doc_carries_edits()):
            return self._gce_run_designer()
        return self._gce_run_text_file(self._gce_parse_text())

    _GCE_RUN_WAIT_MS = 150
    _GCE_RUN_WAIT_MAX_S = 300.0

    def _gce_run_waits(self):
        """Generate builds the design the user last typed, never the one
        before it. A Parameters cell still being typed in is committed, the
        table's held-back rebuild is let go, and while the page's text is
        still being rebuilt from a design change (the debounce, or the
        regenerate worker) Generate waits for it to land and then runs.
        Otherwise a quick Return-then-Generate built the text from before
        the edit -- and, even when it built the new design, the result was
        noted against the old text and read "Changed since Generate".
        True when the run was deferred (it presses itself once the text is
        current); a second press while waiting does nothing more."""
        for name in ("_gcp", "_gcl"):
            st = getattr(self, name, None)
            if st is not None and st.get("shown"):
                try:
                    self._gcp_commit_focused(st)
                except (tk.TclError, KeyError, AttributeError):
                    pass
        flush = getattr(self, "_gcp_flush", None)
        if callable(flush):
            flush()
        if self._gce_doc.get("source") != self._GCE_LINKED:
            return False
        if getattr(self, "_gce_run_wait", None) is not None:
            return True
        if not self._gce_text_behind():
            return False
        self._gce_run_wait = time.time()
        self._gce_notice_show("Bringing the g-code up to date with your last "
                              "change, then Generate…", [], kind="run-wait")
        self.root.after(0, self._gce_run_wait_step)
        return True

    def _gce_text_behind(self):
        """The page's text is still being rebuilt from a design change."""
        return (getattr(self, "_gce_d_after", None) is not None
                or getattr(self, "_gce_regen", None) is not None)

    def _gce_run_wait_step(self):
        t0 = getattr(self, "_gce_run_wait", None)
        if t0 is None:
            return
        aid = getattr(self, "_gce_d_after", None)
        if aid is not None:
            # no reason to sit out the typing debounce: rebuild now
            try:
                self.root.after_cancel(aid)
            except tk.TclError:
                pass
            self._gce_d_regen_now()
        if getattr(self, "_gce_regen", None) is not None \
                and time.time() - t0 < self._GCE_RUN_WAIT_MAX_S:
            self.root.after(self._GCE_RUN_WAIT_MS, self._gce_run_wait_step)
            return
        self._gce_run_wait = None
        self._gce_notice_hide_if("run-wait")
        if getattr(self, "proc", None) is None:
            self.run()

    def _gce_run_designer(self):
        """A linked, unedited text: the designer's own Generate, byte for
        byte -- the same inversion `_MandrelMixin.run` uses, one level up."""
        mode = self._gce_designer_mode()
        remembered = self._last_input_mode
        self._gce_dispatch = mode
        self._last_input_mode = mode
        try:
            return super().run()
        finally:
            self._gce_dispatch = None
            self._last_input_mode = remembered

    def _gce_run_text_file(self, text):
        """Anything else: the converter on a scratch copy of the text. A
        linked text converts with the designer's tube and sag settings; an
        opened file with the Convert-to-STL ones. The opened original is
        never the file converted, so it is never touched."""
        path = self._gce_scratch("edited.gcode")
        try:
            # LF, as every Save and Send writes: in Windows text mode this
            # copy came out CRLF, and Save/Send off the page byte-copied it
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        except OSError as exc:
            self._gce_notice_show(f"The text could not be saved for "
                                  f"Generate: {exc}", [])
            return
        doc = self._gce_doc
        linked = doc["source"] != self._GCE_FILE
        swaps = [(self.in_var, path)]
        if linked:
            swaps += [(self.diameter, str(self._gce_designer_diameter())),
                      (self.faces, self.s_faces.get()),
                      (self.sag_mode, self.s_sag_mode.get())]
        saved = [(v, v.get()) for v, _val in swaps]
        self._gce_run_text = path
        try:
            for v, val in swaps:
                v.set(val)
            super().run()
        finally:
            for v, val in saved:
                v.set(val)
            self._gce_run_text = None
        if getattr(self, "proc", None) is not None:
            # the run converts the scratch text: that is the g-code it made
            self.expected_gcode = path
            self._gce_ran_text = path

    def _on_finished(self, code):
        super()._on_finished(code)
        ran = getattr(self, "_gce_ran_text", None)
        if ran:
            self._gce_ran_text = None
            # the scratch text is already the exported text: never let the
            # output options rewrite it a second time on a later Save
            if self.last_gcode == ran:
                self._last_gcode_is_scaffold = False
                self._last_gcode_is_calibration = False

    # ------------------------------------------------------------------ #
    # a Generate of this page's own text, saved or sent from another page
    # ------------------------------------------------------------------ #
    def _gce_page_result(self):
        """True when the last Generate converted this page's own text (an
        opened file, or a text edited away from its design): `last_gcode`
        is then the scratch copy `_gce_run_text_file` wrote, which is what
        Simulation shows. Save and Send from any other page must write what
        Save on this page writes -- before, Simulation's Save byte-copied
        the scratch (comments kept despite "Strip comments on save") and
        Send stored it as "edited.gcode", while Design's Send stored the
        stripped text as "<file>_edited.gcode"."""
        last = getattr(self, "last_gcode", None)
        if not (last and hasattr(self, "gce") and os.path.isfile(last)):
            return False
        here = os.path.join(self.session_dir, "editor", "edited.gcode")
        return (os.path.normcase(os.path.abspath(last))
                == os.path.normcase(os.path.abspath(here)))

    def _gce_page_save_name(self, folder=None):
        """The name Save on this page offers, and Send stores: an opened
        file's own name + "_edited", else the saved design's name, else
        "scaffold" or "N scaffolds". With `folder` (where the save dialog
        opens) a name a file there already has gets a number, so the
        second design saved is never offered the first one's file."""
        doc = self._gce_doc
        if doc.get("path"):
            stem = os.path.splitext(os.path.basename(doc["path"]))[0] \
                + "_edited"
        else:
            tie = (getattr(self, "_gw_tie", None) or {}).get("name")
            count = getattr(self, "_gce_scaffold_count", None)
            n = count() if callable(count) else 1
            stem = tie or ("scaffold" if n <= 1 else "%d scaffolds" % n)
            # a preset name may hold what a file name cannot
            stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", stem).strip(" .") \
                or "scaffold"
        name, k = stem + ".gcode", 2
        while folder and os.path.exists(os.path.join(folder, name)):
            name, k = "%s %d.gcode" % (stem, k), k + 1
        return name

    def _gce_save_folder(self):
        """Where Save's dialog opens: the Settings panel's save folder, else
        the folder this page last saved into (None: the dialog's own)."""
        import gui_prefs
        d = gui_prefs.save_dir_kw().get("initialdir") \
            or getattr(self, "_gce_saved_dir", None)
        return d if d and os.path.isdir(d) else None

    def _gce_page_save(self):
        """(transform, initial name, the file never to overwrite) for
        gui_run's Save off this page, or None when the last result is not
        this page's text."""
        if not self._gce_page_result():
            return None
        return (self._gce_save_ready,
                self._gce_page_save_name(self._gce_save_folder()),
                self._gce_doc.get("path"))

    def _gcode_save_text(self, filename=None):
        """Send's bytes off the page, for a page result: the scratch text as
        Save on this page writes it."""
        if self._gce_page_result():
            try:
                with open(self.last_gcode, encoding="utf-8",
                          errors="surrogateescape") as fh:
                    return self._gce_save_ready(fh.read())
            except OSError:
                return None
        return super()._gcode_save_text(filename)

    def _ml_default_upload_name(self):
        if self._gce_page_result():
            return self._gce_page_save_name()
        return super()._ml_default_upload_name()

    def save_gcode_as(self):
        """On this page Save writes THIS text, exactly."""
        if not (hasattr(self, "gce") and self._gce_on_page()):
            return super().save_gcode_as()
        text = self.gce.get_text()
        if not text.strip():
            return
        doc = self._gce_doc
        folder = self._gce_save_folder()
        designed = self._gce_from_designer()
        shown = text
        if designed:
            m = self._gce_model_now()
            if m is not None:
                try:
                    shown = self._gce_save_ready(m.export_text())
                except Exception:  # noqa: BLE001 -- the page text stands in
                    shown = text
        if not self._preflight_ok(shown, flavor=self.gce_flavor_name(),
                                  designed=designed):
            return
        path = filedialog.asksaveasfilename(
            title="Save g-code as", defaultextension=".gcode",
            initialfile=self._gce_page_save_name(folder),
            filetypes=[("G-code", "*.gcode"), ("All files", "*.*")],
            **({"initialdir": folder} if folder else {}))
        if not path:
            return
        self._gce_saved_dir = os.path.dirname(os.path.abspath(path))
        if self._gce_from_designer():
            self._gce_record_edits()
            m = self._gce_model_sync()
            if m is not None:
                m.options.context["filename"] = os.path.basename(path)
                text = m.export_text()
        if hasattr(self, "_gce_save_ready"):
            text = self._gce_save_ready(text)
        if doc.get("path") and os.path.normcase(os.path.abspath(path)) == \
                os.path.normcase(os.path.abspath(doc["path"])):
            self._gce_notice_show(
                "MEWracle never overwrites the file you opened. Pick a new "
                "name to save this text.", [])
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        except OSError as exc:
            self._gce_notice_show(f"The file could not be saved: {exc}", [])
            return
        self.status.set(f"Saved {os.path.basename(path)}.")
        if hasattr(self, "_gw_saved_gcode"):
            self._gw_saved_gcode()

    def _refresh_result_controls(self):
        super()._refresh_result_controls()
        btn = getattr(self, "save_gcode_btn", None)
        if btn is not None and hasattr(self, "gce") and self._gce_on_page():
            btn.configure(state="normal" if self.gce.get_text().strip()
                          else "disabled")

    # the top row's design line (gui_summary) on this page
    def _summary_target(self, mode):
        if mode != "gcode" or not hasattr(self, "gce"):
            return super()._summary_target(mode)
        doc = self._gce_doc
        edited = " · hand-edited" if self._gce_edited() else ""
        if doc["source"] == self._GCE_FILE:
            return ("G-code window · opened file",
                    os.path.basename(doc["path"] or "") + edited)
        if doc["source"] == "detached":
            return ("G-code window · detached copy",
                    "of this page's design" + edited)
        return "G-code window", "This page's design" + edited

    def _design_signature(self, mode):
        """What this page's Generate builds is the TEXT, so a result is
        current only while the text is the one it was built from."""
        sig = super()._design_signature(mode)
        if mode == "gcode" and sig is not None and hasattr(self, "gce"):
            # A linked, unedited page is its design: its text refills a moment
            # after the design changes (and after Generate), with no change
            # by the user, so a graded scaffold read "Changed since Generate"
            # the instant its Generate finished. Such a page is signed by its
            # design, as the Printer link's out-of-date check already signs
            # it (`_gw_page_key`); any other page by its text.
            doc = self._gce_doc
            linked = ((doc.get("source") or self._GCE_LINKED)
                      == self._GCE_LINKED and not self._gce_edited())
            sig["__text__"] = "linked" if linked \
                else hash(self.gce.get_text())
            page_key = getattr(self, "_gw_page_key", None)
            if callable(page_key):
                try:
                    sig["__page__"] = page_key()
                except Exception:  # noqa: BLE001 -- a half-built page
                    pass
            # The text is the input. The input path is not: Generate points
            # it at a scratch copy for the length of the run
            # (`_gce_run_text_file`), the run is noted inside that swap, and
            # so every Generate of an opened file read "Changed since
            # Generate" the moment it finished.
            sig.pop("__input__", None)
        return sig

    def on_close(self):
        self._gce_export_preview_cancel()
        if hasattr(self, "_gce_regen"):
            self._gce_stop_regen()
        super().on_close()
