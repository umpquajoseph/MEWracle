"""The unit-cell hover graphic for the 4-axis Pattern selector.

Hovering a pattern name (the ribbon's Pattern menu, the Parameters table's
Pattern combobox, the designer's Pattern combobox) shows that pattern's UNIT
CELL drawn solid, with a few neighbouring cells around it in grey, so the
choice can be judged by what it tiles into before it is picked.

The geometry is NOT redrawn here: the cells come from the same tilers the
tube generator uses (`mew_unit_cell._reentrant_members`, `_brick_members`,
`_custom_members`), on a 3 x 3 patch with unit cell sides, and the middle
cell's members are the solid ones. Only the diamond helix, which has no tiler
(it is one continuous helix), is drawn from its two families of diagonals.

Axis along the page = the tube's axis (u); down the page = around it (v), the
same unwrapped plane the cell editor draws on.

`segments(family, graph)` is pure (no Tk) and returns `(grey, solid)`, two
lists of polylines in patch units; `CellHover` is the popup;
`attach_menu` / `attach_combobox` wire it to a widget.
"""
import math
import tkinter as tk

import mew_unit_cell as uc

#: cells in the patch each way, and where the solid one sits (0-based)
_N = 3
_MID = 1
#: the cell's axial tolerance when picking its members, in cells: a chevron
#: ring bows by up to the amplitude below, so its midpoint is off the grid line
_EPS_U = 0.2
_EPS_V = 1e-6
#: chevron amplitude in the picture, in cells
_AMP = 0.3

#: the picture
_PX = 46            # pixels per cell
_PAD = 10
_GREY = "#b4b4b4"
_INK = "#1f6feb"      # the target cell: blue, not black
_BG = "#ffffff"


def _points(m):
    return [(u, m.v0 + dv) for u, dv in zip(m.us, m.dvs)]


def _in_cell(pts, u0, v0, w=1.0, h=1.0, box_v=None):
    """Is this member's centre inside the cell's box (edges inclusive)?"""
    us = [p[0] for p in pts]
    vs = [p[1] for p in pts]
    cu = 0.5 * (min(us) + max(us))
    cv = 0.5 * (min(vs) + max(vs))
    lo, hi = box_v if box_v is not None else (v0, v0 + h)
    return (u0 - _EPS_U <= cu <= u0 + w + _EPS_U
            and lo - _EPS_V <= cv <= hi + _EPS_V)


def _clip(p, q, lo, hi):
    """Liang-Barsky: the part of segment p-q inside the square [lo, hi]^2."""
    t0, t1 = 0.0, 1.0
    d = (q[0] - p[0], q[1] - p[1])
    for k in (0, 1):
        for sgn, bound in ((-1.0, lo), (1.0, hi)):
            # inside when sgn*(x - bound) <= 0
            dk = sgn * d[k]
            ck = sgn * (p[k] - bound)
            if abs(dk) < 1e-12:
                if ck > 0:
                    return None
            else:
                t = -ck / dk
                if dk < 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
    if t0 > t1:
        return None
    return [(p[0] + t0 * d[0], p[1] + t0 * d[1]),
            (p[0] + t1 * d[0], p[1] + t1 * d[1])]


def _diamond():
    """The diamond helix: two families of diagonals; each lattice cell is a
    diamond of half-diagonal 1/2, the middle one drawn solid."""
    grey = []
    far = float(_N)
    for n in range(-2 * _N, 3 * _N + 1):
        c = n + 0.5
        for a, b in (((c, 0.0), (c - far, far)),       # u + v = c
                     ((c, 0.0), (c + far, far))):      # u - v = c
            seg = _clip(a, b, 0.0, far)
            if seg:
                grey.append(seg)
    cu = cv = _MID + 0.5
    solid = [[(cu, cv - 0.5), (cu + 0.5, cv), (cu, cv + 0.5),
              (cu - 0.5, cv), (cu, cv - 0.5)]]
    return grey, solid


def segments(family, graph=None):
    """(grey, solid): polylines in patch units (cell = 1 x 1, patch = 3 x 3).

    `family` is the engine token (`zigzag`, `reentrant`, `brick-closed`,
    `brick-open`, `chevron`, `custom`); `graph` is the drawn cell's
    `'nodes | edges'` text (custom only). Returns ([], []) for a family the
    tilers refuse, so a hover on a half-drawn cell shows nothing, not a
    traceback."""
    if family == "zigzag":
        return _diamond()
    try:
        if family == "reentrant":
            # strut columns every half cell: a cell is two struts + four ribs
            members, _info = uc._reentrant_members(1.0, 1.0, _N, _N, 30.0)
            box_v = (0.5 * 1.0, 1.5 * 1.0)
            u0 = 1.0
        elif family in ("brick-closed", "brick-open", "chevron"):
            amp = _AMP if family == "chevron" else 0.0
            members = uc._brick_members(
                1.0, 1.0, _N, _N, family == "brick-open", amp,
                "chevron" if family == "chevron" else "straight")
            box_v, u0 = None, float(_MID)
        elif family == "custom":
            nodes, edges = uc.parse_cell_graph(
                graph if graph else _default_graph())
            if not nodes or not edges:
                return [], []
            members = uc._custom_members(1.0, 1.0, _N, _N, nodes, edges)
            box_v, u0 = None, float(_MID)
        else:
            return [], []
    except (uc.UnitCellError, ValueError):
        return [], []
    grey, solid = [], []
    for m in members:
        pts = _points(m)
        (solid if _in_cell(pts, u0, float(_MID), box_v=box_v)
         else grey).append(pts)
    return grey, solid


def _default_graph():
    import gui_mandrel
    return gui_mandrel.MANDREL_DEFAULT_CELL


