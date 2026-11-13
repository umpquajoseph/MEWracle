#!/usr/bin/env python3
"""gui_finder.py -- MODERN-UI ONLY: the Ctrl+F control finder.

Several hundred controls live behind the top row's pages, module cards,
collapsible groups and notebook tabs, and until now the only way to find one was
to remember where it was. `_FinderMixin` adds a single keystroke that answers
"where do I set X": Ctrl+F opens an in-window palette, typing filters every
control in the program by its VISIBLE LABEL, and picking a result switches to
that page, opens whatever contains the control, scrolls it into view and marks
it in yellow -- and the mark stays until the user clicks off it, so the answer
is still on screen a minute later.

Three things make that possible and are worth knowing before changing this file.

**The index is built by WALKING THE LIVE WIDGET TREE, not from a registry.**
There is no list of controls anywhere in the app to read -- a control is a
builder call inside one of thirty `gui_*.py` modules -- and a registry would be a
second place every new field has to be added to, which is exactly the kind of
parity duty `test_ui_shell.py` exists to catch people forgetting. So the finder
reads what is actually on screen. A new field added anywhere is findable with no
edit here. The cost is that a label has to be DERIVED for an unlabelled input
(`_nearest_label`), and that the walk is rebuilt on every Ctrl+F rather than
once, because the tree changes with the collector mode, the scaffold pill and
the selected calibration protocol.

**Nothing is lazily built, so the walk really does see the whole program.**
`gui_shell._build_widgets` constructs every page eagerly and `gui_pagestack`
keeps them all mapped; a page the user has never visited is fully present in the
tree. What is NOT present to the eye is content inside a shut `Disclosure`, an
unopened `ModuleCard` or an unselected notebook tab -- all three keep their
widgets and hide them with `grid_remove()`. Those are indexed with a REVEAL
chain: a list of callables that, run in order, put the control on screen.

**A control the finder cannot reveal is still listed, and says why.** The
collector toggle (`_collector_register`) hides whole subtrees belonging to a
different collector -- a tube's wall thickness simply does not exist on a flat
plate. Revealing one would silently change the user's design, so the finder does
not: the row is listed last with the switch it needs named on it ("needs
Collector: Mandrel"), and picking it navigates to the page and says so in the
status line. The one hidden subtree skipped outright is scaffold 2..5's copy of
the per-scaffold fields, because those are exact label-for-label duplicates of
the selected scaffold's and would quintuple every result.

The finder is a modern-shell feature: the classic developer UI has a visible tab
strip and no sidebar, and `_FinderMixin` is only mixed into `ModernApp`.
"""

import re
import tkinter as tk
from tkinter import ttk
from functools import partial

import gui_widgets

# A label longer than this is a hint paragraph, not a control name.
_MAX_LABEL = 70

# Glyph-only labels that name nothing (the info dot, the chevrons, a swatch).
_GLYPH_ONLY = re.compile(r"^[^\w]*$")

# ttk/tk classes that carry their own visible label.
_SELF_LABELLED = {"TButton", "Button", "TCheckbutton", "Checkbutton",
                  "TRadiobutton", "Radiobutton", "TMenubutton", "Menubutton"}

# ...and the ones whose name is a sibling Label.
_NEEDS_LABEL = {"TEntry", "Entry", "TCombobox", "TSpinbox", "Spinbox",
                "TScale", "Scale", "Listbox", "Treeview"}

_LABEL_CLASSES = {"TLabel", "Label"}


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
def _untrack(text):
    """Undo `gui_widgets._track`'s fake small-caps letterspacing, which stores
    "Get started" as G-thin-E-thin-T-thin-space-thin-S-thin-T-A-R-T-E-D.

    The separator is a THIN SPACE (U+2009), not an ASCII one -- an ASCII-space pattern
    here silently did nothing and put "G E T S T A R T E D" in the results -- and
    the word break survives as thin-space-space-thin, which the whitespace
    collapse afterwards turns back into one space."""
    return re.sub(r"\s+", " ", re.sub(r"(?<=\S)\s(?=\S)", "", text)).strip()


