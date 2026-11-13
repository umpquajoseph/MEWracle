#!/usr/bin/env python3
"""gui_form.py -- MODERN-UI: the one way a page lays out a parameter row.

Every card on every page of the modern shell builds its parameter rows through a
`Form`, so a new user opening any card reads the same software. The pattern it
enforces, and the reason for each rule, is:

  1. The unit lives in the label: ``Speed (mm/min):``. Pass ``unit=`` and the
     builder writes it; never a separate "mm" label beside the field.
  2. Help is a hover tooltip on the label AND the field (``tip=``). No grey text
     under or beside a parameter. Grey text survives only as `intro()` -- one line
     at the top of a card -- and as `live()`, a computed value or warning that a
     tooltip cannot carry because it changes while you type.
  3. One grid per card. Column 0 is the label column (one left edge, as wide as
     THIS card's longest label and no wider -- `_CardLabels`), column 1 the field
     column (one left edge, one width per kind), column 2 the tail (a second
     small field, a button). Column 3 is empty and takes a stretching card's
     spare width, so a tail always sits beside its field. A row of another kind -- a file picker, a
     dropdown, a checkbox, an embedded graph -- starts on those same edges.
  4. An indent means "this row belongs to the toggle above it" and nothing else.
     One step, `INDENT`, passed as ``indent=1``. Never pad a row in by hand.
  5. Checkboxes start on the label edge, with the same padding everywhere.
  6. One vertical rhythm: every row gets the same `PAD`, every `section()` the
     same air above it.

The builder tags every widget it makes (``widget._form = kind``).
An input control, or a label inside a card, that carries no tag is a row laid
out by hand. A
control that is not a parameter row (a search box, a table filter) is marked with
`exempt()` and a reason, which the guard reads too.

Rows are gridded straight into the caller's frame, not wrapped in a frame of their
own, so the columns line up across every row of a card and the Ctrl+F finder
(`gui_finder._nearest_pair`) finds each field's label in the column to its left.
`Row.widgets` is every widget of the row, for `_collector_register` or a
show/hide of the whole row.
"""

import tkinter as tk
from tkinter import ttk

import gui_fonts
import gui_tips
import gui_units

PAD = dict(gui_fonts.PAD)               # every row: padx 8, pady 4
INDENT = gui_fonts.INDENT               # the ONE indent step (px)
SECTION_GAP = gui_fonts.SP["l"]         # air above every section() but the first

# one width per kind of field, in characters
WIDTH = {"num": 10, "text": 24, "combo": 22, "path": 40, "small": 5,
         "coord": 8}

# the attribute every builder-made widget carries; the guard reads it
TAG = "_form"


def _tag(w, kind):
    setattr(w, TAG, kind)
    return w


def show_placeholder(entry, var, text):
    """Grey `text` inside `entry` while its value is blank and it is not
    being typed in. For a field whose blank MEANS something (a default): an
    empty box read as missing. The value stays blank, so a design, its
    presets and its g-code are what they were; clicking the grey text types
    in the box."""
    def colours():
        st = ttk.Style(entry)
        return (st.lookup("TEntry", "fieldbackground") or "white",
                st.lookup("Hint.TLabel", "foreground") or "grey")

    bg, fg = colours()
    lbl = _tag(tk.Label(entry, text=text, bg=bg, fg=fg, bd=0, padx=0, pady=0,
                        font=entry.cget("font") or "TkTextFont"), "live")

    def sync(*_a):
        try:
            blank = not var.get().strip()
            try:
                typing = entry.focus_get() is entry
            except KeyError:            # focus in a popdown menu
                typing = False
            if blank and not typing:
                lbl.place(x=4, rely=0.5, anchor="w")
            else:
                lbl.place_forget()
        except tk.TclError:             # the form has closed
            pass

    def recolour(_e=None):
        try:
            b, f = colours()
            lbl.configure(bg=b, fg=f)
        except tk.TclError:
            pass

    trace = var.trace_add("write", sync)

    def gone(e):
        if e.widget is entry:
            try:
                var.trace_remove("write", trace)
            except (tk.TclError, ValueError):
                pass

    entry.bind("<FocusIn>", sync, add="+")
    entry.bind("<FocusOut>", sync, add="+")
    entry.bind("<<ThemeChanged>>", recolour, add="+")
    entry.bind("<Destroy>", gone, add="+")
    lbl.bind("<Button-1>", lambda _e: entry.focus_set())
    sync()
    return lbl


