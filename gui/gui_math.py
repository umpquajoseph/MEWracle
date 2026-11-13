#!/usr/bin/env python3
"""gui_math.py -- typeset one equation on a Canvas, because Tk has no maths.

**Why this exists.** The Physics page prints each module's LAW above its
parameters. That line was a `ttk.Label` in Consolas 9 carrying a hand-mixed
spelling -- real Unicode superscripts in one term (`πr⁴ΔP`) next to ASCII
carets and underscores in the next (`ε^-b`, `θ_CTS`), a `/` where the law has a
division bar, and no spacing distinction between a relation and a binary
operator. At 9 px in a monospace face it read as a code comment, not as the law,
and the subscripted names ran into their bases.

So this module lays a small TeX-shaped subset out properly: italic serif for
variables, upright roman for numerals, operators and function names, real raised
superscripts and lowered subscripts, stacked fractions with a rule, a drawn
radical, delimiters that grow to their content, and TeX's own spacing classes.
It is ~1 page of layout maths and needs no dependency: everything is
`Font.measure` / `Font.metrics` plus `create_text` and `create_line`.

**The markup** is a deliberately small TeX subset -- enough for the ten laws and
nothing more:

    a^{2}  a_{ref}       superscript / subscript (braces optional for one char)
    \\frac{a}{b}          stacked fraction with a rule
    \\sqrt{a}             radical with an overbar
    \\left( ... \\right)   delimiters scaled to the group's height
    \\eta \\pi \\theta ...   greek and symbol names (see _SYMBOLS)
    \\exp \\arctan ...     function names, set upright (see _FUNCS)
    \\mathrm{...}         force upright
    \\,  \\;  \\quad        thin / medium / quad space
    \\\\                   row break (rows align on their first relation)

Multi-letter runs (`span`, `CTS`, `depth`) are set as ONE italic word rather
than letter-by-letter as TeX would: these laws name their quantities in words,
and `s·p·a·n` in italics is unreadable.

**Theme.** `MathLabel` self-registers with `gui_widgets` and recolours in
`retheme()` like every other custom-drawn composite, so the dark-mode toggle
reaches it. It measures itself once at build time and asks for exactly that
size; it never reads its own width, so it cannot get into the self-feeding
resize loop that `ModuleCard.set_summary_wrap` once did.
"""

import tkinter as tk
from tkinter import font as tkfont

import gui_widgets

# ---------------------------------------------------------------------------
# faces
# ---------------------------------------------------------------------------
# A maths face first: Cambria Math carries a real italic lowercase and proper
# Greek. The fallbacks are ordinary serifs, which still beat a monospace face --
# the italic/upright contrast is what separates a variable from an operator.
# "Cambria" (the text face) BEFORE "Cambria Math": the maths face has a real
# italic and proper Greek, but its line box is sized for stretchy big operators
# -- at 14 pt it reports ascent 59 / descent 47 px -- so anything that lays out
# from `Font.metrics` gets 4x the vertical space it needs. See `_ink` for how
# that is handled; the ordering just prefers the face whose metrics are sane.
_PREFERRED = ("Cambria", "Cambria Math", "Georgia", "Constantia",
              "Times New Roman", "DejaVu Serif", "Times")
_family = None
_fonts = {}
_fonts_tk = None        # the interpreter `_fonts`' wrappers belong to
_pt_px = None


def family(root=None):
    """The serif family actually rendering. Probed once per process."""
    global _family
    if _family is None:
        _family = "Times"
        for want in _PREFERRED:
            try:
                probe = tkfont.Font(root=root, family=want, size=12)
                if probe.actual("family").casefold() == want.casefold():
                    _family = want
                    break
            except tk.TclError:
                continue
    return _family


def _px_per_pt(root=None):
    """Pixels per point on this display. Cached; 96 dpi if Tk cannot say."""
    global _pt_px
    if _pt_px is None:
        try:
            _pt_px = float(tk.Misc.winfo_fpixels(root, "1p")) if root else 96/72.
        except (tk.TclError, AttributeError, TypeError):
            _pt_px = 96 / 72.
        if not (0.5 < _pt_px < 6.0):
            _pt_px = 96 / 72.
    return _pt_px