def _clean(text):
    """A control label as a human would say it: no trailing colon, no ellipsis,
    no leading icon glyph, whitespace collapsed."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = t.strip(" :….→")
    t = re.sub(r"^[^\w(]+\s*", "", t).strip()   # a leading emoji, as nav rows carry
    return t


def _widget_text(w):
    """The `text` of a widget, or "" if it has none, is a glyph, or is a
    paragraph. Section headers are un-letterspaced on the way out."""
    try:
        t = w.cget("text")
    except (tk.TclError, AttributeError):
        return ""
    if not isinstance(t, str) or not t.strip():
        return ""
    try:
        if str(w.cget("style")) == "SectionHeader.TLabel":
            t = _untrack(t)
    except (tk.TclError, AttributeError):
        pass
    t = _clean(t)
    if not t or len(t) > _MAX_LABEL or _GLYPH_ONLY.match(t):
        return ""
    return t


def _is_label(w):
    try:
        return w.winfo_class() in _LABEL_CLASSES
    except tk.TclError:
        return False


def _label_text(w, depth=0):
    """The label text of `w` -- or, when `w` is a small wrapper FRAME, of the
    first label inside it.

    That second half is not a nicety. `_build_scaffold_geometry_fields` grids a
    *frame* holding "Stepover (mm):" and its info dot in the column left of the
    entry, so the entry's grid neighbour is a Frame and not a Label; a
    Label-only test found nothing and left the whole flat-plate geometry grid --
    width, stepover, layers, print speed, every start coordinate -- out of the
    index while the mandrel grid beside it went in."""
    return _label_pair(w, depth)[0]


def _label_widget(w, depth=0):
    """The WIDGET `_label_text` read the text off, or None."""
    return _label_pair(w, depth)[1]


def _label_pair(w, depth=0):
    """`(text, widget)` -- the label text and the widget actually displaying it.

    The widget half is what the finder's mark is painted on, so it has to be
    the thing whose `text` option holds those words, never the wrapper frame
    they were found through."""
    if _is_label(w):
        return _widget_text(w), w
    if depth >= 2:
        return "", None
    try:
        kids = w.winfo_children()
    except tk.TclError:
        return "", None
    if len(kids) > 6:               # a form, not a label cell
        return "", None
    for k in kids:
        t, lw = _label_pair(k, depth + 1)
        if t:
            return t, lw
    return "", None


def _nearest_label(w, depth=0):
    """The visible caption for an input that carries none of its own.

    A widget may carry an explicit `_finder_label` attribute, and that always
    wins. It is for the handful of controls that are deliberately drawn WITHOUT
    a caption -- the collector SegmentedBar reads "3 axis / 4 axis"
    with no "Collector:" in front of it, so nothing in the tree says what the
    bar is called. This is a searchable name, not a new piece of on-screen text,
    and it has NO widget: a hit named that way cannot be marked on its text,
    because there is no text, and falls back to a band round the control.

    The forms in this app are built three ways, so this tries them in the order
    they actually occur: `gui_common._field` grids a Label sticky="e" in the
    column left of the entry; the flat rows pack a Label before the widget; and
    a few wrap the input in a frame of its own, which is why this climbs a level
    on a miss."""
    return _nearest_pair(w, depth)[0]


def _nearest_pair(w, depth=0):
    """`_nearest_label`, as `(text, widget)`. See that docstring."""
    explicit = getattr(w, "_finder_label", "")
    if explicit:
        return _clean(explicit), None
    m = getattr(w, "master", None)
    if m is None or depth > 3:
        return "", None
    try:
        info = w.grid_info()
    except tk.TclError:
        info = {}
    if info:
        row, col = int(info.get("row", 0)), int(info.get("column", 0))
        best = None
        for s_ in m.grid_slaves(row=row):
            if s_ is w or not _label_text(s_):
                continue
            try:
                sc = int(s_.grid_info().get("column", 0))
            except (tk.TclError, ValueError):
                continue
            if sc < col and (best is None or sc > best[0]):
                best = (sc, s_)
        if best is not None:
            return _label_pair(best[1])
        for r in range(row - 1, max(-1, row - 3), -1):   # column-major forms
            for s_ in m.grid_slaves(row=r, column=col):
                if _label_text(s_):
                    return _label_pair(s_)
    else:
        prev = ("", None)
        try:
            slaves = m.pack_slaves()
        except tk.TclError:
            slaves = []
        for s_ in slaves:
            if s_ is w:
                break
            if _label_text(s_):
                prev = _label_pair(s_)
        if prev[0]:
            return prev
    prev = ("", None)              # last resort inside this master
    try:
        for s_ in m.winfo_children():
            if s_ is w:
                break
            if _label_text(s_):
                prev = _label_pair(s_)
    except tk.TclError:
        pass
    return prev if prev[0] else _nearest_pair(m, depth + 1)


def _control_label(w):
    """`_nearest_label`, plus the row's own name when that label is a bare axis
    letter. The motion-limit table is "Printhead travel (mm): X [ ] - [ ]", so
    the label beside the entry is "X" and the six entries of a three-part table
    are indistinguishable; the frame one level up carries the name that tells
    them apart."""
    return _control_pair(w)[0]


def _control_pair(w):
    """`_control_label`, as `(text, widgets)` -- widgets PLURAL, because the
    composite name is read off two labels and the mark has to cover the words
    the user typed wherever they are. "Printhead travel (mm) X" marks both the
    table's name and the axis letter; anything else marks one."""
    lab, lw = _nearest_pair(w)
    m = getattr(w, "master", None)
    if lab and len(lab) <= 3 and m is not None:
        outer, ow = _nearest_pair(m, 1)
        if outer and outer != lab and len(outer) > 3:
            return f"{outer} {lab}", tuple(x for x in (ow, lw) if x is not None)
    return lab, tuple(x for x in (lw,) if x is not None)


# --------------------------------------------------------------------------- #
# one indexed control
# --------------------------------------------------------------------------- #
class Hit:
    """One findable control: what it is called, where it lives, how to show it."""

    __slots__ = ("label", "kind", "page_id", "page_label", "trail", "widget",
                 "page_frame", "reveal", "blocked", "aliases", "mark",
                 "resolve")

    def __init__(self, label, kind, page_id, page_label, trail, widget,
                 page_frame, reveal, blocked=None, aliases=(), mark=(),
                 resolve=None):
        self.label = label
        self.kind = kind
        self.page_id = page_id
        self.page_label = page_label
        self.trail = list(trail)
        self.widget = widget
        self.page_frame = page_frame
        self.reveal = list(reveal)
        self.blocked = blocked          # None, or the switch this control needs
        self.aliases = tuple(aliases)
        # The widget(s) actually DISPLAYING this hit's name -- what the mark is
        # painted on. Empty when the name is a `_finder_label` override or is
        # drawn by a canvas, and the mark then falls back to a band round
        # `widget`.
        self.mark = tuple(w for w in mark if w is not None)
        # For a control that exists only once its box is open (the G-code
        # window's Parameters table): called after `reveal`, it returns
        # (widget, marks) or None.
        self.resolve = resolve

    @property
    def where(self):
        return " › ".join([self.page_label] + self.trail)

    def haystack(self):
        return " ".join([self.label, self.where, *self.aliases]).casefold()

    def __repr__(self):                                 # the dump reads as this
        tail = f"  [{self.blocked}]" if self.blocked else ""
        return f"{self.label} ({self.kind}) -- {self.where}{tail}"


# --------------------------------------------------------------------------- #
# the walk
# --------------------------------------------------------------------------- #
def _collector_reason(app, widget, mode_labels):
    """If `widget` is hidden because it belongs to another collector, the words
    that name the switch; otherwise None."""
    key = str(widget)
    for w, mode in getattr(app, "_collector_widgets", ()):
        if str(w) != key:
            continue
        modes = mode if isinstance(mode, (tuple, list)) else (mode,)
        return "needs Collector: " + " / ".join(
            mode_labels.get(m, m) for m in modes)
    return None


def _skip_frames(app):
    """The per-scaffold frames of scaffolds the pill rail is NOT showing. Their
    labels duplicate the selected scaffold's exactly, so they are not indexed."""
    skip = set()
    cards = getattr(app, "_scaf_module_cards", None)
    if not cards:
        return skip
    try:
        cur = app._scaf_rail.index()
    except Exception:                                   # noqa: BLE001
        cur = 0
    for card in cards:
        for j, f in enumerate(getattr(card, "_scaf_frames", None) or ()):
            if j != cur:
                skip.add(str(f))
    return skip