def exempt(widget, reason):
    """Mark a control that is NOT a parameter row (a search box, a table filter,
    a log pane) so the guard test lets it through. `reason` is required and is
    what the test prints when it lists the exemptions."""
    if not reason:
        raise ValueError("gui_form.exempt needs a reason")
    widget._form_exempt = reason
    return widget


def label_text(text, unit=None):
    """``"Speed", "mm/min"`` -> ``"Speed (mm/min):"``. A trailing colon on `text`
    is tolerated and not doubled; an empty text stays empty."""
    t = (text or "").rstrip()
    if t.endswith(":"):
        t = t[:-1].rstrip()
    if not t:
        return ""
    if unit:
        t = "%s (%s)" % (t, unit)
    return t + ":"


class _CardLabels:
    """The label column of ONE card, shared by every Form gridded inside it.

    A card may grid its rows in several frames (`framed` blocks that show and
    hide per collector mode), and each frame is its own grid, so their label
    columns only line up if they are given the same width. That width is the
    longest label the card is SHOWING, and nothing more: a fixed page-wide
    minimum put a short-label card's fields far from their labels, and counting
    a hidden collector mode's rows let the 4-axis block's longest label push
    the 3-axis fields out too. A block shown or hidden re-measures the card.

    A card is found by walking up from a Form's frame to the nearest widget
    already keyed here, stopping at a ModuleGrid card body (``_form_card``),
    which is always a card of its own even under a page-level Form; a Form
    with neither above it keys its own frame (or, from `framed`, the parent it
    was gridded into, so sibling blocks share one)."""

    _by_path = {}

    def __init__(self, key):
        self.key = key
        self.frames = []
        self.labels = []
        self.width = 0
        self._pending = False
        _CardLabels._by_path[str(key)] = self
        key.bind("<Destroy>", self._forget, add="+")

    @classmethod
    def of(cls, frame, anchor=None):
        w = frame
        while w is not None:
            g = cls._by_path.get(str(w))
            if g is not None and g.key is w:
                return g
            if getattr(w, "_form_card", False):
                return cls(w)
            w = w.master
        return cls(anchor if anchor is not None else frame)

    def _forget(self, event):
        if event.widget is self.key                 and _CardLabels._by_path.get(str(self.key)) is self:
            del _CardLabels._by_path[str(self.key)]

    def add_frame(self, frame):
        self.frames.append(frame)
        frame.columnconfigure(0, minsize=self.width)
        # a block shown or hidden (a collector mode, a toggle's rows)
        for ev in ("<Map>", "<Unmap>"):
            frame.bind(ev, lambda _e: self.schedule(), add="+")

    def add_label(self, lbl):
        self.labels.append(lbl)
        # re-worded (the collector mode's wording), shown or hidden
        for ev in ("<Configure>", "<Map>", "<Unmap>"):
            lbl.bind(ev, lambda _e: self.schedule(), add="+")
        self.schedule()

    def shown(self, w):
        """Is `w` laid out within the card -- neither it nor a frame between
        it and the card grid_remove'd / pack_forget'd?"""
        while w is not None and w is not self.key:
            if not w.winfo_manager():
                return False
            w = w.master
        return True

    def schedule(self):
        if not self._pending:
            self._pending = True
            try:
                self.key.after_idle(self.resize)
            except tk.TclError:
                self._pending = False

    def resize(self):
        self._pending = False
        need = 0
        live = []
        for lbl in self.labels:
            try:
                if not lbl.winfo_exists():
                    continue
                left = int(str(lbl.grid_info().get("padx", 0)).split()[0])
            except (tk.TclError, ValueError, IndexError):
                continue
            live.append(lbl)
            if self.shown(lbl):
                need = max(need, lbl.winfo_reqwidth() + left + PAD["padx"])
        self.labels = live
        if need == 0 or need == self.width:
            return
        self.width = need
        frames = []
        for fr in self.frames:
            try:
                fr.columnconfigure(0, minsize=need)
                frames.append(fr)
            except tk.TclError:
                pass
        self.frames = frames