def _ink(size_pt, root=None):
    """NOMINAL (ascent, descent) in pixels for a point size -- what the layout
    uses instead of `Font.metrics`.

    A maths face's reported line box is not its ink: Cambria Math sizes its box
    for stretchy integrals and braces, so `metrics("ascent")` is ~3x the cap
    height. Laying out from it put a fraction's numerator 50 px above its rule.
    These fractions of the em are close to a serif face's real cap height and
    descender, and they are FACE-INDEPENDENT, which is the point -- the layout
    must not change when the font fallback does.

    Drawing still uses the real `metrics("ascent")`: Tk puts the line box top at
    the anchor and the baseline one real ascent below it, so that number is
    correct for placement even when it is wrong for spacing."""
    px = size_pt * _px_per_pt(root)
    return 0.76 * px, 0.24 * px


def _font(size, slant="roman", weight="normal", root=None):
    """A cached face for one ramp position, on THIS root.

    THE CACHE IS PER-INTERPRETER. A `tkfont.Font` is a handle into one Tk
    interpreter, so a module-level cache that outlives a root hands the next
    root a wrapper whose first `measure()` raises "application has been
    destroyed" -- with no hint that a font is what went wrong. One process
    building two apps is not hypothetical: it is what a suite does, and it took
    the nine real-app cases of `test_gui_positioning` down at `_text_box`.

    Only these wrappers are interpreter-bound. `_family` is a string and
    `_pt_px` a float, so both stay probed-once-per-process as before.
    """
    global _fonts_tk
    tkapp = getattr(root, "tk", None)
    if tkapp is not _fonts_tk:
        _fonts.clear()
        _fonts_tk = tkapp
    key = (size, slant, weight)
    f = _fonts.get(key)
    if f is None:
        f = tkfont.Font(root=root, family=family(root), size=size,
                        slant=slant, weight=weight)
        _fonts[key] = f
    return f


# ---------------------------------------------------------------------------
# the markup vocabulary
# ---------------------------------------------------------------------------
_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "Delta": "Δ", "epsilon": "ε", "varepsilon": "ε",
    "eta": "η", "theta": "θ", "Theta": "Θ", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "pi": "π",
    "Pi": "Π", "rho": "ρ", "sigma": "σ", "Sigma": "Σ",
    "tau": "τ", "phi": "φ", "varphi": "φ", "Phi": "Φ",
    "psi": "ψ", "omega": "ω", "Omega": "Ω", "zeta": "ζ",
    "cdot": "·", "times": "×", "div": "÷", "pm": "±",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "approx": "≈", "propto": "∝",
    "to": "→", "infty": "∞", "partial": "∂",
}
# set upright, and given a thin space after the name the way TeX does
_FUNCS = frozenset({
    "exp", "ln", "log", "sin", "cos", "tan", "arctan", "arcsin", "arccos",
    "sinh", "cosh", "tanh", "min", "max", "sqrt_name",
})
# TeX's spacing classes. A relation breathes more than a binary operator, and
# that difference is most of what makes an equation scannable.
_RELATIONS = frozenset("=≈≤≥≠<>∝→")
_BINOPS = frozenset("+−-·×÷±")
_OPENERS = frozenset("([{")
_CLOSERS = frozenset(")]}")

_STYLE_SCALE = (1.0, 0.72, 0.60)          # display, script, scriptscript


