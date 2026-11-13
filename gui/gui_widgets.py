#!/usr/bin/env python3
"""gui_widgets.py -- MODERN-UI ONLY: the flat typographic primitives of the shell.

One designed surface instead of boxed widgets: pages are built from
  section_header()  small-caps letterspaced overline (the group voice)
  hairline()        1-px divider
  Disclosure        collapsible group (chevron + title + live On/Off badge)
  ModuleCard        one module's tile: title + badge + a one-line "how is this
                    set" summary, clickable anywhere
  ModuleGrid        a page's modules as a REFLOWING grid of those tiles over a
                    shared detail host that COVERS the grid when one opens (a
                    lift, not a re-map -- its docstring has the measurement).
                    Reflow is the point: cards come and go with the collector
                    mode, and a hidden card in a fixed grid leaves a hole
  PillRail          the scaffold selector; implements the contract of the old
                    scaf_nb glue (index / select / show_n-with-clamp)
  SegmentedBar      one joined bar of mutually-exclusive segments on a Tk var
  ToggleSwitch      a keyboard-reachable on/off switch (the viewer rail's toggles)
  ActionRow         Home's flat task row (title + hint + chevron, click-anywhere)
  StepRow           Home's guided-path step: numbered chip + title + hint + a
                    highlightable accent state
  scroll_guard()    keeps a scrollable child (Treeview) from double-scrolling the
                    VScrollFrame page under the wheel

Composite internals are plain tk widgets (not ttk) so hover/selection recolour is
direct; theming flows through set_palette(pal), called by
_ModernThemeMixin._apply_theme after the ttk pass. Instances self-register in a
WeakSet and retheme in place, so the dark-mode toggle reaches them like any ttk
class."""

import weakref

import tkinter as tk
from tkinter import ttk

import gui_fonts
import gui_paint
import gui_tips

# palette fallback before the first _apply_theme (matches gui_modern_theme
# LIGHT; not imported from there to keep this module dependency-free)
_PAL = {
    "bg": "#f6f7f9", "fg": "#1c1e21", "hint": "#646a71", "card": "#ffffff",
    "hover": "#eef1f5", "hairline": "#d9dce1", "disabled": "#9aa0a6",
    "select_bg": "#2f6fd6", "select_fg": "#ffffff",
    "st_ok": "#2e7d32", "st_warn": "#9a5b00", "st_err": "#c0392b",
    "st_info": "#2f6fd6",
}
_REGISTRY = weakref.WeakSet()


def set_palette(pal):
    """Adopt the live palette and retheme every registered composite widget.
    Called by _ModernThemeMixin._apply_theme (after the ttk class pass)."""
    global _PAL
    _PAL = dict(_PAL, **pal)
    for w in list(_REGISTRY):
        try:
            w.retheme()
        except tk.TclError:
            pass


def palette():
    """The live palette dict (a copy). For a custom-drawn widget (e.g. a Canvas)
    that self-registers in _REGISTRY and recolours in its own retheme()."""
    return dict(_PAL)


def register(widget):
    """Add a widget with a `retheme()` method to the theme registry, so the
    dark/light toggle reaches it like any composite primitive. Returns it."""
    _REGISTRY.add(widget)
    return widget


def _track(text):
    """Fake small-caps letterspacing (Tk fonts have no tracking): uppercase
    joined with hair spaces."""
    return " ".join(text.upper())


def section_header(parent, text, **grid_kw):
    """The overline group header. Returns the label (caller grids/packs it
    unless grid_kw is given)."""
    lbl = ttk.Label(parent, text=_track(text), style="SectionHeader.TLabel")
    if grid_kw:
        lbl.grid(**grid_kw)
    return lbl


def hairline(parent, **grid_kw):
    """A 1-px horizontal divider."""
    ln = ttk.Frame(parent, style="Hairline.TFrame", height=1)
    if grid_kw:
        ln.grid(**grid_kw)
    return ln


from tkinter import simpledialog as _simpledialog

_STOCK_ASKSTRING = _simpledialog.askstring
_STOCK_ASKFLOAT = _simpledialog.askfloat