def mark(widget, kind):
    """Tag a widget made outside a Form as builder-made -- for a composite
    control a Row hosts (``row.add_widget``) whose children are plain ttk."""
    return _tag(widget, kind)


def page_intro(body, text, info=None, title=None, row=1, wrap=None):
    """A page's one grey intro line under its title, with an optional ⓘ for
    the longer story. Gridded at `row` of the page body; returns the frame.
    The one grey line a PAGE may carry (a card's is `Form.intro`)."""
    fr = _tag(ttk.Frame(body), "intro")
    fr.grid(row=row, column=0, sticky="w", pady=(2, gui_fonts.GAP_SECTION))
    kw = {"wraplength": wrap} if wrap else {}
    _tag(ttk.Label(fr, style="Hint.TLabel", justify="left", text=text, **kw),
         "intro").pack(side="left")
    if info:
        _tag(gui_tips.info_dot(fr, info, title=title), "intro").pack(
            side="left", padx=(gui_fonts.SP["s"], 0))
    return fr


def framed(parent, row, stretch=False, column=0, columnspan=4):
    """A sub-frame gridded FLUSH (no padding) at `row` of `parent`, and a Form
    on it -- for a block of rows that shows and hides as one, such as one
    collector mode's rows. Returns ``(frame, form)``.

    Blocks in different frames still share the card's edges, because every
    Form in one card is given the same label-column width (`_CardLabels`) and
    every block sits flush at x = 0."""
    fr = ttk.Frame(parent)
    fr.grid(row=row, column=column, columnspan=columnspan,
            sticky=("ew" if stretch else "w"))
    return fr, Form(fr, stretch=stretch, _card=parent)