def _mode_labels(app):
    """collector value -> the word the toggle shows for it, read off the live
    SegmentedBar so the finder's wording can never drift from the control's."""
    bar = getattr(app, "_collector_bar", None)
    out = {}
    for value, text in (getattr(bar, "_opts", None) or ()):
        out[value] = _clean(text)
    return out or {"plate": "Plate", "mandrel": "Mandrel"}


def index(app):
    """Every findable control in the modern shell, in top-row order."""
    hits = []
    skip = _skip_frames(app)
    modes = _mode_labels(app)
    _tag_calibrate(app)
    for pid, frame in getattr(app, "_page_order", ()):
        page_label = _clean(getattr(app, "_page_labels", {}).get(pid, pid))
        _walk(app, frame, hits, pid, page_label, frame, [], [], None,
              skip, modes)
    return _unique(_page_hits(app) + _gcode_hits(app) + hits
                   + _settings_hits(app))


#: a measurement grid's cell: the row's name and the cell's number
_CELL_NO = re.compile(r"\s+\d+$")


def _unique(hits):
    """One row per control the user can tell apart. A measurement grid
    yields a field per CELL, all named alike, so "sag" listed "Midspan sag
    (mm): one cell per reading of that span 12" ninety-three times; the
    first cell stands for its grid."""
    seen, out = set(), []
    for h in hits:
        name = _CELL_NO.sub("", h.label) if h.kind == "field" else h.label
        key = (name.casefold(), h.where.casefold(), h.kind, bool(h.blocked))
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


# --------------------------------------------------------------------------- #
# the G-code window: what the walk cannot see on its own
# --------------------------------------------------------------------------- #
#: Calibrate's cards (gui_calibration's CardBox keys) -> the ribbon panel
#: that shows each one in the G-code window's floating box
_CAL_CARD_PANEL = {"settings": "parameters", "stabilization": "base",
                   "instructions": "instructions", "fit": "measurements",
                   "presets": "presets"}


def _no_step():
    """The reveal of a container that a step beneath it opens."""


def _cal_open(app, card, kind=None):
    """Put a Calibrate form on screen: the mode, then the type, then the
    ribbon panel that holds the card."""
    if not getattr(app, "_cal_on", False):
        app._nav_select("calibration")
    if kind is not None and app._cal_type() != kind:
        app._cal_show_tab(kind)
    panel = _CAL_CARD_PANEL.get(card)
    if panel and getattr(app, "_gce_panel_key", None) != panel:
        app._gce_panel_open(panel)


def _tag_calibrate(app):
    """Give each Calibrate form its way onto the screen. Each sits in a card
    the ribbon shows one at a time, one frame per calibration type, so every
    one of them was indexed as "hidden by another setting" and picking it
    left the user on a closed window. Each frame now declares the step that
    shows it (`_finder_reveal`) and the words that say where it is
    (`_finder_crumb`)."""
    boxes = getattr(app, "_cal_boxes", None)
    if boxes is None or not hasattr(app, "_cal_show_tab"):
        return
    titles = getattr(app, "_GCE_PANEL_TITLES", {})
    try:
        procs = app._cal_procedures()
    except Exception:                                   # noqa: BLE001
        procs = {}
    per_type = set()
    for kind, parts in (getattr(app, "_cal_cards", None) or {}).items():
        tab = _clean(getattr(procs.get(kind), "tab", "") or kind)
        for card, frame in parts.items():
            frame._finder_reveal = partial(_cal_open, app, card, kind)
            frame._finder_crumb = (
                f"Calibrate: {tab} › "
                + titles.get(_CAL_CARD_PANEL.get(card), card.title()))
            per_type.add(card)
    for key, card in getattr(boxes, "cards", {}).items():
        if key not in _CAL_CARD_PANEL:
            continue
        card._finder_reveal = partial(_cal_open, app, key)
        # the box's own scroller is unmapped while the box is shut; the
        # card's step opens it, so it is not "hidden by another setting"
        w = card.master
        while w is not None and getattr(w, "_finder_reveal", None) is None:
            try:
                if w.winfo_manager() == "":
                    w._finder_reveal = _no_step
            except tk.TclError:
                break
            w = w.master
        if key not in per_type:
            card._finder_crumb = "Calibrate › " + titles.get(
                _CAL_CARD_PANEL[key], key.title())


def _gcode_page(app):
    for pid, frame in getattr(app, "_page_order", ()):
        if pid == "gcode":
            return frame
    return None


def _params_open(app):
    """Design's Parameters table, in its per-scaffold view."""
    if getattr(app, "_cal_on", False):
        app._nav_select("gcode")
    app._gce_open_scaffold_view()


def _param_cell(app, key):
    """The open Parameters table's cell for `key`, in the lit scaffold's
    column, focused, and the row's name beside it."""
    st = getattr(app, "_gcp", None)
    if not st or not st.get("shown"):
        return None
    gi = next((i for i, r in enumerate(app._gcp_rows(st)) if r[2] == key),
              None)
    c = st.get("hi") or 0
    cell = st["cells"].get((gi, c)) if gi is not None else None
    if cell is None:
        return None
    w = cell["w"]
    app._gcp_focus(st, gi, c)
    try:
        row = int(w.grid_info()["row"])
        names = st["lgrid"].grid_slaves(row=row, column=0)
    except (tk.TclError, KeyError, ValueError):
        names = []
    return w, tuple(names[:1])


#: words a researcher may search for a Parameters row by, beyond its label
_PARAM_ALIASES = {
    "stepover": ("stepover", "pore", "pore size", "pore width", "spacing",
                 "gap", "hatch"),
    "grade_on": ("gradient", "graded pore", "pore gradient", "graded",
                 "graded porosity", "porosity", "ramp", "grade"),
    "grade_dir": ("gradient direction", "ramp direction", "grade direction",
                  "across the layer", "up through the layers"),
    "grade_end": ("far edge", "top layer", "end spacing"),
    "grade_angle": ("gradient", "gradient direction", "ramp direction"),
}