def ask_string(parent, title, prompt, initialvalue=None, check=None):
    """A one-line text prompt in the app's own look: the body font, a themed
    field, OK and Cancel on the right, Return and Esc. Tk's stock
    simpledialog drew a sunken classic entry and grey classic buttons that
    belonged to no page of the app. Returns the text, or None on Cancel.

    `check(text)` may return a sentence saying what is wrong; the prompt
    then stays open and shows it under the field.

    A harness or test that replaces `simpledialog.askstring` still answers
    in place of this window, as it did before."""
    if _simpledialog.askstring is not _STOCK_ASKSTRING:
        return _simpledialog.askstring(title, prompt, initialvalue=initialvalue,
                                       parent=parent)
    top = tk.Toplevel(parent)
    top.withdraw()
    top.title(title)
    top.transient(parent.winfo_toplevel())
    top.resizable(False, False)
    top.configure(background=_PAL["bg"])
    body = ttk.Frame(top, padding=(gui_fonts.dpi_px(top, 18),
                                   gui_fonts.dpi_px(top, 16)))
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=prompt, wraplength=gui_fonts.dpi_px(top, 360),
              justify="left").pack(anchor="w")
    var = tk.StringVar(value="" if initialvalue is None else str(initialvalue))
    ent = ttk.Entry(body, textvariable=var, width=40)
    ent.pack(anchor="w", fill="x", pady=(gui_fonts.dpi_px(top, 8), 0))
    why = tk.StringVar(value="")
    ttk.Label(body, textvariable=why, style="Warn.TLabel").pack(anchor="w")
    row = ttk.Frame(body)
    row.pack(anchor="e", pady=(gui_fonts.dpi_px(top, 10), 0))
    out = {"value": None}

    def ok(_e=None):
        text = var.get()
        msg = check(text) if check else None
        if msg:
            why.set(msg)
            ent.focus_set()
            return
        out["value"] = text
        top.destroy()

    def cancel(_e=None):
        top.destroy()
    ttk.Button(row, text="OK", command=ok, width=9).pack(side="left")
    ttk.Button(row, text="Cancel", command=cancel, width=9).pack(
        side="left", padx=(gui_fonts.dpi_px(top, 6), 0))
    top.bind("<Return>", ok)
    top.bind("<KP_Enter>", ok)
    top.bind("<Escape>", cancel)
    top.protocol("WM_DELETE_WINDOW", cancel)
    # over the window it belongs to, a third of the way down, as the app's
    # other prompts sit (gui_gcode_work._gw_dialog)
    top.update_idletasks()
    owner = parent.winfo_toplevel()
    try:
        x = owner.winfo_rootx() + max(
            0, (owner.winfo_width() - top.winfo_reqwidth()) // 2)
        y = owner.winfo_rooty() + max(
            0, (owner.winfo_height() - top.winfo_reqheight()) // 3)
        top.geometry("+%d+%d" % (x, y))
    except tk.TclError:
        pass
    top.deiconify()
    try:
        top.grab_set()
    except tk.TclError:          # another window holds the grab
        pass
    ent.focus_set()
    ent.select_range(0, "end")
    top.wait_window()
    return out["value"]


def ask_float(parent, title, prompt, minvalue=None, initialvalue=None):
    """ask_string for a number: refuses what is not one, or is below
    `minvalue`, in a line under the field rather than a second popup.
    Returns the float, or None on Cancel."""
    if _simpledialog.askfloat is not _STOCK_ASKFLOAT:
        return _simpledialog.askfloat(title, prompt, parent=parent,
                                      minvalue=minvalue,
                                      initialvalue=initialvalue)

    def check(text):
        try:
            v = float(text.strip())
        except ValueError:
            return "Type a number."
        if minvalue is not None and v < minvalue:
            return "Type a number above zero." if minvalue <= 1e-9 else \
                "Type a number of at least %g." % minvalue
        return None
    text = ask_string(parent, title, prompt, initialvalue=initialvalue,
                      check=check)
    return None if text is None else float(text.strip())


#: the pointer over each edge or corner of a resizable panel
RESIZE_CURSORS = {"w": "size_we", "e": "size_we", "n": "size_ns",
                  "s": "size_ns", "nw": "size_nw_se", "se": "size_nw_se",
                  "ne": "size_ne_sw", "sw": "size_ne_sw"}
RESIZE_SIDES = ("w", "e", "n", "s", "nw", "ne", "sw", "se")


class ResizeEdges:
    """Invisible strips over the edges and corners of a floating panel
    `box`, so a drag on any of them resizes it, as a desktop app's panels
    resize. Nothing is drawn: the pointer changes over a strip, and that
    is the only sign.

    Each strip is a child of `box` laid over its padding, inside its border,
    so it never covers a control; a side with less than `min_px` of padding
    takes the rest from the content's margin. A corner is an L, `corner`
    pixels along each of its two edges, so it stays as thin as the edges.

    What a drag does is the caller's: `press(sides)` runs on the press and
    returns the state of the drag (or None to refuse it), `drag(state,
    sides, dx, dy)` on each move with the pointer's travel since the press,
    `release(state)` at the end. `sides` names the edge or corner ("w",
    "se"). `place()` lays the strips (again), `forget()` takes them away."""

    def __init__(self, box, press, drag, release=None, sides=RESIZE_SIDES,
                 min_px=5, max_px=8, corner=14):
        self.box = box
        self._press, self._drag, self._release = press, drag, release
        self._min, self._max, self._corner = min_px, max_px, corner
        self._state = None
        self.strips = []                    # (strip, sides, part)
        for s in sides:
            for part in (("h", "v") if len(s) == 2 else ("",)):
                strip = ttk.Frame(box, cursor=RESIZE_CURSORS[s])
                strip._form_exempt = "resize edge"   # gui_form.exempt
                strip.bind("<ButtonPress-1>",
                           lambda e, s=s: self._on_press(e, s))
                strip.bind("<B1-Motion>", lambda e, s=s: self._on_move(e, s))
                strip.bind("<ButtonRelease-1>", self._on_release)
                self.strips.append((strip, s, part))
        self.place()

    def _pads(self):
        """The strip thickness on each side: left, top, right, bottom."""
        try:
            raw = [int(float(str(p)))
                   for p in self.box.tk.splitlist(self.box.cget("padding"))]
        except (tk.TclError, ValueError, TypeError):
            raw = []
        # ttk: top defaults to left, right to left, bottom to top
        raw = raw or [0]
        l = raw[0]
        t = raw[1] if len(raw) > 1 else l
        r = raw[2] if len(raw) > 2 else l
        b = raw[3] if len(raw) > 3 else t
        return tuple(max(self._min, min(self._max, v)) for v in (l, t, r, b))

    def place(self):
        try:
            bw = int(float(str(self.box.cget("borderwidth"))))
        except (tk.TclError, ValueError):
            bw = 0
        pl, pt, pr, pb = self._pads()
        c = self._corner
        for strip, s, part in self.strips:
            kw = {"bordermode": "outside"}
            if len(s) == 2:
                east, south = "e" in s, "s" in s
                thick_x = pr if east else pl
                thick_y = pb if south else pt
                w, h = (c, thick_y) if part == "h" else (thick_x, c)
                kw.update(relx=1.0 if east else 0.0,
                          x=-bw - w if east else bw,
                          rely=1.0 if south else 0.0,
                          y=-bw - h if south else bw, width=w, height=h)
            elif s in ("w", "e"):
                w = pr if s == "e" else pl
                kw.update(relx=1.0 if s == "e" else 0.0,
                          x=-bw - w if s == "e" else bw, y=bw + c, width=w,
                          relheight=1.0, height=-2 * (bw + c))
            else:
                h = pb if s == "s" else pt
                kw.update(rely=1.0 if s == "s" else 0.0,
                          y=-bw - h if s == "s" else bw, x=bw + c, height=h,
                          relwidth=1.0, width=-2 * (bw + c))
            strip.place(**kw)
            strip.lift()

    def lift(self):
        for strip, _s, _p in self.strips:
            strip.lift()

    def forget(self):
        for strip, _s, _p in self.strips:
            strip.place_forget()

    def _on_press(self, e, sides):
        self._state = None
        st = self._press(sides)
        if st is None:
            return None
        self._state = (e.x_root, e.y_root, st)
        return "break"

    def _on_move(self, e, sides):
        if self._state is None:
            return None
        x0, y0, st = self._state
        self._drag(st, sides, e.x_root - x0, e.y_root - y0)
        return "break"

    def _on_release(self, _e=None):
        if self._state is None:
            return None
        st = self._state[2]
        self._state = None
        if self._release is not None:
            self._release(st)
        return "break"


class FitCombobox(ttk.Combobox):
    """A ttk.Combobox as wide as its longest choice, in its own font.

    Tk sizes a combobox by `width` in CHARACTERS, each the width of a "0" in
    its font, and a hand-picked count clips any choice with wider letters
    than "0" or more of them than was counted: "Diamond helix (M" in a
    width-18 Pattern cell, "mm/mi" in a width-7 unit menu. This measures
    every choice in the font the widget draws with and sets `width` to
    hold the widest, again whenever `values` or `font` change. Both sides
    of the sum are in that font's pixels, so the fit holds at any display
    scaling. The drop-down list takes the field's width, so it shows every
    choice whole too.

    A `width` given is a FLOOR, for a box that is also typed in (a typed
    angle cycle can be longer than any listed choice)."""

    def __init__(self, master=None, **kw):
        self._fit_floor = int(kw.pop("width", 0) or 0)
        super().__init__(master, **kw)
        self._fit()

    def _fit(self):
        import tkinter.font as tkfont
        try:
            spec = self.cget("font") or "TkTextFont"
            try:
                font = tkfont.nametofont(str(spec))
            except tk.TclError:
                font = tkfont.Font(font=spec)
            values = self.tk.splitlist(self.cget("values"))
            zero = max(1, font.measure("0"))
            need = max([font.measure(str(v)) for v in values] or [0])
        except tk.TclError:
            return
        # +1: the text area's own inset and the insert cursor
        chars = -(-need // zero) + 1 if need else 0
        super().configure(width=max(self._fit_floor, chars, 1))

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw, cnf = dict(cnf, **kw), None
        if cnf is not None or not kw:       # a query, as Tk's
            return super().configure(cnf)
        if "width" in kw:
            self._fit_floor = int(kw.pop("width") or 0)
        r = super().configure(**kw) if kw else None
        self._fit()
        return r

    config = configure


def scroll_guard(widget):
    """Bind the wheel on `widget` to scroll ITSELF and stop the event there,
    so a Treeview inside a VScrollFrame page doesn't also scroll the page
    (VScrollFrame wheels via bind_all; a widget-level "break" halts the chain)."""
    def _wheel(e):
        try:
            widget.yview_scroll(-1 * int(e.delta / 120), "units")
        except tk.TclError:
            pass
        return "break"
    widget.bind("<MouseWheel>", _wheel)
    return widget


class Disclosure(ttk.Frame):
    """A collapsible group: a full-width clickable header (chevron + title + live
    On/Off badge) over a `.body` frame. The body is hidden with grid_remove(), so
    every widget inside keeps its identity. `status_var` drives the badge and the
    open-state on .sync() (called after preset loads); the user can still open a
    group whose feature is off.

    `info` puts a click-to-open ⓘ dot on the header, for a paragraph too long
    to read as a hover. `tip` is the short alternative: a hover tooltip on the
    whole header, with no dot."""

    def __init__(self, parent, title, status_var=None, open=None,
                 command=None, info=None, tip=None):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self._status_var = status_var
        self._command = command        # called with the new open state
        self._trace = None

        self._head = tk.Frame(self, cursor="hand2")
        self._head.grid(row=0, column=0, sticky="ew")
        self._head.columnconfigure(2, weight=1)
        self._chev = tk.Label(self._head, text="▸", font="MewSection",
                              cursor="hand2")
        self._chev.grid(row=0, column=0, sticky="w",
                        padx=(0, gui_fonts.SP["s"]), pady=gui_fonts.SP["xs"])
        self._title = tk.Label(self._head, text=title, font="MewSection",
                               cursor="hand2")
        self._title.grid(row=0, column=1, sticky="w", pady=gui_fonts.SP["xs"])
        self._badge = tk.Label(self._head, text="", font="MewHint",
                               cursor="hand2")
        self._badge.grid(row=0, column=3, sticky="e",
                         padx=(gui_fonts.SP["s"], gui_fonts.SP["xs"]))
        # one info dot per expandable, on the header: its Button-1 handler returns
        # "break" and the toggle bindings live only on head/chev/title/badge, so
        # clicking the dot opens its popup without collapsing the group. Kept as
        # `.info_dot` so a caller can retarget the text later.
        self.info_dot = None
        if info:
            self.info_dot = gui_tips.info_dot(self._head, info, title=title)
            self.info_dot.grid(row=0, column=2, sticky="w",
                               padx=(gui_fonts.SP["xs"], 0),
                               pady=gui_fonts.SP["xs"])

        self.body = ttk.Frame(self)
        self.body.grid(row=1, column=0, sticky="ew",
                       padx=(gui_fonts.INDENT, 0),
                       pady=(gui_fonts.SP["xs"], 0))
        self.columnconfigure(0, weight=1)

        for w in (self._head, self._chev, self._title, self._badge):
            w.bind("<Button-1>", self._on_click)
            w.bind("<Enter>", self._on_enter, add="+")
            w.bind("<Leave>", self._on_leave, add="+")
            if tip:
                gui_tips.add_tip(w, tip)

        if status_var is not None:
            self._trace = status_var.trace_add("write", self._on_status)
            self.bind("<Destroy>", self._drop_trace, add="+")

        if open is not None:
            self._open = bool(open)
        elif isinstance(status_var, tk.BooleanVar):
            self._open = bool(status_var.get())   # open iff the feature is on
        else:
            self._open = status_var is None       # text-status rows start shut
        self._hovered = False
        _REGISTRY.add(self)
        self.retheme()
        self._apply_open()

    # -- state ----------------------------------------------------------- #
    def bind_status(self, status_var, open_now=None):
        """Late-attach the badge var (for features whose BooleanVar is created
        INSIDE the reused builder, e.g. stab_on). Re-resolves the open state
        from the var unless open_now overrides it."""
        self._drop_trace()
        self._status_var = status_var
        self._trace = status_var.trace_add("write", self._on_status)
        self.bind("<Destroy>", self._drop_trace, add="+")
        self.set_open(bool(status_var.get()) if open_now is None
                      else bool(open_now))

    def is_open(self):
        return self._open

    def set_open(self, flag):
        self._open = bool(flag)
        self._apply_open()
        if self._command is not None:
            self._command(self._open)

    def toggle(self):
        self.set_open(not self._open)

    def sync(self):
        """Post-preset re-sync: open iff the feature is on (the badge already tracks
        the var live). Only a BooleanVar drives the open state -- a text var is a
        status readout, not an on/off feature, so the user's choice is kept."""
        if isinstance(self._status_var, tk.BooleanVar):
            try:
                self.set_open(bool(self._status_var.get()))
            except tk.TclError:
                pass

    def _apply_open(self):
        self._chev.configure(text="▾" if self._open else "▸")
        if self._open:
            self.body.grid()
        else:
            self.body.grid_remove()
            self._reclamp_scroll()
        self._paint_badge()

    def _reclamp_scroll(self):
        """Collapsing near the page bottom can leave the viewport past the new
        scrollregion end; re-issuing the current fraction makes the canvas
        re-clamp. Finds the nearest ancestor canvas (the VScrollFrame's)."""
        w = self.master
        while w is not None and not isinstance(w, tk.Canvas):
            w = getattr(w, "master", None)
        if w is None:
            return
        canvas = w
        # Before the window is mapped (construction / bulk preset-apply) the canvas
        # viewport is pinned at the top and there is nothing to re-clamp, so skip the
        # forced relayout: each of these passes costs a full geometry recompute and
        # dozens of disclosures collapse at boot. Once visible, a user collapse DOES
        # need it, because the viewport may be past the new end.
        try:
            if not canvas.winfo_ismapped():
                return
            # synchronous (update_idletasks, no after_idle): a pending after at
            # window teardown leaves a half-deleted Tcl command behind
            canvas.update_idletasks()
            canvas.yview_moveto(canvas.yview()[0])
        except tk.TclError:
            pass

    # -- events ---------------------------------------------------------- #
    def _on_click(self, _e=None):
        self.toggle()

    def _on_enter(self, _e=None):
        self._hovered = True
        self._paint()

    def _on_leave(self, _e=None):
        self._hovered = False
        self._paint()

    def _on_status(self, *_):
        try:
            self._paint_badge()
        except tk.TclError:
            pass

    def _drop_trace(self, _e=None):
        if self._trace is not None and self._status_var is not None:
            try:
                self._status_var.trace_remove("write", self._trace)
            except tk.TclError:
                pass
            self._trace = None

    # -- painting -------------------------------------------------------- #
    def _paint_badge(self):
        if self._status_var is None:
            self._badge.configure(text="")
            return
        if isinstance(self._status_var, tk.BooleanVar):
            try:
                on = bool(self._status_var.get())
            except tk.TclError:
                on = False
            self._badge.configure(text="On" if on else "Off",
                                  fg=_PAL["select_bg"] if on else _PAL["hint"])
        else:                     # a text var: show its value as the status
            try:
                val = str(self._status_var.get())
            except tk.TclError:
                val = ""
            self._badge.configure(text=val.capitalize(), fg=_PAL["hint"])

    def _paint(self):
        bgc = _PAL["hover"] if self._hovered else _PAL["bg"]
        self._head.configure(background=bgc)
        self._chev.configure(background=bgc, fg=_PAL["hint"])
        self._title.configure(background=bgc, fg=_PAL["fg"])
        self._badge.configure(background=bgc)

    def retheme(self):
        self._paint()
        self._paint_badge()


class ModuleCard(tk.Frame):
    """One module's tile in a ModuleGrid: title + live badge + a one-line summary
    of how that module is currently set. Clickable anywhere, focusable, Return or
    Space opens it.

    The sibling of Disclosure, and identical where they overlap -- `status_var`
    drives the badge, `.body` is where the caller builds that module's fields, and
    `.body` keeps its widget identity for the life of the page. That last part is
    not cosmetic: every persisted Tk var lives in one of these bodies, so a body
    that got destroyed and rebuilt would drop preset keys.

    What differs from Disclosure is WHERE the body lives. A Disclosure expands its
    body underneath itself, inside the same column. A ModuleCard's body sits in the
    grid's shared detail host and REPLACES the whole grid when opened, so the
    fields get the full width of the form pane instead of one card's share of it.
    That is a measurement, not a preference: the scaffold page's form pane is
    582 px (gui_shell._scaf_init_sash seats the sash at 40%), 534 px inside its
    PAGE_MARGIN, so a two-column card is 261 px -- narrower than a single
    label+entry pair in _build_scaffold_geometry_fields, which grids labels at
    columns 0 and 2.

    `summary` may be a plain string or a Tk variable; a variable is traced, so the
    tile re-reads live. Callers that have no summary yet pass nothing and get the
    badge alone, which is exactly what a Disclosure showed."""

    PAD_X = gui_fonts.SP["m"]
    PAD_Y = gui_fonts.SP["s"]

    def __init__(self, parent, title, status_var=None, summary=None,
                 info=None, command=None, wide=False):
        super().__init__(parent, cursor="hand2", highlightthickness=1,
                         takefocus=True)
        self.columnconfigure(0, weight=1)
        self._title_text = title
        self._status_var = status_var
        self._summary_var = None
        self._summary_text = ""
        self._command = command          # called with this card's key by the grid
        self._wide = bool(wide)
        self._hovered = False
        self._focused = False
        # ONE trace slot each, not a list. A per-scaffold module has one card
        # whose status var is re-pointed at scaf_vars[i] on every pill switch, so
        # an append-only list would accumulate a trace per switch and every stale
        # one would keep firing on the scaffold the user has left.
        self._status_trace = None        # (var, cbname)
        self._summary_trace = None

        head = tk.Frame(self, cursor="hand2")
        head.grid(row=0, column=0, sticky="ew",
                  padx=self.PAD_X, pady=(self.PAD_Y, 0))
        head.columnconfigure(1, weight=1)
        self._head = head

        self._title = tk.Label(head, text=title, font="MewSection",
                               anchor="w", justify="left", cursor="hand2")
        self._title.grid(row=0, column=0, sticky="w")

        # the info dot returns "break" from its own Button-1, so it opens its
        # popup instead of opening the card -- the same arrangement Disclosure
        # uses on its header.
        self.info_dot = None
        if info:
            self.info_dot = gui_tips.info_dot(head, info, title=title)
            self.info_dot.grid(row=0, column=1, sticky="w",
                               padx=(gui_fonts.SP["xs"], 0))

        # A CHIP, not bare text: a card is a surface of its own, so a status that
        # is only grey words floats in it and reads as part of the title. The
        # border is a 1-px highlight ring (Tk labels have no radius; the ring plus
        # the padding is as close to a pill as tk gets without a Canvas).
        self._badge = tk.Label(head, text="", font="MewHint", cursor="hand2",
                               padx=6, pady=1, bd=0, highlightthickness=1)
        self._badge.grid(row=0, column=2, sticky="e",
                         padx=(gui_fonts.SP["s"], 0))
        self._badge.grid_remove()        # nothing to say -> no empty ring

        self._summary = tk.Label(self, text="", font="MewHint", anchor="w",
                                 justify="left", cursor="hand2")
        self._summary.grid(row=1, column=0, sticky="ew",
                           padx=self.PAD_X, pady=(2, self.PAD_Y))
        # An EMPTY summary is grid_remove'd rather than left as a blank row: a
        # module with nothing to report should be a compact title tile, not a
        # title floating in reserved whitespace. Cards in a row still match,
        # because sticky="nsew" stretches each to the tallest in its row.
        self._summary.grid_remove()

        # `body` is created by the GRID (in its shared detail host), not here --
        # a body parented to the tile would be destroyed with it on a relayout.
        self.body = None

        for w in (self, head, self._title, self._badge, self._summary):
            w.bind("<Button-1>", self._on_click)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Return>", self._on_click)
        self.bind("<space>", self._on_click)
        # NO <Configure> binding here. The summary's wraplength comes from the
        # GRID via set_summary_wrap -- measuring it off this card's own width is
        # a layout loop that never settles; the full account is in that method.
        self.bind("<Destroy>", self._drop_traces, add="+")

        if status_var is not None:
            self._status_trace = self._watch(status_var)
        self.set_summary(summary)

        _REGISTRY.add(self)
        self.retheme()

    # -- grid integration -------------------------------------------------- #
    # _apply_collector_mode (gui_scaffold.py:265) hides a section by calling
    # grid_remove() on the widget it was handed. In a two-column grid that would
    # leave a HOLE where the hidden card was, so both calls tell the owning
    # ModuleGrid to re-flow instead of moving this tile directly.
    def grid(self, **kw):
        self._grid_wanted = True
        owner = getattr(self, "_owner", None)
        if owner is None:
            return super().grid(**kw)
        owner._set_shown(self, True)

    def grid_remove(self):
        self._grid_wanted = False
        owner = getattr(self, "_owner", None)
        if owner is None:
            return super().grid_remove()
        owner._set_shown(self, False)

    def _place(self, **kw):
        """The grid's own call: actually put the tile down. Bypasses the
        grid() override above, which would recurse."""
        super().grid(**kw)

    def _unplace(self):
        super().grid_remove()

    # -- state ------------------------------------------------------------- #
    def bind_status(self, status_var):
        """(Re)point the badge var. Two callers: a feature whose BooleanVar is
        created INSIDE the reused builder (stab_on), and a per-scaffold module
        re-pointing at scaf_vars[i] when the pill rail moves."""
        self._status_trace = self._drop(self._status_trace)
        self._status_var = status_var
        if status_var is not None:
            self._status_trace = self._watch(status_var)
        self._paint_badge()

    def set_summary(self, summary):
        """Set the one-line 'how is this set' readout. Accepts a string, a Tk
        variable (traced, so the tile follows edits), or None."""
        self._summary_trace = self._drop(self._summary_trace)
        if isinstance(summary, tk.Variable):
            self._summary_var = summary
            self._summary_trace = self._watch(summary)
        else:
            self._summary_var = None
            self._summary_text = "" if summary is None else str(summary)
        self._paint_summary()

    def _watch(self, var):
        return (var, var.trace_add("write", self._on_var))

    @staticmethod
    def _drop(slot):
        if slot is not None:
            var, cb = slot
            try:
                var.trace_remove("write", cb)
            except tk.TclError:
                pass
        return None

    def _drop_traces(self, _e=None):
        self._status_trace = self._drop(self._status_trace)
        self._summary_trace = self._drop(self._summary_trace)

    # -- events ------------------------------------------------------------ #
    def _on_click(self, _e=None):
        self.focus_set()
        if self._command is not None:
            self._command(self)
        return "break"

    def _on_enter(self, _e=None):
        self._hovered = True
        self._paint()

    def _on_leave(self, _e=None):
        self._hovered = False
        self._paint()

    def _on_focus_in(self, _e=None):
        self._focused = True
        self._paint()

    def _on_focus_out(self, _e=None):
        self._focused = False
        self._paint()

    def _on_var(self, *_):
        try:
            self._paint_badge()
            self._paint_summary()
        except tk.TclError:
            pass

    def set_summary_wrap(self, px):
        """Wrap the summary at `px`, which the GRID measures and pushes down.

        Tk labels do not wrap on their own, so without a wraplength the summary
        of a densely-set module stretches its column and the card columns stop
        being equal. But the card must NOT measure it itself, which is what this
        method replaced: `self.bind("<Configure>", ...)` setting the wraplength
        from the card's own width is self-feeding, because the card's requested
        width INCLUDES this label. The card then has two stable widths and Tk
        flips between them forever -- `update_idletasks` never returns and the
        window never finishes laying out. Measured as a width log: the same nine cards
        alternating 228 px and 431 px, thousands of passes, no settle.

        It converged on the Scaffold page by luck -- two columns inside a
        fixed-width sash pane, where the card can never actually get the width it
        asks for. The Physics page's three columns in a width-tracking scroll
        canvas is where the same code diverges. So the driver moved up to
        `ModuleGrid`, which measures the TILES frame: that width is pinned by the
        canvas and owes nothing to any card's content, which breaks the cycle.
        Exactly the arrangement the detail-pane hint already uses a few lines
        down ("the label's own width IS the wraplength once it is set, so
        measuring it feeds itself")."""
        if px and px > 40:
            try:
                self._summary.configure(wraplength=int(px))
            except tk.TclError:
                pass

    # -- painting ----------------------------------------------------------- #
    def _paint_badge(self):
        """Three chip states, and the difference between them is what the tile is
        FOR: a module you can switch off reads On (filled accent) or Off (quiet
        outline); a module that is always active but has a setting worth naming
        reads that value in a neutral outline; a module that is neither -- Export,
        Presets, Geometry -- shows NO chip, because a chip on every card makes the
        ones that mean something invisible. Its summary line carries its state."""
        bgc = _PAL["hover"] if (self._hovered or self._focused) else _PAL["card"]
        if self._status_var is None:
            self._badge.configure(text="")
            self._badge.grid_remove()
            return
        if isinstance(self._status_var, tk.BooleanVar):
            try:
                on = bool(self._status_var.get())
            except tk.TclError:
                on = False
            if on:
                self._badge.configure(text="On", fg=_PAL["select_fg"],
                                      background=_PAL["select_bg"],
                                      highlightbackground=_PAL["select_bg"])
            else:
                self._badge.configure(text="Off", fg=_PAL["hint"],
                                      background=bgc,
                                      highlightbackground=_PAL["hairline"])
        else:
            try:
                val = str(self._status_var.get())
            except tk.TclError:
                val = ""
            if not val:
                self._badge.configure(text="")
                self._badge.grid_remove()
                return
            self._badge.configure(text=val.capitalize(), fg=_PAL["fg"],
                                  background=bgc,
                                  highlightbackground=_PAL["hairline"])
        self._badge.grid()

    def _paint_summary(self):
        if self._summary_var is not None:
            try:
                self._summary_text = str(self._summary_var.get())
            except tk.TclError:
                self._summary_text = ""
        text = self._summary_text
        # Never say the same thing twice. A value-status module (Footprint) puts
        # its setting in the chip, so a summary that repeats it is two copies of
        # one word and makes the tile look like it has more to say than it does.
        if text and text.strip().casefold() == \
                str(self._badge.cget("text")).strip().casefold():
            text = ""
        self._summary.configure(text=text)
        if text:
            self._summary.grid()
        else:
            self._summary.grid_remove()

    def _paint(self):
        bgc = _PAL["hover"] if (self._hovered or self._focused) else _PAL["card"]
        edge = (_PAL["select_bg"] if (self._hovered or self._focused)
                else _PAL["hairline"])
        self.configure(background=bgc, highlightbackground=edge,
                       highlightcolor=_PAL["select_bg"])
        self._head.configure(background=bgc)
        self._title.configure(background=bgc, fg=_PAL["fg"])
        self._summary.configure(background=bgc, fg=_PAL["hint"])
        # the badge's own background depends on its STATE (a filled "On" chip
        # keeps the accent under hover), so _paint_badge owns it, not this.
        self._paint_badge()

    def retheme(self):
        self._paint()
        self._paint_badge()
        self._paint_summary()


# --------------------------------------------------------------------------- #
# ModuleGrid's focus guard: ONE app-wide <FocusIn> binding, shared by every grid.
# A ModuleGrid covers one half of itself with the other instead of unmapping it
# (see the class docstring for the measurement that decided that), and a COVERED
# widget is still `winfo_viewable`, so Tk's Tab traversal walks straight into it.
# This catches focus landing under a covered frame and sends it to the visible
# half's first stop. It cannot loop: the redirect target is in the half that is
# NOT covered, so the second pass finds no covering prefix and stops.
_FOCUS_GUARD = {}          # covered frame's Tk path -> weak ref to its grid
_FOCUS_BOUND = set()       # interpreter ids already carrying the binding


def _install_focus_guard(widget):
    key = id(widget.tk)
    if key in _FOCUS_BOUND:
        return
    _FOCUS_BOUND.add(key)
    widget.bind_all("<FocusIn>", _on_any_focus, add="+")


def _on_any_focus(event):
    if not _FOCUS_GUARD:
        return
    path = str(getattr(event.widget, "_w", event.widget))
    for covered, gref in list(_FOCUS_GUARD.items()):
        # a Tk path is its parent's path plus ".child", so prefix + "." is an
        # exact ancestor test and cannot match a sibling with a longer name
        if path == covered or path.startswith(covered + "."):
            grid = gref()
            if grid is None:                    # the grid is gone
                _FOCUS_GUARD.pop(covered, None)
                return
            # asked, not stored: at build time the grid has no cards yet, so a
            # target captured then would still be the empty frame afterwards
            target = grid._visible_focus()
            if target is not None:
                try:
                    target.focus_set()
                except tk.TclError:
                    pass
            return


class StatusChip(tk.Label):
    """One setup status in the Machine page's Instructions checklist: a symbol
    plus a word or an item name ("✓ Done", "✗ Collector seat"), coloured by
    state. The checklist is the ONLY place statuses are shown; the cards and
    their tile summaries carry none.

    NEVER COLOUR ALONE. The symbol carries the state, so the chip reads the same
    to a colour-blind user and in a grayscale screenshot; the colour only
    repeats it. With a `command` the chip is a keyboard-reachable link: click,
    Enter or Space runs it. States and words come from `machine_setup`."""

    _COLOUR = {"done": "st_ok", "missing": "st_err", "auto": "st_warn",
               "default": "st_info", "waiting": "hint", "optional": "hint"}

    def __init__(self, parent, title="", command=None, font="MewHint"):
        super().__init__(parent, text="", font=font, anchor="w",
                         takefocus=command is not None,
                         cursor="hand2" if command else "", bd=0, padx=2)
        self._state = None
        self._tip = ""
        self._title = title
        self._command = command
        self._label = ""
        for seq in ("<Button-1>", "<Return>", "<space>"):
            self.bind(seq, self._invoke, add="+")
        if command is not None:
            self.bind("<Enter>", lambda _e: self._underline(True), add="+")
            self.bind("<Leave>", lambda _e: self._underline(False), add="+")
        _REGISTRY.add(self)
        self.retheme()

    def set(self, state, tip="", label=None):
        """`label` replaces the state's word, for a checklist item."""
        import machine_setup
        self._state = state
        self._tip = tip or ""
        self._label = label or ""
        text = ("%s %s" % (machine_setup.SYMBOL[state],
                           label or machine_setup.WORD[state])
                if state in machine_setup.SYMBOL else "")
        self.configure(text=text)
        self.retheme()

    def state_key(self):
        return self._state

    def invoke(self):
        if self._command is not None:
            self._command()

    def _invoke(self, _e=None):
        self.invoke()
        return "break"

    def _underline(self, on):
        # the named font is restored on leave, so a font-size change still
        # reaches the chip
        try:
            if on:
                import tkinter.font as tkfont
                self._base_font = self.cget("font")
                self._ufont = tkfont.Font(font=self._base_font, underline=True)
                self.configure(font=self._ufont)
            elif getattr(self, "_base_font", None):
                self.configure(font=self._base_font)
        except tk.TclError:
            pass

    def retheme(self):
        try:
            bg = self.master.cget("background")
        except tk.TclError:
            bg = _PAL["bg"]
        try:
            self.configure(background=bg, fg=_PAL.get(
                self._COLOUR.get(self._state, "hint"), _PAL["hint"]),
                highlightthickness=1, highlightbackground=bg,
                highlightcolor=_PAL["select_bg"])
        except tk.TclError:
            pass


class ModuleGrid(ttk.Frame):
    """A page's modules as a reflowing grid of ModuleCards, over one shared detail
    host that COVERS the grid when a card is opened.

    Two states, never both visible: the tiles, or one module's fields under a back
    link. Nothing is created or destroyed after build -- the same reason
    _show_scaffold_frame (gui_pages.py) stacks the scaffold panels: every
    persisted var has to keep its widget.

    COVERS, not replaces, and that word is the whole of why this class is not three
    grid_remove calls. Both halves sit in ONE cell, both sticky="nsew" so each is
    exactly the cell's size, and the swap is a `lift()` -- a change of stacking
    order, nothing mapped and nothing unmapped.

    The first version did unmap, and it was reported as a page that
    "refreshes and loads bottom up". It was ~85 tile widgets going down and up to
    ~365 coming in, at roughly 1.3 ms each on Windows Tk -- long enough to WATCH
    the form fill in a region at a time. Measured as an open+close round trip,
    interleaved against this version in one process so the machine's drift cancels
    (median of 9):

        module      unmap/remap      lift      speedup
        geometry         386 ms    122 ms        3.2x
        footprint        341 ms     93 ms        3.7x
        export           402 ms    105 ms        3.8x

    Read those as pairs: one click is about half. Nothing in this file is quick by
    the standard of the Disclosure rows the grid replaced, which toggle in 8 ms --
    a Disclosure moves one small body, not a whole page of form.

    Two variants were tried and rejected. Unmapping the covered half on a timer is
    strictly SLOWER (138 ms against 124 ms on the same run): deferring the unmap
    does not defer the re-map, which lands back in the visible path on the way out.
    And a 36-ms pure-lift number measured in a separate process was an artefact --
    that probe never re-gridded anything, and this machine drifts 3x between runs,
    which is why every number above comes from one interleaved process.

    What covering costs, and how each cost is paid:

      * the cell is as tall as the TALLER half, so returning from a long module
        leaves the tile list scrolling a little further than it needs to. Accepted:
        a scroll extent that does not change on every click is the calmer half of
        the bargain.
      * a covered widget is still `winfo_viewable`, so Tab would walk into fields
        nobody can see. One app-wide <FocusIn> guard (`_cover`, and the module-level
        block above this class) sends such a focus to the visible half instead --
        O(1) per swap and 2-4 ms in the table above, because the obvious fix --
        sweeping -takefocus over the covered subtree and restoring each widget's
        own value -- measured 38-51 ms per direction, half the cost of the swap it
        was meant to make cheap. Batching that sweep into one Tcl eval saved 8 ms
        of 109 widgets, which is what says the cost was Tcl-side redisplay rather
        than Python round trips, and that no amount of tuning would save it.

    REFLOW is the part a plain grid of Disclosures did not need. Cards come and go
    with the collector mode (_collector_register / _apply_collector_mode), and a
    hidden card in a fixed two-column grid leaves a hole. So tile positions are not
    assigned at add() time; `relayout()` walks the cards in registration order,
    skips the hidden ones and re-assigns row/column, and ModuleCard.grid /
    .grid_remove route into it. The existing collector hook therefore needs no
    change at all -- it still calls grid()/grid_remove() on the widget it was
    handed.

    add() returns the card; build that module's fields into `card.body`."""

    def __init__(self, parent, columns=2, gap=None, on_open=None):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self._columns = max(1, int(columns))
        self._gap = gui_fonts.SP["m"] if gap is None else gap
        self._on_open = on_open          # notified with (key or None) on nav
        self._cards = []                 # [(key, card)] in registration order
        self._shown = {}                 # key -> bool
        self._open_key = None

        self.rowconfigure(0, weight=1)   # both halves fill the one cell

        # the tiles. sticky="nsew", not "ew": the halves cover each other, so the
        # one on top has to be the whole cell or the one underneath shows past it.
        self._tiles = ttk.Frame(self)
        self._tiles.grid(row=0, column=0, sticky="nsew")
        for c in range(self._columns):
            self._tiles.columnconfigure(c, weight=1, uniform="modcard")
        # The summary labels' wraplength, measured ONCE here and pushed into every
        # card. Bound to the tiles frame and not to each card, because a card's
        # own width is not an independent measurement of it -- see
        # ModuleCard.set_summary_wrap for the layout loop that caused.
        self._wrap_px = 0
        self._tiles.bind("<Configure>", self._on_tiles_configure, add="+")

        # the detail side: a back button over one holder that stacks every module's
        # body in a single cell. Gridded for the life of the page and kept BELOW
        # the tiles; see the class docstring for why it is never unmapped.
        self._detail = ttk.Frame(self)
        self._detail.grid(row=0, column=0, sticky="nsew")
        self._detail.columnconfigure(0, weight=1)

        self._back = ttk.Button(self._detail, text="‹  All cards",
                                takefocus=True, command=self.close)
        self._back.grid(row=0, column=0, sticky="w")
        self._dtitle = ttk.Label(self._detail, text="", style="Title.TLabel")
        self._dtitle.grid(row=1, column=0, sticky="w",
                          pady=(gui_fonts.SP["s"], 0))
        self._dhint = ttk.Label(self._detail, text="", style="Hint.TLabel",
                                justify="left")
        self._dhint.grid(row=2, column=0, sticky="ew",
                         pady=(2, gui_fonts.GAP_HEADER))
        # Tk labels do not wrap on their own, so without this the blurb is
        # CLIPPED mid-sentence at the pane edge rather than running onto a second
        # line -- which is what Quality's did the moment it got one long enough.
        # Bound to the detail frame, not the label: the label's own width IS the
        # wraplength once it is set, so measuring it feeds itself.
        self._detail.bind(
            "<Configure>",
            lambda e: e.width > 80 and self._dhint.configure(
                wraplength=e.width - 4), add="+")
        self._bodies = ttk.Frame(self._detail)
        self._bodies.grid(row=3, column=0, sticky="nsew")
        self._bodies.columnconfigure(0, weight=1)
        self._tiles.lift()
        self._cover(self._detail)

    # -- summary wrapping ----------------------------------------------------- #
    def _on_tiles_configure(self, e=None):
        """One column's usable width, handed to every card's summary label.

        `width - gaps` split `columns` ways, less a card's own horizontal padding.
        Quantised to 8 px and applied only on a real change, so the ordinary
        one-pixel jitter of a resize does not re-wrap ten labels per event."""
        w = e.width if e is not None else self._tiles.winfo_width()
        if w <= 1:
            return
        self._push_footer_wrap(w)
        col = (w - self._gap * (self._columns - 1)) // self._columns
        px = int(col) - 2 * ModuleCard.PAD_X
        px = max(px, 0) // 8 * 8
        if px == self._wrap_px:
            return
        self._wrap_px = px
        for _k, card in self._cards:
            card.set_summary_wrap(px)

    # -- registration -------------------------------------------------------- #
    def add(self, key, title, status_var=None, summary=None, info=None,
            wide=False):
        """Register one module. Returns its ModuleCard; build the module's fields
        into the card's `.body`."""
        card = ModuleCard(self._tiles, title, status_var=status_var,
                          summary=summary, info=info, wide=wide,
                          command=self._on_card_click)
        card._owner = self
        card._key = key
        card._hint = info or ""
        card.body = ttk.Frame(self._bodies)
        # one card = one label column (gui_form._CardLabels): a Form on the
        # page above must not pull every card's fields out to its width
        card.body._form_card = True
        card.body.grid(row=0, column=0, sticky="nsew")
        card.body.columnconfigure(0, weight=1)
        card.body.grid_remove()
        self._cards.append((key, card))
        self._shown[key] = True
        # a card registered after the grid has already been measured starts at
        # the current wrap rather than waiting for the next resize
        card.set_summary_wrap(self._wrap_px)
        self.relayout()
        return card

    def card(self, key):
        for k, c in self._cards:
            if k == key:
                return c
        return None

    def keys(self, visible_only=False):
        return [k for k, _c in self._cards
                if not visible_only or self._shown.get(k, True)]

    def reorder(self, keys):
        """Show the tiles in `keys` order; keys not named keep their place
        after them. For a page whose cards must be BUILT in one order (a later
        card reads widgets an earlier one made) but SHOWN in another. The
        stacking order is raised to match, so Tab walks the tiles in the order
        they are drawn."""
        rank = {k: i for i, k in enumerate(keys)}
        self._cards.sort(key=lambda kc: rank.get(kc[0], len(rank)))
        for _k, card in self._cards:
            card.lift()
        self.relayout()

    # -- visibility / reflow --------------------------------------------------- #
    def _set_shown(self, card, flag):
        key = getattr(card, "_key", None)
        if key is None or self._shown.get(key) == bool(flag):
            return
        self._shown[key] = bool(flag)
        # a module that just disappeared cannot stay open in front of the user
        if not flag and self._open_key == key:
            self.close()
        self.relayout()

    def relayout(self):
        """Re-flow the visible tiles left to right, top to bottom, with no gaps
        where a hidden module used to sit."""
        r = c = 0
        for key, card in self._cards:
            if not self._shown.get(key, True):
                card._unplace()
                continue
            span = self._columns if card._wide else 1
            if card._wide and c:            # a wide tile starts its own row
                r, c = r + 1, 0
            card._place(row=r, column=c, columnspan=span, sticky="nsew",
                        padx=(0 if c == 0 else self._gap, 0),
                        pady=(0, self._gap))
            c += span
            if c >= self._columns:
                r, c = r + 1, 0

    # -- navigation ------------------------------------------------------------ #
    def _on_card_click(self, card):
        self.open(getattr(card, "_key", None))

    def open(self, key):
        card = self.card(key)
        if card is None or not self._shown.get(key, True):
            return
        with gui_paint.paint_fence(self):
            self._open_inner(key, card)

    def _open_inner(self, key, card):
        # only the opened body is a grid slave: the OTHERS really are unmapped,
        # because stacking nine invisible forms would make the cell as tall as
        # the tallest of them and hand every page resize nine extra layouts. It
        # is one map, against the ten unmaps the tiles used to cost.
        for k, c in self._cards:
            if c.body is not None and k != key:
                c.body.grid_remove()
        card.body.grid()
        self._dtitle.configure(text=card._title_text)
        hint = getattr(card, "_hint", "")
        # the info-dot text doubles as the detail blurb; only its first
        # paragraph, because the dot carries the long-form caveats too
        self._dhint.configure(text=hint.split("\n\n")[0] if hint else "")
        self._dhint.grid() if hint else self._dhint.grid_remove()
        self._cover(self._tiles)
        self._detail.lift()
        self._open_key = key
        self._back.focus_set()
        if self._on_open is not None:
            self._on_open(key)

    def close(self):
        """NO paint fence on the way OUT, unlike `open`, and the asymmetry is
        measured rather than tidy.

        Closing a card flashed BLACK for a frame on every attempt: photographing
        the grid at ~120 fps, the darkest frame after the click came back at
        115.9 against the grid's own 241 -- a 52% drop, 4 reps of 4 -- and with
        the fence off, 0 of 4 and no drop at all (controlled with a
        deliberate one-paint-cycle flash, caught 3 times in 3). Opening one, on
        the same host in the same run, does not flash either way.

        That difference is the reason to keep the fence on `open` and drop it
        here. A close UNCOVERS area: the composited surface has nothing drawn
        where the body used to be until the parent repaints its background, so
        Windows presents the hole. An open covers area instead, so there is no
        hole to present. A close is also the cheap direction -- it removes
        content rather than laying it out -- so it has far less to stage than the
        open the fence was measured on (1248 ms -> 275 ms of first-change
        spread)."""
        self._close_inner()

    def _close_inner(self):
        was, self._open_key = self._open_key, None
        card = self.card(was) if was is not None else None
        if card is not None and not self._shown.get(was, True):
            card = None                  # the module vanished under the user
        self._cover(self._detail)
        self._tiles.lift()
        # the body STAYS gridded. Unmapping it is what reopening the same module
        # would then have to pay again, and it buys nothing: covered, it is
        # unreachable by Tab (see _cover), and the only question anything asks --
        # opened() -- is answered by _open_key, not by the geometry manager.
        if card is not None:
            card.focus_set()
        if self._on_open is not None:
            self._on_open(None)

    def _first_card(self):
        for k, c in self._cards:
            if self._shown.get(k, True):
                return c
        return None

    # -- keeping Tab out of the covered half -------------------------------- #
    def _visible_focus(self):
        """Where focus belongs right now: the back link when a module is open,
        otherwise the first tile still on show."""
        return self._back if self._open_key is not None else self._first_card()

    def _cover(self, covered):
        """Record which half is underneath. Focus that lands there is bounced.

        Both halves stay mapped, and Tk's traversal asks `winfo_viewable`, which a
        COVERED widget still answers yes to -- so without this, Tab from the last
        field walks into a tile nobody can see and the focus ring appears to
        vanish.

        The first version swept -takefocus over every widget in the covered subtree
        and restored each one's own value on the way back. It was correct and it
        was HALF THE COST OF THE SWAP: 38-51 ms per direction, because a ttk
        `configure -takefocus` schedules a redisplay and there are 109 of them in
        the Geometry body alone. Batching the sweep into one Tcl eval saved 8 ms of
        109, which is what says the cost is Tcl-side work rather than round trips.
        So nothing is configured at all now: one app-wide <FocusIn> binding notices
        focus landing under a covered frame and sends it to the visible half's
        first stop. O(1) per swap, and per keystroke it is a string prefix test."""
        _FOCUS_GUARD[str(covered)] = weakref.ref(self)
        _FOCUS_GUARD.pop(str(self._tiles if covered is self._detail
                             else self._detail), None)
        _install_focus_guard(self)

    def opened(self):
        return self._open_key

    def sync(self):
        """Post-preset re-sync, the ModuleGrid half of Disclosure.sync(): badges
        and summaries follow their vars live, so only the reflow can be stale."""
        self.relayout()

    # -- footer ------------------------------------------------------------- #
    def add_footer(self, factory):
        """Put one full-width widget under the tiles: `factory(parent)` builds
        it. It lives IN the tiles half, so an opened module covers it like any
        tile. Row 1000 is past anything `relayout` assigns, and `relayout` never
        touches it, so the owner shows and hides it with grid()/grid_remove().

        Its wrap width comes from here, for the reason ModuleCard's does: the
        tiles frame is pinned by the pane, while the footer's own width
        includes its labels and would feed itself (`set_summary_wrap`)."""
        w = factory(self._tiles)
        w.grid(row=1000, column=0, columnspan=self._columns, sticky="ew",
               pady=(0, self._gap))
        self._footer = w
        if self._wrap_px:
            self._push_footer_wrap(self._tiles.winfo_width())
        return w

    def _push_footer_wrap(self, width):
        w = getattr(self, "_footer", None)
        if w is not None and hasattr(w, "set_wrap") and width > 1:
            w.set_wrap(int(width))


class DesignSummary(tk.Frame):
    """The Scaffold designer's read-only Design summary: a title, a hint, a row of
    metric tiles, and the checks under them.

    A card surface like ModuleCard's -- the same white fill, hairline ring and
    padding -- but never clickable, because nothing in it is a setting. Every
    value is text the owner formats; this widget only lays it out.

    The checks follow StatusChip's rule, NEVER COLOUR ALONE: each line leads
    with a symbol ("!" a problem, "✓" all clear) and the colour repeats it."""

    PAD_X = ModuleCard.PAD_X
    PAD_Y = ModuleCard.PAD_Y
    _NOTE = {"warn": ("!", "st_warn"), "ok": ("✓", "st_ok")}

    def __init__(self, parent, title, metrics, columns=3):
        super().__init__(parent, highlightthickness=1, takefocus=False)
        self.columnconfigure(0, weight=1)
        sp = gui_fonts.SP
        head = tk.Frame(self)
        head.grid(row=0, column=0, sticky="ew",
                  padx=self.PAD_X, pady=(self.PAD_Y, 0))
        head.columnconfigure(0, weight=1)
        self._head = head
        self._title = tk.Label(head, text=title, font="MewSection", anchor="w")
        self._title.grid(row=0, column=0, sticky="w")
        self._hint = tk.Label(head, text="", font="MewHint", anchor="e")
        self._hint.grid(row=0, column=1, sticky="e")

        grid = tk.Frame(self)
        grid.grid(row=1, column=0, sticky="ew", padx=self.PAD_X,
                  pady=(sp["s"], 0))
        for c in range(columns):
            grid.columnconfigure(c, weight=1, uniform="dsmetric")
        self._grid = grid
        self._tiles = {}
        for n, (key, label) in enumerate(metrics):
            tile = tk.Frame(grid)
            tile.grid(row=n // columns, column=n % columns, sticky="nsew",
                      padx=(0 if n % columns == 0 else sp["s"], 0),
                      pady=(0 if n < columns else sp["s"], 0))
            lab = tk.Label(tile, text=label, font="MewHint", anchor="w")
            lab.grid(row=0, column=0, sticky="w", padx=sp["s"],
                     pady=(sp["xs"], 0))
            val = tk.Label(tile, text="—", font="MewSection", anchor="w")
            val.grid(row=1, column=0, sticky="w", padx=sp["s"],
                     pady=(0, sp["xs"]))
            self._tiles[key] = (tile, lab, val)

        self._notes_host = tk.Frame(self)
        self._notes_host.grid(row=2, column=0, sticky="ew", padx=self.PAD_X,
                              pady=(sp["s"], self.PAD_Y))
        self._notes_host.columnconfigure(0, weight=1)
        self._notes = []                 # [(label, state)]
        self._wrap = 0
        _REGISTRY.add(self)
        self.retheme()

    # -- content ----------------------------------------------------------- #
    def set_hint(self, text):
        self._hint.configure(text=text or "")

    def set_metric(self, key, value, label=None):
        _tile, lab, val = self._tiles[key]
        val.configure(text=value or "—")
        if label is not None:
            lab.configure(text=label)

    def set_notes(self, notes):
        """`notes` is [(state, text)], state "warn" or "ok". The labels are
        reused when the count matches, so an edit does not rebuild them."""
        notes = list(notes)
        while len(self._notes) > len(notes):
            self._notes.pop()[0].destroy()
        while len(self._notes) < len(notes):
            lb = tk.Label(self._notes_host, font="MewHint", anchor="w",
                          justify="left")
            lb.grid(row=len(self._notes), column=0, sticky="ew",
                    pady=(0 if not self._notes else 2, 0))
            self._notes.append([lb, None])
        for slot, (state, text) in zip(self._notes, notes):
            sym, _c = self._NOTE[state]
            slot[1] = state
            slot[0].configure(text="%s  %s" % (sym, text))
        self._notes_host.grid() if notes else self._notes_host.grid_remove()
        self.set_wrap(self._wrap)
        self._paint_notes()

    def set_wrap(self, width):
        """Wrap the check lines at the GRID's width (see ModuleGrid.add_footer)."""
        self._wrap = width
        # quantised like ModuleGrid's own push, so a resize's one-pixel jitter
        # does not re-wrap every line
        px = (int(width) - 2 * self.PAD_X - 4) // 8 * 8
        if px > 40:
            for lb, _s in self._notes:
                lb.configure(wraplength=px)

    # -- painting ----------------------------------------------------------- #
    def _paint_notes(self):
        for lb, state in self._notes:
            _s, colour = self._NOTE.get(state, ("", "hint"))
            lb.configure(background=_PAL["card"],
                         fg=_PAL.get(colour, _PAL["hint"]))

    def retheme(self):
        card = _PAL["card"]
        self.configure(background=card, highlightbackground=_PAL["hairline"],
                       highlightcolor=_PAL["hairline"])
        for w in (self._head, self._grid, self._notes_host):
            w.configure(background=card)
        self._title.configure(background=card, fg=_PAL["fg"])
        self._hint.configure(background=card, fg=_PAL["hint"])
        for tile, lab, val in self._tiles.values():
            tile.configure(background=_PAL["bg"])
            lab.configure(background=_PAL["bg"], fg=_PAL["hint"])
            val.configure(background=_PAL["bg"], fg=_PAL["fg"])
        self._paint_notes()


class PillRail(ttk.Frame):
    """Flat pills as a segmented selector. The contract mirrors the three old scaf_nb
    glue sites exactly -- index() (0-based active), select(i) (restyle + notify),
    show_n(n) (display the first n pills and clamp the selection into range).

    Two shapes, one widget family. The scaffold selector passes nothing and gets the
    numbered row it always had. The Calibration page passes `labels` (one per
    calibrated quantity) and gets the same pills carrying words -- ttk.Notebook clips
    its tab strip rather than wrapping it, so seven labelled tabs could not live
    there.

    A LABELLED rail is EQUAL-WIDTH and FLUSH LEFT, and two rules do it:

      * every column joins one `uniform` group and every pill is `sticky="ew"`, so
        all pills are as wide as the widest and none is centred inside an over-wide
        column. That centring is what pushed one pill in from the margin while the
        next row started at 0.
      * the pills-per-row is MEASURED, not passed in. On `<Configure>` the rail asks
        how many pills of the natural widest width fit the width it has been given,
        and re-grids only when that number changes -- so seven wrap in a narrow form
        pane with every row still starting at the margin."""

    def __init__(self, parent, count=5, command=None, labels=None,
                 per_row=None, bound=None):
        super().__init__(parent)
        # `bound`: a wider widget whose right edge the rail wraps at, for a
        # rail packed at its own width (it would otherwise measure itself one
        # pill wide and stack every pill in a column)
        self._bound = bound
        self._command = command
        self._index = 0
        self._pills = []
        self._shown = None                 # show_n's clamp, None = all
        labels = list(labels) if labels is not None else None
        if labels is not None:
            count = len(labels)
        self._labelled = labels is not None
        self._per_row = max(1, int(per_row) if per_row else count)
        for i in range(count):
            b = ttk.Button(self,
                           text=(labels[i] if labels is not None
                                 else str(i + 1)),
                           # a labelled pill sizes to its text; the numbered one
                           # keeps the fixed width the scaffold rail is drawn at
                           width=(0 if labels is not None else 3),
                           style="Pill.TButton", takefocus=False,
                           command=lambda i=i: self.select(i))
            self._pills.append(b)
        self._regrid()
        # measured, so a long numbered rail (as many scaffolds as asked for)
        # wraps too instead of running off the form
        self.bind("<Configure>", self._on_configure, add="+")
        if bound is not None:
            bound.bind("<Configure>", self._on_configure, add="+")
        self._restyle()

    # -- layout ----------------------------------------------------------- #
    def _pill_width(self):
        """The natural width of the widest pill, in pixels. `winfo_reqwidth`
        before the first map already reflects the text and the style padding,
        so this needs no font arithmetic of its own."""
        try:
            return max(b.winfo_reqwidth() for b in self._pills)
        except tk.TclError:
            return 1

    def _fit_per_row(self, avail):
        """How many equal pills of the natural widest width fit `avail` px."""
        step = self._pill_width() + gui_fonts.SP["xs"]
        if step <= 1 or avail <= 1:
            return len(self._pills)
        return max(1, min(len(self._pills), int(avail + gui_fonts.SP["xs"])
                          // step))

    def _regrid(self):
        n = self._shown if self._shown is not None else len(self._pills)
        rows = -(-len(self._pills) // self._per_row)
        # only the columns actually in use join the uniform group -- a leftover
        # empty column still in it would be forced to the group's width and
        # leave a pill-sized hole at the end of the row.
        for c in range(len(self._pills)):
            self.columnconfigure(
                c, weight=0,
                uniform=("pill" if self._labelled and c < self._per_row else ""))
        for i, b in enumerate(self._pills):
            b.grid(row=i // self._per_row, column=i % self._per_row,
                   sticky=("ew" if self._labelled else ""),
                   padx=(0, gui_fonts.SP["xs"]),
                   pady=(0, gui_fonts.SP["xs"] if rows > 1 else 0))
            if i >= n:
                b.grid_remove()

    def _avail(self):
        if self._bound is None:
            return self.winfo_width()
        b = self._bound
        return b.winfo_rootx() + b.winfo_width() - self.winfo_rootx()

    def _on_configure(self, _e=None):
        want = self._fit_per_row(self._avail())
        if want != self._per_row:
            self._per_row = want
            self._regrid()

    def index(self):
        return self._index

    def select(self, i):
        i = max(0, min(len(self._pills) - 1, int(i)))
        self._index = i
        self._restyle()
        if self._command is not None:
            self._command(i)

    def show_n(self, n):
        n = max(1, min(len(self._pills), int(n)))
        self._shown = n
        for i, b in enumerate(self._pills):
            if i < n:
                b.grid()
            else:
                b.grid_remove()
        if self._index >= n:
            self.select(n - 1)

    def add_pill(self):
        """One more numbered pill (the scaffold rail grows with the count)."""
        i = len(self._pills)
        self._pills.append(ttk.Button(
            self, text=str(i + 1), width=3, style="Pill.TButton",
            takefocus=False, command=lambda i=i: self.select(i)))
        self._per_row = self._fit_per_row(self._avail())
        self._regrid()
        self._restyle()

    def _restyle(self):
        for i, b in enumerate(self._pills):
            b.configure(style="PillSel.TButton" if i == self._index
                        else "Pill.TButton")


class SegmentedBar(tk.Frame):
    """One JOINED bar of mutually-exclusive segments, driven by a Tk variable.

    The sibling of PillRail, and the difference is the whole point: a PillRail is
    a row of separate pills with air between them, which reads as several
    controls. A SegmentedBar is a single enclosed surface cut into parts by
    hairlines, which reads as ONE control with one part lit -- what the Scaffold
    designer's 3/4/5-axis mode toggle wants, since the three values are one
    choice and not three buttons.

    Drawn from tk.Labels rather than ttk.Buttons because ttk gives every button
    its own border and focus ring; flush segments need the separator to be the
    ONE px of the container's own ground showing between them, which only a raw
    frame can supply.

    `options` is [(value, label), ...] and `variable` is the StringVar the rest
    of the app already reads. A click writes the variable and THEN calls
    `command`; a programmatic set of the variable only restyles -- the same
    split the radios had, so loading a preset cannot re-enter the mode-swap."""

    PAD_X = 14
    PAD_Y = 4

    def __init__(self, parent, options, variable, command=None):
        super().__init__(parent, bd=0, highlightthickness=1)
        self._var = variable
        self._command = command
        self._opts = [(str(v), str(t)) for v, t in options]
        # NOT `self._options`: tkinter.Misc._options is the method
        # `configure` flattens its keywords through, and shadowing it with
        # a list makes every recolour raise "'list' object is not callable".
        self._segs = {}
        self._hover = None
        for i, (value, text) in enumerate(self._opts):
            lb = tk.Label(self, text=text, font="MewNav", cursor="hand2",
                          padx=self.PAD_X, pady=self.PAD_Y, bd=0,
                          highlightthickness=0)
            # the 1-px gap after every segment but the last IS the separator:
            # the frame's own ground shows through it.
            lb.pack(side="left", fill="both", expand=True,
                    padx=(0, 0 if i == len(self._opts) - 1 else 1))
            lb.bind("<Button-1>", lambda _e, v=value: self._clicked(v))
            lb.bind("<Enter>", lambda _e, v=value: self._set_hover(v))
            lb.bind("<Leave>", lambda _e, v=value: self._set_hover(None))
            self._segs[value] = lb
        # The callback is HELD rather than passed anonymously: Tk names a
        # registered command `id(func) + func.__name__`, so a lambda whose only
        # reference is Tcl's can be freed and have its id reused by the next
        # one, putting two identical names in the master's _tclCommands.
        self._repaint_cb = lambda *_a: self._paint()
        self._trace = variable.trace_add("write", self._repaint_cb)
        register(self)
        self._paint()

    # -- api -------------------------------------------------------------- #
    def values(self):
        return [v for v, _t in self._opts]

    def invoke(self, value):
        """Exactly what a click on `value` does -- the test/driver entry point,
        standing in for the TRadiobutton.invoke() this replaced."""
        self._clicked(str(value))

    def segment(self, value):
        return self._segs[str(value)]

    def set_enabled(self, value, enabled):
        """Grey one segment out (it ignores clicks), or bring it back."""
        value = str(value)
        off = getattr(self, "_off", set())
        self._off = (off - {value}) if enabled else (off | {value})
        try:
            self._segs[value].configure(cursor="hand2" if enabled else "")
        except (KeyError, tk.TclError):
            pass
        self._paint()

    # -- behaviour -------------------------------------------------------- #
    def _clicked(self, value):
        if value in getattr(self, "_off", ()):
            return
        if self._var.get() != value:
            self._var.set(value)
        self._paint()
        if self._command is not None:
            self._command()

    def _set_hover(self, value):
        self._hover = value
        self._paint()

    def _paint(self):
        p = _PAL
        try:
            live = self._var.get()
        except tk.TclError:
            return
        try:
            self.configure(bg=p["hairline"], highlightbackground=p["hairline"],
                           highlightcolor=p["hairline"])
            off = getattr(self, "_off", ())
            for value, lb in self._segs.items():
                if value in off:
                    lb.configure(bg=p["card"], fg=p["disabled"])
                elif value == live:
                    lb.configure(bg=p["select_bg"], fg=p["select_fg"])
                elif value == self._hover:
                    lb.configure(bg=p["hover"], fg=p["fg"])
                else:
                    lb.configure(bg=p["card"], fg=p["fg"])
        except tk.TclError:
            pass

    def retheme(self):
        self._paint()


class ToggleSwitch(tk.Canvas):
    """A small on/off switch: a rounded track with a knob, lit in the accent
    while on. The embedded viewer rail draws its Show/Hide controls with it.

    Keyboard-reachable (Tab focus ring, Space or Return toggles) and never
    colour alone: the knob's SIDE carries the state too. `command()` runs on a
    user toggle; `set(on)` only repaints, so a state pushed from elsewhere
    cannot re-fire the command."""

    W, H = 30, 16

    def __init__(self, parent, on=False, command=None, surface="card"):
        super().__init__(parent, width=self.W, height=self.H, bd=0,
                         highlightthickness=1, takefocus=True, cursor="hand2")
        self._on = bool(on)
        self._command = command
        self._surface = surface
        self._enabled = True
        for seq in ("<Button-1>", "<space>", "<Return>"):
            self.bind(seq, self._clicked)
        register(self)
        self._paint()

    def get(self):
        return self._on

    def set(self, on):
        on = bool(on)
        if on != self._on:
            self._on = on
            self._paint()

    def set_enabled(self, enabled):
        """A disabled switch is greyed and ignores clicks and keys."""
        enabled = bool(enabled)
        if enabled != self._enabled:
            self._enabled = enabled
            self.configure(cursor="hand2" if enabled else "",
                           takefocus=enabled)
            self._paint()

    def invoke(self):
        self._clicked()

    def _clicked(self, _e=None):
        if self._enabled and self._command is not None:
            self._command()
        return "break"

    def _paint(self):
        p = _PAL
        bg = p.get(self._surface, p["card"])
        track = p["select_bg"] if self._on else p["hairline"]
        knob = p["select_fg"] if self._on else p.get("hint", "#888888")
        if not self._enabled:
            track, knob = p["hairline"], bg
        w, h = self.W, self.H
        r = h / 2.0
        try:
            self.configure(background=bg, highlightbackground=bg,
                           highlightcolor=p["select_bg"])
            self.delete("all")
            self.create_oval(1, 1, h - 1, h - 1, fill=track, outline=track)
            self.create_oval(w - h + 1, 1, w - 1, h - 1, fill=track,
                             outline=track)
            self.create_rectangle(r, 1, w - r, h - 1, fill=track, outline=track)
            kx = w - h + 3 if self._on else 3
            self.create_oval(kx, 3, kx + h - 6, h - 3, fill=knob, outline=knob)
        except tk.TclError:
            pass

    def retheme(self):
        self._paint()


def _keyboard_row(row):
    """Make a Home row (ActionRow, StepRow) a Tab stop: Return or Space does
    what a click does, and a 1-px accent ring shows which row has focus.
    Without it the rows were mouse-only: Tab from the top row skipped every
    step, Start here included."""
    row._row.configure(takefocus=1, highlightthickness=1)
    row._row.bind("<Return>", row._on_click)
    row._row.bind("<space>", row._on_click)


def _paint_ring(row):
    """The focus ring's colours, re-read on every paint so dark mode follows."""
    row._row.configure(highlightbackground=_PAL["bg"],
                       highlightcolor=_PAL["select_bg"])


class ActionRow(ttk.Frame):
    """Home's flat task row: title, one-line hint, a trailing chevron; the
    whole row is the hit target. set_enabled(False) greys the text and drops
    the click/hover (replaces the old card button's state=disabled)."""

    def __init__(self, parent, title, hint, command):
        super().__init__(parent)
        self._command = command
        self._enabled = True
        self._hovered = False

        self._row = tk.Frame(self, cursor="hand2")
        self._row.grid(row=0, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        self._row.columnconfigure(0, weight=1)
        px, py = gui_fonts.SP["m"], gui_fonts.SP["s"]
        self._title = tk.Label(self._row, text=title, font="MewSection",
                               anchor="w", cursor="hand2")
        self._title.grid(row=0, column=0, sticky="w", padx=(px, 0),
                         pady=(py, 0))
        self._hint = tk.Label(self._row, text=hint, font="MewHint", anchor="w",
                              cursor="hand2", justify="left")
        self._hint.grid(row=1, column=0, sticky="w", padx=(px, 0),
                        pady=(0, py))
        self._chev = tk.Label(self._row, text="→", font="MewTitle",
                              cursor="hand2")
        self._chev.grid(row=0, column=1, rowspan=2, sticky="e",
                        padx=(gui_fonts.SP["s"], px))

        for w in (self._row, self._title, self._hint, self._chev):
            w.bind("<Button-1>", self._on_click)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
        _keyboard_row(self)
        _REGISTRY.add(self)
        self.retheme()

    def set_enabled(self, flag):
        self._enabled = bool(flag)
        cur = "hand2" if self._enabled else "arrow"
        self._row.configure(takefocus=1 if self._enabled else 0)
        for w in (self._row, self._title, self._hint, self._chev):
            w.configure(cursor=cur)
        self._paint()

    def _on_click(self, _e=None):
        if self._enabled and self._command is not None:
            self._command()

    def _on_enter(self, _e=None):
        self._hovered = True
        self._paint()

    def _on_leave(self, _e=None):
        self._hovered = False
        self._paint()

    def _paint(self):
        bgc = _PAL["hover"] if (self._hovered and self._enabled) else _PAL["bg"]
        fg = _PAL["fg"] if self._enabled else _PAL["disabled"]
        hint = _PAL["hint"] if self._enabled else _PAL["disabled"]
        self._row.configure(background=bgc)
        self._title.configure(background=bgc, fg=fg)
        self._hint.configure(background=bgc, fg=hint)
        self._chev.configure(background=bgc,
                             fg=_PAL["select_bg"] if self._enabled
                             else _PAL["disabled"])
        _paint_ring(self)

    def retheme(self):
        self._paint()


class StepRow(ttk.Frame):
    """Home's guided-path step: a leading number chip, a title + one-line hint, an
    optional trailing tag chip ("Start here" / "Ready") and a chevron -- the whole row
    is the hit target. Like ActionRow, plus the step number and a highlightable ACCENT
    state, so Home can point at the single recommended next action. set_enabled(False)
    greys the row; set_accent(True, tag=...) promotes it."""

    def __init__(self, parent, number, title, hint, command):
        super().__init__(parent)
        self._command = command
        self._enabled = True
        self._accent = False
        self._tag = ""
        self._hovered = False

        self._row = tk.Frame(self, cursor="hand2")
        self._row.grid(row=0, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        self._row.columnconfigure(1, weight=1)          # the text block stretches
        px, py = gui_fonts.SP["m"], gui_fonts.SP["s"]

        self._num = tk.Label(self._row, text=str(number), font="MewSection",
                             width=2, cursor="hand2")
        self._num.grid(row=0, column=0, rowspan=2, padx=(px, px), pady=py,
                       ipadx=2, ipady=2)
        self._title = tk.Label(self._row, text=title, font="MewSection",
                               anchor="w", cursor="hand2")
        self._title.grid(row=0, column=1, sticky="w", pady=(py, 0))
        self._hint = tk.Label(self._row, text=hint, font="MewHint", anchor="w",
                              cursor="hand2", justify="left")
        self._hint.grid(row=1, column=1, sticky="w", pady=(0, py))
        # where the step stands ("Printer preset “X” loads at every start"):
        # shown only while there is something to say, under the hint
        self._number = str(number)
        self._done = False
        self._state = tk.Label(self._row, text="", font="MewHint", anchor="w",
                               cursor="hand2", justify="left")
        self._state.grid(row=2, column=1, sticky="w", pady=(0, py))
        self._state.grid_remove()
        self._tagl = tk.Label(self._row, text="", font="MewHint", padx=6, pady=1,
                              cursor="hand2")
        self._tagl.grid(row=0, column=2, rowspan=3, sticky="e",
                        padx=(gui_fonts.SP["s"], 0))
        self._tagl.grid_remove()
        self._chev = tk.Label(self._row, text="→", font="MewTitle",
                              cursor="hand2")
        self._chev.grid(row=0, column=3, rowspan=3, sticky="e",
                        padx=(gui_fonts.SP["s"], px))
        self._num.grid_configure(rowspan=3)

        for w in (self._row, self._num, self._title, self._hint, self._state,
                  self._tagl, self._chev):
            w.bind("<Button-1>", self._on_click)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
        _keyboard_row(self)
        _REGISTRY.add(self)
        self.retheme()

    def set_enabled(self, flag):
        self._enabled = bool(flag)
        cur = "hand2" if self._enabled else "arrow"
        self._row.configure(takefocus=1 if self._enabled else 0)
        for w in (self._row, self._num, self._title, self._hint, self._state,
                  self._tagl, self._chev):
            w.configure(cursor=cur)
        self._paint()

    def set_state(self, done=False, text=""):
        """Mark the step done (its number becomes a ✓) and put one line under
        the hint saying where it stands; "" hides that line."""
        self._done = bool(done)
        self._num.configure(text="✓" if self._done else self._number)
        self._state.configure(text=text)
        if text:
            self._state.grid()
        else:
            self._state.grid_remove()
        self._paint()

    def set_accent(self, flag, tag=""):
        """Promote (True) or demote (False) this step to the recommended one.
        `tag` is the chip shown at the right edge when promoted ("" hides it)."""
        self._accent = bool(flag)
        self._tag = tag if flag else ""
        if self._tag:
            self._tagl.configure(text=self._tag)
            self._tagl.grid()
        else:
            self._tagl.grid_remove()
        self._paint()

    def _on_click(self, _e=None):
        if self._enabled and self._command is not None:
            self._command()

    def _on_enter(self, _e=None):
        self._hovered = True
        self._paint()

    def _on_leave(self, _e=None):
        self._hovered = False
        self._paint()

    def _paint(self):
        _paint_ring(self)
        p = _PAL
        if not self._enabled:
            base = p["bg"]
            self._num.configure(background=p.get("active", base),
                                fg=p["disabled"])
            for w in (self._row, self._title, self._hint, self._state,
                      self._chev, self._tagl):
                w.configure(background=base)
            for w, fg in ((self._title, p["disabled"]),
                          (self._hint, p["disabled"]),
                          (self._chev, p["disabled"])):
                w.configure(fg=fg)
            return
        # enabled: the accented step gets a soft wash + a filled number/tag chip
        if self._accent:
            base = p.get("active", p["hover"]) if self._hovered else p["hover"]
            self._num.configure(background=p["select_bg"], fg=p["select_fg"])
        else:
            base = p["hover"] if self._hovered else p["bg"]
            self._num.configure(background=p.get("active", base),
                                fg=p.get("st_ok", p["hint"]) if self._done
                                else p["hint"])
        self._row.configure(background=base)
        self._title.configure(background=base, fg=p["fg"])
        self._hint.configure(background=base, fg=p["hint"])
        self._state.configure(background=base,
                              fg=p.get("st_ok", p["fg"]) if self._done
                              else p["fg"])
        self._chev.configure(background=base, fg=p["select_bg"])
        self._tagl.configure(background=p["select_bg"], fg=p["select_fg"])

    def retheme(self):
        self._paint()