class Row:
    """One laid-out row. `label` and `field` may be None (a checkbox row has no
    separate label); `widgets` holds everything the row gridded, tail included,
    in grid order."""

    def __init__(self, form, r, indent):
        self.form = form
        self.row = r
        self.indent = indent
        self.label = None
        self.field = None
        self.widgets = []
        self.tips = []
        self.sublabels = []
        self._tail = None
        self._inline = None

    # -- tooltips ---------------------------------------------------------
    def set_tip(self, text):
        """Change the hover text of every tooltip on this row (a row whose
        meaning follows the collector mode)."""
        for t in self.tips:
            t.text = text

    def _tip(self, w, text):
        if text:
            self.tips.append(gui_tips.Tooltip(w, text))

    # -- visibility -------------------------------------------------------
    def grid_remove(self):
        for w in self.widgets:
            w.grid_remove()

    def grid(self):
        for w in self.widgets:
            w.grid()

    def show(self, flag):
        (self.grid if flag else self.grid_remove)()

    # -- extra widgets on the same row -----------------------------------
    def _tail_frame(self):
        if self._tail is None:
            self._tail = _tag(ttk.Frame(self.form.frame), "tail")
            self._tail.grid(row=self.row, column=2, sticky="w", pady=PAD["pady"])
            self.widgets.append(self._tail)
        return self._tail

    def _host(self):
        # a group row puts its controls in the FIELD column; any other row puts
        # extras in the tail column after its field
        return self._inline if self._inline is not None else self._tail_frame()

    def _gap(self, host, but=None):
        kids = [w for w in host.winfo_children() if w is not but]
        return (0, 0) if not kids else (gui_fonts.SP["m"], 0)

    def add_label(self, text, unit=None, tip=None):
        """A sub-label inside the row (``X (mm):`` before a coordinate field)."""
        host = self._host()
        lbl = _tag(ttk.Label(host, text=label_text(text, unit)), "sublabel")
        lbl.pack(side="left", padx=self._gap(host, but=lbl))
        self._tip(lbl, tip)
        self.sublabels.append(lbl)
        return lbl

    def add_entry(self, label, var, tip, unit=None, width="num", **kw):
        """A second field on the row, with its own sub-label (may be "")."""
        label, unit = gui_units.split_label(label, unit)
        host = self._host()
        lbl = self.add_label(label, unit, tip) if label else None
        e = _tag(ttk.Entry(host, textvariable=var,
                           width=WIDTH.get(width, width), **kw), "field")
        e.pack(side="left", padx=(gui_fonts.SP["xs"] if label
                                  else self._gap(host, but=e)[0], 0))
        self._tip(e, tip)
        # display units (gui_units): a speed, pressure or fiber-diameter field
        # shows the Settings panel's unit; its var keeps the canonical one
        gui_units.bind(e, var, unit, lbl, label, relabel=label_text)
        return e

    def add_combo(self, label, var, values, tip, unit=None, width="combo",
                  readonly=True, **kw):
        host = self._host()
        if label:
            self.add_label(label, unit, tip)
        c = _tag(ttk.Combobox(host, textvariable=var, values=list(values),
                              width=WIDTH.get(width, width),
                              state=("readonly" if readonly else "normal"),
                              **kw), "field")
        c.pack(side="left", padx=(gui_fonts.SP["xs"] if label
                                  else self._gap(host, but=c)[0], 0))
        self._tip(c, tip)
        return c

    def add_check(self, text, var, tip, **kw):
        host = self._host()
        cb = _tag(ttk.Checkbutton(host, text=text, variable=var, **kw), "check")
        cb.pack(side="left", padx=self._gap(host, but=cb))
        self._tip(cb, tip)
        return cb

    def add_radio(self, text, var, value, tip, **kw):
        host = self._host()
        rb = _tag(ttk.Radiobutton(host, text=text, variable=var, value=value,
                                  **kw), "radio")
        rb.pack(side="left", padx=self._gap(host, but=rb))
        self._tip(rb, tip)
        return rb

    def add_button(self, text, command, tip=None, **kw):
        host = self._host()
        b = _tag(ttk.Button(host, text=text, command=command, **kw), "button")
        b.pack(side="left", padx=self._gap(host, but=b))
        self._tip(b, tip)
        return b

    def add_widget(self, widget, tip=None, kind="slot"):
        """Any widget already made with `row.host()` as its parent."""
        _tag(widget, kind)
        widget.pack(side="left", padx=self._gap(widget.master, but=widget))
        self._tip(widget, tip)
        return widget

    def host(self):
        """The frame `add_widget` expects its widget to be made in."""
        return self._host()