def _gcode_hits(app):
    """The G-code window's Parameters table and its text Find.

    The table is built the first time it opens and destroyed with the page's
    box, so the walk never sees it, and "stepover" from anywhere but the
    legacy designer found nothing. Its rows are read from the table's own
    row list (`gui_gcode_params.PARAM_ROWS`, or the tube rows on a
    mandrel), so a row added there is findable with no edit here."""
    frame = _gcode_page(app)
    if frame is None or not hasattr(app, "_gce_open_scaffold_view"):
        return []
    import gui_gcode_params as gp
    page_label = _clean(getattr(app, "_page_labels", {}).get("gcode", "gcode"))
    out = []
    if hasattr(app, "_gce_find"):
        out.append(Hit("Find in the g-code", "command", "gcode", page_label,
                       [], None, frame, [app._gce_find],
                       aliases=("find text", "search the g-code", "replace",
                                "find and replace", "g-code text")))
    try:
        tube = bool(app._gcp_tube())
    except Exception:                                   # noqa: BLE001
        tube = False
    for grp, label, key, unit, kind in (gp.TUBE_PARAM_ROWS if tube
                                        else gp.PARAM_ROWS):
        if kind in ("out", "sketch"):
            continue                    # a readout, nothing to set
        if key == "diameter" and not tube:
            import gui_units
            unit = gui_units.shown("fiber_d", "mm")
        text = f"{label} ({unit})" if unit else label
        out.append(Hit(text, "field", "gcode", page_label,
                       ["Parameters", grp], None, frame,
                       [partial(_params_open, app)],
                       aliases=("parameter",) + _PARAM_ALIASES.get(key, ()),
                       resolve=partial(_param_cell, app, key)))
    return out


def _page_hits(app):
    """Every page itself, so "printer link" or "log" opens that page rather
    than landing on a button somewhere that happens to share its name. Marked
    on its top-row button."""
    out = []
    btns = getattr(app, "_nav_btns", {})
    tab_of = getattr(app, "_nav_tab_of", {})
    for pid, frame in getattr(app, "_page_order", ()):
        label = _clean(getattr(app, "_page_labels", {}).get(pid, pid))
        if not label:
            continue
        btn = btns.get(tab_of.get(str(frame)))
        out.append(Hit(label, "page", pid, "Page", [], btn or frame, frame,
                       [], None, ("page",)))
    return out


#: what the Settings window holds, as the finder offers it:
#: (label, section id, words a user might type for it instead)
_SETTINGS_ROWS = (
    ("Settings", "general", ("preferences", "options")),
    ("Dark mode", "general", ("appearance", "theme", "night")),
    ("Perspective view", "general",
     ("orthographic", "projection", "camera", "depth")),
    ("Show G-code zero", "general",
     ("origin", "axes", "triad", "xyz", "zero")),
    ("Display units", "general",
     ("units", "speed", "pressure", "fiber diameter", "pore size",
      "fiber length", "volume", "print time", "mm/min", "mm/s", "psi",
      "bar", "kpa", "µm", "um")),
    ("Default presets", "general",
     ("startup", "default preset", "printer preset", "physics preset",
      "scaffold preset")),
    ("Save-as folder", "general", ("folder", "directory", "export folder")),
    ("G-code output", "output",
     ("export", "comments", "header", "strip comments", "decimals")),
    ("Quality", "quality",
     ("facets", "curve quality", "corner resolution", "preview quality",
      "stl quality", "mesh")),
)


def _settings_hits(app):
    """The Settings window's controls. The window is built only while it is
    open, so the walk never sees them: a user who typed "units" or "dark
    mode" was told nothing matched. Picking one opens Settings on its
    section, from whatever page is showing."""
    gear = getattr(app, "_settings_btn", None)
    opener = getattr(app, "open_settings", None)
    if gear is None or not callable(opener):
        return []
    try:
        frame = app.root.nametowidget(app.nb.select())
    except (AttributeError, tk.TclError, KeyError):
        frame = None
    have = getattr(app, "_settings_have_design", lambda: True)()
    names = {i: n for _h, rows in getattr(app, "SETTINGS_SECTIONS", ())
             for i, n in rows}
    out = []
    for label, sid, aliases in _SETTINGS_ROWS:
        if sid != "general" and not have:
            continue
        trail = [names.get(sid, sid)] if names.get(sid) != label else []
        out.append(Hit(label, "setting", None, "Settings", trail, gear, frame,
                       [partial(opener, section=sid)], None,
                       ("settings",) + aliases))
    return out


def _emit(hits, label, kind, ctx, widget, reveal, blocked, aliases=(),
          mark=()):
    label = _clean(label)
    if not label or _GLYPH_ONLY.match(label):
        return
    pid, page_label, page_frame, trail = ctx
    hits.append(Hit(label, kind, pid, page_label, trail, widget, page_frame,
                    reveal, blocked, aliases, mark))