# ---------------------------------------------------------------------------
# tokenizer
# ---------------------------------------------------------------------------
def _tokenize(src):
    """(kind, value) pairs. kind is one of 'cmd', 'word', 'num', 'char',
    'open', 'close', 'sup', 'sub'."""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            j = i + 1
            if j < n and not src[j].isalpha():
                out.append(("cmd", src[j]))       # \, \; \\ \{ \}
                i = j + 1
                continue
            k = j
            while k < n and src[k].isalpha():
                k += 1
            out.append(("cmd", src[j:k]))
            i = k
            continue
        if c == "{":
            out.append(("open", "{")); i += 1; continue
        if c == "}":
            out.append(("close", "}")); i += 1; continue
        if c == "^":
            out.append(("sup", "^")); i += 1; continue
        if c == "_":
            out.append(("sub", "_")); i += 1; continue
        if c.isspace():
            i += 1; continue                      # explicit spaces only
        if c.isdigit() or c == ".":
            k = i
            while k < n and (src[k].isdigit() or src[k] == "."):
                k += 1
            out.append(("num", src[i:k])); i = k; continue
        if c.isalpha():
            k = i
            while k < n and src[k].isalpha():
                k += 1
            out.append(("word", src[i:k])); i = k; continue
        out.append(("char", c)); i += 1
    return out


# ---------------------------------------------------------------------------
# boxes
# ---------------------------------------------------------------------------
class _Box:
    """A laid-out fragment: draw ops in coordinates relative to (left, baseline),
    plus the width and the extents above/below that baseline.

    An op is either
        ("text", dx, dy, font, string)   dy = the text's own baseline offset
        ("line", x1, y1, x2, y2, width)
    """

    __slots__ = ("ops", "w", "asc", "desc", "rel_x")

    def __init__(self, ops=None, w=0.0, asc=0.0, desc=0.0):
        self.ops = ops or []
        self.w = w
        self.asc = asc          # extent ABOVE the baseline (positive)
        self.desc = desc        # extent BELOW the baseline (positive)
        self.rel_x = None       # x of the first relation, for row alignment

    def place(self, other, dx, dy):
        """Absorb `other`, its origin offset by (dx, dy) from ours."""
        for op in other.ops:
            if op[0] == "text":
                self.ops.append(("text", op[1] + dx, op[2] + dy, op[3], op[4]))
            else:
                self.ops.append(("line", op[1] + dx, op[2] + dy,
                                 op[3] + dx, op[4] + dy, op[5]))
        self.asc = max(self.asc, other.asc - dy)
        self.desc = max(self.desc, other.desc + dy)
        return self


def _text_box(s, font, size_pt, root=None):
    asc, desc = _ink(size_pt, root)
    return _Box([("text", 0.0, 0.0, font, s)], font.measure(s), asc, desc)