class Form:
    """The rows of one card, gridded into `frame` (a plain ttk.Frame the caller
    owns and places). `row` is the first grid row to use; `stretch=True` lets
    file rows grow with the card."""

    def __init__(self, frame, row=0, stretch=False, _card=None):
        self.frame = frame
        self.row = row
        self._first = True
        self.stretch = stretch
        self._labels = _CardLabels.of(frame, _card)
        self._labels.add_frame(frame)
        if stretch:
            # the spare width goes to an EMPTY fourth column, so a tail stays
            # beside its field; only a file row spans into it and stretches
            frame.columnconfigure(3, weight=1)

    # -- internals --------------------------------------------------------
    def _next(self, row):
        if row is None:
            row = self.row
        self.row = max(self.row, row + 1)
        return row

    @staticmethod
    def _lpad(indent):
        return (PAD["padx"] + INDENT * indent, PAD["padx"])

    def _start(self, text, unit, tip, indent, row):
        r = self._next(row)
        rw = Row(self, r, indent)
        self._first = False
        if text is not None:
            lbl = _tag(ttk.Label(self.frame, text=label_text(text, unit)),
                       "label")
            lbl.grid(row=r, column=0, sticky="w", padx=self._lpad(indent),
                     pady=PAD["pady"])
            rw.label = lbl
            rw.widgets.append(lbl)
            rw._tip(lbl, tip)
            self._labels.add_label(lbl)
        return rw

    def _place(self, rw, w, tip, sticky="w"):
        w.grid(row=rw.row, column=1, sticky=sticky, padx=PAD["padx"],
               pady=PAD["pady"])
        rw.field = w
        rw.widgets.append(w)
        rw._tip(w, tip)
        return rw

    # -- the row kinds ----------------------------------------------------
    def entry(self, label, var, tip, unit=None, width="num", indent=0,
              row=None, placeholder=None, **kw):
        """A typed value. `width` is a kind ("num", "text", "path", "small")
        or a character count.

        A unit written into `label` ("Speed (mm/min):") is read as if passed
        in `unit`, and the label comes out the same. A speed, pressure or
        fiber-diameter field shows the Settings panel's display unit
        (gui_units); its var keeps the canonical one.

        `placeholder` is what a BLANK field stands for (a default): it shows
        greyed in the empty box (`placeholder`)."""
        label, unit = gui_units.split_label(label, unit)
        rw = self._start(label, unit, tip, indent, row)
        e = _tag(ttk.Entry(self.frame, textvariable=var,
                           width=WIDTH.get(width, width), **kw), "field")
        gui_units.bind(e, var, unit, rw.label, label, relabel=label_text)
        if placeholder:
            show_placeholder(e, var, placeholder)
        return self._place(rw, e, tip)

    def spin(self, label, var, tip, from_, to, increment=1, unit=None,
             width="num", indent=0, row=None, **kw):
        rw = self._start(label, unit, tip, indent, row)
        s = _tag(ttk.Spinbox(self.frame, textvariable=var, from_=from_, to=to,
                             increment=increment,
                             width=WIDTH.get(width, width), **kw), "field")
        return self._place(rw, s, tip)

    def combo(self, label, var, values, tip, unit=None, width="combo",
              readonly=True, indent=0, row=None, **kw):
        rw = self._start(label, unit, tip, indent, row)
        c = _tag(ttk.Combobox(self.frame, textvariable=var, values=list(values),
                              width=WIDTH.get(width, width),
                              state=("readonly" if readonly else "normal"),
                              **kw), "field")
        return self._place(rw, c, tip)

    def check(self, text, var, tip, indent=0, row=None, inline=False, **kw):
        """A toggle. It starts on the LABEL edge and spans the row; rows that
        belong to it pass ``indent=1``. ``inline=True`` lets the row carry the
        toggle's own value after it (``row.add_entry``) on the same line."""
        rw = self._start(None, None, None, indent, row)
        host = self.frame
        if inline:
            host = _tag(ttk.Frame(self.frame), "group")
            host.grid(row=rw.row, column=0, columnspan=4, sticky="w",
                      padx=self._lpad(indent), pady=PAD["pady"])
            rw.widgets.append(host)
            rw._inline = host
        cb = _tag(ttk.Checkbutton(host, text=text, variable=var, **kw),
                  "check")
        if inline:
            cb.pack(side="left")
        else:
            cb.grid(row=rw.row, column=0, columnspan=4, sticky="w",
                    padx=self._lpad(indent), pady=PAD["pady"])
        rw.field = cb
        rw.widgets.append(cb)
        rw._tip(cb, tip)
        return rw

    def file(self, label, var, tip, command, button="Browse…", indent=0,
             row=None, **kw):
        """A path: a stretching entry and a Browse button in the tail."""
        rw = self._start(label, None, tip, indent, row)
        inner = _tag(ttk.Frame(self.frame), "group")
        inner.grid(row=rw.row, column=1, columnspan=3, sticky="ew",
                   padx=PAD["padx"], pady=PAD["pady"])
        rw.widgets.append(inner)
        rw._inline = inner
        e = _tag(ttk.Entry(inner, textvariable=var, width=WIDTH["path"], **kw),
                 "field")
        e.pack(side="left", fill="x", expand=self.stretch)
        rw.field = e
        rw._tip(e, tip)
        rw.browse = rw.add_button(button, command)
        return rw

    def button(self, label, text, command, tip, indent=0, row=None, **kw):
        """A labelled action ("Nozzle tip:  [Pick in 3D…]")."""
        rw = self._start(label, None, tip, indent, row)
        b = _tag(ttk.Button(self.frame, text=text, command=command, **kw),
                 "button")
        return self._place(rw, b, tip)

    def group(self, label, tip, unit=None, indent=0, row=None):
        """A row whose field is several small controls in a line -- a point's
        X/Y/Z, a set of hide checkboxes. Add them with `row.add_*`; they start
        on the field edge."""
        rw = self._start(label, unit, tip, indent, row)
        inner = _tag(ttk.Frame(self.frame), "group")
        self._place(rw, inner, None)
        rw._inline = inner
        return rw

    def slot(self, label, widget, tip=None, unit=None, indent=0, row=None,
             sticky="w", span=1):
        """An odd widget -- an embedded graph, a custom editor -- in the field
        column. `widget` must be a child of `form.frame`. `label=None` lets it
        start on the label edge instead and span the row."""
        if label is None:
            r = self._next(row)
            self._first = False
            rw = Row(self, r, indent)
            if not hasattr(widget, TAG):
                _tag(widget, "slot")
            widget.grid(row=r, column=0, columnspan=4, sticky=sticky,
                        padx=self._lpad(indent), pady=PAD["pady"])
            rw.field = widget
            rw.widgets.append(widget)
            rw._tip(widget, tip)
            return rw
        rw = self._start(label, unit, tip, indent, row)
        if not hasattr(widget, TAG):
            _tag(widget, "slot")
        widget.grid(row=rw.row, column=1, columnspan=span, sticky=sticky,
                    padx=PAD["padx"], pady=PAD["pady"])
        rw.field = widget
        rw.widgets.append(widget)
        rw._tip(widget, tip)
        return rw

    # -- text that is not a parameter --------------------------------------
    def section(self, text, row=None, first=None, info=None, title=None):
        """A heading inside a card (``Section.TLabel``; the letter-spaced
        overline is a PAGE's group header, not a card's). Same air above it
        everywhere, none above the card's first row. `first` overrides the
        guess, for a heading whose Form is not the first thing in the card.

        `info` adds an ⓘ beside the heading, for a note about the whole section
        rather than one parameter; ``row.dot`` is it (``set_info_text`` works),
        and the heading and dot then share one tagged frame on the row."""
        r = self._next(row)
        if first is None:
            first = self._first
        pady = ((0 if first else SECTION_GAP), 0)
        rw = Row(self, r, 0)
        rw.dot = None
        if info:
            box = _tag(ttk.Frame(self.frame), "section")
            box.grid(row=r, column=0, columnspan=4, sticky="w",
                     padx=PAD["padx"], pady=pady)
            h = _tag(ttk.Label(box, text=text, style="Section.TLabel"),
                     "section")
            h.pack(side="left")
            rw.dot = _tag(gui_tips.info_dot(box, info, title=title), "section")
            rw.dot.pack(side="left", padx=(gui_fonts.SP["s"], 0))
            rw.widgets.append(box)
        else:
            h = _tag(ttk.Label(self.frame, text=text, style="Section.TLabel"),
                     "section")
            h.grid(row=r, column=0, columnspan=4, sticky="w",
                   padx=PAD["padx"], pady=pady)
            rw.widgets.append(h)
        self._first = False
        rw.label = h
        return rw

    def intro(self, text, row=None, wrap=560):
        """The ONE grey line allowed in a card: an intro at its top."""
        r = self._next(row)
        h = _tag(ttk.Label(self.frame, style="Hint.TLabel",
                           wraplength=wrap, justify="left"), "intro")
        # a unit named in the intro follows the display units (gui_units)
        gui_units.track(h, text)
        h.grid(row=r, column=0, columnspan=4, sticky="w", padx=PAD["padx"],
               pady=(0, PAD["pady"]))
        rw = Row(self, r, 0)
        rw.label = h
        rw.widgets.append(h)
        return rw

    def live(self, label=None, text="", textvariable=None, tip=None,
             indent=0, row=None, wrap=520, warn=False):
        """Grey text a tooltip cannot carry: a value computed while you type, or
        a warning. In the field column after `label`, or spanning the row when
        `label` is None."""
        kw = {"textvariable": textvariable} if textvariable is not None else {}
        lbl = _tag(ttk.Label(self.frame, text=text, style="Hint.TLabel",
                             wraplength=wrap, justify="left", **kw), "live")
        if warn:
            lbl._form_warn = True
        if label is None:
            return self.slot(None, lbl, tip, indent=indent, row=row)
        return self.slot(label, lbl, tip, indent=indent, row=row)