def _walk(app, parent, hits, pid, page_label, page_frame, trail, reveal,
          blocked, skip, modes, head=None):
    """Index `parent`'s subtree.

    `head` is a one-element list holding the last `section_header` seen, and it
    is shared with every PLAIN frame below this one so that a header gridded in
    its own wrapper frame still names the fields gridded in the next wrapper
    along -- which is how `_build_scaffold_color_row` and most of the Machine
    page are laid out. A real container (a module card, a notebook tab, a
    disclosure) starts a fresh one, because its heading already scopes it."""
    if head is None:
        head = [None]
    try:
        children = parent.winfo_children()
    except tk.TclError:
        return
    base_reveal = reveal
    for child in children:
        if str(child) in skip:
            continue
        try:
            cls = child.winfo_class()
        except tk.TclError:
            continue
        ctx = (pid, page_label, page_frame,
               trail + ([head[0]] if head[0] else []))

        # A container that hides its siblings by STACKING them in one grid cell
        # and raising the chosen one cannot be recognised by class the way a
        # Notebook, a ModuleGrid or a Disclosure can -- there is no class, just
        # frames. The embedded viewer rail's tabs work exactly that way
        # (gui_embed._build_rail: one pane per tab, `tkraise` to switch), which
        # is why "Travels" on the Viewer page, indexed with an EMPTY reveal chain,
        # would land on the page with the Simulation tab still shut. Such a container
        # declares its own step instead, and the
        # whole subtree below it inherits it.
        own = getattr(child, "_finder_reveal", None)
        reveal = base_reveal + [own] if callable(own) else base_reveal

        # --- containers that hide their contents, each with its own reveal -- #
        if isinstance(child, gui_widgets.ModuleGrid):
            for key, card in list(child._cards):
                title = _clean(getattr(card, "_title_text", "") or "")
                sub_blocked = blocked or (
                    None if child._shown.get(key, True)
                    else _collector_reason(app, card, modes)
                    or "not available for this collector")
                if title:
                    _emit(hits, title, "module", ctx, card,
                          reveal + [child.close], sub_blocked,
                          mark=(getattr(card, "_title", None),))
                body = getattr(card, "body", None)
                if body is not None:
                    _walk(app, body, hits, pid, page_label, page_frame,
                          trail + ([title] if title else []),
                          reveal + [partial(child.open, key)], sub_blocked,
                          skip, modes)
            continue

        if isinstance(child, ttk.Notebook):
            for tab_id in child.tabs():
                try:
                    text = _clean(child.tab(tab_id, "text"))
                    tabf = child.nametowidget(tab_id)
                except tk.TclError:
                    continue
                _walk(app, tabf, hits, pid, page_label, page_frame,
                      trail + ([text] if text else []),
                      reveal + [partial(child.select, tab_id)], blocked,
                      skip, modes)
            continue

        if isinstance(child, gui_widgets.Disclosure):
            title = _label_text(child._title)
            open_it = partial(child.set_open, True)
            if title:
                _emit(hits, title, "group", ctx, child, reveal + [open_it],
                      blocked, mark=(_label_widget(child._title),))
            _walk(app, child.body, hits, pid, page_label, page_frame,
                  trail + ([title] if title else []), reveal + [open_it],
                  blocked, skip, modes)
            continue

        # --- composites that ARE one control ------------------------------- #
        if isinstance(child, gui_widgets.SegmentedBar):
            text, marks = _control_pair(child)
            _emit(hits, text, "toggle", ctx, child, reveal, blocked,
                  aliases=[t for _v, t in child._opts], mark=marks)
            continue

        if isinstance(child, (gui_widgets.ActionRow, gui_widgets.StepRow)):
            _emit(hits, _label_text(child._title), "action", ctx, child,
                  reveal, blocked, mark=(_label_widget(child._title),))
            continue

        if isinstance(child, gui_widgets.ToggleSwitch):
            # A Canvas that draws its own switch: the words "Travels" are on a
            # sibling Label, which is what gets marked.
            text, marks = _control_pair(child)
            _emit(hits, text, "toggle", ctx, child, reveal, blocked,
                  mark=marks)
            continue

        if isinstance(child, (gui_widgets.PillRail, gui_widgets.StatusChip)):
            continue               # a view selector / a readout, not a setting

        # --- a subtree hidden by something the finder must not undo -------- #
        child_blocked = blocked
        try:
            # not hidden by a setting when it declares the step that shows it
            if child.winfo_manager() == "" and not callable(own):
                child_blocked = blocked \
                    or _collector_reason(app, child, modes) \
                    or "hidden by another setting"
        except tk.TclError:
            pass

        # --- plain controls ------------------------------------------------ #
        if cls in _SELF_LABELLED:
            low = cls.lower()
            kind = "toggle" if "check" in low or "radio" in low else "button"
            override = getattr(child, "_finder_label", "")
            # Its own `text` IS the name, so the control is its own mark --
            # unless the name is an override, which is searchable but nowhere
            # on screen.
            _emit(hits, override or _widget_text(child), kind, ctx, child,
                  reveal, child_blocked,
                  mark=() if override else (child,))
        elif cls in _NEEDS_LABEL:
            text, marks = _control_pair(child)
            _emit(hits, text, "field", ctx, child, reveal, child_blocked,
                  mark=marks)
        elif _is_label(child):
            try:
                # Both heading styles: the letter-spaced overline, and
                # gui_form's `Form.section` (Section.TLabel), which is the one
                # every converted card uses. With only the first, the cards'
                # section names fell out of every breadcrumb.
                if str(child.cget("style")) in ("SectionHeader.TLabel",
                                                "Section.TLabel"):
                    text = _widget_text(child)
                    _emit(hits, text, "group",
                          (pid, page_label, page_frame, trail), child,
                          reveal, child_blocked, mark=(child,))
                    head[0] = text or None
            except tk.TclError:
                pass

        # Descend with a COPY of the section-header box, and adopt what the
        # child left in it only if the child was a header wrapper -- a frame
        # that contributed headings and no controls of its own. That is the
        # difference between `_build_scaffold_color_row`, which grids its
        # "Appearance" header in one frame and the colour row in the next (the
        # header has to reach across), and the linking-loops block, which holds
        # its header AND its fields (its header must NOT reach the mandrel and
        # curved-collector grids gridded after it).
        crumb = getattr(child, "_finder_crumb", None)
        if crumb:
            # a container that names itself scopes its own headings, as a
            # module card does
            _walk(app, child, hits, pid, page_label, page_frame,
                  trail + [crumb], reveal, child_blocked, skip, modes)
            continue
        before = len(hits)
        sub = [head[0]]
        _walk(app, child, hits, pid, page_label, page_frame, trail, reveal,
              child_blocked, skip, modes, sub)
        if all(h.kind == "group" for h in hits[before:]):
            head[0] = sub[0]


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
def search(hits, query, limit=40):
    """Rank `hits` against a query. Every whitespace-separated token has to
    appear somewhere in the label, the breadcrumb or an alias; a token matching
    the LABEL, and matching it at the start, sorts first. Controls the finder
    cannot reveal sort last, whatever they match."""
    q = query.casefold().strip()
    if not q:
        return []
    tokens = q.split()
    out = []
    seen = set()
    for h in hits:
        hay = h.haystack()
        if not all(t in hay for t in tokens):
            continue
        lab = h.label.casefold()
        if lab == q:
            rank = 0
        elif lab.startswith(q):
            rank = 1
        elif q in lab:
            rank = 2
        elif all(t in lab for t in tokens):
            rank = 3
        else:
            rank = 4
        key = (h.label, h.where)
        if key in seen:
            continue
        seen.add(key)
        # a page wins a tie with a control of the same name: "Printer link"
        # opens the page, not the Home button that leads to it
        out.append((bool(h.blocked), rank, h.kind != "page", len(h.label),
                    h.label.lower(), h))
    out.sort(key=lambda r: r[:5])
    return [r[5] for r in out[:limit]]