# ---------------------------------------------------------------------------
# parser + layout, in one recursive pass
# ---------------------------------------------------------------------------
class _Layout:
    def __init__(self, size, root=None):
        self.size = size
        self.root = root

    def _sz(self, style):
        return max(6, int(round(self.size * _STYLE_SCALE[min(style, 2)])))

    def tx(self, text, sz, slant="roman"):
        """One run of glyphs as a box: the font for drawing, nominal ink for
        layout."""
        return _text_box(text, _font(sz, slant, root=self.root), sz, self.root)

    # -- one group (to the matching close brace, or the whole token list) --- #
    def row(self, toks, i=0, stop=None, style=0):
        """Lay out tokens from `i` until `stop` (a token kind) or the end.
        Returns (box, next_index)."""
        sz = self._sz(style)
        box = _Box()
        x = 0.0
        prev_kind = None        # "ord" | "rel" | "bin" | "open" | None
        last = None             # (box, dx) of the atom a ^/_ would attach to
        pending_func = False

        def emit(atom, kind):
            nonlocal x, prev_kind, last
            gap = 0.0
            if prev_kind is not None:
                if kind == "rel" or prev_kind == "rel":
                    gap = 0.30 * sz
                elif kind == "bin" or prev_kind == "bin":
                    gap = 0.20 * sz
                elif prev_kind == "punct":
                    gap = 0.26 * sz
                elif prev_kind == "func":
                    gap = 0.12 * sz
                elif prev_kind == "open" or kind == "close":
                    gap = 0.0
                elif kind == "ord" and prev_kind == "ord":
                    gap = 0.07 * sz          # implicit multiplication
            x += gap
            if kind == "bin" and prev_kind is None:
                kind = "open"       # a UNARY sign hugs its operand
            box.place(atom, x, 0.0)
            last = (atom, x)
            if kind == "rel" and box.rel_x is None:
                box.rel_x = x
            x += atom.w
            prev_kind = kind

        while i < len(toks):
            kind, val = toks[i]
            if stop and kind == stop:
                box.w = max(box.w, x)
                return box, i + 1

            # -- scripts attach to the atom just emitted -------------------- #
            if kind in ("sup", "sub"):
                if last is None:
                    i += 1
                    continue
                sub_box, i = self.argument(toks, i + 1, style + 1)
                base, bx = last
                if kind == "sup":
                    dy = -(0.44 * sz + max(0.0, sub_box.desc - 0.15 * sz))
                    dy = min(dy, -(base.asc * 0.55))
                else:
                    dy = 0.24 * sz + max(0.0, sub_box.asc - 0.55 * sz) * 0.35
                    dy = max(dy, base.desc * 0.35 + 0.18 * sz)
                sx = bx + base.w + 0.02 * sz
                box.place(sub_box, sx, dy)
                # the italic script face overhangs its advance, so a bare
                # `sx + w` let the next atom touch it -- "theta_CTS+" printed
                # with no space at all before the sign.
                x = max(x, sx + sub_box.w + 0.08 * sz)
                # a following script attaches to the same base, not to this one
                continue

            if kind == "open":
                inner, i = self.row(toks, i + 1, stop="close", style=style)
                emit(inner, "ord")
                continue
            if kind == "close":
                box.w = max(box.w, x)
                return box, i + 1

            if kind == "cmd":
                atom, akind, i = self.command(toks, i, style)
                if atom is None:
                    continue
                emit(atom, akind)
                continue

            if kind == "num":
                emit(self.tx(val, sz), "ord")
                i += 1
                continue

            if kind == "word":
                if val in _FUNCS:
                    emit(self.tx(val, sz), "func")
                else:
                    emit(self.tx(val, sz, "italic"), "ord")
                i += 1
                continue

            # a bare character: operator, delimiter or punctuation
            ch = "−" if val == "-" else val
            atom = self.tx(ch, sz)
            if ch in _RELATIONS:
                emit(atom, "rel")
            elif ch in _BINOPS:
                emit(atom, "bin")
            elif ch in _OPENERS:
                emit(atom, "open")
            elif ch in _CLOSERS:
                emit(atom, "close")
            elif ch in ",;":
                emit(atom, "punct")
            else:
                emit(atom, "ord")
            i += 1

        box.w = max(box.w, x)
        return box, i

    # -- the argument of ^, _, \frac, \sqrt: a braced group or one token ---- #
    def argument(self, toks, i, style):
        if i >= len(toks):
            return _Box(), i
        kind, _val = toks[i]
        if kind == "open":
            return self.row(toks, i + 1, stop="close", style=style)
        return self.row(toks[i:i + 1], 0, style=style)[0], i + 1

    # -- commands ---------------------------------------------------------- #
    def command(self, toks, i, style):
        """Returns (box_or_None, spacing_kind, next_index)."""
        name = toks[i][1]
        sz = self._sz(style)

        if name == "frac":
            num, i = self.argument(toks, i + 1, style)
            den, i = self.argument(toks, i, style)
            return self._frac(num, den, sz), "ord", i
        if name == "sqrt":
            arg, i = self.argument(toks, i + 1, style)
            return self._sqrt(arg, sz), "ord", i
        if name == "mathrm" or name == "text":
            if i + 1 < len(toks) and toks[i + 1][0] == "open":
                inner, i = self._upright_group(toks, i + 2, style)
                return inner, "ord", i
            return None, "ord", i + 1
        if name in ("left", "right"):
            # \left( body \right)  -- scale both delimiters to the body
            if name == "right":
                return None, "ord", i + 1          # consumed by \left
            return self._fenced(toks, i + 1, style)
        if name == ",":
            return _Box(w=0.17 * sz), "space", i + 1
        if name == ";":
            return _Box(w=0.28 * sz), "space", i + 1
        if name == "quad":
            return _Box(w=1.0 * sz), "space", i + 1
        if name in _SYMBOLS:
            ch = _SYMBOLS[name]
            kind = ("rel" if ch in _RELATIONS else
                    "bin" if ch in _BINOPS else "ord")
            # Greek variables are italic like any other variable; operators
            # and relations stay upright.
            slant = "italic" if kind == "ord" and ch.isalpha() else "roman"
            return self.tx(ch, sz, slant), kind, i + 1
        if name in _FUNCS:
            return self.tx(name, sz), "func", i + 1
        # unknown command: render its name upright rather than vanish
        return self.tx(name, sz), "ord", i + 1

    def _upright_group(self, toks, i, style):
        sz = self._sz(style)
        buf = []
        while i < len(toks) and toks[i][0] != "close":
            buf.append(toks[i][1])
            i += 1
        return self.tx("".join(buf), sz), i + 1

    def _frac(self, num, den, sz):
        axis = -0.30 * sz                 # the fraction bar, above the baseline
        gap = 0.16 * sz + 1.0
        w = max(num.w, den.w) + 0.36 * sz
        box = _Box(w=w)
        box.place(num, (w - num.w) / 2.0, axis - gap - num.desc)
        box.place(den, (w - den.w) / 2.0, axis + gap + den.asc)
        box.ops.append(("line", 0.0, axis, w, axis, max(1, int(sz / 11) + 1)))
        box.asc = max(box.asc, -axis + 1)
        box.desc = max(box.desc, axis + 1)
        box.w = w
        return box

    def _sqrt(self, arg, sz):
        pad = 0.18 * sz + 1.0
        asc = arg.asc + pad
        desc = arg.desc
        h = asc + desc
        wr = 0.62 * sz
        lw = max(1, int(sz / 11) + 1)
        box = _Box(w=wr + arg.w + 0.28 * sz)
        box.place(arg, wr + 0.12 * sz, 0.0)
        top = -asc
        box.ops.append(("line", 0.04 * sz, top + 0.62 * h,
                        0.34 * wr, top + h, lw))
        box.ops.append(("line", 0.34 * wr, top + h,
                        0.72 * wr, top, lw))
        box.ops.append(("line", 0.66 * wr, top, box.w, top, lw))
        box.asc = max(box.asc, asc + lw)
        box.desc = max(box.desc, desc)
        return box

    def _fenced(self, toks, i, style):
        """`\\left<d> ... \\right<d>` -- the body, wrapped in delimiters grown to
        it. Tk cannot stretch a glyph, so the delimiter is the same character at
        a larger point size, clamped so a tall fraction does not produce a
        cartoon bracket."""
        sz = self._sz(style)
        if i >= len(toks):
            return None, "ord", i
        open_ch = toks[i][1]
        if open_ch == ".":
            open_ch = ""
        i += 1
        body, i, close_ch = self._until_right(toks, i, style)
        need = body.asc + body.desc
        have = sum(_ink(sz, self.root))
        scale = max(1.0, min(2.4, need / max(1.0, have) * 1.02))
        dsz = max(6, int(round(sz * scale)))
        box = _Box()
        x = 0.0
        mid = -0.30 * sz                    # the maths axis both fences centre on
        for ch, part in ((open_ch, None), (None, body), (close_ch, None)):
            if part is not None:
                box.place(part, x, 0.0)
                x += part.w
            elif ch:
                da, dd = _ink(dsz, self.root)
                # centre the delimiter's ink on the maths axis
                dy = mid + (da - dd) / 2.0
                b = self.tx(ch, dsz)
                box.place(b, x, dy)
                x += b.w + 0.04 * sz
        box.w = x
        return box, "ord", i

    def _until_right(self, toks, i, style):
        """Lay out up to the matching `\\right<d>`; returns (box, i, close_ch)."""
        depth = 0
        j = i
        while j < len(toks):
            k, v = toks[j]
            if k == "cmd" and v == "left":
                depth += 1
            elif k == "cmd" and v == "right":
                if depth == 0:
                    break
                depth -= 1
            j += 1
        body, _ = self.row(toks[i:j], 0, style=style)
        close_ch = ""
        if j < len(toks):
            j += 1                                  # the \right
            if j < len(toks):
                close_ch = toks[j][1]
                if close_ch == ".":
                    close_ch = ""
                j += 1
        return body, j, close_ch