class CellHover(object):
    """One small borderless popup that shows a cell; reused across hovers."""

    def __init__(self, owner):
        self.owner = owner
        self._tip = None
        self._cv = None
        self._shown = None

    def show(self, family, graph, x, y, caption=""):
        key = (family, graph)
        try:
            if self._tip is None or not self._tip.winfo_exists():
                self._tip = tk.Toplevel(self.owner)
                self._tip.wm_overrideredirect(True)
                try:
                    self._tip.attributes("-topmost", True)
                except tk.TclError:
                    pass
                side = 2 * _PAD + _N * _PX
                self._cv = tk.Canvas(self._tip, width=side, height=side + 18,
                                     bg=_BG, highlightthickness=1,
                                     highlightbackground="#888888")
                self._cv.pack()
                self._shown = None
            if self._shown != key:
                self._draw(family, graph, caption)
                self._shown = key
            self._tip.wm_geometry("+%d+%d" % (x, y))
            self._tip.deiconify()
        except tk.TclError:
            self._tip = None

    def hide(self):
        self._shown = None
        try:
            if self._tip is not None:
                self._tip.withdraw()
        except tk.TclError:
            self._tip = None

    def _draw(self, family, graph, caption):
        cv = self._cv
        cv.delete("all")
        grey, solid = segments(family, graph)

        def xy(pts):
            flat = []
            for u, v in pts:
                flat += [_PAD + u * _PX, _PAD + v * _PX]
            return flat

        for pts in grey:
            cv.create_line(*xy(pts), fill=_GREY, width=2, capstyle="round")
        for pts in solid:
            cv.create_line(*xy(pts), fill=_INK, width=3, capstyle="round",
                           joinstyle="round")
        if not grey and not solid:
            cv.create_text(_PAD + _N * _PX / 2, _PAD + _N * _PX / 2,
                           text="no cell drawn yet", fill=_GREY)
        cv.create_text(_PAD + _N * _PX / 2, _PAD * 2 + _N * _PX - 2,
                       text=caption or "one cell, with its neighbours",
                       fill="#666666", font=("TkDefaultFont", 8))


def _hover_for(widget):
    root = widget._root()
    h = getattr(root, "_cell_hover", None)
    if h is None:
        h = root._cell_hover = CellHover(root)
    return h


def attach_menu(menu, entries, graph_of=None):
    """Hover on a `tk.Menu`: `entries` maps an entry INDEX to its engine token,
    re-read at every highlight (the menu is rebuilt on each open)."""
    hover = _hover_for(menu)

    def on_select(_e=None):
        try:
            idx = menu.index("active")
        except tk.TclError:
            idx = None
        toks = entries() if callable(entries) else entries
        if idx is None or idx not in toks:
            hover.hide()
            return
        # where the menu is: Tk knows only for a menu it mapped itself, and on
        # Windows a posted menu is native, so fall back to where it was posted
        # (`_post_xy`, set by the caller) and to its widest label for its width
        x0, y0 = getattr(menu, "_post_xy", None) or (
            menu.winfo_rootx(), menu.winfo_rooty())
        w = menu.winfo_width()
        if w <= 1:
            import tkinter.font as tkfont
            f = tkfont.Font(font=menu.cget("font"))
            w = 56 + max(f.measure(str(menu.entrycget(i, "label")))
                         for i in toks)
        hover.show(toks[idx], graph_of() if graph_of else None,
                   x0 + w + 4, y0 + max(0, menu.yposition(idx) - 30))

    menu.bind("<<MenuSelect>>", on_select, add="+")
    menu.bind("<Unmap>", lambda _e: hover.hide(), add="+")


def attach_combobox(combo, label_to_token, graph_of=None):
    """Hover on the rows of a ttk.Combobox's drop-down list.

    The list is bound the first time it opens (the combobox's
    `-postcommand`, which ttk runs before it makes and shows the list), not
    when the page is built. Made at build time, the list is a hidden
    top-level window that Windows handed the keyboard focus to while the
    app's own window was still withdrawn (the app builds withdrawn). Tk then
    reported focus in a window tkinter has no widget for, and every
    `focus_get()` raised KeyError('popdown') until the main window was
    activated: in test_gcode_editor, which never activates it, "+ New edit"
    and 17 other cases failed."""
    hover = _hover_for(combo)
    last = {"i": None}
    prev = str(combo.cget("postcommand") or "")

    def post():
        if not last.get("bound"):
            last["bound"] = True
            _bind_list(combo, hover, last, label_to_token, graph_of)
        if prev:
            combo.tk.eval(prev)

    combo.configure(postcommand=post)


def _bind_list(combo, hover, last, label_to_token, graph_of):
    """Bind the hover to `combo`'s drop-down list, making the list now."""
    pop = str(combo.tk.eval("ttk::combobox::PopdownWindow %s" % combo))
    lb = pop + ".f.l"

    def on_motion(ey):
        ey = int(ey)
        try:
            i = int(combo.tk.call(lb, "nearest", ey))
            label = combo.tk.call(lb, "get", i)
        except tk.TclError:
            return
        tok = label_to_token.get(str(label))
        if tok is None:
            hover.hide()
            return
        if last["i"] == i:
            return
        last["i"] = i
        x = combo.tk.call("winfo", "rootx", pop) + int(
            combo.tk.call("winfo", "width", pop)) + 4
        y = int(combo.tk.call("winfo", "rooty", pop)) + max(0, ey - 30)
        hover.show(tok, graph_of() if graph_of else None, int(x), int(y))

    def off(_e=None):
        last["i"] = None
        hover.hide()

    combo.tk.call("bind", lb, "<Motion>", combo.register(on_motion) + " %y")
    combo.tk.call("bind", lb, "<Leave>", combo.register(off))
    combo.tk.call("bind", pop, "<Unmap>", combo.register(off))