# --------------------------------------------------------------------------- #
# revealing and marking
# --------------------------------------------------------------------------- #
def _scroll_into_view(widget):
    """Scroll the VScrollFrame the control sits on until it is visible. A no-op
    on a page that does not scroll."""
    canvas = getattr(widget, "master", None)
    while canvas is not None and not isinstance(canvas, tk.Canvas):
        canvas = getattr(canvas, "master", None)
    if canvas is None:
        return
    body = next(iter(canvas.winfo_children()), None)
    if body is None:
        return
    try:
        canvas.update_idletasks()
        inner, view = body.winfo_height(), canvas.winfo_height()
        if inner <= view or inner <= 1:
            return
        y = widget.winfo_rooty() - body.winfo_rooty()
        frac = (y - view * 0.30) / float(inner)
        canvas.yview_moveto(max(0.0, min(1.0, frac)))
    except tk.TclError:
        pass


# The fallback band's thickness, for a hit whose name is nowhere on screen.
_RING_PX = 4

# The ink laid over the wash. Fixed in both themes rather than taken from the
# palette: the wash is the same yellow in both, so dark mode's near-white `fg`
# would be unreadable on it.
_MARK_FG = "#1c1e21"


def _place_bars(bars, widget):
    """Put the four fallback bars back around `widget` at its CURRENT geometry.

    Re-run on every <Configure> of the window, because the mark now outlives
    the layout that was standing when it was made: resize the window and four
    frames placed at the old coordinates are a yellow box around nothing."""
    try:
        x, y = widget.winfo_x(), widget.winfo_y()
        w, h = widget.winfo_width(), widget.winfo_height()
    except tk.TclError:
        return
    t = _RING_PX
    for f, (bx, by, bw, bh) in zip(bars, ((x - t, y - t, w + 2 * t, t),
                                          (x - t, y + h, w + 2 * t, t),
                                          (x - t, y, t, h),
                                          (x + w, y, t, h))):
        try:
            f.place(x=bx, y=by, width=max(1, bw), height=max(1, bh))
            f.lift()
        except tk.TclError:
            pass


def _wash(widget, colour):
    """Lay a yellow wash behind `widget`'s own text, and return the callable
    that takes it off again -- or None if this widget cannot take one.

    Two mechanisms, because the shell mixes both toolkits. A `tk` widget owns
    its `-background` and `-foreground` outright, so those are set and put back.
    A `ttk` widget's colours belong to its STYLE, so it gets a private style of
    its own derived from whatever style it already had -- ttk resolves a name
    right to left, so `FinderMark42.Viewer.TLabel` inherits everything
    `Viewer.TLabel` defines and overrides only the two colours. Restoring means
    handing back the original style name, and "" is legal there: it means the
    widget's class default."""
    try:
        if isinstance(widget, ttk.Widget):
            was = str(widget.cget("style") or "")      # "" == the class default
            name = "FinderMark%d.%s" % (id(widget) & 0xffffff,
                                        was or widget.winfo_class())
            st = ttk.Style(widget)
            st.configure(name, background=colour, foreground=_MARK_FG)
            # `configure` alone is not enough. A ttk style's per-state MAP wins
            # over its base setting, and the themes map both of these for
            # `disabled`, `active` and `pressed` -- which is why washing the
            # Analyze page's "Apply to Calibration" did nothing at all: the
            # button is disabled until the analysis has run, so clam went on
            # painting it the disabled grey the inherited map asked for. Every
            # state is pinned to the wash for as long as the mark is up.
            states = ("disabled", "!disabled", "active", "pressed", "selected",
                      "focus", "readonly")
            st.map(name,
                   background=[(x, colour) for x in states],
                   foreground=[(x, _MARK_FG) for x in states],
                   fieldbackground=[(x, colour) for x in states])
            widget.configure(style=name)

            def undo(_w=widget, _s=was):
                try:
                    _w.configure(style=_s)
                except tk.TclError:
                    pass
            return undo
        old_bg = widget.cget("background")
        old_fg = widget.cget("foreground")
        widget.configure(background=colour, foreground=_MARK_FG)

        def undo(_w=widget, _bg=old_bg, _fg=old_fg):
            try:
                _w.configure(background=_bg, foreground=_fg)
            except tk.TclError:
                pass
        return undo
    except (tk.TclError, AttributeError):
        return None


def _within(widget, ancestor):
    """True if `widget` IS `ancestor` or sits inside it. Tk widget paths are
    dot-separated and prefix-structured, which is the cheapest way to ask."""
    if widget is ancestor:
        return True
    return str(widget).startswith(str(ancestor) + ".")


def _dismiss_on_click(app, event):
    if not marked(app):
        return
    w = getattr(event, "widget", None)
    # Clicking INTO the control the finder found -- or onto the marked words
    # themselves -- is the point of being sent there, so those clicks keep the
    # mark; any other click clears it.
    keep = [getattr(app, "_finder_ring_widget", None)]
    keep.extend(getattr(app, "_finder_mark_widgets", None) or ())
    if w is not None and any(t is not None and _within(w, t) for t in keep):
        return
    clear_ring(app)


def _dismiss_on_page(app):
    if not marked(app):
        return
    nb = getattr(app, "nb", None)
    if nb is None:
        return
    try:
        here = nb.select()
    except tk.TclError:
        clear_ring(app)
        return
    if here != getattr(app, "_finder_ring_page", None):
        clear_ring(app)


def _reflow(app):
    # Only the fallback band is placed by hand; a wash rides its own widget.
    bars = getattr(app, "_finder_ring", None)
    target = getattr(app, "_finder_ring_widget", None)
    if bars and target is not None:
        _place_bars(bars, target)


def _install_dismiss(app):
    """Bind the three dismissal handlers ONCE per app, not once per mark.

    They live for the life of the window and do nothing while no mark is up,
    rather than being bound and unbound around each one. That is not tidiness:
    `Misc.unbind(sequence, funcid)` on Python 3.12 removes EVERY binding for
    that sequence on that widget, not just the funcid it is handed (fixed only
    in 3.13). A mark that unbound itself would therefore take the shell's own
    two <<NotebookTabChanged>> handlers with it (gui_shell.py:343-344) and the
    top row would silently stop following the page."""
    if getattr(app, "_finder_dismiss_bound", False):
        return
    root = getattr(app, "root", None)
    if root is None:
        return
    # <Button-1> on the toplevel sees clicks on every descendant: a child's
    # bindtags are (widget, class, toplevel, all), so the event reaches here
    # after the widget's own handlers have had it.
    root.bind("<Button-1>", lambda e: _dismiss_on_click(app, e), add="+")
    root.bind("<Configure>", lambda _e: _reflow(app), add="+")
    nb = getattr(app, "nb", None)
    if nb is not None:
        nb.bind("<<NotebookTabChanged>>", lambda _e: _dismiss_on_page(app),
                add="+")
    app._finder_dismiss_bound = True