def layout(src, size, root=None):
    """Lay `src` out as one or more rows; returns (ops, width, height).

    Rows are separated by `\\\\` and aligned on their first relation symbol when
    every row has one, which is what makes a two-line law read as a pair of
    statements rather than as two unrelated strings."""
    eng = _Layout(size, root)
    rows = []
    for chunk in src.split("\\\\"):
        if not chunk.strip():
            continue
        box, _ = eng.row(_tokenize(chunk), 0)
        rows.append(box)
    if not rows:
        return [], 1, 1
    rel = [b.rel_x for b in rows]
    shift = [0.0] * len(rows)
    if len(rows) > 1 and all(r is not None for r in rel):
        target = max(rel)
        shift = [target - r for r in rel]
    ops = []
    y = 0.0
    width = 0.0
    lead = 0.42 * size
    for b, dx in zip(rows, shift):
        y += b.asc
        for op in b.ops:
            if op[0] == "text":
                ops.append(("text", op[1] + dx, op[2] + y, op[3], op[4]))
            else:
                ops.append(("line", op[1] + dx, op[2] + y,
                            op[3] + dx, op[4] + y, op[5]))
        width = max(width, b.w + dx)
        y += b.desc + lead
    height = y - lead
    return ops, int(width + 2), int(height + 2)


# ---------------------------------------------------------------------------
# the widget
# ---------------------------------------------------------------------------
class MathLabel(tk.Canvas):
    """One typeset equation, sized to its content.

    `size` is the point size of the display-style text; scripts derive from it.
    `fg=None` takes the palette's foreground; pass a key name ("hint", "fg") or
    a literal colour. The background follows `bg_key` so the label can sit in a
    card ("card") or in a tinted strip ("hover")."""

    def __init__(self, parent, source, size=13, fg="fg", bg_key="card",
                 padx=0, pady=0, **kw):
        self._src = source
        self._size = size
        self._fg_key = fg
        self._bg_key = bg_key
        self._padx = padx
        self._pady = pady
        pal = gui_widgets.palette()
        tk.Canvas.__init__(self, parent, highlightthickness=0, bd=0,
                           background=pal.get(bg_key, bg_key), **kw)
        self._ops, w, h = layout(source, size, root=parent)
        self.configure(width=w + 2 * padx, height=h + 2 * pady)
        self._draw()
        gui_widgets.register(self)

    def _draw(self):
        pal = gui_widgets.palette()
        fill = pal.get(self._fg_key, self._fg_key)
        self.delete("all")
        for op in self._ops:
            if op[0] == "text":
                f = op[3]
                self.create_text(op[1] + self._padx,
                                 op[2] + self._pady - f.metrics("ascent"),
                                 anchor="nw", text=op[4], font=f, fill=fill)
            else:
                self.create_line(op[1] + self._padx, op[2] + self._pady,
                                 op[3] + self._padx, op[4] + self._pady,
                                 fill=fill, width=op[5], capstyle="round")

    def set_source(self, source):
        self._src = source
        self._ops, w, h = layout(source, self._size, root=self)
        self.configure(width=w + 2 * self._padx, height=h + 2 * self._pady)
        self._draw()

    def retheme(self):
        pal = gui_widgets.palette()
        try:
            self.configure(background=pal.get(self._bg_key, self._bg_key))
        except tk.TclError:
            return
        self._draw()