def ring(app, widget, marks=()):
    """Mark the finder's landing and LEAVE IT THERE, until the user clicks
    somewhere other than the control or leaves the page.

    **The mark goes on the WORDS, not on the box.** `marks` is the widget or
    widgets displaying the hit's name -- the caption beside a field, a button's
    own text, the Label next to a canvas switch -- and each gets a yellow wash
    behind its text, the way find-on-a-page has worked everywhere for twenty
    years. Ringing the control instead was wrong in three ways at once, all of
    them reported together: on the Analyze page the band sat on a hint fragment
    a row below the button it claimed to have found; on the Viewer rail it drew
    a sliver next to "Travels", because the switch there is a bare Canvas about
    four pixels wide and the word is on a different widget; and even when it
    landed correctly it outlined a box while the user was reading for a word.
    A wash needs no geometry, so it cannot land in the wrong place, and it
    moves and scrolls with the text because it IS the text's background.

    **The band survives only as the fallback**, for a hit whose name is nowhere
    on screen: a `_finder_label` override exists to make a caption-less control
    searchable, and there is no text to wash. Four thin frames rather than one
    lowered behind the control, because a ttk widget does not necessarily paint
    its own background, so a frame underneath shows THROUGH it as a block of
    colour instead of around it, and a frame on top would cover the thing the
    user was sent to look at.

    Neither form blinks, and neither is the accent colour. Three blinks of
    `select_bg` over about a second and a half, then nothing, would leave a
    user who looks back at the window a moment after the jump no sign of where
    the finder took them, and while it was up, blue is what
    Tk paints a focused entry with, so the mark was indistinguishable from the
    focus ring the jump also leaves behind. Dismissal belongs to the user now,
    which is what lets the mark be permanent."""
    clear_ring(app)
    colour = gui_widgets.palette().get("find_hl", "#f2c200")

    undos = []
    for m in marks or ():
        u = _wash(m, colour)
        if u is not None:
            undos.append(u)
    if undos:
        app._finder_undo = undos
        app._finder_mark_widgets = tuple(marks)
        app._finder_ring_widget = widget
        _remember_page(app)
        _install_dismiss(app)
        return

    # nothing on screen carries the name: fall back to a band round the control
    try:
        widget.update_idletasks()
        w, h = widget.winfo_width(), widget.winfo_height()
        master = widget.master
    except (tk.TclError, AttributeError):
        return
    if w <= 1 or h <= 1 or master is None:
        return
    bars = []
    for _ in range(4):
        try:
            bars.append(tk.Frame(master, bg=colour, bd=0, highlightthickness=0))
        except tk.TclError:
            break
    if len(bars) != 4:                  # a partial box is worse than none
        for f in bars:
            try:
                f.destroy()
            except tk.TclError:
                pass
        return
    _place_bars(bars, widget)
    app._finder_ring = bars
    app._finder_ring_widget = widget
    _remember_page(app)
    _install_dismiss(app)


def _remember_page(app):
    try:
        app._finder_ring_page = app.nb.select()
    except (AttributeError, tk.TclError):
        app._finder_ring_page = None


def clear_ring(app):
    for undo in getattr(app, "_finder_undo", None) or ():
        undo()
    app._finder_undo = None
    app._finder_mark_widgets = None
    for f in getattr(app, "_finder_ring", None) or ():
        try:
            f.destroy()
        except tk.TclError:
            pass
    app._finder_ring = None
    app._finder_ring_widget = None
    app._finder_ring_page = None


def marked(app):
    """True while a mark is up, in either form. The one thing to ask; the two
    private attributes are an implementation detail of which form is in use."""
    return bool(getattr(app, "_finder_undo", None)
                or getattr(app, "_finder_ring", None))