class MathStrip(tk.Frame):
    """A `MathLabel` with the air around it -- how a module card prints its law.

    **`bg`, and NOT `card`.** An opened module's `card.body` is re-parented into
    `ModuleGrid._bodies`, a plain `ttk.Frame` -- so the surface an open card's
    law actually sits on is the PAGE background (`#232527` dark / `#f6f7f9`
    light), not the tile colour the card has while it is closed. A tinted band
    was tried first and read as a patch of the wrong colour behind the equation,
    which is the report this default answers. Check with
    `ttk.Style().lookup("TFrame", "background")` before changing it; a Canvas has
    no transparency, so this has to be named explicitly and cannot be inherited.

    That is also why there is a class here at all rather than a bare `MathLabel`:
    a plain `tk.Frame` does not follow the dark-mode toggle, and a mismatched
    frame behind a correctly-themed Canvas is exactly the seam this avoids."""

    def __init__(self, parent, source, size=14, bg_key="bg", **kw):
        self._bg_key = bg_key
        pal = gui_widgets.palette()
        tk.Frame.__init__(self, parent,
                          background=pal.get(bg_key, bg_key), **kw)
        self.label = MathLabel(self, source, size=size, bg_key=bg_key,
                               padx=2, pady=6)
        self.label.pack(anchor="w")
        gui_widgets.register(self)

    def retheme(self):
        pal = gui_widgets.palette()
        try:
            self.configure(background=pal.get(self._bg_key, self._bg_key))
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# an expression -> this module's markup
# ---------------------------------------------------------------------------
# So a law the USER typed is printed the same way a shipped one is. Without this
# an edited law would have to be shown as raw source -- the monospace line the
# tinted strip replaced -- and the card would get uglier exactly when the user
# took ownership of it.
#
# EVERY string below is a raw literal. Writing them unraw is not a style slip: a
# bare "\frac" is a FORMFEED followed by "rac", which parses, renders, and is
# only visible as a missing fraction. It happened once here already.
_GREEK = {
    "alpha", "beta", "gamma", "delta", "epsilon", "eta", "theta", "kappa",
    "lambda", "mu", "nu", "pi", "rho", "sigma", "tau", "phi", "psi", "omega",
    "zeta", "Delta", "Omega", "Phi", "Sigma", "Theta",
}
# names the physics spells out but mathematics does not
_RENAME = {
    "eta": r"\eta", "phi": r"\varphi",
    # `visc_ratio` would otherwise print as eta with the subscript "ratio"; it is
    # eta_r by convention, and keeping it one symbol is what stops a reader
    # confusing the dimensionless ratio with the absolute eta beside it.
    "visc_ratio": r"\eta_{r}",
    "pi": r"\pi", "theta": r"\theta", "delta": r"\delta",
    "epsilon": r"\varepsilon", "tau": r"\tau",
    # the melt activation energy. One symbol, not an E whose subscript the
    # underscore rule below would have to invent -- `Ea` is how it is written.
    "Ea": r"E_{a}",
    # the process values, spelled in full in the language and as symbols on a card
    "speed": "v", "voltage": "V", "pressure": "P", "temperature": "T",
    "gap": "H", "inner_radius": "r", "length": "L",
}
_FUNC_WORDS = {"exp", "ln", "log", "log10", "sin", "cos", "tan", "asin",
               "acos", "atan", "atan2", "sinh", "cosh", "tanh", "min", "max",
               "abs", "clamp", "hypot", "floor", "ceil", "pow",
               "radians", "degrees"}


def _name_markup(name):
    """One identifier as markup: a greek name becomes its letter, and a trailing
    `_suffix` becomes a real subscript, so `cts_ref` prints as CTS with a
    subscript rather than with a stray underscore."""
    name = str(name)
    if name in _RENAME:
        return _RENAME[name]
    if name in _GREEK:
        return "\\" + name
    head, sep, tail = name.partition("_")
    base = _RENAME.get(head, ("\\" + head) if head in _GREEK else head)
    if sep and tail:
        # a THIN SPACE, not a real one: `_tokenize` drops whitespace, so a bare
        # space made `sag_span_exp` print as one run "spanexp"
        return "%s_{%s}" % (base, tail.replace("_", r"\,"))
    return base


# Binding powers, used only to decide where a bracket is NEEDED. A markup
# fraction or radical already brackets its own content visually, so the pairs
# that survive are the ones a reader would otherwise mis-group.
_P_OR, _P_AND, _P_CMP, _P_ADD, _P_MUL, _P_UNARY, _P_POW, _P_ATOM = range(1, 9)


def _paren(text, need):
    return (r"\left( %s \right)" % text) if need else text


def _expr_markup(node, parent=0):
    import ast
    if isinstance(node, ast.Expression):
        return _expr_markup(node.body)
    if isinstance(node, ast.Constant):
        return ("%g" % node.value) if isinstance(node.value, (int, float)) \
            else str(node.value)
    if isinstance(node, ast.Name):
        return _name_markup(node.id)

    if isinstance(node, ast.UnaryOp):
        op = "-" if isinstance(node.op, ast.USub) else "+"
        return _paren(op + _expr_markup(node.operand, _P_UNARY),
                      parent > _P_UNARY)

    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Div):
            # a division becomes a RULE, which is most of why this is worth
            # doing -- but a fraction RAISED to a power still needs its bracket,
            # or (V/V_ref)^a prints as V over V_ref-to-the-a.
            out = r"\frac{%s}{%s}" % (_expr_markup(node.left),
                                      _expr_markup(node.right))
            return _paren(out, parent >= _P_POW)
        if isinstance(node.op, ast.Pow):
            return "%s^{%s}" % (_expr_markup(node.left, _P_POW),
                                _expr_markup(node.right))
        sym = {ast.Add: "+", ast.Sub: "-", ast.Mult: r"\cdot",
               ast.Mod: r"\%"}.get(type(node.op), "?")
        prec = _P_MUL if isinstance(node.op, (ast.Mult, ast.Mod)) else _P_ADD
        out = "%s %s %s" % (_expr_markup(node.left, prec), sym,
                            _expr_markup(node.right, prec + 1))
        return _paren(out, parent > prec)

    if isinstance(node, ast.Compare):
        sym = {ast.Lt: "<", ast.LtE: r"\le", ast.Gt: ">", ast.GtE: r"\ge",
               ast.Eq: "=", ast.NotEq: r"\neq"}
        out = _expr_markup(node.left, _P_CMP)
        for op, comp in zip(node.ops, node.comparators):
            out += " %s %s" % (sym.get(type(op), "?"),
                               _expr_markup(comp, _P_CMP))
        return _paren(out, parent > _P_CMP)

    if isinstance(node, ast.BoolOp):
        join = (r" \;\mathrm{and}\; " if isinstance(node.op, ast.And)
                else r" \;\mathrm{or}\; ")
        return _paren(join.join(_expr_markup(v, _P_AND) for v in node.values),
                      parent > _P_AND)

    if isinstance(node, ast.IfExp):
        # INLINE, never a row break. A conditional can sit anywhere in a bigger
        # expression -- the shipped fusion law has one inside a min(max(...)) --
        # and a `\` there would split the equation mid-term.
        return _paren(
            r"%s \;\mathrm{if}\; %s \;\mathrm{else}\; %s" % (
                _expr_markup(node.body, _P_CMP),
                _expr_markup(node.test, _P_CMP),
                _expr_markup(node.orelse, _P_CMP)),
            True)

    if isinstance(node, ast.Call):
        fname = getattr(node.func, "id", "f")
        args = [_expr_markup(a) for a in node.args]
        if fname == "sqrt" and len(args) == 1:
            return r"\sqrt{%s}" % args[0]
        head = ("\\" + fname) if fname in _FUNC_WORDS else fname
        return r"%s\left( %s \right)" % (head, r",\; ".join(args))

    return "?"


def from_expression(source, lhs=None):
    """A `mew_lawlang` expression -> this module's markup, `lhs` on the left of an
    `=` when given.

    Returns None when the source will not parse, so a card mid-edit falls back to
    showing the shipped law rather than printing nonsense."""
    import ast
    try:
        tree = ast.parse(str(source or ""), mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    try:
        body = _expr_markup(tree)
    except RecursionError:
        return None
    return ("%s = %s" % (_name_markup(lhs), body)) if lhs else body