# --------------------------------------------------------------------------- #
# the mixin
# --------------------------------------------------------------------------- #
def row_text(hit, budget=118):
    """One result line: the label, the page trail, and the switch it needs.

    Elided in the MIDDLE OF THE TRAIL rather than at the end. A tk.Listbox
    neither wraps nor scrolls sideways, so an over-long row is simply cut off at
    the right edge -- which is where the "(needs Collector: 4 axis)" note lives,
    the one part of the row a user cannot infer. Photographed at 660 px the rows
    for the Calibration page lost exactly that."""
    label, where = hit.label, hit.where
    tail = f"   ({hit.blocked})" if hit.blocked else ""
    fixed = len(label) + 6 + len(tail)
    room = budget - fixed
    if room < 12:                       # a very long label: keep the page only
        where = where.split(" › ")[0]
    elif len(where) > room:
        keep = max(4, (room - 3) // 2)
        where = where[:keep] + "…" + where[-(room - 3 - keep):]
    return f"{label}    —  {where}{tail}"


class _FinderMixin:
    """Ctrl+F over every control in the shell. Mixed into ModernApp only."""

    # Wide enough for a deep breadcrumb: "Calibration > Calibration settings >
    # Spacing, lead-in & base speed" plus a label plus the blocked note is ~110
    # characters, and a Listbox row that does not fit is cut off, not wrapped.
    FINDER_W = 940
    FINDER_ROWS = 9

    # -- the palette -------------------------------------------------------- #
    def _finder_page_id(self):
        """Registry id of the page on top ("gcode", "scaffold", ...), or None."""
        try:
            return self._nav_pid_of.get(self.nb.select())
        except (AttributeError, tk.TclError):
            return None

    def _finder_index(self):
        """The hits Ctrl+F may offer from where the user is. The Scaffold
        designer is the legacy form behind the Design tab: its controls are
        findable only from that page, or in the Legacy UI, where every page is
        the legacy form. From anywhere else a match there would pull the user
        out of the CAD window onto a page they did not ask for."""
        hits = index(self)
        if getattr(self, "_legacy_on", False)                 or self._finder_page_id() == "scaffold":
            return hits
        return [h for h in hits if h.page_id != "scaffold"]

    def open_finder(self, _event=None):
        """Show the finder, or re-focus and re-index it if it is already up.

        Ctrl+F is this finder on EVERY page, as Home says ("Ctrl+F  Find a
        setting"). It used to open the g-code text's Find on the G-code
        window, so the one page whose controls are hardest to locate was the
        one where the key did something else. The text's own Find is still
        Ctrl+F while the g-code text has focus (gui_gcode_editor binds it on
        the widget), and the finder offers it as "Find in the g-code"."""
        existing = getattr(self, "_finder_frame", None)
        if existing is not None and existing.winfo_exists():
            self._finder_hits = self._finder_index()
            self._finder_entry.focus_set()
            self._finder_entry.select_range(0, "end")
            existing.lift()
            self._finder_refresh()
            return "break"
        clear_ring(self)
        self._finder_hits = self._finder_index()
        pal = gui_widgets.palette()
        card = pal.get("card", "#ffffff")
        # place(), not grid/pack: the finder floats OVER the page without taking
        # part in the shell's layout, so nothing reflows when it opens or closes.
        f = tk.Frame(self.root, bg=card, highlightthickness=1,
                     highlightbackground=pal.get("hairline", "#d9dce1"),
                     highlightcolor=pal.get("hairline", "#d9dce1"))
        f.place(relx=0.5, y=6, anchor="n", width=self.FINDER_W)
        f.lift()
        self._finder_frame = f

        head = tk.Frame(f, bg=card)
        head.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(head, text="Find a control", font="MewSection", bg=card,
                 fg=pal.get("fg", "#1c1e21")).pack(side="left")
        tk.Label(head, text="↑↓ choose · Enter go · "
                            "Esc close", font="MewHint", bg=card,
                 fg=pal.get("hint", "#646a71")).pack(side="right")

        self._finder_query = tk.StringVar()
        entry = ttk.Entry(f, textvariable=self._finder_query, font="MewBody")
        entry.pack(fill="x", padx=10, pady=(4, 6))
        self._finder_entry = entry

        lb = tk.Listbox(f, height=self.FINDER_ROWS, activestyle="none",
                        font="MewBody", bd=0, highlightthickness=0,
                        exportselection=False, bg=card,
                        fg=pal.get("fg", "#1c1e21"),
                        selectbackground=pal.get("select_bg", "#2f6fd6"),
                        selectforeground=pal.get("select_fg", "#ffffff"))
        lb.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self._finder_list = lb

        self._finder_note = tk.Label(f, text="", font="MewHint", anchor="w",
                                     justify="left", bg=card,
                                     fg=pal.get("hint", "#646a71"))
        self._finder_note.pack(fill="x", padx=10, pady=(0, 8))

        self._finder_query.trace_add("write",
                                     lambda *_a: self._finder_refresh())
        for seq, fn in (("<Escape>", self.close_finder),
                        ("<Return>", self._finder_go),
                        ("<Down>", partial(self._finder_step, 1)),
                        ("<Up>", partial(self._finder_step, -1))):
            entry.bind(seq, fn)
            lb.bind(seq, fn)
        lb.bind("<Double-Button-1>", self._finder_go)
        entry.focus_set()
        self._finder_refresh()
        return "break"

    def close_finder(self, _event=None):
        f = getattr(self, "_finder_frame", None)
        if f is not None:
            try:
                f.destroy()
            except tk.TclError:
                pass
        self._finder_frame = None
        self._finder_results = []
        return "break"

    # -- results ------------------------------------------------------------ #
    def _finder_refresh(self):
        lb = getattr(self, "_finder_list", None)
        if lb is None or not lb.winfo_exists():
            return
        q = self._finder_query.get()
        self._finder_results = search(self._finder_hits, q)
        lb.delete(0, "end")
        for h in self._finder_results:
            lb.insert("end", row_text(h))
        if self._finder_results:
            lb.selection_clear(0, "end")
            lb.selection_set(0)
            lb.see(0)
        n, total = len(self._finder_results), len(self._finder_hits)
        if not q.strip():
            note = f"{total} controls indexed. Type part of a label."
        elif n:
            note = f"{n} match{'' if n == 1 else 'es'} of {total} controls."
        else:
            note = f"Nothing matches “{q.strip()}”."
        self._finder_note.configure(text=note)

    def _finder_step(self, delta, _event=None):
        lb = self._finder_list
        if not self._finder_results:
            return "break"
        cur = lb.curselection()
        i = max(0, min(len(self._finder_results) - 1,
                       (cur[0] if cur else 0) + delta))
        lb.selection_clear(0, "end")
        lb.selection_set(i)
        lb.see(i)
        return "break"

    # -- the jump ------------------------------------------------------------ #
    def _finder_go(self, _event=None):
        if not getattr(self, "_finder_results", None):
            return "break"
        cur = self._finder_list.curselection()
        hit = self._finder_results[cur[0] if cur else 0]
        self.close_finder()
        self.reveal_control(hit)
        return "break"

    def reveal_control(self, hit):
        """Navigate to `hit` and mark it. The public half of the finder: a page
        that wants to point at one of its own controls can call this too."""
        if hit.page_frame is not None:
            self.goto_page(hit.page_frame)
        if hit.kind in ("page", "setting", "command"):
            for step in hit.reveal:
                try:
                    step()
                except tk.TclError:
                    pass
            if hit.widget is not None:
                ring(self, hit.widget, hit.mark)
            self.status.set(f"Opened {hit.label}." if hit.kind == "page" else
                            f"“{hit.label}” is in {hit.where}."
                            if hit.kind == "setting" else
                            f"{hit.label}: type what to look for.")
            return
        for step in hit.reveal:
            try:
                step()
            except tk.TclError:
                pass
        widget, marks = hit.widget, hit.mark
        if hit.resolve is not None:
            try:
                self.root.update_idletasks()
                got = hit.resolve()
            except tk.TclError:
                got = None
            if got:
                widget, marks = got
        if widget is None:
            self.status.set(f"“{hit.label}” is on {hit.where}.")
            return
        try:
            self.root.update_idletasks()
            # Scroll to the WORDS when there are any: the caption can sit a row
            # above the control, and it is the caption the user is looking for.
            _scroll_into_view(marks[0] if marks else widget)
            self.root.update_idletasks()
        except tk.TclError:
            return
        ring(self, widget, marks)
        if hit.blocked:
            self.status.set(f"“{hit.label}” is on {hit.where}, "
                            f"but it {hit.blocked}.")
        else:
            self.status.set(f"“{hit.label}” is on {hit.where}.")
